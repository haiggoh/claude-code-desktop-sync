# Package map and frozen boundaries (v2 overhaul, Phase 2)

Decision record for plan §5.2 (the "portable root vs preserved host root" gate):
**COORDINATION C1 Option B adopted** — no evidence overturned it, so no
COORDINATION §6 entry was needed.

## Host package vs portable package

| Layer | What | Where |
| --- | --- | --- |
| Host package (unchanged) | repo root: `bin/`, `hooks/`, `install.py`, `uninstall.py`, `tests/`, `core/`, `scripts/build-portable.py` | repo root |
| Portable package (generated, committed) | `portable/` — same manifest `name`, **no top-level `bin/`**, hook scripts in `scripts/`, skill scripts in `skills/<s>/scripts/`, copies never symlinks | `portable/`, built by `scripts/build-portable.py` |
| Distribution artifact | `dist/<name>-portable.zip`, manifest at zip root, deterministic bytes, gitignored | `dist/` |
| Build metadata | `portable/PORTABLE-BUILD.json`, schema `haiggoh-portable-build/1` (C2) | inside `portable/` |

Freshness is enforced: `python3 scripts/build-portable.py --check` exits 1 when
the committed `portable/` differs from a fresh build; CI runs `--check`.

## Source → host → portable → installer → hook → skill → test → shortcut map

| Source (repo) | Host artifact | Portable artifact | Installer | Hook | Skill | Test | Shortcut |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `bin/sync_code_desktop.py` (engine) | `bin/sync_code_desktop.py` (unchanged) | `scripts/sync_code_desktop.py` + `skills/desktop-sync/scripts/sync_code_desktop.py` (byte-identical copies) | `install.py` copies to `~/.claude/desktop-sync/` | `hooks/hooks.json` → `python3 ${CLAUDE_PLUGIN_ROOT}/bin/…` (host) / `…/scripts/…` (portable) | `${CLAUDE_SKILL_DIR}/../../bin/…` (host) / `${CLAUDE_SKILL_DIR}/scripts/…` (portable) | `tests/test_sync.py`, `tests/overhaul/` | none (hook/skill invoked) |
| `hooks/hooks.json` | unchanged | rewritten `bin/`→`scripts/` | — | — | — | `test_portable.py` | — |
| `skills/desktop-sync/SKILL.md` | unchanged | reference rewritten | — | — | `/claude-code-desktop-sync:desktop-sync` | `test_portable.py` | skill name |
| `install.py` / `uninstall.py` | unchanged (host-only) | **excluded** (`unsupported`, code-only) | self | — | — | CI dry-run step | — |
| `templates/CLAUDE.snippet.md` | unchanged | excluded (installer-only) | install.py | — | — | — | — |
| `tests/`, `.github/`, `rtc-ship.json`, `core/`, `scripts/build-portable.py` | unchanged | excluded (host/tooling) | — | — | — | self | — |
| `README.md`, `docs/`, `CHANGELOG.md`, `LICENSE` | unchanged | verbatim copies | — | — | — | `test_portable.py` | — |

## Entry-point contract (frozen)

- `python3 bin/sync_code_desktop.py` (no args) = **legacy sync run** — unchanged
  forever; it never becomes a menu.
- New v2 CLI entry `bin/desktop-sync`: bare invocation on a TTY opens the
  numbered menu; any subcommand/args/flag/`--help`/JSON is direct CLI and never
  opens the menu; no-TTY bare invocation exits 2 with a useful message (no hang).
- Hook wrapper may return hook-safe 0 but must preserve truthful structured
  results; status 0 is not success evidence (plan §5.4 exit mapping 0–5).

## Automatic-action modes (frozen, plan §5.8 — `core/automodes.py`)

1 `MODE_STATUS_ONLY` (startup default): bounded read-only status, never writes.
2 `MODE_OPT_IN_REGEN`: opt-in regeneration of tool-owned outputs after
ownership/freshness/privacy checks; no account upload, no third-party execution.
3 `MODE_APP_OWNED`: app-owned config write only via verified coordination;
otherwise explicit deferred apply — never an unsafe auto write at SessionStart.
SessionStart never opens a window/menu/bootstrap.

## Host vs target boundary (plan §5.3 — `core/hostguard.py`)

- Host manager may inventory/apply only under a **verified multi-signal grant**
  (`detect_context` requires code-session marker + plugin-root marker + desktop
  platform + TTY + existing `~/.claude.json` — no single spoofable variable, no
  HOME/OS/config-name lookalikes). Default/unknown/cloud context ⇒ all
  host-management writes refused at the single choke point
  `HostManager.write_config`.
- The target companion (Desktop/Cowork side) may invoke qualified target
  scripts and prepare proposals; it never reconciles host config.
- Cloud fixture with plausible `.claude.json` + Desktop config and CI/no-TTY
  signals: detected as non-host; writes refused (tested, with bypass and
  forged-grant negatives).

## Component verdicts

Recorded per component in `portable/PORTABLE-BUILD.json` using the C3 vocabulary
(`native-unchanged | mechanical | qualified-adapter | reduced-useful |
unsupported | unknown`) × evidence level (`source-inspected | fixture-tested |
runtime-qualified`) × surfaces (`chat | cowork | code`). Current portable build:
hooks = qualified-adapter (Chat ignores hooks; Cowork loads them — fact 1),
engine/skill scripts = mechanical relocations, `bin/` + installers =
unsupported on Chat/Cowork (kept in host root). All evidence source-inspected;
runtime qualification stays pending on the user-run live gates
(COORDINATION §5).
