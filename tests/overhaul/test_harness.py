"""Contract tests for the overhaul harness itself: env rooting, the launch
guard (with contained negative controls), engine isolation, and write purity.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness


class HarnessTests(unittest.TestCase):
    def test_leaky_filter_is_case_insensitive(self):
        # Windows stores PATH/USERPROFILE as "Path"/"UserProfile" — the filter
        # must remove them regardless of case, or the real ones survive next to
        # the sandbox copies and win in the child's env block.
        with mock.patch.dict(os.environ, {"Path": "C:\\real\\bin",
                                          "UserProfile": "C:\\real\\user",
                                          "HOMEDRIVE": "C:"}, clear=False):
            clean = harness.clean_env()
            for key in clean:
                self.assertNotIn(key.upper(), harness.LEAKY_ENV_UPPER, key)

    def test_build_env_is_rooted(self):
        with tempfile.TemporaryDirectory() as d:
            env = harness.build_env(d)
            root = Path(d).resolve()
            for key in harness.REQUIRED_ROOT_KEYS:
                self.assertEqual(Path(env[key]).resolve().parent, root,
                                 "%s must be a direct child of the root" % key)
            self.assertIn(str(root / "bin"), env["PATH"])
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_guard_refuses_missing_override(self):
        # Contained: even if the guard were broken, HOME still points into the
        # sandbox, so a mutated run could not reach the real machine.
        with tempfile.TemporaryDirectory() as d:
            env = harness.build_env(d)
            del env["CLAUDE_SYNC_HOME"]
            with self.assertRaises(harness.HarnessError):
                harness.run_engine(env, d)

    def test_guard_refuses_escape(self):
        # Contained: the escape target is a sibling of the temp dir (still inside
        # the system temp area), cleaned up afterwards either way.
        with tempfile.TemporaryDirectory() as d:
            env = harness.build_env(d)
            escape = Path(d).parent / (Path(d).name + "-escape")
            env["CLAUDE_SYNC_HOME"] = str(escape)
            try:
                with self.assertRaises(harness.HarnessError):
                    harness.run_engine(env, d)
            finally:
                shutil.rmtree(escape, ignore_errors=True)

    def test_engine_runs_isolated(self):
        with tempfile.TemporaryDirectory() as d:
            env = harness.build_env(d)
            Path(env["CLAUDE_CODE_CONFIG"]).write_text(
                json.dumps({"mcpServers": {}}), encoding="utf-8")
            Path(env["CLAUDE_DESKTOP_CONFIG"]).write_text(
                json.dumps({"mcpServers": {}}), encoding="utf-8")
            (Path(env["HOME"]) / ".claude" / "skills" / "only-skill").mkdir(parents=True)
            r = harness.run_engine(env, d)
            self.assertEqual(r.returncode, 0, r.stderr)
            state = json.loads((Path(env["CLAUDE_SYNC_HOME"])
                                / "last-sync-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["skills"], ["only-skill"])

    def test_writes_stay_under_sync_home_on_noop(self):
        with tempfile.TemporaryDirectory() as d:
            env = harness.build_env(d)
            Path(env["CLAUDE_CODE_CONFIG"]).write_text(
                json.dumps({"mcpServers": {}}), encoding="utf-8")
            Path(env["CLAUDE_DESKTOP_CONFIG"]).write_text(
                json.dumps({"mcpServers": {}}), encoding="utf-8")
            before = harness.tree_snapshot(d)
            harness.run_engine(env, d)
            changed = harness.changed_files(d, before)
            self.assertTrue(changed, "expected the state file to be written")
            # Path parts, not a "/" prefix — Windows relpaths use backslashes
            self.assertTrue(all(Path(c).parts[0] == "sync" for c in changed),
                            "writes outside the sync home: %s" % sorted(changed))


if __name__ == "__main__":
    unittest.main()
