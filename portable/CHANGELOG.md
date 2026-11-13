# Changelog

## 2.0.2

### Fixed
- `bin/sync_code_desktop.py`, `install.py`, `uninstall.py` now parse arguments before doing
  anything: `--help`/`-h` prints usage and exits 0, an unknown flag exits 2 with usage on
  stderr — previously `--help` ran a real sync/install/uninstall. The no-arg hook route is
  unchanged (regression-tested). Fixes waypoint `cds-legacy-engine-ignores-help`.

## 2.0.1

### Fixed — bare `desktop-sync` in a terminal showed nothing and looked dead (user report 2026-11-13)
- The plain menu printed through bare `stdout.write`: no newline per line and no flush, so the
  title, the options and the prompt sat unseen in the terminal's line buffer while the menu
  waited for input; on the first keypress it dumped as one run-on line. Every menu line now ends
  with a newline and is flushed, and every read flushes first.
- Why tests stayed green: the menu test passed a discarding `print_fn`, so what the menu SHOWS was
  never asserted. New delivery-level test runs the real `bin/desktop-sync` on a PTY and requires
  the title and each option on its own line BEFORE any input (macOS/Linux; mutation-checked).
- `portable/` rebuilt (it carries its own `core/`).

### Added — get-haiggoh family contract (get-haiggoh `docs/menu-spec.md`)
- `desktop-sync --menu-spec` (the FLAG the consumer probes) returns the same pure menu-spec v1
  JSON as the `menu-spec` subcommand. 2.0.0 only had the subcommand, so get-haiggoh's probe got
  `unknown command` (rc 2) and could not show the menu.
- `desktop-sync --return-to get-haiggoh` opens the menu with `👨🏻‍💻 Back to Get Haiggoh` as the
  LAST item (absent when launched standalone); choosing it returns to get-haiggoh, running
  nothing. Any other `--return-to` value exits 2.
- Contract test validates our spec with get-haiggoh's OWN `menuspec.validate` (skips loudly when
  no get-haiggoh source is present, e.g. CI; `GET_HAIGGOH_SRC` points at one). Both new tests
  mutation-checked.

## 2.0.0

**v2 overhaul (2026-11-13).** A compatibility/reconciliation product on top of the unchanged legacy
engine: a new `desktop-sync` CLI + dependency-free menu (`menu-spec` v1), a deterministic builder that
turns unchanged Code plugins — and this plugin itself — into a Chat/Cowork-installable `portable/`
package (no top-level `bin/`, same manifest name), a host-management guard, read-only inventory, a
pure planner, lifecycle/recovery, selected-record export/import and a hardened MCP reconciler.
**Unchanged:** `hooks/hooks.json`, `bin/sync_code_desktop.py` (the SessionStart engine), `install.py`,
`uninstall.py` — existing Code installs behave exactly as 1.0.3 at session start.

**Evidence, stated honestly:** fixture-tested on macOS/Linux/Windows CI (legacy + 116 overhaul tests)
and two independent review rounds; the Terminal.app window driver was qualified live. **NOT TESTED
live:** uploading the portable zip to claude.ai, its behaviour in Chat/Cowork, and account-sync
coexistence with the Code install — those surfaces are reported as fixture-tested, never
runtime-qualified, until you run the live checks (docs/V2-OVERVIEW.md).

The phase-by-phase detail follows. Phase 1 of the v2 overhaul:

### Fixed
- Legacy test runner is now fully isolated: each engine subprocess gets a sandbox
  `HOME`/`USERPROFILE` and a restricted `PATH`, and leaky env vars are stripped
  first — case-insensitively, because Windows stores `Path`/`UserProfile` in mixed
  case and a case-sensitive filter let the real ones survive (caught by CI
  windows-3.12 run 43315625862; `ntpath.expanduser` prefers `USERPROFILE` over
  `HOME`). New `home-isolation` / `path-isolation` / `case-insensitive filter`
  regression tests prove the engine reads only the sandbox (red before each fix,
  green after; mutation controls re-confirmed red).

### Added
- `tests/overhaul/` stdlib harness (`python3 -m unittest discover -s tests/overhaul -p 'test_*.py'`):
  rooted env builder, a launch guard that REFUSES any tool run whose writable paths
  escape the test root, and write-purity snapshots. Contract tests included; the
  guard's negative control was shown failing, then restored passing.

### CI
- Legacy Python 3.8 job now runs in a pinned `python:3.8` container (setup-python has
  no 3.8 x64 manifest for ubuntu-latest 26.04) — 3.8 support kept, not dropped.
- `fail-fast: false` so one job cannot cancel the matrix; separate latest-runtime
  (3.14) job; the overhaul suite runs alongside the legacy runner.
- Actions bumped to the `@v7` majors (Node 24 native), clearing the Node-20
  deprecation warning the `@v4`/`@v5` majors triggered.

### Added (Phase 2 — packaging boundary, C1 Option B)
- `scripts/build-portable.py`: deterministic stdlib builder for the committed
  `portable/` package (same manifest name, no top-level `bin/`, hook scripts in
  `scripts/`, skill scripts in `skills/<s>/scripts/`, copies never symlinks),
  the `dist/<name>-portable.zip` artifact (gitignored, manifest at zip root,
  byte-deterministic), and `portable/PORTABLE-BUILD.json`
  (schema `haiggoh-portable-build/1`). `--check` verifies freshness; `--help`
  and unknown-flag exit codes tested. `--source-commit` keeps the builder
  git-free in sandboxes; `source_commit` is masked in freshness comparisons
  (it is always the parent commit) but verified against repo history when git
  is available.
- `.gitattributes` (`* text=auto eol=lf`, `*.zip -text`): checkouts are
  byte-identical on every platform so portable hashes match (Windows CI
  autocrlf would otherwise break them).
- `core/hostguard.py`: host-management writes require a verified multi-signal
  grant (code-session + plugin-root + desktop platform + TTY + existing
  `~/.claude.json`); cloud lookalikes, missing files, forged grants, and
  outside-root targets are all refused (mutation-tested).
- `core/automodes.py`: frozen auto-action modes (plan §5.8).
- `docs/PACKAGE-MAP.md`: source→host→portable→installer→hook→skill→test→shortcut
  map, entry-point contract, boundary rules, component verdicts.
- CI runs `scripts/build-portable.py --check` in every matrix job.
- Tests: `tests/overhaul/` now 20 tests (harness, hostguard, portable builder);
  freshness negative control proven (mutated file → `--check` exit 1 → rebuild
  → exit 0, byte-identical).

### Phases 6–13 (same unreleased branch)
- `core/capability.py` (evidence levels per surface; runtime-qualified requires a live receipt),
  `core/distribution.py` + `core/coexistence.py` (artifact readiness, pending account steps,
  active-version verify, same-name shadowed / distinct-name double-run refused),
  `core/lifecycle.py` (owned locks, verified backups, 0600 temps, journal, recovery),
  `core/records.py` (allowlisted records, revision-protected proposals, tombstones, offline
  never deletes), `core/mcp.py` (hardened v2 MCP sync — edit/delete conflict fixed, typed
  snapshot, partial-journal), `core/menu.py` + `core/cli.py` + `bin/desktop-sync` (numbered
  menu, pure menu-spec v1, exit codes 0–5), `core/windowdriver.py` (bundled nonprivileged
  Terminal.app launcher, per-launch receipts — runtime-qualified on darwin 2026-11-13),
  skill rewired to the v2 CLI with a single resolved route (no stderr-suppression fallback),
  portable package now carries `core/` + the skill-invoked CLI entry.
- Phase 12: full offline journeys chained end-to-end (111 overhaul tests + legacy runner green).
- Docs: `docs/V2-OVERVIEW.md`, `docs/PACKAGE-MAP.md`, link-integrity test.
- `rtc-ship.json` test_cmd now runs both suites. Nothing is released; version bump happens
  only at an authorized ship.

## 1.0.3

### Changed
- The plugin description (plugin.json, and the marketplace listing it feeds) is rewritten to describe
  the plugin's full current capabilities.

