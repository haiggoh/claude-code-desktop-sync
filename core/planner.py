"""core.planner — pure, revision-bound planning (plan §8 Phase 4).

build_plan() is PURE: it reads only its inputs, writes nothing, spawns nothing.
A plan is bound to the exact input revision it was built from; check_freshness()
detects any input change afterwards, and revise() turns that into a conflict
(exit 4) carrying ZERO writable actions — a stale plan can never authorize a
write. JSON and human renderings agree on scope/verdicts/plan_id.

Defaults < explicit config < documented env overrides < CLI (the effective-
settings ladder of §5.5) is resolved by the caller before inputs reach us;
unknown supported keys are preserved but never used to authorize merges.

Stdlib only; Python 3.8 compatible.
"""

import hashlib
import json

from . import exitcodes
from . import state as state_mod

STATUS_OK = "ok"
STATUS_CONFLICT = "conflict"
STATUS_UNAVAILABLE = "unavailable"
STATUS_RECOVERY = "recovery-required"


class Action:
    def __init__(self, action_id, kind, destination, revision, side_effects):
        self.action_id = action_id
        self.kind = kind                  # "add" | "update" | "delete" | "hold" | "report"
        self.destination = destination
        self.revision = revision
        self.side_effects = side_effects  # human-readable, exact

    def to_dict(self):
        return {"action_id": self.action_id, "kind": self.kind,
                "destination": self.destination, "revision": self.revision,
                "side_effects": self.side_effects}


class Plan:
    def __init__(self, plan_id, inputs_revision, actions, status, exit_code, notes=""):
        self.plan_id = plan_id
        self.inputs_revision = inputs_revision
        self.actions = actions
        self.status = status
        self.exit_code = exit_code
        self.notes = notes

    def to_dict(self):
        return {"plan_id": self.plan_id, "inputs_revision": self.inputs_revision,
                "status": self.status, "exit_code": self.exit_code,
                "notes": self.notes,
                "actions": [a.to_dict() for a in self.actions]}

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True)

    def to_human(self):
        lines = ["plan %s — status: %s (exit %d)" % (self.plan_id, self.status, self.exit_code)]
        for a in self.actions:
            lines.append("  %s %s -> %s [%s]" % (a.action_id, a.kind, a.destination, a.side_effects))
        if self.notes:
            lines.append("  notes: %s" % self.notes)
        return "\n".join(lines)


def inputs_revision(inputs):
    """Stable content revision of the plan inputs (hash of the canonical JSON)."""
    canon = json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def build_plan(inputs, baseline, first_use):
    """Pure plan construction. baseline=None + first_use=FRESH -> approved safe
    union (adds only, never deletes). LOST/INVALID baseline -> recovery-required
    with no actions. No sources -> unavailable (exit 3), never 'empty'."""
    rev = inputs_revision(inputs)
    plan_id = "plan-" + rev
    sources = inputs.get("sources") or []
    if not sources:
        return Plan(plan_id, rev, [], STATUS_UNAVAILABLE, exitcodes.EXIT_PREREQ,
                    notes="no sources available (absent target/source is unavailable, not empty)")
    if first_use in (state_mod.LOST_BASELINE, state_mod.INVALID_BASELINE):
        return Plan(plan_id, rev, [], STATUS_RECOVERY, exitcodes.EXIT_FAILURE,
                    notes="baseline %s — recovery required before any write; refused" % first_use)
    actions = []
    for i, src in enumerate(sources):
        actions.append(Action("act-%d" % i, "add", src.get("name", "?"),
                              src.get("revision", "?"),
                              "write prepared artifact for %s (approved union; no deletions on first use)"
                              % src.get("name", "?")))
    if first_use == state_mod.EXISTING and baseline:
        for name in baseline.get("revisions", {}):
            if name not in {s.get("name") for s in sources}:
                actions.append(Action("hold-%s" % name, "hold", name, "?",
                                      "source vanished — held, never deleted"))
    return Plan(plan_id, rev, actions, STATUS_OK, exitcodes.EXIT_OK)


def check_freshness(plan, current_inputs):
    """True when the inputs still match the plan's bound revision."""
    return inputs_revision(current_inputs) == plan.inputs_revision


def revise(plan, current_inputs):
    """Re-evaluate a plan against changed inputs: stale -> conflict, exit 4,
    and the conflicted plan carries no writable actions."""
    if check_freshness(plan, current_inputs):
        return plan
    conflicted = Plan(plan.plan_id, inputs_revision(current_inputs), [],
                      STATUS_CONFLICT, exitcodes.EXIT_CONFLICT_STALE,
                      notes="inputs changed after planning (revision %s -> %s); zero target writes"
                            % (plan.inputs_revision, inputs_revision(current_inputs)))
    return conflicted
