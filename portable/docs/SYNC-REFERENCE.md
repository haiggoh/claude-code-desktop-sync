# Sync reference

## How it runs
- `install.py` copies the engine to `~/.claude/desktop-sync/sync_code_desktop.py` and registers a
  **SessionStart hook** in `~/.claude/settings.json` so it runs at the start of every Claude Code session.
- It is **silent** unless it changes something or a manual step is pending.
- Run it by hand anytime (standalone install):
  ```
  python3 ~/.claude/desktop-sync/sync_code_desktop.py
  ```
- **Plugin install:** the engine is in the versioned plugin cache, not at the path above — invoke the
  `desktop-sync` skill (or ask Claude to sync) instead of hardcoding a path.

## Config locations
| File | Path |
|---|---|
| Claude Code MCP + state | `~/.claude.json` (all platforms) |
| Claude Code settings/hooks | `~/.claude/settings.json` |
| Claude Code skills | `~/.claude/skills/` |
| Desktop config (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Desktop config (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Desktop config (Linux) | `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json` (or `~/.config/Claude/...`) |
| Sync state + backups | `~/.claude/desktop-sync/` (override with `CLAUDE_SYNC_HOME`) |

## What syncs, what doesn't

| Thing | Status | Mechanism |
|---|---|---|
| **MCP servers (stdio: command/args/env)** | ✅ auto, both directions | Mirrored between the two configs. Relative launchers (`npx`, `uvx`, `python`…) are resolved to an absolute path when written to Desktop, because Desktop has no shell `PATH`. |
| **MCP servers (HTTP / hosted / header-auth, e.g. a PAT connector)** | ⚠️ manual (one-time) | Cannot live in the Desktop config file. The script always reminds you: **Desktop → Settings → Connectors → Add custom connector**. |
| **Skills** (`~/.claude/skills/`) | ❌ not file-syncable; reminder only | Desktop manages skills in-app and does not read that folder. |
| **Memory** (`~/.claude/CLAUDE.md` + auto-memory) | ❌ separate systems | Desktop has its own memory feature; it does not read Code's memory files. |
| **Settings** (`settings.json` vs Desktop `preferences`) | ❌ different schemas | Copying between them would corrupt config. |
| **Extensions (DXT)** | ❌ Desktop-only concept | Code has no equivalent (its analog is MCP/plugins). |
| **Sessions / conversations** | ❌ not shareable | Code stores JSONL under `~/.claude/projects/`; Desktop uses its own in-app / account store. No supported bridge. |

**Why the direction is asymmetric:** Claude Desktop is a separate app that does NOT read Code's
instruction files, hooks, or memory, and cannot "push" changes on its own. So the only bridge from
**Desktop → Code** is the hook noticing a Desktop-side change the next time a Code session starts.
Code → Desktop happens whenever the hook runs after you change something in Code.

## Conflict handling
Reconciliation is **per-server and content-based**, using a snapshot in
`~/.claude/desktop-sync/last-sync-state.json` that records the exact last-synced value of each server
on **each** side. No file-timestamp guessing is used (`~/.claude.json` is rewritten constantly by
Claude Code for unrelated reasons, so its mtime is meaningless as a "what changed" signal).

For each server:
- **Changed on exactly one side** → that side's version is propagated to the other.
- **Added on one side** → added to the other.
- **Deleted on one side** (was in the snapshot, now gone) → removed from the other.
- **Changed differently on BOTH sides** → a real **conflict**: *both sides are left untouched* and
  the conflict is reported by name for you to resolve manually. Nothing is silently overwritten.
- **First run (no snapshot):** servers on only one side are added to the other (a safe union —
  nothing is deleted); a same-named server that already differs on both sides is treated as a conflict
  and left alone.

The snapshot records the relative→absolute launcher rewrite (e.g. `npx` → `/opt/homebrew/bin/npx`),
so that translation is never mistaken for an edit — while a genuine command change (e.g.
`npx` → `/opt/custom/npx`, or `foo.cmd` → `foo.exe`) is detected and synced.

Concurrent Claude Code sessions are serialized by an exclusive lock in `~/.claude/desktop-sync/`; if a
sync is already running, the second invocation exits silently.

## Backups & undo
- Every write is preceded by a timestamped backup in `~/.claude/desktop-sync/backups/` (mode `600`,
  because `~/.claude.json` may contain a plaintext token). Restore by copying the `.bak` back.
- `install.py` also backs up `settings.json` and `CLAUDE.md` before editing them.

## Disable / uninstall
- `python3 uninstall.py` removes the hook and the CLAUDE.md block (keeps script/state/backups).
- `python3 uninstall.py --purge` also deletes `~/.claude/desktop-sync/`.
- Or just remove the `SessionStart` hook from `~/.claude/settings.json` to pause it.
