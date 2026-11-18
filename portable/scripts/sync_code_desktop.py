#!/usr/bin/env python3
"""
claude-code-desktop-sync - sync engine.

Keeps the MCP-server configuration consistent between:
  - Claude Code:    ~/.claude.json                         (key: "mcpServers")
  - Claude Desktop: <platform Desktop config>              (key: "mcpServers")

Desktop config targets (one run syncs every discovered target whose config file exists):
  - standard build:  macOS   ~/Library/Application Support/Claude/claude_desktop_config.json
                     Windows %APPDATA%\\Claude\\claude_desktop_config.json
                     Linux   $XDG_CONFIG_HOME/Claude/claude_desktop_config.json (or ~/.config/Claude/...)
  - 3P build:        macOS   ~/Library/Application Support/Claude-3p/claude_desktop_config.json
                     Windows %LOCALAPPDATA%\\Claude-3p\\claude_desktop_config.json
                             (MSIX: %LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Local\\Claude-3p\\...)
                     Linux   $XDG_CONFIG_HOME/Claude-3p/claude_desktop_config.json (or ~/.config/Claude-3p/...)
  Each target keeps its OWN snapshot: last-sync-state.json for the standard build,
  last-sync-state-3p.json for 3P (see desktop_config_targets()) -- a first sync against an
  empty 3P config must be purely additive, never a mass delete from ~/.claude.json.

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
  - 2.1.0 on-demand loading: a server may carry "x-cds-profile": "<name>"; only the ACTIVE profile's
    tagged servers are written to Desktop (default profile "full" = everything on, i.e. 2.0.x
    behaviour). Inactive-profile servers are removed from the Desktop configs only, never from
    Code, and are excluded from the snapshot so no merge path can delete them from Code.
    Per-project servers live in <project>/.mcp.json and never enter the global sync.

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

# 2.1.0 on-demand loading: servers may carry "x-cds-profile": "<name>"; only the active profile's
# servers are synced to Desktop. The active profile lives in ACTIVE_PROFILE_FILE (default "full").
ACTIVE_PROFILE_FILE = SYNC_HOME / "active-profile"
DEFAULT_PROFILE = "full"
PROFILE_MARKER = "x-cds-profile"

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


def desktop_config_targets():
    """All Claude Desktop config targets, as (label, config_path, state_file) tuples.

    CLAUDE_DESKTOP_CONFIG set => exactly that one target, keeping the legacy state file
    (unchanged contract for tests and non-standard setups). Otherwise the standard Desktop
    config plus the third-party ("3P") build are discovered per platform
    (claude.com/docs/third-party/claude-desktop/data-storage.md), each with its OWN snapshot:
    the standard target keeps last-sync-state.json (no migration, no re-baseline); the 3P
    target gets last-sync-state-3p.json. A target with no snapshot syncs purely additively --
    reusing the standard snapshot against an empty 3P file would make the three-way merge read
    every synced server as "deleted in Desktop" and remove it from ~/.claude.json.
    """
    override = os.environ.get("CLAUDE_DESKTOP_CONFIG")
    if override:
        return [("Desktop", Path(override), STATE_FILE)]
    if sys.platform == "darwin":
        base = HOME / "Library" / "Application Support"
        return [
            ("Desktop", base / "Claude" / "claude_desktop_config.json", STATE_FILE),
            ("3P Desktop", base / "Claude-3p" / "claude_desktop_config.json",
             SYNC_HOME / "last-sync-state-3p.json"),
        ]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        appdata = Path(appdata) if appdata else HOME / "AppData" / "Roaming"
        local = os.environ.get("LOCALAPPDATA")
        local = Path(local) if local else HOME / "AppData" / "Local"
        return [
            ("Desktop", appdata / "Claude" / "claude_desktop_config.json", STATE_FILE),
            ("3P Desktop", local / "Claude-3p" / "claude_desktop_config.json",
             SYNC_HOME / "last-sync-state-3p.json"),
            ("3P Desktop (MSIX)",
             local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Local" / "Claude-3p"
             / "claude_desktop_config.json",
             SYNC_HOME / "last-sync-state-3p-msix.json"),
        ]
    base = os.environ.get("XDG_CONFIG_HOME")
    # XDG spec: a relative XDG_CONFIG_HOME is invalid and must be ignored, not resolved vs CWD
    base = Path(base) if base and Path(base).is_absolute() else HOME / ".config"
    return [
        ("Desktop", base / "Claude" / "claude_desktop_config.json", STATE_FILE),
        ("3P Desktop", base / "Claude-3p" / "claude_desktop_config.json",
         SYNC_HOME / "last-sync-state-3p.json"),
    ]


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


def backup(path, stamp, tag=""):
    path = Path(path)
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    # tag keeps per-target backups distinct: every Desktop target's config shares the basename
    # claude_desktop_config.json and the stamp is only second-resolution, so without the label
    # two targets writing in the same run would overwrite each other's pre-run backup.
    dst = BACKUP_DIR / f"{tag}{path.name}.{stamp}.bak"
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

def load_state(state_file):
    """Returns (state_or_None, corrupt_bool). corrupt=True means the file exists but is unreadable
    -- distinct from a genuine first run (file absent)."""
    if not state_file.exists():
        return None, False
    try:
        state = load_json(state_file)
    except Exception:
        return None, True
    if not isinstance(state, dict) or not isinstance(state.get("servers", {}), dict):
        return None, True  # valid JSON but wrong shape: same unsafe-merge guard as unparseable
    return state, False


def save_state(state_file, servers, remote_names, skills):
    SYNC_HOME.mkdir(parents=True, exist_ok=True)
    write_json(state_file, {
        "servers": servers,
        "remote": sorted(remote_names),
        "skills": sorted(skills),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def load_active_profile():
    try:
        name = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return DEFAULT_PROFILE
    return name or DEFAULT_PROFILE


def save_active_profile(name):
    SYNC_HOME.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_PROFILE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(name + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ACTIVE_PROFILE_FILE)


def profile_of(entry):
    return entry.get(PROFILE_MARKER) if isinstance(entry, dict) else None


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
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            if f.read().strip() != str(os.getpid()):
                return  # our lock went stale and another run reclaimed it; leave theirs alone
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
    out_lines.clear()  # per-target output: the next target must not re-print this one's lines


def _run(label, desk_path, state_file, shared_manual=True, dry_run=False, profile=None):
    """Sync ONE Desktop target. The Code config is re-loaded on every call -- a previous
    target in the same run may have written it. shared_manual=True (first target only) emits
    the Code-derived manual reminders once per run instead of once per target. dry_run=True
    prints the planned changes and writes NOTHING. profile overrides the active profile
    (used by `profile <name> --dry-run`)."""
    tag = f"[{label}] "
    btag = "".join(c if (c.isalnum() or c in "-_") else "-" for c in label)
    btag = f"{btag}-" if btag else ""
    code = load_json(CODE_CONFIG)
    desk = load_json(desk_path)

    code_srv = code.get("mcpServers", {})
    desk_srv = desk.get("mcpServers", {})
    if not isinstance(code_srv, dict):
        say(f"[claude-sync] {tag}'{CODE_CONFIG.name}' has a non-object mcpServers; skipping (fix it to sync).")
        _flush()
        return
    if not isinstance(desk_srv, dict):
        say(f"[claude-sync] {tag}'{desk_path.name}' has a non-object mcpServers; skipping (fix it to sync).")
        _flush()
        return

    # A name that is stdio on one side but remote/other on the other must NOT be auto-synced -- that
    # would overwrite (and destroy) a connector + its token. Hold it as a conflict and exclude it from
    # reconciliation entirely, so both sides keep their own entry untouched.
    type_conflicts = sorted(
        n for n in (set(code_srv) & set(desk_srv))
        if is_stdio(code_srv[n]) != is_stdio(desk_srv[n])
    )

    # ----- profile gating (2.1.0 on-demand loading) -----
    # "full" (the default) is the wildcard profile: every tagged server is active, matching
    # 2.0.x behaviour. Any other active profile gates: tagged servers of other profiles are
    # inactive.
    active = profile if profile else load_active_profile()

    def tag_of(name):
        e = code_srv.get(name)
        if e is None:
            e = desk_srv.get(name)
        return profile_of(e)

    inactive = []
    for n in set(code_srv) | set(desk_srv):
        t = tag_of(n)
        if t is not None and t != active and active != DEFAULT_PROFILE:
            inactive.append(n)
    inactive.sort()
    removed_inactive = [n for n in inactive if n in desk_srv]
    desk_srv_orig = dict(desk_srv)  # change detection must compare against the PRE-strip content
    if removed_inactive:
        # Deliberate Desktop-side-only removal: inactive-profile servers leave the Desktop
        # config but stay in Code. They are excluded from the reconcile inputs and from the
        # rebuilt snapshot, so no merge path can read them as "deleted" and ever touch Code.
        desk_srv = {n: e for n, e in desk_srv.items() if n not in inactive}

    code_stdio = {n: e for n, e in stdio_of(code_srv).items() if n not in type_conflicts and n not in inactive}
    desk_stdio = {n: e for n, e in stdio_of(desk_srv).items() if n not in type_conflicts and n not in inactive}
    remote_names = {n for n, e in code_srv.items() if is_remote(e)} - set(type_conflicts)

    state, corrupt = load_state(state_file)
    if corrupt:
        say(f"[claude-sync] {tag}snapshot at {state_file} is unreadable; skipping this target to avoid "
            f"an unsafe merge. Delete that file to force a clean re-baseline.")
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
        changed = True
        if dry_run:
            say(f"[claude-sync] {tag}DRY-RUN would update Claude Code config (no write)")
        else:
            b = backup(CODE_CONFIG, stamp, btag)
            code["mcpServers"] = new_code_srv
            write_json(CODE_CONFIG, code)
            say(f"[claude-sync] {tag}updated Claude Code config  (backup: {b})")
            say(f"  {tag}(MCP changes take effect in your NEXT Claude Code session.)")
    if new_desk_srv != desk_srv_orig:
        changed = True
        if dry_run:
            say(f"[claude-sync] {tag}DRY-RUN would update Desktop config {desk_path} (no write)")
        else:
            b = backup(desk_path, stamp, btag)
            desk["mcpServers"] = new_desk_srv
            write_json(desk_path, desk)
            say(f"[claude-sync] {tag}updated Desktop config {desk_path}  (backup: {b})")

    def line(label, names):
        if names:
            say(f"  {tag}{label}: {', '.join(sorted(names))}")

    if changed:
        line("+ added to Desktop", rep["added_desk"])
        line("+ added to Code", rep["added_code"])
        line("~ updated on Desktop", rep["updated_desk"])
        line("~ updated on Code", rep["updated_code"])
        line("- removed from Desktop", rep["removed_desk"])
        line("- removed from Code", rep["removed_code"])
        line("- removed from Desktop (inactive profile '%s')" % active, removed_inactive)

    if rep["conflicts"]:
        say(f"[claude-sync] {tag}CONFLICT: these servers were changed differently on BOTH sides and were "
            f"left untouched -- resolve by hand: {', '.join(sorted(rep['conflicts']))}")
    if type_conflicts:
        say(f"[claude-sync] {tag}TYPE CONFLICT: these names are a stdio server on one side and a "
            f"remote/other server on the other; left BOTH untouched to avoid destroying a connector -- "
            f"rename or reconcile by hand: {', '.join(type_conflicts)}")

    # ----- manual-step reminders -----
    manual = []
    new_remote = remote_names - prev_remote
    if shared_manual and remote_names:
        new_tag = " (NEW)" if new_remote else ""  # NB: not `tag` — that is the target label
        manual.append(
            f"HTTP/auth MCP server(s) {sorted(remote_names)}{new_tag} cannot go in the Desktop config "
            "file. To use them in Desktop: Settings -> Connectors -> Add custom connector.")
    if unresolved:
        manual.append(
            f"Could not resolve an absolute path for launcher(s) {sorted(set(unresolved))}; they were "
            "written to Desktop as-is and may fail to start there (Desktop has no shell PATH). Set an "
            "absolute 'command' for these servers.")
    new_skills = set(skills) - prev_skills
    if shared_manual and new_skills and state is not None:
        manual.append(
            f"New skill(s) in Claude Code: {sorted(new_skills)}. Skills don't auto-sync -- add them "
            "in Claude Desktop's UI if you want them there too.")

    if (changed or new_skills or new_remote or unresolved or rep["conflicts"] or type_conflicts) and manual:
        say(f"[claude-sync] {tag}manual steps (not file-syncable):")
        for m in manual:
            say(f"  - {m}")

    if not dry_run:
        save_state(state_file, new_snapshot, remote_names, skills)
    _flush()


USAGE = """sync_code_desktop.py — sync stdio MCP servers between Claude Code and Claude Desktop.

Usage:
  python3 sync_code_desktop.py            # run one sync now (the SessionStart hook route; no args)
  python3 sync_code_desktop.py --help     # print this help and exit 0 (never runs a sync)

Commands (2.1.0, on-demand loading):
  profile                             show the active profile (default: full)
  profile <name>                      set the active profile, then sync: servers tagged
                                      "x-cds-profile": "<name>" in the Code config are synced
                                      to Desktop ONLY while that profile is active; switching
                                      away removes them from the Desktop configs (never Code)
  profile <name> --dry-run            print what would change; write NOTHING
  project move <server> <dir>         move a server from the global Code config into <dir>/.mcp.json
                                      (project scope in Claude Code; the global Desktop sync
                                      drops it from the Desktop configs on the next run)
  project show <dir>                  list the MCP servers in <dir>/.mcp.json

Unknown commands/flags exit 2 with this usage on stderr. The no-argument route is the hook
contract and is unchanged.

Targets: with CLAUDE_DESKTOP_CONFIG set, exactly that one file is synced (legacy state file
last-sync-state.json). Otherwise every discovered Desktop config whose file exists is synced
in one run under one lock: the standard build (Claude/) plus third-party "3P" builds
(Claude-3p/, incl. the Windows MSIX package path), each with its OWN snapshot
(last-sync-state-3p.json for 3P) so a first sync against an empty 3P config is purely
additive and never deletes servers from ~/.claude.json. The Code config is re-loaded before
each target. --help lists the targets discovered on this machine.

Environment variables read: CLAUDE_CODE_CONFIG, CLAUDE_DESKTOP_CONFIG, CLAUDE_SYNC_HOME
(override the default config/state paths).
"""


def usage_text():
    text = USAGE.rstrip() + "\n\nDesktop targets discovered on this machine:\n"
    for label, path, state in desktop_config_targets():
        mark = "" if path.exists() else "   (config file not present — skipped this run)"
        text += f"  - {label}: {path}   [state: {state.name}]{mark}\n"
    text += f"\nActive profile: {load_active_profile()}\n"
    return text


def run_sync(dry_run=False, profile=None):
    """The no-arg hook route (and the body of `profile <name>`): sync all existing targets."""
    if not CODE_CONFIG.exists():
        return  # Claude Code config not present here -> nothing to sync
    targets = [t for t in desktop_config_targets() if t[1].exists()]
    if not targets:
        return  # no Desktop config present -> nothing to sync
    if dry_run:
        for i, (label, desk_path, state_file) in enumerate(targets):
            _run(label, desk_path, state_file, shared_manual=(i == 0), dry_run=True, profile=profile)
        return
    if not acquire_lock():
        return  # another session is already syncing
    try:
        for i, (label, desk_path, state_file) in enumerate(targets):
            try:
                _run(label, desk_path, state_file, shared_manual=(i == 0), profile=profile)
            except Exception as e:  # one broken target must not abort the others; flush what it said
                say(f"[claude-sync] [{label}] skipped (non-fatal): {e}")
                _flush()
    finally:
        release_lock()


def cmd_profile(args):
    dry = "--dry-run" in args
    names = [a for a in args if a != "--dry-run"]
    if len(names) > 1 or (names and names[0].startswith("-")):
        sys.stderr.write("usage: sync_code_desktop.py profile [<name>] [--dry-run]\n")
        sys.exit(2)
    if not names:
        say(f"[claude-sync] active profile: {load_active_profile()}")
        _flush()
        return
    name = names[0]
    if not name or "/" in name or "\\" in name:
        sys.stderr.write("profile name must be a plain word (no path separators)\n")
        sys.exit(2)
    if dry:
        say(f"[claude-sync] DRY-RUN: would set active profile to '{name}', then sync:")
        run_sync(dry_run=True, profile=name)
        return
    save_active_profile(name)
    say(f"[claude-sync] active profile set to '{name}'")
    run_sync()


def cmd_project(args):
    usage = "usage: sync_code_desktop.py project <move <server> <dir> | show <dir>>\n"
    if not args:
        sys.stderr.write(usage)
        sys.exit(2)
    sub = args[0]
    if sub == "show" and len(args) == 2:
        p = Path(args[1]) / ".mcp.json"
        if not p.exists():
            say(f"[claude-sync] project {args[1]}: no .mcp.json")
            _flush()
            return
        data = load_json(p)
        ms = data.get("mcpServers", {}) if isinstance(data, dict) else {}
        names = sorted(ms) if isinstance(ms, dict) else []
        say(f"[claude-sync] project {args[1]}: MCP servers: {', '.join(names) if names else 'none'}")
        for n in names:
            t = profile_of(ms[n])
            if t:
                say(f"  - {n} (profile: {t})")
        _flush()
        return
    if sub == "move" and len(args) == 3:
        server, dirpath = args[1], Path(args[2])
        if not dirpath.is_dir():
            sys.stderr.write(f"project dir does not exist: {dirpath}\n")
            sys.exit(2)
        if not CODE_CONFIG.exists():
            sys.stderr.write("no Claude Code config present\n")
            sys.exit(2)
        if not acquire_lock():
            sys.stderr.write("another sync is running; try again\n")
            sys.exit(2)
        try:
            code = load_json(CODE_CONFIG)
            srv = code.get("mcpServers", {})
            if not isinstance(srv, dict) or server not in srv:
                sys.stderr.write(f"no such server in {CODE_CONFIG.name}: {server}\n")
                sys.exit(2)
            entry = srv.pop(server)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            b = backup(CODE_CONFIG, stamp, "cli-")
            code["mcpServers"] = srv
            write_json(CODE_CONFIG, code)
            say(f"[claude-sync] moved '{server}' out of {CODE_CONFIG.name} (backup: {b})")
            p = dirpath / ".mcp.json"
            data = load_json(p) if p.exists() else {}
            if not isinstance(data, dict):
                data = {}
            ms = data.get("mcpServers", {})
            if not isinstance(ms, dict):
                ms = {}
            ms[server] = entry
            data["mcpServers"] = ms
            if p.exists():
                b2 = backup(p, stamp, "cli-")
                say(f"[claude-sync] (backup of existing .mcp.json: {b2})")
            write_json(p, data)
            say(f"[claude-sync] wrote {p} — project-scoped; the global Desktop sync drops it "
                f"from the Desktop configs on the next run")
        finally:
            release_lock()
        _flush()
        return
    sys.stderr.write(usage)
    sys.exit(2)


def main():
    argv = sys.argv[1:]
    if argv:
        if argv in (["--help"], ["-h"]):
            sys.stdout.write(usage_text())
            return
        if argv[0] == "profile":
            cmd_profile(argv[1:])
            return
        if argv[0] == "project":
            cmd_project(argv[1:])
            return
        sys.stderr.write("sync_code_desktop.py: unknown argument %r\n%s" % (argv[0], usage_text()))
        sys.exit(2)
    run_sync()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block session start
        sys.stderr.write(f"[claude-sync] skipped (non-fatal): {e}\n")
    sys.exit(0)
