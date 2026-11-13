"""core.mcp — hardened legacy-MCP reconciliation (plan §8 Phase 10, §5.9).

The v2 engine for the mcpServers mirror, fixing the legacy defects found in
Phase 0: edit/delete no longer drops the survivor unchecked (an edit on the
surviving side turns the delete into a held conflict); the snapshot is typed
and schema-validated with unknown fields preserved (never used to authorize
merges); backups/temps/state are unique and private (0600); an owned lock
serializes runs; each file write is journaled so a mid-transaction failure is
recorded as partial, never complete (two config files are NOT one
transaction); a backup failure means zero target writes.

The legacy bin/sync_code_desktop.py stays untouched (host contract); this
module is wired in by the Phase 11 CLI.

Stdlib only; Python 3.8 compatible.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

from . import lifecycle

SNAPSHOT_SCHEMA = "cds-mcp-snapshot/1"


class SnapshotError(Exception):
    pass


class SyncError(Exception):
    pass


def _is_remote(entry):
    if not isinstance(entry, dict):
        return False
    if str(entry.get("type", "")).lower() in ("http", "sse", "streamable-http", "ws"):
        return True
    return bool(entry.get("url") or entry.get("headers"))


def _is_stdio(entry):
    return isinstance(entry, dict) and "command" in entry and not _is_remote(entry)


def _exact(entry):
    return {"command": entry.get("command", ""),
            "args": list(entry.get("args", [])),
            "env": dict(entry.get("env", {}))}


def _resolve_for_desktop(command):
    if not command or os.path.isabs(command):
        return command
    found = shutil.which(command)
    if found:
        return found
    return command


def _to_desktop(entry):
    e = {"command": _resolve_for_desktop(entry.get("command", ""))}
    if "args" in entry:
        e["args"] = list(entry["args"])
    if entry.get("env"):
        e["env"] = dict(entry["env"])
    return e


def _same_server(code_entry, desk_entry):
    return _exact(_to_desktop(code_entry)) == _exact(desk_entry)


def _load_json(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _write_json_private(path, data, private=True):
    """Write via a unique temp + atomic replace. M3 (review): a secrets-bearing
    existing target keeps its CURRENT mode (never silently widened to 0644 by
    the tmp, never silently tightened); only our own private state files are
    forced to 0600."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    try:
        if path.exists():
            mode = os.stat(path).st_mode & 0o777  # preserve user-owned mode
        else:
            mode = 0o600 if private else 0o666 & ~_umask()
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def _umask():
    current = os.umask(0o022)
    os.umask(current)
    return current


class Out:
    def __init__(self):
        self.changed = []
        self.conflicts = []
        self.type_conflicts = []
        self.notes = []


class SyncState:
    def __init__(self, snap_path, backup_dir=None, journal=None, lock_stale=120):
        self.snap_path = Path(snap_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.snap_path.parent / "backups"
        self.journal = Path(journal) if journal else self.snap_path.parent / "journal.jsonl"
        self.lock_stale = lock_stale

    def _j(self, entry):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with open(self.journal, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    def load(self):
        """Typed, schema-validated snapshot load; unknown fields preserved."""
        if not self.snap_path.exists():
            return {"schema": SNAPSHOT_SCHEMA, "servers": {}}
        try:
            doc = json.loads(self.snap_path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise SnapshotError("snapshot unreadable: %s — refusing unsafe merge" % e) from None
        if not isinstance(doc, dict) or doc.get("schema") != SNAPSHOT_SCHEMA:
            raise SnapshotError("unknown snapshot schema %r (this build understands %s)"
                                % (doc.get("schema") if isinstance(doc, dict) else type(doc),
                                   SNAPSHOT_SCHEMA))
        doc.setdefault("servers", {})
        return doc

    def save(self, doc):
        self.snap_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_private(self.snap_path, doc)

    def run(self, code_path, desk_path, fault_inject=None):
        """fault_inject(committed_count) runs before each file write — the
        owned-fixture seam for testing mid-transaction failure."""
        code_path, desk_path = Path(code_path), Path(desk_path)
        out = Out()
        if not lifecycle.acquire_owned_lock(self.snap_path.parent, self.lock_stale):
            out.notes.append("locked")
            return out
        try:
            code = _load_json(code_path)
            desk = _load_json(desk_path)
            code_srv = code.get("mcpServers", {})
            desk_srv = desk.get("mcpServers", {})
            if not isinstance(code_srv, dict) or not isinstance(desk_srv, dict):
                out.notes.append("non-object mcpServers; skipped")
                return out
            type_conflicts = sorted(n for n in (set(code_srv) & set(desk_srv))
                                    if _is_stdio(code_srv[n]) != _is_stdio(desk_srv[n]))
            out.type_conflicts = type_conflicts
            code_stdio = {n: e for n, e in code_srv.items() if _is_stdio(e) and n not in type_conflicts}
            desk_stdio = {n: e for n, e in desk_srv.items() if _is_stdio(e) and n not in type_conflicts}
            snap = self.load()
            snap_servers = snap.get("servers", {})

            code_out, desk_out = {}, {}
            for name in sorted(set(code_stdio) | set(desk_stdio)):
                ce, de = code_stdio.get(name), desk_stdio.get(name)
                s = snap_servers.get(name)
                if ce and de:
                    if _same_server(ce, de):
                        code_out[name], desk_out[name] = ce, de
                    elif _exact(ce) == _exact(de):
                        code_out[name] = ce
                        desk_out[name] = _to_desktop(ce)
                        out.changed.append(name)
                    else:
                        c_changed = s is None or _exact(ce) != s.get("code")
                        d_changed = s is None or _exact(de) != s.get("desk")
                        if c_changed and not d_changed:
                            code_out[name] = ce
                            desk_out[name] = _to_desktop(ce)
                            out.changed.append(name)
                        elif d_changed and not c_changed:
                            code_out[name] = dict(de)
                            desk_out[name] = de
                            out.changed.append(name)
                        else:
                            code_out[name], desk_out[name] = ce, de
                            out.conflicts.append(name)
                elif ce and not de:
                    c_changed = s is None or _exact(ce) != s.get("code")
                    if s is not None and c_changed:
                        # deleted on desk BUT edited on code since the snapshot
                        # -> conflict, hold both (legacy defect fixed)
                        code_out[name] = ce
                        out.conflicts.append(name)
                    elif s is not None:
                        pass  # deleted on desk, unchanged on code -> propagate delete (drop from code_out)
                        out.changed.append(name)
                    else:
                        code_out[name] = ce
                        desk_out[name] = _to_desktop(ce)
                        out.changed.append(name)
                elif de and not ce:
                    d_changed = s is None or _exact(de) != s.get("desk")
                    if s is not None and d_changed:
                        desk_out[name] = de
                        out.conflicts.append(name)
                    elif s is not None:
                        out.changed.append(name)
                    else:
                        code_out[name] = dict(de)
                        desk_out[name] = de
                        out.changed.append(name)

            new_code_srv = dict(code_srv)
            for n in code_stdio:
                new_code_srv.pop(n, None)
            new_code_srv.update(code_out)
            new_desk_srv = dict(desk_srv)
            for n in desk_stdio:
                new_desk_srv.pop(n, None)
            new_desk_srv.update(desk_out)

            # backups verified BEFORE any write; failure => zero writes
            targets = []
            if new_code_srv != code_srv:
                targets.append((code_path, code, "mcpServers", new_code_srv))
            if new_desk_srv != desk_srv:
                targets.append((desk_path, desk, "mcpServers", new_desk_srv))
            for tpath, _doc, _key, _val in targets:
                if tpath.exists():
                    try:
                        lifecycle.backup_file(tpath, self.backup_dir)
                    except OSError as e:
                        self._j({"status": "failed-prewrite", "target": str(tpath), "reason": str(e)})
                        raise SyncError("backup failed for %s: %s — zero target writes" % (tpath, e)) from None
            committed = 0
            try:
                for tpath, doc, key, val in targets:
                    if fault_inject is not None:
                        fault_inject(committed)
                    doc[key] = val
                    _write_json_private(tpath, doc)
                    self._j({"status": "committed", "target": str(tpath)})
                    committed += 1
            except OSError as e:
                self._j({"status": "partial", "committed": committed, "total": len(targets),
                         "error": str(e)})
                raise SyncError("partial write: %d/%d committed: %s" % (committed, len(targets), e)) from None

            new_snap = dict(snap)  # preserve unknown fields
            new_snap["schema"] = SNAPSHOT_SCHEMA
            servers = {name: {"code": _exact(code_out[name]), "desk": _exact(desk_out[name])}
                       for name in code_out
                       if name in desk_out and name not in out.conflicts}
            # H4 (review): held conflicts MUST stay in the snapshot with their
            # last-known values, else the next run sees no baseline and re-propagates
            # the survivor — silently reverting the user's deletion
            for name in out.conflicts:
                if name in snap_servers:
                    servers[name] = snap_servers[name]
                else:
                    sides = {}
                    if name in code_out:
                        sides["code"] = _exact(code_out[name])
                    if name in desk_out:
                        sides["desk"] = _exact(desk_out[name])
                    servers[name] = sides
            new_snap["servers"] = servers
            self.save(new_snap)
            return out
        finally:
            lifecycle.release_lock(self.snap_path.parent)
