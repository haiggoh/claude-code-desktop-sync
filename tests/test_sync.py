#!/usr/bin/env python3
"""
Self-contained functional test for the sync engine. No test framework required:

    python3 tests/test_sync.py

Runs the engine as a subprocess against throwaway config files (paths come from the environment at
import time). Covers: Code<->Desktop push/pull with launcher resolution, removal propagation,
remote-server handling on both sides, first-run same-name clash, two-sided conflict, genuine
command-change detection, no-churn on the relative->absolute rewrite, and malformed mcpServers.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENGINE = REPO / "bin" / "sync_code_desktop.py"

failures = 0


def check(cond, label):
    global failures
    print(("  ok  " if cond else " FAIL ") + label)
    if not cond:
        failures += 1


def write(p, obj):
    Path(p).write_text(json.dumps(obj, indent=2), encoding="utf-8")


def read(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


class Sandbox:
    def __init__(self, tmp):
        self.code = Path(tmp) / "code.json"
        self.desk = Path(tmp) / "desktop.json"
        self.env = {
            "CLAUDE_CODE_CONFIG": str(self.code),
            "CLAUDE_DESKTOP_CONFIG": str(self.desk),
            "CLAUDE_SYNC_HOME": str(Path(tmp) / "sync"),
        }

    def run(self):
        r = subprocess.run([sys.executable, str(ENGINE)], capture_output=True, text=True,
                           env=dict(os.environ, **self.env))
        return (r.stdout + r.stderr).strip()


def scenario(fn):
    with tempfile.TemporaryDirectory() as d:
        fn(Sandbox(d))


def test_push_pull_remove(sb):
    print("\n[sequence: push / idempotent / pull / remove]")
    write(sb.code, {"mcpServers": {
        "fs": {"command": "/usr/bin/true", "args": ["-x"], "env": {"X": "1"}},
        "gh": {"type": "http", "url": "https://ex/mcp", "headers": {"Authorization": "Bearer t"}},
    }})
    write(sb.desk, {"preferences": {"keep": True}, "mcpServers": {}})

    out = sb.run()
    desk = read(sb.desk)
    check("fs" in desk["mcpServers"], "push: stdio server added to Desktop")
    check("gh" not in desk["mcpServers"], "push: remote server NOT written to Desktop")
    check(read(sb.desk).get("preferences", {}).get("keep") is True, "push: unrelated Desktop keys preserved")
    check("Connectors" in out, "push: reports manual Connectors step for remote server")

    check(sb.run() == "", "second run is silent (idempotent)")

    desk = read(sb.desk)
    desk["mcpServers"]["newone"] = {"command": "/usr/bin/true", "args": []}
    write(sb.desk, desk)
    os.utime(sb.desk, None)
    sb.run()
    code = read(sb.code)
    check("newone" in code["mcpServers"], "pull: Desktop-added server pulled into Code")
    check("gh" in code["mcpServers"], "pull: remote server preserved in Code")

    code = read(sb.code)
    del code["mcpServers"]["fs"]
    write(sb.code, code)
    os.utime(sb.code, None)
    sb.run()
    check("fs" not in read(sb.desk)["mcpServers"], "remove: deletion in Code propagates to Desktop")


def test_first_run_clash(sb):
    print("\n[first-run same-name clash -> conflict, both untouched]")
    write(sb.code, {"mcpServers": {"fs": {"command": "/projects"}}})
    write(sb.desk, {"mcpServers": {"fs": {"command": "/work"}}})
    out = sb.run()
    check("CONFLICT" in out, "clash: reported as CONFLICT")
    check(read(sb.code)["mcpServers"]["fs"]["command"] == "/projects", "clash: Code side left untouched")
    check(read(sb.desk)["mcpServers"]["fs"]["command"] == "/work", "clash: Desktop side left untouched")


def test_two_sided_conflict(sb):
    print("\n[two-sided conflict after a baseline]")
    write(sb.code, {"mcpServers": {"s": {"command": "/a"}}})
    write(sb.desk, {"mcpServers": {"s": {"command": "/a"}}})
    sb.run()  # baseline
    check(sb.run() == "", "conflict: baseline is stable/silent")
    write(sb.code, {"mcpServers": {"s": {"command": "/code-edit"}}})
    write(sb.desk, {"mcpServers": {"s": {"command": "/desk-edit"}}})
    os.utime(sb.code, None)
    os.utime(sb.desk, None)
    out = sb.run()
    check("CONFLICT" in out, "conflict: both-sides edit reported as CONFLICT")
    check(read(sb.code)["mcpServers"]["s"]["command"] == "/code-edit", "conflict: Code edit preserved")
    check(read(sb.desk)["mcpServers"]["s"]["command"] == "/desk-edit", "conflict: Desktop edit preserved")


def test_command_change_detected(sb):
    print("\n[genuine command change is detected; no churn on relative->absolute]")
    # no-churn: a resolvable relative launcher on Code should sync once then stay silent
    write(sb.code, {"mcpServers": {"p": {"command": "python3", "args": ["-V"]}}})
    write(sb.desk, {"mcpServers": {}})
    sb.run()
    check(sb.run() == "", "no-churn: relative->absolute rewrite does not re-trigger every run")
    # genuine change: switch the command to a different absolute path -> must propagate
    write(sb.code, {"mcpServers": {"p": {"command": "/opt/custom/python3", "args": ["-V"]}}})
    os.utime(sb.code, None)
    sb.run()
    check(read(sb.desk)["mcpServers"]["p"]["command"] == "/opt/custom/python3",
          "change: command edit on Code propagated to Desktop")


def test_remote_in_desktop(sb):
    print("\n[remote server living in the Desktop file is left intact]")
    write(sb.code, {"mcpServers": {}})
    write(sb.desk, {"mcpServers": {
        "stdio1": {"command": "/usr/bin/true"},
        "weird": {"type": "http", "url": "https://ex"},
    }})
    sb.run()
    check("stdio1" in read(sb.code)["mcpServers"], "remote-in-desktop: stdio pulled to Code")
    check("weird" not in read(sb.code)["mcpServers"], "remote-in-desktop: Desktop remote NOT pulled to Code")
    check("weird" in read(sb.desk)["mcpServers"], "remote-in-desktop: Desktop remote left intact")


def test_malformed_mcpservers(sb):
    print("\n[non-object mcpServers is skipped gracefully, no crash]")
    write(sb.code, {"mcpServers": "disabled"})
    write(sb.desk, {"mcpServers": {}})
    out = sb.run()
    check("skipping" in out.lower(), "malformed: reports skip instead of crashing")
    check("Traceback" not in out, "malformed: no unhandled exception")


def test_same_relative_launcher(sb):
    print("\n[same RELATIVE launcher on both sides -> resolve, no false conflict (regression)]")
    write(sb.code, {"mcpServers": {"p": {"command": "python3", "args": ["-V"]}}})
    write(sb.desk, {"mcpServers": {"p": {"command": "python3", "args": ["-V"]}}})
    out1 = sb.run()
    check("CONFLICT" not in out1, "same-rel: NOT reported as a conflict")
    check(sb.run() == "", "same-rel: converges and is silent on the next run")


def test_type_conflict_preserves_connector(sb):
    print("\n[stdio-vs-remote same name -> held, connector + token preserved (regression)]")
    write(sb.code, {"mcpServers": {"github": {"command": "npx", "args": ["-y", "server-github"]}}})
    write(sb.desk, {"mcpServers": {"github": {"type": "http", "url": "https://ex/mcp",
                                              "headers": {"Authorization": "Bearer SECRET"}}}})
    out = sb.run()
    check("TYPE CONFLICT" in out, "type-conflict: reported")
    d = read(sb.desk)["mcpServers"]["github"]
    check(d.get("headers", {}).get("Authorization") == "Bearer SECRET",
          "type-conflict: Desktop connector + token NOT overwritten")
    check(read(sb.code)["mcpServers"]["github"].get("command") == "npx",
          "type-conflict: Code stdio entry left intact")


def test_bom_tolerant(sb):
    print("\n[UTF-8 BOM in a config does not defeat the sync (regression)]")
    Path(sb.code).write_text("﻿" + json.dumps({"mcpServers": {"z": {"command": "/usr/bin/true"}}}),
                             encoding="utf-8")
    write(sb.desk, {"mcpServers": {}})
    out = sb.run()
    check("skipped" not in out and "Traceback" not in out, "bom: no crash / no skip")
    check("z" in read(sb.desk)["mcpServers"], "bom: server still synced despite BOM")


def main():
    for fn in (test_push_pull_remove, test_first_run_clash, test_two_sided_conflict,
               test_command_change_detected, test_remote_in_desktop, test_malformed_mcpservers,
               test_same_relative_launcher, test_type_conflict_preserves_connector, test_bom_tolerant):
        scenario(fn)
    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
