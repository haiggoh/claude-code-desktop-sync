#!/usr/bin/env python3
"""
Uninstaller for the STANDALONE (install.py) setup of claude-code-desktop-sync.

Reverses install.py:
  1. Removes the SessionStart hook entry from ~/.claude/settings.json
  2. Removes ALL marker-bounded blocks (and any dangling markers) from ~/.claude/CLAUDE.md
  3. Leaves the installed script, backups, and sync state in place unless --purge is given.

If you installed via the Claude Code PLUGIN system, this script cannot remove the hook (the plugin
registers its own hook, not one in settings.json). Uninstall the plugin instead:
    /plugin uninstall claude-code-desktop-sync@haiggoh

Usage:
    python3 uninstall.py            # remove hook + instruction block(s)
    python3 uninstall.py --purge    # also delete ~/.claude/desktop-sync (script, state, backups)
"""

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_MD = CLAUDE_DIR / "CLAUDE.md"
INSTALL_DIR = CLAUDE_DIR / "desktop-sync"
BACKUP_DIR = INSTALL_DIR / "backups"

BEGIN = "<!-- claude-code-desktop-sync:begin -->"
END = "<!-- claude-code-desktop-sync:end -->"
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
# Must match install.py's HOOK_MARKER: the engine filename is how we identify our own hook.
HOOK_MARKER = "sync_code_desktop.py"
PURGE = "--purge" in sys.argv


def backup(path, stamp):
    if path.exists():
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            dst = BACKUP_DIR / f"{path.name}.{stamp}.pre-sync-uninstall.bak"
            shutil.copy2(path, dst)
            os.chmod(dst, 0o600)
        except OSError:
            pass


def remove_hook(stamp):
    """Returns True if a hook was removed from settings.json."""
    if not SETTINGS.exists():
        return False
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    except Exception:
        print("! settings.json not valid JSON; skipping hook removal.")
        return False
    if not isinstance(settings, dict):
        return False
    groups = settings.get("hooks", {}).get("SessionStart", []) if isinstance(settings.get("hooks"), dict) else []
    if isinstance(groups, dict):     # tolerate a single-group object, mirroring install.py
        groups = [groups]
    elif not isinstance(groups, list):
        return False
    new_groups = []
    removed = False
    for g in groups:
        if not isinstance(g, dict):
            new_groups.append(g)
            continue
        g_hooks = g.get("hooks", []) if isinstance(g.get("hooks"), list) else []
        kept = [h for h in g_hooks if not (isinstance(h, dict) and HOOK_MARKER in str(h.get("command", "")))]
        if len(kept) != len(g_hooks):
            removed = True
        if kept:
            new_groups.append(dict(g, hooks=kept))
    if not removed:
        return False
    if new_groups:
        settings["hooks"]["SessionStart"] = new_groups
    else:
        settings["hooks"].pop("SessionStart", None)
        if not settings["hooks"]:
            settings.pop("hooks", None)
    backup(SETTINGS, stamp)
    tmp = SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS)
    print("• Removed SessionStart hook from settings.json.")
    return True


def remove_snippet(stamp):
    if not CLAUDE_MD.exists():
        return
    text = CLAUDE_MD.read_text(encoding="utf-8")
    if BEGIN not in text and END not in text:
        print("• No instruction block found in CLAUDE.md.")
        return
    # Remove every complete block, then strip any dangling lone markers.
    new = BLOCK_RE.sub("", text).replace(BEGIN, "").replace(END, "")
    new = new.strip()
    backup(CLAUDE_MD, stamp)
    if new:
        CLAUDE_MD.write_text(new + "\n", encoding="utf-8")
    else:
        CLAUDE_MD.unlink()  # file held only our block(s)
    print("• Removed instruction block(s) from CLAUDE.md.")


def main():
    stamp = time.strftime("%Y%m%d-%H%M%S")
    hook_removed = remove_hook(stamp)
    remove_snippet(stamp)
    if PURGE and INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
        print(f"• Purged {INSTALL_DIR}")
    elif INSTALL_DIR.exists():
        print(f"• Left {INSTALL_DIR} in place (use --purge to delete script/state/backups).")

    if not hook_removed:
        print("• No sync hook found in settings.json.")
        print("  If you installed via the plugin system, disable it instead:")
        print("    /plugin uninstall claude-code-desktop-sync@haiggoh")
    print("Uninstalled." if hook_removed else "Done (nothing to remove from settings.json).")


if __name__ == "__main__":
    main()
