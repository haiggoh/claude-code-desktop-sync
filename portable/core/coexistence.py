"""core.coexistence — overlay coexistence guard (plan §5.7, fact 4).

A portable overlay with the SAME manifest name as the Code-installed host
package is shadowed (loaded once, reported not-loaded) — safe. A DISTINCT name
would load BOTH and double-run hooks after syncing back to Code — refused
unless the user explicitly approves stacking.

Disabling the original or the overlay requires scoped user approval; this
module never disables anything by itself.

Stdlib only; Python 3.8 compatible. Pure: no writes, no subprocess.
"""


class CoexistenceError(RuntimeError):
    pass


def coexistence_verdict(host_name, overlay_name):
    """Same manifest name -> shadowed (fact 4), never double-run.
    Distinct name -> double-run-risk: both copies load and hooks double-run."""
    if overlay_name == host_name:
        return {"verdict": "shadowed", "double_run": False,
                "notes": "same manifest name: Code loads the installed copy and reports "
                         "the synced copy as not loaded (fact 4)"}
    return {"verdict": "double-run-risk", "double_run": True,
            "notes": "distinct name %r would load alongside %r and double-run hooks "
                     "after syncing back to Code" % (overlay_name, host_name)}


def may_activate(verdict, explicit_approval):
    """shadowed -> may activate. double-run-risk -> requires explicit user
    approval; without it the activation is refused (never auto-stacked)."""
    if verdict.get("verdict") == "shadowed" and not verdict.get("double_run"):
        return True
    if verdict.get("double_run") and not explicit_approval:
        raise CoexistenceError("refusing to activate overlay: distinct-name copy would "
                               "double-run hooks (fact 4); needs explicit user approval")
    return bool(explicit_approval)
