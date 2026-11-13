"""core — the v2 stdlib core of claude-code-desktop-sync (plan §5.1).

Module layout (implementation decision recorded after Phase 0 preflight, as the
plan requires):
  core.hostguard  — host-management boundary: verified multi-signal grants only
  core.automodes  — frozen automatic-action modes (plan §5.8)
Later phases add inventory/planner/builder/lifecycle modules here. The legacy
engine at bin/sync_code_desktop.py is preserved untouched (host root unchanged,
COORDINATION C1 Option B).
"""
