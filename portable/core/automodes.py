"""core.automodes — the frozen automatic-action policy (plan §5.8).

Frozen for the whole overhaul; changing a mode's meaning is a plan amendment,
not a code tweak:

  MODE_STATUS_ONLY   (1) bounded read-only startup status; never writes
  MODE_OPT_IN_REGEN  (2) opt-in regeneration of tool-owned local outputs after
                         ownership/freshness/privacy checks; no account upload,
                         no third-party execution
  MODE_APP_OWNED     (3) app-owned config write only via verified coordination;
                         otherwise explicit deferred apply with qualified
                         quiescence — never an unsafe auto write at SessionStart

SessionStart may never open a window/menu/bootstrap (plan §6) and may not write
an app-owned file just because the hook holds its own lock.
"""

MODE_STATUS_ONLY = 1
MODE_OPT_IN_REGEN = 2
MODE_APP_OWNED = 3

ALL_MODES = (MODE_STATUS_ONLY, MODE_OPT_IN_REGEN, MODE_APP_OWNED)

# The startup hook's default mode.
DEFAULT_STARTUP_MODE = MODE_STATUS_ONLY
