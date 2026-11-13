"""Read-only inventory tests (plan §8 Phase 3): coverage across sources,
identity/scope/shadowing records, purity (zero writes, no subprocess), and the
hide-one-adapter negative (incomplete/unknown, never absent).

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
from core import inventory


def make_claude_home(root):
    """A fake ~/.claude with mixed-origin fixtures: installed registry, plugin
    cache (two versions of one plugin), loose skills (one Unicode), a malformed
    manifest, and NO synced/ dir (fact 5)."""
    home = Path(root) / ".claude"
    plugins = home / "plugins"
    cache = plugins / "cache" / "haiggoh"
    for ver in ("0.9.0", "1.0.3"):
        d = cache / "waypoints" / ver / ".claude-plugin"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps(
            {"name": "waypoints", "version": ver}), encoding="utf-8")
    bad = cache / "broken" / "0.1.0" / ".claude-plugin"
    bad.mkdir(parents=True)
    (bad / "plugin.json").write_text("{not json", encoding="utf-8")
    (plugins / "installed_plugins.json").write_text(json.dumps({
        "waypoints": {"installPath": str(cache / "waypoints" / "1.0.3"),
                      "version": "1.0.3", "scope": "user"}}), encoding="utf-8")
    skills = home / "skills"
    for skill_name in ("my-skill", "unicode-skill-é中"):
        d = skills / skill_name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: %s\n---\n" % skill_name, encoding="utf-8")
    commands = home / "commands"
    commands.mkdir(parents=True)
    (commands / "deploy.md").write_text("deploy!", encoding="utf-8")
    return home


class CollectTests(unittest.TestCase):
    def test_covers_all_sources_with_identities(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_claude_home(d)
            inv = inventory.Inventory({"claude_home": home})
            recs = inv.collect()
            by_name = {r.name: r for r in recs}
            self.assertIn("waypoints", by_name)
            wp = by_name["waypoints"]
            self.assertEqual(wp.installed_version, "1.0.3")  # highest of 0.9.0/1.0.3
            self.assertEqual(wp.scope, "user")
            self.assertTrue(wp.loaded_observable)
            self.assertIn("my-skill", by_name)
            self.assertIn("unicode-skill-é中", by_name)
            self.assertEqual(by_name["my-skill"].kind, "loose-skill")
            self.assertEqual(by_name["deploy"].kind, "loose-command")
            # synced/ absent (fact 5) is recorded as an absence fact, not an error
            synced = [r for r in recs if r.source == "synced"]
            self.assertEqual(synced, [])
            self.assertIn("synced-absent", inv.notes)

    def test_malformed_manifest_is_unknown_not_absent(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_claude_home(d)
            recs = inventory.Inventory({"claude_home": home}).collect()
            broken = [r for r in recs if r.name == "broken"]
            self.assertEqual(len(broken), 1)
            self.assertEqual(broken[0].evidence_level, "unknown")
            self.assertIn("malformed", broken[0].notes)

    def test_hide_one_adapter_means_unknown_not_absent(self):
        # Negative control: remove the cache adapter's view (delete cache dir)
        # — the registry adapter must still resolve waypoints, marked unknown-
        # loaded, never "absent".
        with tempfile.TemporaryDirectory() as d:
            home = make_claude_home(d)
            import shutil
            shutil.rmtree(home / "plugins" / "cache")
            recs = inventory.Inventory({"claude_home": home}).collect()
            by_name = {r.name: r for r in recs}
            self.assertIn("waypoints", by_name)
            self.assertFalse(by_name["waypoints"].loaded_observable)
            self.assertIn("cache-missing", by_name["waypoints"].notes)

    def test_purity_zero_writes_no_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_claude_home(d)
            before = harness.tree_snapshot(d)
            inv = inventory.Inventory({"claude_home": home})
            orig_run, orig_popen = subprocess.run, subprocess.Popen
            def boom(*a, **k):
                raise AssertionError("inventory must not spawn processes")
            subprocess.run = boom
            subprocess.Popen = boom
            try:
                recs = inv.collect()
            finally:
                subprocess.run, subprocess.Popen = orig_run, orig_popen
            self.assertTrue(recs)
            self.assertEqual(harness.changed_files(d, before), set(),
                             "inventory must not write anything")

    def test_scoped_roots_only(self):
        with tempfile.TemporaryDirectory() as d:
            home = make_claude_home(d)
            outside = Path(d) / "elsewhere"
            outside.mkdir()
            (outside / ".claude" / "plugins").mkdir(parents=True)
            (outside / ".claude" / "skills" / "hidden-skill").mkdir(parents=True)
            (outside / ".claude" / "skills" / "hidden-skill" / "SKILL.md").write_text(
                "---\nname: hidden-skill\n---\n", encoding="utf-8")
            recs = inventory.Inventory({"claude_home": home}).collect()
            self.assertNotIn("hidden-skill", {r.name for r in recs})


if __name__ == "__main__":
    unittest.main()
