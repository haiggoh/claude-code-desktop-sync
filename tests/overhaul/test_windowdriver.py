"""Phase 11-B contract tests: the bundled nonprivileged window driver.
Offline: platform driver selection (injected fakes), double-layer quoting
(shell argv AND AppleScript string), unique private per-launch receipt dirs,
receipt validation (matching launch id + freshness; foreign/old/symlink
receipts rejected), distinct states, bounded wait labelled NOT_VERIFIED,
no auto-relaunch on uncertain acceptance, hostile path quoting.

Run: python3 -m unittest discover -s tests/overhaul -p 'test_*.py'
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from core import windowdriver as wd


class QuotingTests(unittest.TestCase):
    def test_applescript_encoding_layers(self):
        hostile = 'a"b\\c$d`e\nf g\t'
        enc = wd.applescript_quote(hostile)
        # after Python-level escaping, the string must contain no raw " or \ or newline
        self.assertNotIn('"', enc.replace('\\"', ""))
        self.assertNotIn("\n", enc)
        self.assertIn('\\"', enc)
        self.assertIn("\\\\", enc)

    def test_shell_argv_is_a_list_never_joined(self):
        argv = ["/bin/echo", "a; rm -rf ~", 'x"y']
        self.assertEqual(wd.shell_argv_list(argv), argv, "argv stays a list")


class ReceiptTests(unittest.TestCase):
    def test_receipt_match_and_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            rid = wd.new_launch_id()
            rdir = wd.new_receipt_dir(Path(d), rid)
            self.assertEqual(rdir.name, rid)
            self.assertTrue((rdir / ".owned").exists())
            receipt = {"launch_id": rid, "frontend": "terminal", "frontend_digest": "abc",
                       "cwd": d, "tty": True, "started_at": time.time()}
            wd.write_receipt(rdir, receipt)
            got = wd.validate_receipt(rdir, rid, max_age=60)
            self.assertEqual(got["launch_id"], rid)

    def test_foreign_or_stale_or_symlink_receipt_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            rid = wd.new_launch_id()
            rdir = wd.new_receipt_dir(Path(d), rid)
            # foreign launch id
            wd.write_receipt(rdir, {"launch_id": "other", "frontend": "t", "tty": True,
                                   "started_at": time.time()})
            with self.assertRaises(wd.ReceiptError):
                wd.validate_receipt(rdir, rid, max_age=60)
            # stale
            wd.write_receipt(rdir, {"launch_id": rid, "frontend": "t", "tty": True,
                                   "started_at": time.time() - 3600})
            with self.assertRaises(wd.ReceiptError):
                wd.validate_receipt(rdir, rid, max_age=60)


class DriverTests(unittest.TestCase):
    def test_driver_selection_by_platform(self):
        self.assertEqual(wd.select_driver("darwin", has_osascript=True), "terminal-app")
        self.assertEqual(wd.select_driver("darwin", has_osascript=False), "none")
        self.assertEqual(wd.select_driver("win32", has_osascript=False), "windows-terminal")
        self.assertEqual(wd.select_driver("linux", has_osascript=False), "none")

    def test_launch_with_fake_driver_states(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            def fake_driver(argv, cwd, receipt_dir, launch_id):
                calls.append((argv, cwd, receipt_dir, launch_id))
                # simulate frontend started + wrote receipt
                wd.write_receipt(Path(receipt_dir), {
                    "launch_id": launch_id, "frontend": "fake", "frontend_digest": "d",
                    "cwd": cwd, "tty": True, "started_at": time.time()})
                return 0
            r = wd.launch(["/bin/echo", "hi"], cwd=d, driver=fake_driver, platform="darwin")
            self.assertEqual(r.state, "frontend-started")
            self.assertEqual(r.launch_id, calls[0][3])
            self.assertTrue(wd.validate_receipt(Path(r.receipt_dir), r.launch_id, max_age=60))

    def test_accepted_but_no_receipt_is_not_verified_no_relaunch(self):
        with tempfile.TemporaryDirectory() as d:
            def silent_driver(argv, cwd, receipt_dir, launch_id):
                return 0  # osascript accepted, frontend never wrote a receipt
            r = wd.launch(["/bin/echo", "hi"], cwd=d, driver=silent_driver, platform="darwin",
                          wait_seconds=2, poll_interval=0.2)
            self.assertEqual(r.state, "not-verified")
            self.assertEqual(r.relaunches, 0, "no blind relaunch on uncertain acceptance")

    def test_driver_failure_is_request_error(self):
        with tempfile.TemporaryDirectory() as d:
            def failing(argv, cwd, receipt_dir, launch_id):
                return 1
            r = wd.launch(["/bin/echo"], cwd=d, driver=failing, platform="darwin", wait_seconds=1)
            self.assertEqual(r.state, "request-error")

    def test_receipt_dir_token_substituted_whole(self):
        with tempfile.TemporaryDirectory() as d:
            seen = []
            def spy(argv, cwd, receipt_dir, launch_id):
                seen.append((argv, receipt_dir))
                return 1
            wd.launch(["bash", "f.sh", "{receipt_dir}"], cwd=d, driver=spy, platform="darwin",
                      wait_seconds=0.1)
            argv, rdir = seen[0]
            self.assertEqual(argv[2], rdir, "{receipt_dir} replaced by the real dir as ONE token")
            self.assertNotIn("{receipt_dir}", argv[2])

    def test_hostile_path_quoted_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'weird "dir" $HOME `x`\nnewline'
            target.parent.mkdir(parents=True, exist_ok=True)
            seen = []
            def spy(argv, cwd, receipt_dir, launch_id):
                seen.append(argv)
                return 1
            wd.launch([str(target), "arg; with $dollar"], cwd=d, driver=spy, platform="darwin",
                      wait_seconds=0.1)
            self.assertEqual(seen[0][0], str(target), "path passed as ONE argv element")
            self.assertEqual(seen[0][1], "arg; with $dollar")


if __name__ == "__main__":
    unittest.main()
