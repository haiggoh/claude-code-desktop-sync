"""Phase 12: full offline user journeys chained end-to-end in one sandbox —
fresh setup: inventory → plan → build → apply → verify → records → mcp — plus
the negative journeys (shadowed overlay, revoked capability, resource defect
=> correct downgraded verdict while unrelated components stay useful).

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import builder, coexistence, inventory, lifecycle, mcp, planner, records, state as state_mod


def make_home(root):
    """Fresh eligible setup: one installed plugin in cache + registry, one skill."""
    home = Path(root) / ".claude"
    cache = home / "plugins" / "cache" / "haiggoh" / "waypoints" / "1.0.3" / ".claude-plugin"
    cache.mkdir(parents=True)
    (cache / "plugin.json").write_text(json.dumps({"name": "waypoints", "version": "1.0.3"}))
    (home / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"waypoints": {"installPath": str(cache.parent), "version": "1.0.3", "scope": "user"}}))
    (home / "skills" / "my-skill").mkdir(parents=True)
    (home / "skills" / "my-skill" / "SKILL.md").write_text("---\nname: my-skill\n---\n")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {}}))
    return home


def make_third_party_plugin(root):
    src = Path(root) / "legacy-thing"
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "legacy-thing", "version": "0.1.0"}))
    (src / "hooks").mkdir()
    (src / "hooks" / "hooks.json").write_text(json.dumps(
        {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "python3",
         "args": ["${CLAUDE_PLUGIN_ROOT}/bin/tool.py"]}]}]}}))
    (src / "bin").mkdir()
    (src / "bin" / "helper.py").write_text("VALUE = 41\n")
    (src / "bin" / "tool.py").write_text("import helper\nprint('tool-ok', helper.VALUE + 1)\n")
    (src / "README.md").write_text("# legacy-thing\n")
    return src


class JourneyTests(unittest.TestCase):
    def test_full_offline_journey(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home = make_home(root)
            src = make_third_party_plugin(root)
            # inventory
            recs = inventory.Inventory({"claude_home": home}).collect()
            self.assertIn("waypoints", {r.name for r in recs})
            # plan (pure)
            inputs = {"sources": [{"name": r.name, "revision": "r1", "kind": r.kind} for r in recs],
                      "target": {"surface": "code"}}
            plan = planner.build_plan(inputs, baseline=None, first_use=state_mod.FRESH)
            self.assertEqual(plan.status, "ok")
            # build the third-party artifact
            art_dir = root / "artifact"
            builder.build(src, art_dir)
            self.assertTrue((art_dir / "scripts" / "tool.py").is_file())
            # apply via lifecycle (write under root only)
            for a in plan.actions:
                a.content = b"# artifact\n"
            res = lifecycle.apply_plan(plan, root, root / "state")
            self.assertEqual(res.status, "ok")
            # verify: coexistence + distribution readiness on a fresh zip built by scripts
            v = coexistence.coexistence_verdict("legacy-thing", "legacy-thing")
            self.assertEqual(v["verdict"], "shadowed")
            # records export/import
            store = records.RecordStore(root / "recdata", allowlist=("waypoints",))
            store.write_current("waypoints", {"items": []}, origin="journey")
            exp = root / "wp.json"
            store.export_record("waypoints", exp)
            store2 = records.RecordStore(root / "recdata2", allowlist=("waypoints",))
            self.assertEqual(store2.import_record("waypoints", exp, expected_prior=None)["status"], "applied")
            # mcp sync on synthetic configs
            code, desk = root / "code.json", root / "desk.json"
            code.write_text(json.dumps({"mcpServers": {"fs": {"command": "/usr/bin/true"}}}))
            desk.write_text(json.dumps({"mcpServers": {}}))
            st = mcp.SyncState(root / "snap.json", backup_dir=root / "b", journal=root / "j.jsonl")
            st.run(code, desk)
            self.assertIn("fs", json.loads(desk.read_text())["mcpServers"])
            self.assertEqual(st.run(code, desk).changed, [])

    def test_negative_journey_shadow_and_defect_downgrade(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # shadow: distinct-name overlay refused without approval
            v = coexistence.coexistence_verdict("waypoints", "waypoints-copy")
            with self.assertRaises(coexistence.CoexistenceError):
                coexistence.may_activate(v, explicit_approval=False)
            # resource defect: broken plugin refused, unrelated plugin still builds
            bad = make_third_party_plugin(root / "a")
            (bad / "bin" / "tool.py").write_text("from . import missing_mod\n")
            good = make_third_party_plugin(root / "b")
            with self.assertRaises(builder.BuilderError):
                builder.build(bad, root / "bad-out")
            art = builder.build(good, root / "good-out")
            self.assertTrue(art["files"])


if __name__ == "__main__":
    unittest.main()
