# claude-code-desktop-sync

Keep **Claude Code** and **Claude Desktop** in sync. Add or change an MCP server in one, and it is
mirrored to the other automatically — no manual copy-paste, no drifting configs. For everything that
*can't* be synced (a hard platform limit), the tool tells you the exact manual step instead of failing
silently.

It installs on a **vanilla** Claude Code + Claude Desktop setup, merges cleanly into existing config
(never clobbers), backs up before every change, and is fully reversible.

## What it does

- ✅ **Auto-syncs stdio MCP servers both ways** between `~/.claude.json` (Code) and Claude Desktop's
  config file. Relative launchers like `npx` are rewritten to absolute paths for Desktop (which has no
  shell `PATH`).
- ⚠️ **Detects HTTP / header-auth MCP servers** (e.g. a PAT-authenticated `github` connector) that
  *can't* live in the Desktop config file, and prints the one-time manual step (Desktop → Connectors UI).
- ❌ **Clearly documents what is not syncable** — skills, memory, settings, extensions, and
  sessions/conversations live in separate stores the two apps don't share.

Runs automatically via a Claude Code **SessionStart hook**, and stays silent unless something changed.

> **Why not everything?** Claude Desktop is a separate application that does not read Claude Code's
> files, hooks, or memory. Only configuration that lives in a **file both apps read** (the MCP server
> list) can be synced automatically. See [`docs/SYNC-REFERENCE.md`](docs/SYNC-REFERENCE.md) for the full
> matrix and the reasoning.

## Requirements

- Claude Code and/or Claude Desktop installed (the sync is a no-op if only one is present).
- **Python 3.8+** (standard library only — no pip installs). The plugin hook runs `python3`, so it
  must be on `PATH` (it is on macOS/Linux and with Microsoft Store Python on Windows). On Windows with
  python.org Python (which installs `python`/`py`, not `python3`), use the standalone `install.py`
  instead — it resolves whichever interpreter you have and writes an absolute path into the hook.
- macOS, Windows, or Linux.

## Install

### Option A — as a Claude Code plugin (recommended)

This repo is a Claude Code plugin, part of the `haiggoh` marketplace (catalog now hosted
at [haiggoh/get-haiggoh](https://github.com/haiggoh/get-haiggoh), not here). From inside
Claude Code:

```
/plugin marketplace add haiggoh/get-haiggoh
/plugin install claude-code-desktop-sync@haiggoh
```

That's it — the plugin registers the `SessionStart` hook itself (no files copied into your
`settings.json`) and ships a companion skill. Restart Claude Code (and Claude Desktop, if it was open)
so each app reloads its config.

### Option B — standalone (no plugin system)

If you'd rather just wire the hook directly into your own config:

```bash
git clone https://github.com/haiggoh/claude-code-desktop-sync.git
cd claude-code-desktop-sync
python3 install.py          # or: python3 install.py --dry-run
```

The standalone installer:
1. Copies the engine to `~/.claude/desktop-sync/sync_code_desktop.py`.
2. Adds a `SessionStart` hook to `~/.claude/settings.json` (merged into any existing hooks).
3. Adds a marker-bounded instruction block to `~/.claude/CLAUDE.md` so Claude Code sessions know the
   sync rules and always surface manual steps.
4. Runs an initial sync.

Restart Claude Code (and Claude Desktop, if it was open) so each app reloads its config.

## The companion skill

The plugin bundles a model-invoked skill, `desktop-sync`, for on-demand help — run a sync now, check
what is/isn't syncing, or troubleshoot a missing server. Invoke it with
`/claude-code-desktop-sync:desktop-sync`, or just ask Claude in natural language (e.g. "sync my MCP
servers to Desktop" / "why isn't my github connector in Desktop?") and it loads automatically.

## Usage

Once installed there's nothing to do — it runs at each Code session start. To sync on demand:

- **Plugin install (Option A):** ask Claude to "sync my MCP servers to Desktop", or invoke the
  companion skill `/claude-code-desktop-sync:desktop-sync`. The engine lives in the versioned plugin
  cache (e.g. `~/.claude/plugins/cache/haiggoh/claude-code-desktop-sync/<version>/bin/sync_code_desktop.py`),
  so don't hardcode its path — the skill resolves it for you.
- **Standalone install (Option B):** run the copied engine directly:

  ```bash
  python3 ~/.claude/desktop-sync/sync_code_desktop.py
  ```

Example output when you've added a server in Code:

```
[claude-sync] updated Claude Desktop config  (backup: ~/.claude/desktop-sync/backups/claude_desktop_config.json.20260701-183531.bak)
  + added to Desktop: my-new-server
```

## Conflict handling

Reconciliation is **per-server and content-based** (a snapshot of the last-synced value on each side),
never timestamp-based. For each server: a change on exactly one side is propagated to the other; an add
is mirrored; a delete (present in the snapshot, now gone) is mirrored. If the **same server was changed
differently on both sides**, it is a **conflict**: both sides are left **untouched** and reported by
name for you to resolve — nothing is silently overwritten. A name that is a stdio server in one app but
an HTTP/connector in the other is likewise held and reported (so a connector and its token are never
clobbered). On the first run, servers present on only one side are added to the other (a safe union —
nothing is deleted).

## Safety

- Backs up every file before writing it (timestamped, under `~/.claude/desktop-sync/backups/`; mode
  `600` since `~/.claude.json` may hold a plaintext token).
- Only ever changes the `mcpServers` block — all other keys and values are preserved (the file is
  re-serialized with 2-space indentation and `ensure_ascii=False`, so values are kept but original
  whitespace/formatting is normalized).
- The hook never raises: if anything goes wrong it exits cleanly so it can't block a session from
  starting.

## Uninstall

```bash
python3 uninstall.py           # remove hook + CLAUDE.md block
python3 uninstall.py --purge   # also delete ~/.claude/desktop-sync
```

## How it works

See [`docs/SYNC-REFERENCE.md`](docs/SYNC-REFERENCE.md) for config locations, the full sync matrix,
conflict rules, and the backup/restore procedure.

## License

MIT — see [LICENSE](LICENSE).

---

*Not affiliated with Anthropic. "Claude" is a trademark of Anthropic. This is a community tool.*
