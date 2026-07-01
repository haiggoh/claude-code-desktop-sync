#!/usr/bin/env python3
"""
claude-code-desktop-sync - sync engine.

Keeps the MCP-server configuration consistent between:
  - Claude Code:    ~/.claude.json                         (key: "mcpServers")
  - Claude Desktop: <platform Desktop config>              (key: "mcpServers")

Desktop config location by platform:
  - macOS:   ~/Library/Application Support/Claude/claude_desktop_config.json
  - Windows: %APPDATA%\\Claude\\claude_desktop_config.json
  - Linux:   $XDG_CONFIG_HOME/Claude/claude_desktop_config.json  (or ~/.config/Claude/...)

Behaviour:
  - Silent auto-apply. Prints ONLY when something changed or a manual step is pending.
  - Per-server three-way reconciliation against a stored snapshot (no whole-file mtime guessing):
      * changed on exactly one side  -> that side's version is propagated
      * added on one side            -> added to the other
      * deleted on one side (was in the snapshot, now gone) -> removed from the other
      * changed differently on BOTH sides -> a real conflict: both sides are left untouched and the
        conflict is reported for the user to resolve (never silently overwritten)
  - Only stdio MCP servers (command/args/env) are auto-synced. HTTP / header-auth servers (e.g. a PAT
    github connector) are reported as a manual "Connectors UI" step, never written to Desktop.
  - A name that is stdio on one side but a remote/other server on the other is a TYPE CONFLICT: both
    sides are left untouched and it is reported. (Prevents silently overwriting a connector + its token.)

---------------------------------------------------------------------------------------------------
Design notes for maintainers (read before changing reconcile()):

  * `exact(entry)` is THE canonical identity / diff key for the whole engine. It is used for change
    detection (vs the snapshot), for the before/after write gate, and inside `same_server`. It and
    `to_desktop_entry()` (the Desktop write shape) MUST enumerate the SAME set of fields.
    >> EXTENDING: to sync a new stdio field (e.g. "cwd"), add it to BOTH exact() and to_desktop_entry()
       in the same way. Add it to only one and servers will look permanently changed (churn) or
       permanently in-sync (silent no-sync). If it distinguishes transports, also update is_remote().

  * The snapshot (~/.claude/desktop-sync/last-sync-state.json) records, per server, the exact
    last-synced value on EACH side. This is what makes change detection precise: the relative->absolute
    launcher rewrite for Desktop (e.g. "npx" -> "/opt/homebrew/bin/npx") is recorded as the Desktop-side
    value, so it is not mistaken for a user edit; a genuine command change IS detected.

  * The snapshot is rebuilt from scratch each run and DELIBERATELY excludes conflicts and one-sided
    (deleted) servers. Do NOT seed it from the previous snapshot: excluding a held conflict is what
    makes the next run re-detect and re-hold it instead of silently baselining (and later overwriting)
    an unresolved conflict. See the comment at the rebuild loop.

Safe by construction: an exclusive lock serialises concurrent sessions; every file is backed up before
it is written; only the "mcpServers" block is touched (all other keys and non-stdio servers are left
byte-identical); writes are atomic + fsync'd; and main() never raises out of the process.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

HOME = Path.home()
# Config paths can be overridden via env (used by tests and non-standard setups).
CODE_CONFIG = Path(os.environ.get("CLAUDE_CODE_CONFIG", HOME / ".claude.json"))
SKILLS_DIR = HOME / ".claude" / "skills"

SYNC_HOME = Path(os.environ.get("CLAUDE_SYNC_HOME", HOME / ".claude" / "desktop-sync"))
BACKUP_DIR = SYNC_HOME / "backups"
STATE_FILE = SYNC_HOME / "last-sync-state.json"
LOCK_FILE = SYNC_HOME / ".sync.lock"
LOCK_STALE_SECONDS = 120

# Extra directories to search for a launcher when it is not on PATH (Desktop has no shell PATH).
COMMON_BIN_DIRS = [
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
    str(HOME / ".local" / "bin"),
    str(HOME / ".nvm" / "current" / "bin"),
    "/opt/local/bin",
]
if os.name == "nt":
    _appdata = os.environ.get("APPDATA", "")
    _localappdata = os.environ.get("LOCALAPPDATA", "")
    _pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    COMMON_BIN_DIRS += [d for d in [
        os.path.join(_appdata, "npm") if _appdata else "",
        os.path.join(_pf, "nodejs"),
        os.path.join(_localappdata, "Microsoft", "WinGet", "Links") if _localappdata else "",
        os.path.join(_localappdata, "Programs", "Python") if _localappdata else "",
    ] if d]

out_lines = []


def say(msg=""):
    out_lines.append(msg)


def desktop_config_path():
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        base = Path(base) if base else HOME / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    base = os.environ.get("XDG_CONFIG_HOME")
    base = Path(base) if base else HOME / ".config"
    return base / "Claude" / "claude_desktop_config.json"


DESKTOP_CONFIG = desktop_config_path()


# ---------- json helpers ----------

def load_json(path):
    # utf-8-sig transparently strips a UTF-8 BOM (written by some Windows editors) and also decodes
    # plain UTF-8, so a BOM'd config does not silently defeat the sync.
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:  # write back without a BOM
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backup(path, stamp):
    path = Path(path)
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"{path.name}.{stamp}.bak"
    shutil.copy2(path, dst)
    try:
        os.chmod(dst, 0o600)  # may contain a plaintext token
    except OSError:
        pass
    return str(dst)


# ---------- server classification ----------

def is_remote(entry):
    """A server that cannot be represented in the Desktop config file (HTTP / hosted / header-auth)."""
    if not isinstance(entry, dict):
        return False
    if str(entry.get("type", "")).lower() in ("http", "sse", "streamable-http", "ws"):
        return True
    if entry.get("url") or entry.get("headers"):
        return True
    return False


def is_stdio(entry):
    return isinstance(entry, dict) and "command" in entry and not is_remote(entry)


def exact(entry):
    """The canonical identity/diff key for an stdio server. MUST enumerate the same fields as
    to_desktop_entry() (see the maintainer notes at the top of this file)."""
    return {
        "command": entry.get("command", ""),
        "args": list(entry.get("args", [])),
        "env": dict(entry.get("env", {})),
    }


def resolve_for_desktop(command):
    """Resolve a launcher to an absolute path (Desktop has no shell PATH). Returns the command
    unchanged if it cannot be located; the caller reports unresolved commands to the user."""
    if not command or os.path.isabs(command):
        return command
    found = shutil.which(command)
    if found:
        return found
    exts = [""] + ([e for e in os.environ.get("PATHEXT", "").split(os.pathsep) if e]
                   if os.name == "nt" else [".cmd", ".exe"])
    for d in COMMON_BIN_DIRS:
        for ext in exts:
            cand = Path(d) / (command + ext)
            if cand.exists():
                return str(cand)
    return command


def to_desktop_entry(entry):
    """The Desktop write shape for an stdio server: same fields as exact(), but with the launcher
    resolved to an absolute path. MUST enumerate the same fields as exact()."""
    e = {"command": resolve_for_desktop(entry.get("command", ""))}
    if "args" in entry:
        e["args"] = list(entry["args"])
    if entry.get("env"):
        e["env"] = dict(entry["env"])
    return e


def same_server(code_entry, desk_entry):
    """True if the Desktop entry already equals what we would write from the Code entry -- i.e. the
    two sides are in sync, tolerating the relative->absolute launcher rewrite."""
    return exact(to_desktop_entry(code_entry)) == exact(desk_entry)


# ---------- state ----------

def load_state():
    """Returns (state_or_None, corrupt_bool). corrupt=True means the file exists but is unreadable
    -- distinct from a genuine first run (file absent)."""
    if not STATE_FILE.exists():
        return None, False
    try:
        return load_json(STATE_FILE), False
    except Exception:
        return None, True


def save_state(servers, remote_names, skills):
    SYNC_HOME.mkdir(parents=True, exist_ok=True)
    write_json(STATE_FILE, {
        "servers": servers,
        "remote": sorted(remote_names),
        "skills": sorted(skills),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def list_skills():
    try:
        return sorted(
            d.name for d in SKILLS_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except Exception:
        return []


# ---------- locking (cross-platform, no fcntl needed) ----------

def acquire_lock():
    SYNC_HOME.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(LOCK_FILE)
            except OSError:
                age = 0
            if age > LOCK_STALE_SECONDS:
                try:
                    os.unlink(LOCK_FILE)
                    continue
                except OSError:
                    return False
            return False
    return False


def release_lock():
    try:
        os.unlink(LOCK_FILE)
    except OSError:
        pass


# ---------- reconciliation ----------

def stdio_of(servers):
    return {n: e for n, e in servers.items() if is_stdio(e)}


def reconcile(code_stdio, desk_stdio, snap_servers):
    """Per-server three-way merge over stdio servers only.

    Returns (code_out, desk_out, report, new_snapshot, unresolved). code_out/desk_out are the desired
    stdio servers for each side after this run; the caller merges them back into the full config.
    """
    code_out, desk_out = {}, {}
    rep = {"added_code": [], "added_desk": [], "removed_code": [], "removed_desk": [],
           "updated_code": [], "updated_desk": [], "conflicts": []}
    unresolved = []

    def push_to_desk(name, ce):
        de = to_desktop_entry(ce)
        if de.get("command") and not os.path.isabs(de["command"]):
            unresolved.append(name)
        return de

    for name in sorted(set(code_stdio) | set(desk_stdio)):
        ce, de = code_stdio.get(name), desk_stdio.get(name)
        snap = snap_servers.get(name)

        if ce and de:
            if same_server(ce, de):
                # Already in sync (Desktop holds the resolved form of Code's launcher).
                code_out[name], desk_out[name] = ce, de
            elif exact(ce) == exact(de):
                # Both sides hold the SAME raw definition (e.g. both "npx"). Not a conflict; Desktop
                # just needs the launcher resolved to an absolute path so it can actually start it.
                code_out[name] = ce
                desk_out[name] = push_to_desk(name, ce)
                if exact(desk_out[name]) != exact(de):
                    rep["updated_desk"].append(name)
            else:
                c_changed = snap is None or exact(ce) != snap.get("code")
                d_changed = snap is None or exact(de) != snap.get("desk")
                if c_changed and not d_changed:
                    code_out[name] = ce
                    desk_out[name] = push_to_desk(name, ce)
                    rep["updated_desk"].append(name)
                elif d_changed and not c_changed:
                    code_out[name] = dict(de)
                    desk_out[name] = de
                    rep["updated_code"].append(name)
                elif c_changed and d_changed:
                    # Diverged on both sides (or no baseline to arbitrate) -> real conflict; hold both.
                    code_out[name], desk_out[name] = ce, de
                    rep["conflicts"].append(name)
                else:
                    # Neither side changed vs the snapshot yet not same_server -- unreachable with our
                    # own writes (we only ever baseline same_server pairs); keep both, defensively.
                    code_out[name], desk_out[name] = ce, de
        elif ce and not de:
            if snap is not None:            # was in the snapshot, now gone from Desktop -> delete
                rep["removed_code"].append(name)
            else:                           # newly added in Code -> push to Desktop
                code_out[name] = ce
                desk_out[name] = push_to_desk(name, ce)
                rep["added_desk"].append(name)
        elif de and not ce:
            if snap is not None:            # was in the snapshot, now gone from Code -> delete
                rep["removed_desk"].append(name)
            else:                           # newly added in Desktop -> pull into Code
                code_out[name] = dict(de)
                desk_out[name] = de
                rep["added_code"].append(name)

    # Rebuild the baseline from the reconciled result. Record ONLY servers that ended up present on
    # BOTH sides and are NOT conflicts. Conflicts and one-sided (deleted) servers are intentionally
    # excluded so that (1) an unresolved conflict re-conflicts next run instead of being silently
    # baselined and later overwritten, and (2) a genuine deletion is not resurrected. Never seed this
    # from the previous snapshot.
    new_snapshot = {
        name: {"code": exact(code_out[name]), "desk": exact(desk_out[name])}
        for name in code_out
        if name in desk_out and name not in rep["conflicts"]
    }
    return code_out, desk_out, rep, new_snapshot, unresolved


# ---------- main ----------

def _flush():
    text = "\n".join(out_lines).strip()
    if text:
        print(text)


def _run():
    code = load_json(CODE_CONFIG)
    desk = load_json(DESKTOP_CONFIG)

    code_srv = code.get("mcpServers", {})
    desk_srv = desk.get("mcpServers", {})
    if not isinstance(code_srv, dict):
        say(f"[claude-sync] '{CODE_CONFIG.name}' has a non-object mcpServers; skipping (fix it to sync).")
        _flush()
        return
    if not isinstance(desk_srv, dict):
        say(f"[claude-sync] '{DESKTOP_CONFIG.name}' has a non-object mcpServers; skipping (fix it to sync).")
        _flush()
        return

    # A name that is stdio on one side but remote/other on the other must NOT be auto-synced -- that
    # would overwrite (and destroy) a connector + its token. Hold it as a conflict and exclude it from
    # reconciliation entirely, so both sides keep their own entry untouched.
    type_conflicts = sorted(
        n for n in (set(code_srv) & set(desk_srv))
        if is_stdio(code_srv[n]) != is_stdio(desk_srv[n])
    )

    code_stdio = {n: e for n, e in stdio_of(code_srv).items() if n not in type_conflicts}
    desk_stdio = {n: e for n, e in stdio_of(desk_srv).items() if n not in type_conflicts}
    remote_names = {n for n, e in code_srv.items() if is_remote(e)} - set(type_conflicts)

    state, corrupt = load_state()
    if corrupt:
        say(f"[claude-sync] snapshot at {STATE_FILE} is unreadable; skipping this run to avoid an "
            f"unsafe merge. Delete that file to force a clean re-baseline.")
        _flush()
        return
    snap_servers = (state or {}).get("servers", {})
    prev_remote = set((state or {}).get("remote", []))
    prev_skills = set((state or {}).get("skills", []))

    skills = list_skills()
    code_out, desk_out, rep, new_snapshot, unresolved = reconcile(code_stdio, desk_stdio, snap_servers)

    # Merge the reconciled stdio servers back into each full config, leaving every other entry
    # (remote servers, type-conflict entries, unrelated keys) exactly as it was.
    def merged(full_srv, managed_stdio, out):
        new = dict(full_srv)
        for n in managed_stdio:       # drop the stdio servers we manage...
            new.pop(n, None)
        new.update(out)               # ...and replace them with the reconciled result
        return new

    new_code_srv = merged(code_srv, code_stdio, code_out)
    new_desk_srv = merged(desk_srv, desk_stdio, desk_out)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    changed = False
    if new_code_srv != code_srv:
        b = backup(CODE_CONFIG, stamp)
        code["mcpServers"] = new_code_srv
        write_json(CODE_CONFIG, code)
        changed = True
        say(f"[claude-sync] updated Claude Code config  (backup: {b})")
        say("  (MCP changes take effect in your NEXT Claude Code session.)")
    if new_desk_srv != desk_srv:
        b = backup(DESKTOP_CONFIG, stamp)
        desk["mcpServers"] = new_desk_srv
        write_json(DESKTOP_CONFIG, desk)
        changed = True
        say(f"[claude-sync] updated Claude Desktop config  (backup: {b})")

    def line(label, names):
        if names:
            say(f"  {label}: {', '.join(sorted(names))}")

    if changed:
        line("+ added to Desktop", rep["added_desk"])
        line("+ added to Code", rep["added_code"])
        line("~ updated on Desktop", rep["updated_desk"])
        line("~ updated on Code", rep["updated_code"])
        line("- removed from Desktop", rep["removed_desk"])
        line("- removed from Code", rep["removed_code"])

    if rep["conflicts"]:
        say(f"[claude-sync] CONFLICT: these servers were changed differently on BOTH sides and were "
            f"left untouched -- resolve by hand: {', '.join(sorted(rep['conflicts']))}")
    if type_conflicts:
        say(f"[claude-sync] TYPE CONFLICT: these names are a stdio server on one side and a "
            f"remote/other server on the other; left BOTH untouched to avoid destroying a connector -- "
            f"rename or reconcile by hand: {', '.join(type_conflicts)}")

    # ----- manual-step reminders -----
    manual = []
    new_remote = remote_names - prev_remote
    if remote_names:
        tag = " (NEW)" if new_remote else ""
        manual.append(
            f"HTTP/auth MCP server(s) {sorted(remote_names)}{tag} cannot go in the Desktop config "
            "file. To use them in Desktop: Settings -> Connectors -> Add custom connector.")
    if unresolved:
        manual.append(
            f"Could not resolve an absolute path for launcher(s) {sorted(set(unresolved))}; they were "
            "written to Desktop as-is and may fail to start there (Desktop has no shell PATH). Set an "
            "absolute 'command' for these servers.")
    new_skills = set(skills) - prev_skills
    if new_skills and state is not None:
        manual.append(
            f"New skill(s) in Claude Code: {sorted(new_skills)}. Skills don't auto-sync -- add them "
            "in Claude Desktop's UI if you want them there too.")

    if (changed or new_skills or new_remote or unresolved or rep["conflicts"] or type_conflicts) and manual:
        say("[claude-sync] manual steps (not file-syncable):")
        for m in manual:
            say(f"  - {m}")

    save_state(new_snapshot, remote_names, skills)
    _flush()


def main():
    if not CODE_CONFIG.exists() or not DESKTOP_CONFIG.exists():
        return  # one or both apps not installed here -> nothing to sync
    if not acquire_lock():
        return  # another session is already syncing
    try:
        _run()
    finally:
        release_lock()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block session start
        sys.stderr.write(f"[claude-sync] skipped (non-fatal): {e}\n")
    sys.exit(0)
