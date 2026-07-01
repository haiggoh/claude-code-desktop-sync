---
name: desktop-sync
description: >-
  Manage and troubleshoot MCP-server sync between Claude Code and Claude Desktop.
  Use when the user wants to sync now, check what is/isn't syncing between the two apps,
  add or mirror an MCP server, diagnose why a server or the GitHub connector isn't showing
  up in Claude Desktop, or understand what can and can't be shared between Code and Desktop.
allowed-tools: Bash, Read
---

# Manage Claude Code ⇄ Claude Desktop sync

This skill manages the `claude-code-desktop-sync` plugin, which mirrors **stdio MCP servers**
between Claude Code (`~/.claude.json`) and Claude Desktop's config file, both directions, via a
`SessionStart` hook. Use it to run a sync on demand, report status, and guide the user through the
steps that can't be automated.

## Where things live
- Engine (plugin install): `${CLAUDE_SKILL_DIR}/../../bin/sync_code_desktop.py`
  (`${CLAUDE_SKILL_DIR}` IS substituted inside skill bodies; `${CLAUDE_PLUGIN_ROOT}` is NOT — only
  use the latter in hooks.)
- Engine (standalone `install.py` install): `~/.claude/desktop-sync/sync_code_desktop.py`
- Sync state (per-server snapshot): `~/.claude/desktop-sync/last-sync-state.json`
- Backups (timestamped, mode 600): `~/.claude/desktop-sync/backups/`
- Full reference: the plugin's `docs/SYNC-REFERENCE.md`.

## Common actions

**Sync now:** run the engine and relay its report. Try the plugin path first, then the standalone
path — whichever exists on this machine:
```
python3 "${CLAUDE_SKILL_DIR}/../../bin/sync_code_desktop.py" 2>/dev/null || python3 ~/.claude/desktop-sync/sync_code_desktop.py
```
It is silent when nothing changed. Any output tells you what synced and any manual steps. (Use
`python` instead of `python3` on Windows if `python3` is not found.)

**Show status / what is syncing:** read `~/.claude/desktop-sync/last-sync-state.json` and summarize:
the `stdio` servers (auto-synced both ways), `remote` servers (need the manual Desktop connector),
and `skills` (not auto-synced).

**"I added a server in Code but Desktop doesn't have it":** MCP changes take effect at the *next*
session, or run a sync now (above). Then have the user restart Claude Desktop so it reloads its config.

## What this can and cannot do — state this plainly, never over-promise
- ✅ **stdio MCP servers** (command/args/env) sync automatically, both directions. Relative launchers
  like `npx` are rewritten to absolute paths for Desktop (which has no shell `PATH`).
- ⚠️ **HTTP / hosted / header-auth MCP servers** (e.g. a PAT-authenticated `github` connector) can't
  live in the Desktop config file. Tell the user to add it manually:
  **Claude Desktop → Settings → Connectors → Add custom connector**.
- ❌ **Skills** don't auto-sync (Desktop manages skills in-app) — the user adds them in Desktop's UI.
- ❌ **Memory, settings, extensions, and sessions/conversations** are not syncable — separate stores /
  schemas / app-only concepts. Never claim they are shared; give the manual equivalent if asked.

Claude Desktop is a separate app that doesn't read Claude Code's files or run its instructions; the
only Desktop→Code bridge is the hook picking up Desktop config changes at the next Code session start.

## Safety
The engine backs up any file before writing it and only ever edits the `mcpServers` block. On the very
first run it *merges* both sides (never deletes), so it can't wipe servers that exist on only one side.
