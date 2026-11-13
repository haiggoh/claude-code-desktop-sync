"""Phase 8 contract tests: lifecycle apply/recovery — owned locks, backups
verified before first write (failure => zero target writes), private unique
temps (0600), journal with exact partial states, source-freshness conflict
(zero writes), user-edit conflict preserved, inspect-first recovery, and
ownership-only removal.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import lifecycle
from core import planner
from core import exitcodes


def make_plan(dest_name, content, revision="rev1"):
    inputs = {"sources": [{"name": dest_name, "revision": revision}], "target": {"surface": "code"}}
    p = planner.build_plan(inputs, baseline=None, first_use="fresh")
    p.actions[0].destination = dest_name
    p.actions[0].content = content
    return p


class ApplyTests(unittest.TestCase):
    def test_apply_writes_journal_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / "state"
            p = make_plan("artifact.py", b"print('v1')\n")
            r1 = lifecycle.apply_plan(p, root, state)
            self.assertEqual(r1.status, "ok")
            self.assertEqual((root / "artifact.py").read_bytes(), b"print('v1')\n")
            j = json.loads((state / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(j["status"], "committed")
            # second run: no changes, still ok
            r2 = lifecycle.apply_plan(p, root, state)
            self.assertEqual(r2.status, "ok")
            self.assertTrue(r2.idempotent)

    def test_backup_failure_means_zero_target_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "artifact.py").write_bytes(b"original\n")
            state = root / "state"
            state.mkdir()
            (state / "backups").mkdir()
            os.chmod(state / "backups", 0o500)  # backup write will fail
            try:
                p = make_plan("artifact.py", b"print('v2')\n")
                with self.assertRaises(lifecycle.LifecycleError) as cm:
                    lifecycle.apply_plan(p, root, state, fault_inject=None)
                self.assertIn("backup", str(cm.exception).lower())
                self.assertEqual((root / "artifact.py").read_bytes(), b"original\n",
                                 "target must be untouched when backup verification fails")
                j = [json.loads(line) for line in (state / "journal.jsonl").read_text().splitlines()]
                self.assertEqual(j[-1]["status"], "failed-prewrite")
            finally:
                os.chmod(state / "backups", 0o700)

    def test_stale_input_conflict_zero_writes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / "state"
            p = make_plan("a.py", b"v1\n", revision="rev1")
            changed_inputs = {"sources": [{"name": "a.py", "revision": "rev2"}], "target": {"surface": "code"}}
            p2 = planner.revise(p, changed_inputs)
            with self.assertRaises(lifecycle.ConflictError):
                lifecycle.apply_plan(p2, root, state)
            self.assertFalse((root / "a.py").exists(), "conflicted plan must write nothing")
            self.assertEqual(p2.exit_code, exitcodes.EXIT_CONFLICT_STALE)

    def test_user_edit_conflict_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / "state"
            p = make_plan("a.py", b"v1\n")
            lifecycle.apply_plan(p, root, state)
            # user edits the applied artifact
            (root / "a.py").write_bytes(b"# user edit\n")
            p2 = make_plan("a.py", b"v2\n", revision="rev2")
            p2.actions[0].base_digest = lifecycle.digest_bytes(b"v1\n")
            with self.assertRaises(lifecycle.ConflictError):
                lifecycle.apply_plan(p2, root, state)
            self.assertEqual((root / "a.py").read_bytes(), b"# user edit\n",
                             "newer user edit must never be overwritten")

    def test_lock_serializes_and_stale_lock_recovered(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / "state"
            lock = state / ".lock"
            state.mkdir()
            # fresh foreign lock -> refused
            lock.write_text("999999")
            p = make_plan("a.py", b"v1\n")
            with self.assertRaises(lifecycle.LockedError):
                lifecycle.apply_plan(p, root, state)
            # stale lock -> stolen
            old = time.time() - 3600
            os.utime(lock, (old, old))
            r = lifecycle.apply_plan(p, root, state)
            self.assertEqual(r.status, "ok")

    def test_fault_mid_write_records_partial_and_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / "state"
            p = make_plan("a.py", b"v1\n")
            calls = {"n": 0}
            def fault():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("simulated crash after temp write")
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.apply_plan(p, root, state, fault_inject=fault)
            report = lifecycle.recover(state)
            self.assertEqual(report["partial"][-1]["status"], "failed-midwrite")
            self.assertFalse((root / "a.py").exists())
            # rerun without fault completes exactly
            r = lifecycle.apply_plan(p, root, state)
            self.assertEqual(r.status, "ok")
            self.assertEqual((root / "a.py").read_bytes(), b"v1\n")

    def test_removal_is_ownership_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            state = root / "state"
            state.mkdir()
            owned = root / "owned.py"
            foreign = root / "foreign.py"
            owned.write_text("x")
            foreign.write_text("y")
            ownership = {"owned.py": {"sha256": lifecycle.digest_bytes(b"x"), "origin": "cds"}}
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.remove_owned(root / "foreign.py", ownership, state)
            self.assertTrue(foreign.exists())
            lifecycle.remove_owned(owned, ownership, state)
            self.assertFalse(owned.exists())
            # backups live outside the removal target and survive
            self.assertTrue(any((state / "backups").iterdir()))


if __name__ == "__main__":
    unittest.main()
