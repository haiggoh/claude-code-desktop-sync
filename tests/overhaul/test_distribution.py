"""Phase 7 contract tests: native distribution readiness (offline-checkable
parts only — upload/enable stay pending user-run gates) and overlay
coexistence (same-name shadowing vs distinct-name double-run guard), plus
active-version verification (installed row with old marker must FAIL).

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import distribution, coexistence

REPO = Path(__file__).resolve().parent.parent.parent


class ArtifactReadyTests(unittest.TestCase):
    def test_real_dist_zip_is_ready(self):
        zips = sorted((REPO / "dist").glob("*-portable.zip"))
        if not zips:
            self.skipTest("dist/ is gitignored — build first (scripts/build-portable.py)")
        ok, problems = distribution.check_artifact_ready(zips[0])
        self.assertEqual(problems, [], problems)
        self.assertTrue(ok)

    def test_zip_without_root_manifest_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr("sub/plugin.json", "{}")
            ok, problems = distribution.check_artifact_ready(bad)
            self.assertFalse(ok)
            self.assertTrue(any("manifest" in p for p in problems))

    def test_zip_with_top_level_bin_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "bad2.zip"
            with zipfile.ZipFile(bad, "w") as z:
                z.writestr(".claude-plugin/plugin.json", json.dumps({"name": "x", "version": "1"}))
                z.writestr("bin/tool", "#!/bin/sh\n")
            ok, problems = distribution.check_artifact_ready(bad)
            self.assertFalse(ok)
            self.assertTrue(any("bin" in p for p in problems))

    def test_zip_over_limits_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            big = Path(d) / "big.zip"
            with zipfile.ZipFile(big, "w", compression=zipfile.ZIP_DEFLATED) as z:
                z.writestr(".claude-plugin/plugin.json", json.dumps({"name": "x", "version": "1"}))
                z.writestr("big.bin", b"\0" * (5 * 1024 * 1024))
            limits = {"max_bytes": 1024 * 1024}
            ok, problems = distribution.check_artifact_ready(big, limits=limits)
            self.assertFalse(ok)
            self.assertTrue(any("limit" in p.lower() for p in problems))

    def test_pending_steps_never_fabricated(self):
        steps = distribution.pending_steps("dist/claude-code-desktop-sync-portable.zip")
        self.assertTrue(steps)
        for s in steps:
            self.assertEqual(s["status"], "pending", "account steps must stay pending until observed")
            self.assertTrue(s["where"], "every step names where it runs")
        # the upload/enable steps specifically live in the claude.ai UI
        self.assertTrue(any("claude.ai" in s["where"] for s in steps[:2]))


class ActiveVersionTests(unittest.TestCase):
    def test_stale_marker_fails_active_version(self):
        # negative: an installed row whose marker predates the deployed version
        self.assertEqual(
            distribution.verify_active_version({"version": "1.0.3"}, {"seen_version": "1.0.2"}),
            "stale")
        self.assertEqual(
            distribution.verify_active_version({"version": "1.0.3"}, {"seen_version": "1.0.3"}),
            "ok")
        self.assertEqual(
            distribution.verify_active_version({"version": "1.0.3"}, {"shadowed_by": "1.0.3@other-origin"}),
            "shadowed")


class CoexistenceTests(unittest.TestCase):
    def test_same_name_shadowed_not_double_run(self):
        v = coexistence.coexistence_verdict(host_name="claude-code-desktop-sync",
                                             overlay_name="claude-code-desktop-sync")
        self.assertEqual(v["verdict"], "shadowed")
        self.assertFalse(v["double_run"])

    def test_distinct_name_double_run_refused_without_approval(self):
        v = coexistence.coexistence_verdict(host_name="claude-code-desktop-sync",
                                             overlay_name="claude-code-desktop-sync-copy")
        self.assertEqual(v["verdict"], "double-run-risk")
        self.assertTrue(v["double_run"])
        with self.assertRaises(coexistence.CoexistenceError):
            coexistence.may_activate(v, explicit_approval=False)
        # explicit user approval is required to stack a distinct-name overlay
        coexistence.may_activate(v, explicit_approval=True)


if __name__ == "__main__":
    unittest.main()
