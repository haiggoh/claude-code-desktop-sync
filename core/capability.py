"""core.capability — bounded capability qualification (plan §8 Phase 6).

Evidence levels are reported SEPARATELY per surface and never laundered:
source-inspected < fixture-tested < runtime-qualified. A runtime-qualified
claim REQUIRES a live receipt id from the user-run gates (COORDINATION §5);
without one it is downgraded to fixture-tested with a note — no live approval
means NOT TESTED, and fixture evidence is never presented as runtime proof.

Pre-action enforcement is qualified only by a denied-sentinel probe: the action
must be blocked AND the sentinel unchanged. Inert/missing/wrong-matcher guards
(or a sentinel changed by the guard after the action) yield False/unknown —
never True.

Hook placement is qualified separately from shell PATH (different channel);
storage persistence is explicit (persistent vs temporary are different claims).

PURE: no subprocess, no writes. Stdlib only; Python 3.8 compatible.
"""

EVIDENCE_SOURCE_INSPECTED = "source-inspected"
EVIDENCE_FIXTURE_TESTED = "fixture-tested"
EVIDENCE_RUNTIME_QUALIFIED = "runtime-qualified"
EVIDENCE_LEVELS = (EVIDENCE_SOURCE_INSPECTED, EVIDENCE_FIXTURE_TESTED, EVIDENCE_RUNTIME_QUALIFIED)

CHANNEL_SHELL = "shell-path"
CHANNEL_HOOK = "hook-event"


class Qualification:
    def __init__(self, surface, component, claim, evidence_level, channel,
                 pre_action_enforcement=None, storage=None, notes=""):
        self.surface = surface          # chat | cowork | code
        self.component = component
        self.claim = claim
        self.evidence_level = evidence_level
        self.channel = channel          # shell-path | hook-event
        self.pre_action_enforcement = pre_action_enforcement  # True/False/None
        self.storage = storage          # "persistent" | "temporary" | None
        self.notes = notes
        # cross-channel fields stay None: qualifying shell says nothing about hooks
        self.hook_enforcement = pre_action_enforcement if channel == CHANNEL_HOOK else None
        self.path_enforcement = pre_action_enforcement if channel == CHANNEL_SHELL else None

    def to_dict(self):
        return {"surface": self.surface, "component": self.component, "claim": self.claim,
                "evidence_level": self.evidence_level, "channel": self.channel,
                "pre_action_enforcement": self.pre_action_enforcement,
                "storage": self.storage, "notes": self.notes}


def qualify_enforcement_pre_action(sentinel_before, action_blocked, sentinel_after):
    """Pure verdict for pre-action enforcement. True only when the action was
    blocked AND the sentinel is unchanged (guard fired BEFORE the action).
    Not blocked -> None (unknown: inert/missing/wrong-matcher guard).
    Blocked but sentinel changed -> False (after-action at best)."""
    if not action_blocked:
        return None
    if sentinel_after != sentinel_before:
        return False
    return True


def qualify(surface, component, claim, evidence_level, channel=CHANNEL_SHELL,
            pre_action_enforcement=None, storage=None, live_receipt=None, notes=""):
    """Build a Qualification, enforcing the evidence-level rules."""
    if evidence_level not in EVIDENCE_LEVELS:
        evidence_level = EVIDENCE_SOURCE_INSPECTED
        notes = (notes + ";unknown evidence level coerced to source-inspected").strip(";")
    if evidence_level == EVIDENCE_RUNTIME_QUALIFIED and not live_receipt:
        evidence_level = EVIDENCE_FIXTURE_TESTED
        notes = (notes + ";runtime claim downgraded: no live receipt (NOT TESTED without one)").strip(";")
    return Qualification(surface, component, claim, evidence_level, channel,
                         pre_action_enforcement=pre_action_enforcement,
                         storage=storage, notes=notes)
