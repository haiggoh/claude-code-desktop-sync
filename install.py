#!/usr/bin/env python3
"""
Standalone installer for claude-code-desktop-sync (the non-plugin path).

Installs into a vanilla Claude Code + Claude Desktop setup:
  1. Copies the sync engine to ~/.claude/desktop-sync/sync_code_desktop.py
  2. Adds a SessionStart hook to ~/.claude/settings.json  (merged, never clobbered)
  3. Adds a standing-instruction block to ~/.claude/CLAUDE.md (marker-bounded, idempotent)

Safe: backs up settings.json and CLAUDE.md (into ~/.claude/desktop-sync/backups, mode 600) before
editing, is idempotent (re-runnable), and touches nothing else. Run `python3 uninstall.py` to reverse.

NOTE: if you installed via the Claude Code plugin system (`/plugin install ...`), do NOT also run
this installer -- the plugin already registers the SessionStart hook, and running both would create
two hooks that both sync on every session.

Usage:
    python3 install.py            # install / update
    python3 install.py --dry-run  # show what would change, write nothing
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
SRC = REPO / "bin" / "sync_code_desktop.py"
SNIPPET = REPO / "templates" / "CLAUDE.snippet.md"

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
INSTALL_DIR = CLAUDE_DIR / "desktop-sync"
INSTALLED_SCRIPT = INSTALL_DIR / "sync_code_desktop.py"
BACKUP_DIR = INSTALL_DIR / "backups"

# We identify OUR SessionStart hook by the engine filename appearing in its command. This is the sole
# dedupe/removal key and MUST stay identical in uninstall.py. Other users' hooks are never touched.
HOOK_MARKER = INSTALLED_SCRIPT.name  # "sync_code_desktop.py"

BEGIN = "<!-- claude-code-desktop-sync:begin -->"
END = "<!-- claude-code-desktop-sync:end -->"
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)

DRY = "--dry-run" in sys.argv


def log(msg):
    print(msg)


def interpreter():
    return shutil.which("python3") or shutil.which("python") or sys.executable


def backup(path, stamp):
    if not path.exists():
        return
    if not DRY:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = BACKUP_DIR / f"{path.name}.{stamp}.pre-sync-install.bak"
        shutil.copy2(path, dst)
        try:
            os.chmod(dst, 0o600)  # may contain a plaintext token
        except OSError:
            pass
        log(f"  backed up {path.name} -> {dst}")
    else:
        log(f"  would back up {path.name}")


def hook_command():
    """Absolute command string for the SessionStart hook (standalone install)."""
    def q(s):
        return f'"{s}"' if " " in str(s) else str(s)
    return f"{q(interpreter())} {q(INSTALLED_SCRIPT)}"


def install_script():
    log("• Installing sync engine")
    if not DRY:
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC, INSTALLED_SCRIPT)
        os.chmod(INSTALLED_SCRIPT, 0o755)
    log(f"  -> {INSTALLED_SCRIPT}")


def install_hook(stamp):
    """Returns True if the hook is registered (or already present), False if it could not be."""
    log("• Registering SessionStart hook in settings.json")
    settings = {}
    if SETTINGS.exists():
        try:
            # utf-8-sig tolerates a UTF-8 BOM (some Windows editors add one).
            settings = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
        except Exception as e:
            log(f"  ! settings.json is not valid JSON ({e}); leaving it untouched.")
            log("    Fix the file and re-run, or add this SessionStart command manually:")
            log(f"    {hook_command()}")
            return False
    if not isinstance(settings, dict):
        log("  ! settings.json is not a JSON object; leaving it untouched.")
        return False

    cmd = hook_command()
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        log("  ! settings.json 'hooks' is not an object; leaving it untouched.")
        return False

    # Normalize SessionStart to a list of groups. Some hand-written/third-party configs store a single
    # group object instead of a list; accept that, but refuse shapes we don't understand rather than crash.
    session_start = hooks.get("SessionStart")
    if session_start is None:
        session_start = []
    elif isinstance(session_start, dict):
        session_start = [session_start]
    elif not isinstance(session_start, list):
        log("  ! settings.json SessionStart has an unexpected shape; leaving it untouched.")
        return False
    hooks["SessionStart"] = session_start

    refreshed = False
    for g in session_start:
        if not isinstance(g, dict):
            continue
        for h in g.get("hooks", []) if isinstance(g.get("hooks"), list) else []:
            if isinstance(h, dict) and HOOK_MARKER in str(h.get("command", "")):
                h["command"] = cmd
                refreshed = True
    if refreshed:
        log("  hook already present — command refreshed.")
    else:
        session_start.append({"hooks": [{"type": "command", "command": cmd}]})
        log("  hook added.")

    backup(SETTINGS, stamp)
    if not DRY:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, SETTINGS)
    log(f"  command: {cmd}")
    return True


def install_snippet(stamp):
    log("• Adding standing instruction to CLAUDE.md")
    existing = CLAUDE_MD.read_text(encoding="utf-8-sig") if CLAUDE_MD.exists() else ""
    # Remove ALL existing blocks (dedupe) and any dangling lone markers, then append one clean block.
    body = BLOCK_RE.sub("", existing).replace(BEGIN, "").replace(END, "").rstrip()
    block = f"{BEGIN}\n{SNIPPET.read_text(encoding='utf-8').strip()}\n{END}"
    new = (body + "\n\n" + block + "\n") if body.strip() else (block + "\n")
    backup(CLAUDE_MD, stamp)
    if not DRY:
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        CLAUDE_MD.write_text(new, encoding="utf-8")
    log(f"  wrote standing-instruction block to {CLAUDE_MD}")


def main():
    if not SRC.exists() or not SNIPPET.exists():
        sys.exit("error: run this from inside the cloned repo (bin/ and templates/ missing).")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log("claude-code-desktop-sync installer" + ("  [DRY RUN]" if DRY else ""))
    log(f"  home: {HOME}")
    log("")
    install_script()
    hook_ok = install_hook(stamp)
    install_snippet(stamp)
    log("")
    if DRY:
        log("Dry run complete — no files were changed.")
        return

    log("Done. Running an initial sync now...")
    log("-" * 60)
    sys.stdout.flush()
    rc = subprocess.run([interpreter(), str(INSTALLED_SCRIPT)]).returncode
    log("-" * 60)
    if rc != 0:
        log(f"  ! initial sync exited with code {rc} (see any message above).")
    if hook_ok:
        log("Installed. The sync runs automatically at the start of each Claude Code session.")
    else:
        log("PARTIAL INSTALL: the SessionStart hook was NOT registered (see the settings.json")
        log("warning above). Auto-sync will NOT run until you fix settings.json and re-run.")
        log(f"You can sync manually anytime with:  {hook_command()}")
    log("Reference: docs/SYNC-REFERENCE.md   |   Uninstall: python3 uninstall.py")


if __name__ == "__main__":
    main()
