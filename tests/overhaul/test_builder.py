"""Phase 5 contract tests: the deterministic legacy-artifact builder.
Mandatory envelope: native plugin, loose skill + resources, hook→bin script,
skill→bin script, python sibling imports, offline complete, dynamic dependency
(disclosed unknown). Negatives: missing sibling, symlink escape, duplicate
normalized path, private marker, excessive size, source-changed-mid-build.
The restored complete fixture must produce REAL usable script output.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import harness
from core import builder


def make_legacy_plugin(root, with_missing_sibling=False, with_private=False,
                       with_dup=False, with_escape_link=False):
    """An independent third-party-style legacy plugin fixture (not the author's)."""
    src = Path(root) / "legacy-thing"
    (src / ".claude-plugin").mkdir(parents=True)
    (src / ".claude-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "legacy-thing", "version": "0.1.0"}), encoding="utf-8")
    (src / "hooks").mkdir()
    (src / "hooks" / "hooks.json").write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "python3",
            "args": ["${CLAUDE_PLUGIN_ROOT}/bin/tool.py"]}]}]}}), encoding="utf-8")
    (src / "bin").mkdir()
    helper = "VALUE = 41\n" if not with_missing_sibling else ""
    (src / "bin" / "helper.py").write_text(helper, encoding="utf-8")
    tool = ("import helper\n"
            "import missing_mod\n" if with_missing_sibling else "import helper\n") + \
           "print('tool-ok', helper.VALUE + 1)\n"
    (src / "bin" / "tool.py").write_text(tool, encoding="utf-8")
    skill = src / "skills" / "legacy-thing"
    (skill / "resources").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "run `${CLAUDE_SKILL_DIR}/../../bin/tool.py` for the tool\n", encoding="utf-8")
    (skill / "resources" / "data.txt").write_text("resource-data", encoding="utf-8")
    (src / "README.md").write_text("# legacy-thing\n", encoding="utf-8")
    if with_private:
        # not a private-looking NAME — only the content marker can catch this
        (src / "notes.txt").write_text("backup: PRIVATE-MARKER-abc123\n", encoding="utf-8")
    if with_dup:
        (src / "Data.txt").write_text("x", encoding="utf-8")
        (src / "data.txt").write_text("y", encoding="utf-8")
    if with_escape_link:
        outside = Path(root) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (src / "link.txt").symlink_to(outside)
    return src


class BuilderTests(unittest.TestCase):
    def test_complete_fixture_builds_usable_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d)
            dest = Path(d) / "out"
            art = builder.build(src, dest)
            # hook + skill scripts relocated, sibling import preserved
            self.assertTrue((dest / "scripts" / "tool.py").is_file())
            self.assertTrue((dest / "scripts" / "helper.py").is_file())
            self.assertTrue((dest / "skills" / "legacy-thing" / "scripts" / "tool.py").is_file())
            hooks = json.loads((dest / "hooks" / "hooks.json").read_text(encoding="utf-8"))
            args = hooks["hooks"]["SessionStart"][0]["hooks"][0]["args"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/tool.py", args)
            skill_md = (dest / "skills" / "legacy-thing" / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("${CLAUDE_SKILL_DIR}/scripts/tool.py", skill_md)
            # resources preserved; manifest name unchanged; no top-level bin
            self.assertTrue((dest / "skills" / "legacy-thing" / "resources" / "data.txt").is_file())
            self.assertEqual(json.loads((dest / ".claude-plugin" / "plugin.json").read_text())["name"],
                             "legacy-thing")
            self.assertFalse((dest / "bin").exists())
            # artifact manifest with hashes + provenance
            self.assertTrue(art["files"])
            self.assertTrue(all(len(v) == 64 for v in art["files"].values()))
            meta = json.loads((dest / "ARTIFACT.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["files"], art["files"])
            # REAL usable output: the built script runs and prints
            env = harness.build_env(d)
            r = harness.run_tool([sys.executable, str(dest / "scripts" / "tool.py")], env, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("tool-ok 42", r.stdout)

    def test_missing_sibling_refuses_component(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d, with_missing_sibling=True)
            with self.assertRaises(builder.BuilderError) as cm:
                builder.build(src, Path(d) / "out")
            self.assertIn("missing_mod", str(cm.exception))

    def test_private_marker_refused(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d, with_private=True)
            with self.assertRaises(builder.BuilderError) as cm:
                builder.build(src, Path(d) / "out")
            self.assertIn("private", str(cm.exception).lower())

    def test_duplicate_normalized_path_refused(self):
        # A case-insensitive FS cannot hold both files, so the name list is
        # checked directly (this also covers zip inputs where both coexist).
        with self.assertRaises(builder.BuilderError) as cm:
            builder.check_duplicate_names(["Data.txt", "data.txt"])
        self.assertIn("duplicate", str(cm.exception).lower())
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d)  # no dup — a normal tree must not raise
            builder.build(src, Path(d) / "out")

    def test_symlink_escape_refused(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d, with_escape_link=True)
            with self.assertRaises(builder.BuilderError) as cm:
                builder.build(src, Path(d) / "out")
            self.assertIn("escape", str(cm.exception).lower())

    def test_excessive_size_refused(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d)
            (src / "big.bin").write_bytes(b"\0" * 4096)
            limits = dict(builder.DEFAULT_LIMITS)
            limits["max_bytes"] = 1024
            with self.assertRaises(builder.BuilderError) as cm:
                builder.build(src, Path(d) / "out", limits=limits)
            self.assertIn("limit", str(cm.exception).lower())

    def test_source_changed_mid_build_detected(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d)
            before = builder.snapshot_source(src)
            (src / "README.md").write_text("# changed mid-build\n", encoding="utf-8")
            after = builder.snapshot_source(src)
            with self.assertRaises(builder.BuilderError):
                builder.verify_source_stable(before, after)

    def test_dynamic_dependency_disclosed_as_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            src = make_legacy_plugin(d)
            hooks = json.loads((src / "hooks" / "hooks.json").read_text(encoding="utf-8"))
            hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"] = "definitely-not-on-path-cmd"
            (src / "hooks" / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")
            art = builder.build(src, Path(d) / "out", external_on_path=[])
            unknowns = [c for c in art["components"] if c.get("adaptation_class") == "unknown"]
            self.assertTrue(unknowns, "dynamic dependency must be disclosed as unknown, not fabricated")
            self.assertIn("definitely-not-on-path-cmd", unknowns[0]["notes"])


if __name__ == "__main__":
    unittest.main()
