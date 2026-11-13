"""Phase 10 contract tests: hardened legacy-MCP reconciliation in core/mcp.py.
Fixes for the legacy defects found in Phase 0: edit/delete no longer drops the
survivor unchecked; typed validated snapshot with unknown-field preservation;
unique private (0600) backups/temps; owned lock; per-file journal with exact
partial states (two config files are never one transaction); backup failure =>
zero writes; idempotent convergence.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import mcp


def write(p, obj):
    Path(p).write_text(json.dumps(obj, indent=2), encoding="utf-8")


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


class ReconcileTests(unittest.TestCase):
    def test_push_pull_remove_converge(self):
        with tempfile.TemporaryDirectory() as d:
            code, desk, snap = Path(d) / "code.json", Path(d) / "desk.json", Path(d) / "snap.json"
            write(code, {"mcpServers": {"fs": {"command": "/usr/bin/true"}}, "other": 1})
            write(desk, {"mcpServers": {}})
            st = mcp.SyncState(snap)
            st.run(code, desk)
            self.assertIn("fs", read(desk)["mcpServers"])
            self.assertEqual(read(desk).get("other"), None)
            self.assertEqual(read(code)["other"], 1, "non-mcp keys preserved")
            out2 = st.run(code, desk)
            self.assertEqual(out2.changed, [], "second run is a no-op")
            # remove propagates
            c = read(code)
            del c["mcpServers"]["fs"]
            write(code, c)
            st.run(code, desk)
            self.assertNotIn("fs", read(desk)["mcpServers"])

    def test_edit_delete_conflict_holds_both(self):
        # the legacy defect: delete on one side + edit on the other used to
        # delete the survivor unchecked
        with tempfile.TemporaryDirectory() as d:
            code, desk, snap = Path(d) / "c.json", Path(d) / "d.json", Path(d) / "s.json"
            write(code, {"mcpServers": {"s": {"command": "/a"}}})
            write(desk, {"mcpServers": {"s": {"command": "/a"}}})
            st = mcp.SyncState(snap)
            st.run(code, desk)  # baseline
            c = read(code)
            c["mcpServers"]["s"] = {"command": "/edited"}
            write(code, c)  # edit code
            write(desk, {"mcpServers": {}})  # delete on desk
            out = st.run(code, desk)
            self.assertIn("s", out.conflicts, "edit+delete must conflict, not delete")
            self.assertEqual(read(code)["mcpServers"]["s"]["command"], "/edited")
            self.assertNotIn("s", read(desk)["mcpServers"])  # desk side stays as-is

    def test_two_sided_edit_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            code, desk, snap = Path(d) / "c.json", Path(d) / "d.json", Path(d) / "s.json"
            write(code, {"mcpServers": {"s": {"command": "/a"}}})
            write(desk, {"mcpServers": {"s": {"command": "/a"}}})
            st = mcp.SyncState(snap)
            st.run(code, desk)
            c = read(code)
            c["mcpServers"]["s"] = {"command": "/c"}
            write(code, c)
            dd = read(desk)
            dd["mcpServers"]["s"] = {"command": "/d"}
            write(desk, dd)
            out = st.run(code, desk)
            self.assertIn("s", out.conflicts)
            self.assertEqual(read(code)["mcpServers"]["s"]["command"], "/c")
            self.assertEqual(read(desk)["mcpServers"]["s"]["command"], "/d")

    def test_held_conflict_persists_across_runs(self):
        # review H4: a held edit/delete conflict must NOT be re-propagated on
        # the next run (the user's deletion must not be silently reverted)
        with tempfile.TemporaryDirectory() as d:
            code, desk, snap = Path(d) / "c.json", Path(d) / "d.json", Path(d) / "s.json"
            write(code, {"mcpServers": {"s": {"command": "/a"}}})
            write(desk, {"mcpServers": {"s": {"command": "/a"}}})
            st = mcp.SyncState(snap)
            st.run(code, desk)
            c = read(code)
            c["mcpServers"]["s"] = {"command": "/edited"}
            write(code, c)
            write(desk, {"mcpServers": {}})
            out2 = st.run(code, desk)
            self.assertIn("s", out2.conflicts)
            out3 = st.run(code, desk)  # no further edits
            self.assertIn("s", out3.conflicts, "conflict must persist, not re-propagate")
            self.assertNotIn("s", read(desk)["mcpServers"],
                             "the user's deletion must not be silently reverted")

    def test_type_conflict_preserves_connector(self):
        with tempfile.TemporaryDirectory() as d:
            code, desk, snap = Path(d) / "c.json", Path(d) / "d.json", Path(d) / "s.json"
            write(code, {"mcpServers": {"gh": {"command": "npx", "args": ["x"]}}})
            write(desk, {"mcpServers": {"gh": {"type": "http", "url": "https://x", "headers": {"A": "Bearer S"}}}})
            st = mcp.SyncState(snap)
            out = st.run(code, desk)
            self.assertIn("gh", out.type_conflicts)
            self.assertEqual(read(desk)["mcpServers"]["gh"]["headers"]["A"], "Bearer S")

    def test_snapshot_schema_validated_unknown_fields_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "s.json"
            write(snap, {"schema": "cds-mcp-snapshot/1", "servers": {}, "future_field": {"x": 1}})
            st = mcp.SyncState(snap)
            self.assertEqual(st.load().get("future_field"), {"x": 1}, "unknown fields preserved, not used to authorize merges")
            write(snap, {"schema": "bogus/9"})
            with self.assertRaises(mcp.SnapshotError):
                mcp.SyncState(snap).load()

    def test_backup_failure_zero_writes(self):
        with tempfile.TemporaryDirectory() as d:
            code, desk, snap = Path(d) / "c.json", Path(d) / "d.json", Path(d) / "s.json"
            write(code, {"mcpServers": {"fs": {"command": "/usr/bin/true"}}})
            write(desk, {"mcpServers": {}})
            # blocker file where backups dir must be -> fails even as root
            (Path(d) / "backups").write_text("x")
            st = mcp.SyncState(snap, backup_dir=Path(d) / "backups")
            with self.assertRaises(mcp.SyncError) as cm:
                st.run(code, desk)
            self.assertIn("backup", str(cm.exception).lower())
            self.assertNotIn("fs", read(desk)["mcpServers"], "zero target writes on backup failure")
            jpath = Path(d) / "journal.jsonl"
            jr = [json.loads(line) for line in jpath.read_text().splitlines()] if jpath.exists() else []
            self.assertTrue(all(e["status"] != "committed" for e in jr))

    def test_second_file_failure_journals_partial(self):
        with tempfile.TemporaryDirectory() as d:
            code, desk, snap = Path(d) / "c.json", Path(d) / "d.json", Path(d) / "s.json"
            write(code, {"mcpServers": {"a": {"command": "/usr/bin/true"}}})
            write(desk, {"mcpServers": {}})
            st = mcp.SyncState(snap, backup_dir=Path(d) / "backups", journal=Path(d) / "journal.jsonl")
            st.run(code, desk)  # baseline
            # change BOTH sides so both files are written, and fault the 2nd write
            c = read(code)
            c["mcpServers"]["b"] = {"command": "/usr/bin/true"}
            write(code, c)
            dd = read(desk)
            dd["mcpServers"]["c"] = {"command": "/usr/bin/true"}
            write(desk, dd)
            def fault(committed):
                if committed == 1:
                    raise OSError("simulated second-write failure")
            st2 = mcp.SyncState(Path(d) / "s2.json", backup_dir=Path(d) / "backups",
                                journal=Path(d) / "j2.jsonl")
            with self.assertRaises(mcp.SyncError):
                st2.run(code, desk, fault_inject=fault)
            jr = [json.loads(line) for line in (Path(d) / "j2.jsonl").read_text().splitlines()]
            self.assertTrue(any(e["status"] == "partial" for e in jr),
                            "mid-transaction failure must be recorded as partial, not complete")
            self.assertEqual(sum(1 for e in jr if e["status"] == "committed"), 1)

    def test_snapshot_file_permissions_private(self):
        with tempfile.TemporaryDirectory() as d:
            snap = Path(d) / "s.json"
            write(Path(d) / "c.json", {"mcpServers": {}})
            write(Path(d) / "d.json", {"mcpServers": {}})
            st = mcp.SyncState(snap, backup_dir=Path(d) / "b", journal=Path(d) / "j.jsonl")
            st.run(Path(d) / "c.json", Path(d) / "d.json")
            if os.name == "posix":
                mode = oct(os.stat(snap).st_mode & 0o777)
                self.assertEqual(mode, "0o600", "snapshot may hold server env -> 0600, got %s" % mode)
            else:
                # Windows chmod only maps the read-only bit; ACL policy is
                # platform-specific and documented as best-effort there
                self.assertTrue(snap.exists())


if __name__ == "__main__":
    unittest.main()
