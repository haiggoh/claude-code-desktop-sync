# v2 overhaul overview (branch `feat/v2-overhaul`, unreleased)

The narrow legacy stdio-MCP synchronizer is growing into a compatibility/reconciliation
product. The legacy engine (`bin/sync_code_desktop.py`), installer, hook, and skill name are
preserved unchanged (host root); everything new lives in the stdlib `core/` package plus the
`bin/desktop-sync` v2 CLI.

## Architecture (plan §5.1)

```
core/inventory.py     read-only effective inventory (registry/cache/synced/skills/commands;
                      scoped roots only; pure: no writes, no subprocess)
core/hostguard.py     host-management boundary: verified multi-signal grants only
core/automodes.py     frozen auto-action modes (status-only default at SessionStart)
core/planner.py       pure revision-bound plans (stale input => conflict, exit 4, zero writes)
core/state.py         versioned typed state schema (cds-state/1), loud on drift
core/builder.py       deterministic legacy-artifact builder (safety limits, private-marker
                      refusals, source-stability gate, dynamic deps disclosed unknown)
core/capability.py    evidence levels per surface; runtime-qualified needs a live receipt
core/distribution.py  artifact readiness + honest pending account steps + active-version verify
core/coexistence.py   same-name shadowed / distinct-name double-run refused
core/lifecycle.py     apply/recovery: owned locks, verified backups, 0600 temps, journal
core/records.py       allowlisted records: export/import, proposals, tombstones
core/mcp.py           hardened v2 MCP reconciliation (edit/delete conflict fixed, ...)
core/menu.py          numbered menu + pure menu-spec v1 (get-haiggoh interop)
core/cli.py           bin/desktop-sync subcommands; exit codes 0-5
core/windowdriver.py  bundled nonprivileged Terminal.app launcher + per-launch receipts
scripts/build-portable.py  deterministic portable/ builder (+ PORTABLE-BUILD.json, dist zip)
```

## Contracts

- Exit codes 0–5 (`core/exitcodes.py`); JSON and human output agree.
- Evidence levels per surface: source-inspected / fixture-tested / runtime-qualified. Only the
  window driver is runtime-qualified so far (darwin Terminal.app, synthetic fixture,
  2026-11-13). Everything else is source-inspected or fixture-tested; live Desktop/Cowork/
  claude.ai behavior is PENDING the user-run gates (COORDINATION §5).
- Portable package: same manifest name, no top-level `bin/`, `${CLAUDE_SKILL_DIR}` for skill
  scripts, copies never symlinks; freshness enforced by `scripts/build-portable.py --check` in CI.

## Tests

- Legacy runner: `python3 tests/test_sync.py` (fully isolated since Phase 1).
- Overhaul suite: `python3 -m unittest discover -s tests/overhaul -p 'test_*.py'` (111 tests).
- CI: `.github/workflows/smoke-test.yml` — 3.8 in a pinned container, 3.12/3.14 native,
  macOS/Windows, fail-fast off, both suites + portable freshness.
