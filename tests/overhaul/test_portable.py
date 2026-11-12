"""Contract tests for the portable package builder (COORDINATION C1 Option B):
CLI behavior (--help runs, unknown flag exits 2, --check freshness), portable
layout + transforms, PORTABLE-BUILD.json schema/hashes, zip safety+determinism.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import harness

REPO = Path(__file__).resolve().parent.parent.parent
BUILDER = REPO / "scripts" / "build-portable.py"
ADAPTATION_CLASSES = ("native-unchanged", "mechanical", "qualified-adapter",
                      "reduced-useful", "unsupported", "unknown")
EVIDENCE_LEVELS = ("source-inspected", "fixture-tested", "runtime-qualified")


def run_builder(args, root):
    return harness.run_tool([sys.executable, str(BUILDER)] + args,
                            harness.build_env(root), root, cwd=REPO)


class BuilderCLITests(unittest.TestCase):
    def test_help_runs_and_documents_flags(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_builder(["--help"], d)
            self.assertEqual(r.returncode, 0)
            self.assertIn("--check", r.stdout)

    def test_unknown_flag_exits_2_with_usage(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_builder(["--bogus"], d)
            self.assertEqual(r.returncode, 2)
            self.assertIn("usage", (r.stderr + r.stdout).lower())

    def test_check_passes_on_committed_portable(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_builder(["--check"], d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class PortableLayoutTests(unittest.TestCase):
    def test_layout_and_transforms(self):
        port = REPO / "portable"
        self.assertTrue((port / ".claude-plugin" / "plugin.json").is_file())
        self.assertFalse((port / "bin").exists(), "top-level bin/ must not exist in portable/")
        root_name = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
        port_name = json.loads((port / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
        self.assertEqual(root_name, port_name, "same manifest name required (fact 4)")
        hooks = json.loads((port / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        args = hooks["hooks"]["SessionStart"][0]["hooks"][0]["args"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}/scripts/sync_code_desktop.py", args)
        skill = (port / "skills" / "desktop-sync" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("${CLAUDE_SKILL_DIR}/scripts/sync_code_desktop.py", skill)
        self.assertNotIn("${CLAUDE_SKILL_DIR}/../../bin/", skill)
        self.assertTrue((port / "scripts" / "sync_code_desktop.py").is_file())
        self.assertTrue((port / "skills" / "desktop-sync" / "scripts" / "sync_code_desktop.py").is_file())

    def test_build_meta_schema_and_hashes(self):
        port = REPO / "portable"
        meta = json.loads((port / "PORTABLE-BUILD.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["schema"], "haiggoh-portable-build/1")
        self.assertEqual(meta["builder"], "scripts/build-portable.py")
        for rel, digest in meta["files"].items():
            self.assertEqual(hashlib.sha256((port / rel).read_bytes()).hexdigest(), digest, rel)
        paths = {c["path"] for c in meta["components"]}
        self.assertIn("bin/", paths, "the excluded host-only bin/ must be recorded as a component")
        for c in meta["components"]:
            self.assertIn(c["adaptation_class"], ADAPTATION_CLASSES)
            self.assertIn(c["evidence_level"], EVIDENCE_LEVELS)
            self.assertEqual(set(c["surfaces"].keys()), {"chat", "cowork", "code"})

    def test_zip_rooted_safe_and_deterministic(self):
        zips = sorted((REPO / "dist").glob("*-portable.zip"))
        self.assertEqual(len(zips), 1, "exactly one dist zip expected (gitignored)")
        names = zipfile.ZipFile(zips[0]).namelist()
        self.assertIn(".claude-plugin/plugin.json", names, "manifest at zip root (fact 6)")
        self.assertFalse(any(n == "bin" or n.startswith("bin/") for n in names))
        self.assertFalse(any(n.startswith("/") or ".." in n.split("/") for n in names))
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            blobs = []
            for d in (d1, d2):
                r = run_builder(["--out", str(Path(d) / "portable"),
                                 "--dist", str(Path(d) / "dist")], d)
                self.assertEqual(r.returncode, 0, r.stderr)
                blob = next((Path(d) / "dist").glob("*-portable.zip")).read_bytes()
                blobs.append(blob)
            self.assertEqual(blobs[0], blobs[1], "zip must be byte-deterministic across rebuilds")


if __name__ == "__main__":
    unittest.main()
