# Changelog

## Unreleased

Phase 1 of the v2 overhaul (branch `feat/v2-overhaul`; not released):

### Fixed
- Legacy test runner is now fully isolated: each engine subprocess gets a sandbox
  `HOME` and a restricted `PATH`, and leaky env vars (`HOME`, `PATH`, `APPDATA`,
  `XDG_CONFIG_HOME`, …) are stripped first. New `home-isolation` / `path-isolation`
  regression tests prove the engine reads only the sandbox (red before the fix,
  green after; mutation control re-confirmed red).

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

## 1.0.3

### Changed
- The plugin description (plugin.json, and the marketplace listing it feeds) is rewritten to describe
  the plugin's full current capabilities.

