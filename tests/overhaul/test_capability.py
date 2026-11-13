"""Phase 6 contract tests: bounded capability qualification. Evidence levels
stay separate per surface; pre-action enforcement requires a denied-sentinel
probe (blocked AND sentinel unchanged); runtime-qualified claims need a live
receipt or they are downgraded; hook placement is qualified separately from
shell; storage persistence is explicit.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import harness
from core import capability


class EnforcementVerdictTests(unittest.TestCase):
    def test_truth_table(self):
        # blocked + sentinel unchanged -> pre-action enforcement qualifies
        self.assertTrue(capability.qualify_enforcement_pre_action(
            {"mode": "rw"}, action_blocked=True, sentinel_after={"mode": "rw"}))
        # blocked but sentinel changed -> guard fired after the action, or wrong
        # matcher: NOT pre-action
        self.assertFalse(capability.qualify_enforcement_pre_action(
            {"mode": "rw"}, action_blocked=True, sentinel_after={"mode": "ro"}))
        # not blocked (inert/missing guard) -> unknown, never qualified
        self.assertIsNone(capability.qualify_enforcement_pre_action(
            {"mode": "rw"}, action_blocked=False, sentinel_after={"mode": "rw"}))


class QualificationTests(unittest.TestCase):
    def test_runtime_qualified_needs_live_receipt(self):
        q = capability.qualify("cowork", "hooks", "hooks load at session start",
                               capability.EVIDENCE_RUNTIME_QUALIFIED, live_receipt=None)
        self.assertEqual(q.evidence_level, capability.EVIDENCE_FIXTURE_TESTED)
        self.assertIn("downgraded", q.notes)
        q2 = capability.qualify("cowork", "hooks", "hooks load at session start",
                                capability.EVIDENCE_RUNTIME_QUALIFIED, live_receipt="receipt-abc")
        self.assertEqual(q2.evidence_level, capability.EVIDENCE_RUNTIME_QUALIFIED)

    def test_hook_and_shell_are_separate_qualifications(self):
        q_shell = capability.qualify("code", "bin", "bare command on PATH",
                                     capability.EVIDENCE_SOURCE_INSPECTED)
        q_hook = capability.qualify("cowork", "hooks", "hook fires at SessionStart",
                                    capability.EVIDENCE_SOURCE_INSPECTED,
                                    channel=capability.CHANNEL_HOOK)
        self.assertNotEqual(q_shell.channel, q_hook.channel)
        # qualifying shell says NOTHING about hooks
        self.assertIsNone(q_shell.hook_enforcement)
        self.assertIsNone(q_hook.path_enforcement)

    def test_storage_persistence_explicit(self):
        q = capability.qualify("code", "plugin-data", "state persists",
                               capability.EVIDENCE_SOURCE_INSPECTED, storage="persistent")
        self.assertEqual(q.storage, "persistent")
        q2 = capability.qualify("code", "plugin-data", "state persists",
                                capability.EVIDENCE_SOURCE_INSPECTED, storage="temporary")
        self.assertEqual(q2.storage, "temporary")
        self.assertNotEqual(q.storage, q2.storage)

    def test_purity(self):
        with tempfile.TemporaryDirectory() as d:
            before = harness.tree_snapshot(d)
            orig = (subprocess.run, subprocess.Popen)
            subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no subprocess"))
            subprocess.Popen = subprocess.run
            try:
                capability.qualify("chat", "skills", "skill loads", capability.EVIDENCE_SOURCE_INSPECTED)
                capability.qualify_enforcement_pre_action({}, True, {})
            finally:
                subprocess.run, subprocess.Popen = orig
            self.assertEqual(harness.changed_files(d, before), set())


if __name__ == "__main__":
    unittest.main()
