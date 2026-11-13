"""Phase 11-A contract tests: the v2 CLI (bin/desktop-sync) and the mandatory
dependency-free numbered menu + pure menu-spec v1 (get-haiggoh interop at
0227eb5). Rules: subcommands/help/JSON never open the menu; bare no-TTY exits 2
without hanging; blank Enter defaults to read-only preview; confirmation
defaults to No; empty bulk selection is not all; menu-spec validates per the
family contract; effectful actions keep preview/freshness checks in the core.

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
from core import menu as menu_mod

REPO = Path(__file__).resolve().parent.parent.parent
ENTRY = REPO / "bin" / "desktop-sync"


def run_cli(args, root, stdin_text=None):
    env = harness.build_env(root)
    r = subprocess.run([sys.executable, str(ENTRY)] + args, capture_output=True, text=True,
                       input=stdin_text, env={k: v for k, v in env.items()}, cwd=str(root))
    return r


class CLITests(unittest.TestCase):
    def test_help_runs_no_menu_no_writes(self):
        with tempfile.TemporaryDirectory() as d:
            before = harness.tree_snapshot(d)
            r = run_cli(["--help"], d)
            self.assertEqual(r.returncode, 0)
            self.assertIn("menu-spec", r.stdout)
            self.assertIn("status", r.stdout)
            self.assertEqual(harness.changed_files(d, before), set())

    def test_unknown_flag_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli(["--bogus"], d)
            self.assertEqual(r.returncode, 2)

    def test_bare_notty_exits_2_without_hang(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_cli([], d)  # stdin is a pipe, not a TTY
            self.assertEqual(r.returncode, 2)
            self.assertIn("terminal", (r.stderr + r.stdout).lower())

    def test_subcommand_json_agrees_with_human(self):
        with tempfile.TemporaryDirectory() as d:
            rj = run_cli(["status", "--json"], d)
            rh = run_cli(["status"], d)
            self.assertEqual(rj.returncode, 0)
            self.assertEqual(rh.returncode, 0)
            doc = json.loads(rj.stdout)
            self.assertIn("status", doc)
            self.assertIn("status", rh.stdout.lower())

    def test_menu_spec_is_pure_valid_v1(self):
        with tempfile.TemporaryDirectory() as d:
            before = harness.tree_snapshot(d)
            r = run_cli(["menu-spec"], d)
            self.assertEqual(r.returncode, 0)
            spec = json.loads(r.stdout)
            self.assertEqual(spec["menu_spec"], 1)
            self.assertTrue(spec["title"])
            self.assertTrue(spec["actions"])
            self.assertEqual(menu_mod.validate_spec(spec), [], menu_mod.validate_spec(spec))
            self.assertEqual(harness.changed_files(d, before), set(), "menu-spec must be pure")
            labels = [a["label"] for a in spec["actions"]]
            for required in ("Status", "Doctor", "Plan", "Build", "Apply", "Verify",
                             "Recover", "Disable", "Help", "Exit"):
                self.assertTrue(any(required.lower() in lab.lower() for lab in labels),
                                "missing menu action: %s" % required)


class MenuSpecValidatorTests(unittest.TestCase):
    def test_placeholder_without_prompt_rejected(self):
        spec = {"menu_spec": 1, "title": "t", "actions": [
            {"label": "x", "argv": ["tool", "{amount}"], "output": "panel", "prompts": []}]}
        errs = menu_mod.validate_spec(spec)
        self.assertTrue(any("placeholder" in e for e in errs))

    def test_prompt_without_placeholder_rejected(self):
        spec = {"menu_spec": 1, "title": "t", "actions": [
            {"label": "x", "argv": ["tool"], "output": "panel",
             "prompts": [{"id": "a", "question": "q", "type": "text", "default": None, "pattern": None}]}]}
        errs = menu_mod.validate_spec(spec)
        self.assertTrue(any("not referenced" in e for e in errs))

    def test_choice_requires_choices(self):
        spec = {"menu_spec": 1, "title": "t", "actions": [
            {"label": "x", "argv": ["tool", "{c}"], "output": "panel",
             "prompts": [{"id": "c", "question": "q", "type": "choice"}]}]}
        errs = menu_mod.validate_spec(spec)
        self.assertTrue(any("choices" in e for e in errs))

    def test_bad_output_and_version_rejected(self):
        spec = {"menu_spec": 2, "title": "t", "actions": []}
        self.assertTrue(menu_mod.validate_spec(spec))
        spec2 = {"menu_spec": 1, "title": "t", "actions": [
            {"label": "x", "argv": ["tool"], "output": "bogus", "prompts": []}]}
        self.assertTrue(any("output" in e for e in menu_mod.validate_spec(spec2)))

    def test_argv_never_shell_joined(self):
        spec = {"menu_spec": 1, "title": "t", "actions": [
            {"label": "x", "argv": ["tool", "{a}"], "output": "panel",
             "prompts": [{"id": "a", "question": "q", "type": "text", "default": None, "pattern": None}]}]}
        out = menu_mod.build_argv(spec["actions"][0], {"a": "evil; rm -rf ~"})
        self.assertEqual(out, ["tool", "evil; rm -rf ~"],
                         "whole-token substitution keeps the answer ONE argv element")


class PlainMenuTests(unittest.TestCase):
    def test_blank_enter_is_preview_not_apply(self):
        with tempfile.TemporaryDirectory() as d:
            before = harness.tree_snapshot(d)
            # feed blank line then quit: must not apply anything
            r = run_cli(["menu", "--plain"], d, stdin_text="\nq\n")
            self.assertIn(r.returncode, (0, 2))
            self.assertEqual(harness.changed_files(d, before), set(),
                             "blank Enter + q must not mutate")

    def test_blank_enter_runs_first_action_only(self):
        spec = menu_mod.build_spec()
        seen = []
        lines = iter(["", "q"])
        def input_fn():
            try:
                return next(lines)
            except StopIteration:
                raise EOFError from None
        ran = menu_mod.run_plain_menu(spec, input_fn, lambda t: None, seen.append)
        self.assertEqual(ran, 1)
        self.assertEqual(seen, [spec["actions"][0]],
                         "blank Enter must run the read-only preview action, nothing else")

    def test_unknown_action_rejected_no_apply(self):
        with tempfile.TemporaryDirectory() as d:
            before = harness.tree_snapshot(d)
            r = run_cli(["menu", "--plain"], d, stdin_text="999\nq\n")
            self.assertEqual(harness.changed_files(d, before), set())
            self.assertIn("invalid", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main()
