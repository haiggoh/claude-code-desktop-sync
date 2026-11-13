"""core.exitcodes — the frozen modern CLI exit mapping (plan §5.4).

  0 EXIT_OK               complete requested operation
  1 EXIT_FAILURE          failure
  2 EXIT_INVALID          invalid invocation
  3 EXIT_PREREQ           prerequisite/unsupported
  4 EXIT_CONFLICT_STALE   conflict / stale (revision changed after planning)
  5 EXIT_PARTIAL          partial / pending

A hook wrapper may intentionally return hook-safe 0, but must preserve truthful
structured results — status 0 is never success evidence.
"""

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID = 2
EXIT_PREREQ = 3
EXIT_CONFLICT_STALE = 4
EXIT_PARTIAL = 5
