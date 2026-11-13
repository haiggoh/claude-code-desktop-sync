"""Phase 9 contract tests: selected records via explicit allowlist —
snapshot/export/propose/import with one authoritative writer, revision and
expected-prior IDs, provenance, explicit tombstones, idempotent duplicate
proposals, concurrent conflict held, and offline/missing NEVER deletes.
Explicit export/import is the mandatory fallback (native folder transport is
not runtime-qualified on this machine).

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import records


class RecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = records.RecordStore(self.dir / "data", allowlist=("waypoints",))

    def tearDown(self):
        self._tmp.cleanup()

    def test_allowlist_enforced(self):
        with self.assertRaises(records.RecordError):
            self.store.snapshot("not-allowlisted")

    def test_export_import_roundtrip_with_provenance(self):
        self.store.write_current("waypoints", {"items": ["a"]}, origin="test")
        exp = self.dir / "export.json"
        meta = self.store.export_record("waypoints", exp)
        self.assertIn("revision", meta)
        self.assertIn("provenance", meta)
        # import into a fresh store
        other = records.RecordStore(self.dir / "data2", allowlist=("waypoints",))
        r = other.import_record("waypoints", exp, expected_prior=None)
        self.assertEqual(r["status"], "applied")
        self.assertEqual(other.snapshot("waypoints")["content"], {"items": ["a"]})

    def test_stale_expected_prior_refused(self):
        self.store.write_current("waypoints", {"v": 1}, origin="test")
        exp = self.dir / "e.json"
        self.store.export_record("waypoints", exp)
        other = records.RecordStore(self.dir / "data2", allowlist=("waypoints",))
        other.write_current("waypoints", {"v": 2}, origin="test")
        with self.assertRaises(records.RecordError) as cm:
            other.import_record("waypoints", exp, expected_prior="bogus-revision")
        self.assertIn("stale", str(cm.exception).lower())
        self.assertEqual(other.snapshot("waypoints")["content"], {"v": 2},
                         "stale import must not overwrite")

    def test_duplicate_proposal_idempotent_conflict_held(self):
        self.store.write_current("waypoints", {"v": 1}, origin="test")
        rev = self.store.snapshot("waypoints")["revision"]
        p1 = self.store.propose("waypoints", {"v": 2}, expected_prior=rev, author="a")
        p2 = self.store.propose("waypoints", {"v": 2}, expected_prior=rev, author="a")
        self.assertEqual(p1["proposal_id"], p2["proposal_id"])
        self.assertEqual(p2["status"], "duplicate-idempotent")
        # a different content with the same (now stale) prior is a conflict, held
        p3 = self.store.propose("waypoints", {"v": 3}, expected_prior=rev, author="b")
        self.assertEqual(p3["status"], "conflict-held")

    def test_offline_or_missing_never_deletes(self):
        self.store.write_current("waypoints", {"v": 1}, origin="test")
        # transport "disconnected": export file removed after export
        exp = self.dir / "e.json"
        self.store.export_record("waypoints", exp)
        exp.unlink()
        other = records.RecordStore(self.dir / "data2", allowlist=("waypoints",))
        other.write_current("waypoints", {"v": 9}, origin="test")
        r = other.import_record("waypoints", exp, expected_prior=None)
        self.assertEqual(r["status"], "offline-missing")
        self.assertEqual(other.snapshot("waypoints")["content"], {"v": 9},
                         "a missing export must never delete the authoritative record")

    def test_unknown_schema_refused_loudly(self):
        bad = self.dir / "bad.json"
        bad.write_text(json.dumps({"schema": "other/9", "revision": "x", "content": {}}))
        with self.assertRaises(records.RecordError) as cm:
            self.store.import_record("waypoints", bad, expected_prior=None)
        self.assertIn("schema", str(cm.exception).lower())

    def test_tombstone_explicit(self):
        self.store.write_current("waypoints", {"v": 1}, origin="test")
        t = self.store.tombstone("waypoints", reason="retired")
        self.assertTrue(t["tombstone"])
        snap = self.store.snapshot("waypoints")
        self.assertTrue(snap.get("tombstone"))


if __name__ == "__main__":
    unittest.main()
