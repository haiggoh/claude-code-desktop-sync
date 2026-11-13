"""Phase 4 contract tests: versioned typed state schema (refuse wrong shape /
drift loudly), first-use state distinction, and the pure revision-bound planner
(change input after plan -> conflict, exit 4, zero target writes).

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import harness
from core import state as state_mod
from core import planner
from core import exitcodes


class StateSchemaTests(unittest.TestCase):
    def test_valid_state_roundtrips(self):
        doc = state_mod.new_state(source="marketplace-installed", install_name="waypoints")
        ok, errors = state_mod.validate_state(doc)
        self.assertEqual(errors, [])
        self.assertTrue(ok)

    def test_wrong_shape_refused_loudly(self):
        ok, errors = state_mod.validate_state({"schema": state_mod.STATE_SCHEMA, "sources": "not-a-list"})
        self.assertFalse(ok)
        self.assertTrue(any("sources" in e for e in errors))

    def test_schema_drift_refused(self):
        doc = state_mod.new_state(source="x", install_name="y")
        doc["schema"] = "cds-state/2"
        ok, errors = state_mod.validate_state(doc)
        self.assertFalse(ok)
        self.assertTrue(any("schema" in e for e in errors))

    def test_not_json_refused(self):
        ok, errors = state_mod.validate_state([1, 2, 3])
        self.assertFalse(ok)
        self.assertTrue(errors)

    def test_first_use_states_distinguished(self):
        self.assertEqual(state_mod.first_use_kind(absent=True, corrupt=False, valid_shape=False),
                         state_mod.FRESH)
        self.assertEqual(state_mod.first_use_kind(absent=False, corrupt=True, valid_shape=False),
                         state_mod.LOST_BASELINE)
        self.assertEqual(state_mod.first_use_kind(absent=False, corrupt=False, valid_shape=False),
                         state_mod.INVALID_BASELINE)
        self.assertEqual(state_mod.first_use_kind(absent=False, corrupt=False, valid_shape=True),
                         state_mod.EXISTING)


class PlannerTests(unittest.TestCase):
    def _inputs(self, extra=None):
        base = {
            "sources": [{"name": "waypoints", "revision": "abc123", "kind": "plugin"}],
            "target": {"surface": "code"},
            "overrides": {},
        }
        if extra:
            base.update(extra)
        return base

    def test_fresh_plan_is_union_no_deletions(self):
        p = planner.build_plan(self._inputs(), baseline=None, first_use=state_mod.FRESH)
        self.assertEqual(p.status, "ok")
        self.assertTrue(p.actions)
        self.assertFalse(any(a.kind == "delete" for a in p.actions),
                         "first-use approved union must never delete")

    def test_lost_baseline_refuses_writes(self):
        p = planner.build_plan(self._inputs(), baseline="CORRUPT", first_use=state_mod.LOST_BASELINE)
        self.assertEqual(p.status, "recovery-required")
        self.assertEqual(p.actions, [])
        self.assertEqual(p.exit_code, exitcodes.EXIT_FAILURE)

    def test_absent_source_is_unavailable_not_empty(self):
        inputs = self._inputs()
        inputs["sources"] = []
        p = planner.build_plan(inputs, baseline=None, first_use=state_mod.FRESH)
        self.assertEqual(p.status, "unavailable")
        self.assertEqual(p.exit_code, exitcodes.EXIT_PREREQ)

    def test_stale_input_conflicts_with_zero_writes(self):
        p = planner.build_plan(self._inputs(), baseline=None, first_use=state_mod.FRESH)
        changed = self._inputs()
        changed["sources"][0]["revision"] = "def456"
        self.assertFalse(planner.check_freshness(p, changed))
        p2 = planner.revise(p, changed)
        self.assertEqual(p2.status, "conflict")
        self.assertEqual(p2.exit_code, exitcodes.EXIT_CONFLICT_STALE)
        self.assertEqual(p2.actions, [], "a conflicted plan carries no writable actions")

    def test_planner_is_pure(self):
        with tempfile.TemporaryDirectory() as d:
            before = harness.tree_snapshot(d)
            orig = (subprocess.run, subprocess.Popen)
            subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no subprocess"))
            subprocess.Popen = subprocess.run
            try:
                p = planner.build_plan(self._inputs(), baseline=None, first_use=state_mod.FRESH)
                planner.check_freshness(p, self._inputs())
            finally:
                subprocess.run, subprocess.Popen = orig
            self.assertEqual(harness.changed_files(d, before), set())
            self.assertEqual(p.status, "ok")

    def test_json_and_human_agree(self):
        p = planner.build_plan(self._inputs(), baseline=None, first_use=state_mod.FRESH)
        doc = json.loads(p.to_json())
        human = p.to_human()
        self.assertEqual(doc["status"], "ok")
        self.assertIn("ok", human)
        self.assertEqual(doc["plan_id"], p.plan_id)
        self.assertIn(p.plan_id, human)


if __name__ == "__main__":
    unittest.main()
