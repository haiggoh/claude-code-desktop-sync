## Keep Claude Code and Claude Desktop consistent (claude-code-desktop-sync)
A SessionStart hook runs `~/.claude/desktop-sync/sync_code_desktop.py` automatically and mirrors
**stdio MCP servers** between Claude Code (`~/.claude.json`) and Claude Desktop's config, both ways.
When you change the setup in a Code session, apply these:

- **MCP servers** auto-sync (they take effect in the *next* session, or run the script now to apply).
- **HTTP / hosted / header-auth MCP servers** (e.g. a PAT-authenticated `github` connector) CANNOT be
  written to the Desktop config file — always tell the user the manual step:
  **Desktop → Settings → Connectors → Add custom connector**.
- **Skills** (`~/.claude/skills/`) do NOT auto-sync; when you add one, tell the user to add it in
  Claude Desktop's UI if they want it there.
- **Memory, settings, extensions, and sessions/conversations** are NOT syncable between the two apps
  (separate stores / schemas / app-only concepts). Never claim they are shared; give the manual
  equivalent if asked.

Claude Desktop is a separate app that does not read Code's files or run its instructions; the only
Desktop→Code bridge is the hook picking up Desktop config changes at the next Code session start.
Full details: `~/.claude/desktop-sync/` and the project's `docs/SYNC-REFERENCE.md`.
