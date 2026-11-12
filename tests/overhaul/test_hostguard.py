"""Host-management boundary tests (plan §5.3): only a verified multi-signal
context may perform host-management writes; cloud lookalikes, missing files,
and forged grants must all be refused.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # repo root, for `core`
import harness
from core import hostguard


class DetectTests(unittest.TestCase):
    def test_single_spoofable_signal_is_not_enough(self):
        ctx, _ = hostguard.detect_context(
            env={"CLAUDE_CODE_SESSION_ID": "abc"}, platform="darwin", home="/h", tty=True)
        self.assertNotEqual(ctx, hostguard.CODE_HOST)
        ctx, _ = hostguard.detect_context(
            env={"CLAUDE_PLUGIN_ROOT": "/p"}, platform="darwin", home="/h", tty=True)
        self.assertNotEqual(ctx, hostguard.CODE_HOST)

    def test_code_host_needs_all_signals_plus_config(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".claude.json").write_text("{}", encoding="utf-8")
            env = {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PLUGIN_ROOT": "/p"}
            ctx, ev = hostguard.detect_context(env=env, platform="darwin", home=d, tty=True)
            self.assertEqual(ctx, hostguard.CODE_HOST)
            self.assertTrue(ev["code_session"] and ev["plugin_root"] and ev["config_present"])

    def test_missing_config_is_unknown_not_authority(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PLUGIN_ROOT": "/p"}
            ctx, _ = hostguard.detect_context(env=env, platform="darwin", home=d, tty=True)
            self.assertNotEqual(ctx, hostguard.CODE_HOST)

    def test_cloud_lookalike_with_plausible_configs_is_not_host(self):
        # A cloud sandbox with plausible .claude.json + Desktop config files and
        # CI/no-TTY signals must NOT be treated as the user's host.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".claude.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            desk = Path(d) / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            desk.parent.mkdir(parents=True)
            desk.write_text("{}", encoding="utf-8")
            env = harness.build_env(d)
            env["CI"] = "true"
            ctx, _ = hostguard.detect_context(env=env, platform="linux", home=d, tty=False)
            self.assertNotEqual(ctx, hostguard.CODE_HOST)
            with self.assertRaises(hostguard.HostGuardError):
                hostguard.HostManager.for_context(ctx, {}, root=d)


class ManagerTests(unittest.TestCase):
    def _manager(self, root):
        env = {"CLAUDE_CODE_SESSION_ID": "s", "CLAUDE_PLUGIN_ROOT": "/p"}
        (Path(root) / ".claude.json").write_text("{}", encoding="utf-8")
        ctx, ev = hostguard.detect_context(env=env, platform="darwin", home=root, tty=True)
        return hostguard.HostManager.for_context(ctx, ev, root=root)

    def test_valid_grant_writes_inside_root(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = self._manager(d)
            target = Path(d) / ".claude.json"
            mgr.write_config(target, {"mcpServers": {"a": {"command": "/bin/true"}}})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["a"]["command"],
                             "/bin/true")

    def test_write_outside_verified_root_refused(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            mgr = self._manager(d)
            with self.assertRaises(hostguard.HostGuardError):
                mgr.write_config(Path(other) / ".claude.json", {})

    def test_forged_grant_with_empty_evidence_refused(self):
        # Bypass detection: a hand-built grant without evidence must not pass.
        with tempfile.TemporaryDirectory() as d:
            grant = hostguard.HostGrant(kind=hostguard.CODE_HOST, root=d, evidence=())
            with self.assertRaises(hostguard.HostGuardError):
                hostguard.HostManager(grant).write_config(Path(d) / ".claude.json", {})

    def test_wrong_kind_refused(self):
        with tempfile.TemporaryDirectory() as d:
            grant = hostguard.HostGrant(kind="cloud", root=d,
                                        evidence=hostguard.REQUIRED_SIGNALS)
            with self.assertRaises(hostguard.HostGuardError):
                hostguard.HostManager(grant).write_config(Path(d) / ".claude.json", {})


if __name__ == "__main__":
    unittest.main()
