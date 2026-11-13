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


# Env vars that would leak the real machine into the child engine: home/profile
# (Path.home, config fallbacks), PATH (shutil.which), and platform config roots.
# Windows stores some of these in mixed case ("Path", "UserProfile"), so the
# filter compares case-insensitively — see tests/overhaul for the regression test.
_LEAKY_ENV = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATH",
              "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA", "PATHEXT", "ProgramFiles",
              "CLAUDE_CODE_CONFIG", "CLAUDE_DESKTOP_CONFIG", "CLAUDE_SYNC_HOME",
              "PYTHONDONTWRITEBYTECODE")
_LEAKY_ENV_UPPER = frozenset(k.upper() for k in _LEAKY_ENV)


class Sandbox:
    def __init__(self, tmp):
        self.code = Path(tmp) / "code.json"
        self.desk = Path(tmp) / "desktop.json"
        self.env = {
            "HOME": str(Path(tmp) / "home"),
            "USERPROFILE": str(Path(tmp) / "home"),  # ntpath.expanduser prefers USERPROFILE on Windows
            "PATH": os.pathsep.join([str(Path(tmp) / "bin"), "/usr/bin", "/bin"]),
            "CLAUDE_CODE_CONFIG": str(self.code),
            "CLAUDE_DESKTOP_CONFIG": str(self.desk),
            "CLAUDE_SYNC_HOME": str(Path(tmp) / "sync"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run(self, args=()):
        # Start from the real environment MINUS every leaky key, then overlay the
        # sandbox: the engine must never see the real HOME/PATH/config overrides.
        env = {k: v for k, v in os.environ.items() if k.upper() not in _LEAKY_ENV_UPPER}
        env.update(self.env)
        r = subprocess.run([sys.executable, str(ENGINE)] + list(args), capture_output=True,
                           encoding="utf-8", errors="replace", env=env)
        self.rc = r.returncode
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


def test_home_isolation(sb):
    print("\n[HOME isolation: the engine's skills snapshot must come from the sandbox HOME only]")
    tmp = Path(sb.env["CLAUDE_SYNC_HOME"]).parent
    fake_home = tmp / "home"
    (fake_home / ".claude" / "skills" / "sentinel-skill").mkdir(parents=True, exist_ok=True)
    write(sb.code, {"mcpServers": {}})
    write(sb.desk, {"mcpServers": {}})
    sb.run()
    state = read(Path(sb.env["CLAUDE_SYNC_HOME"]) / "last-sync-state.json")
    check(state.get("skills") == ["sentinel-skill"],
          "home-isolation: snapshot skills == fake-HOME skills only (real HOME not read)")


def test_path_isolation(sb):
    print("\n[PATH isolation: a launcher must resolve via the sandbox PATH, not the real one]")
    tmp = Path(sb.env["CLAUDE_SYNC_HOME"]).parent
    fakebin = tmp / "bin"
    fakebin.mkdir(exist_ok=True)
    # On Windows, shutil.which with X_OK only matches names whose extension is in
    # PATHEXT (cmd.exe semantics — shutil.py win32 branch), so an extensionless
    # file is never "executable" there (CI run 43322361462, windows-3.12).
    launcher = "faketool.exe" if os.name == "nt" else "faketool"
    tool = fakebin / launcher
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    write(sb.code, {"mcpServers": {"t": {"command": launcher, "args": []}}})
    write(sb.desk, {"mcpServers": {}})
    sb.run()
    check(read(sb.desk)["mcpServers"]["t"]["command"] == str(tool),
          "path-isolation: %r resolved to the sandbox bin dir" % launcher)


def test_help_flag_no_side_effects(sb):
    print("\n[--help prints usage, exits 0, and writes NOTHING (waypoint cds-legacy-engine-ignores-help)]")
    write(sb.code, {"mcpServers": {"fs": {"command": "/usr/bin/true"}}})
    write(sb.desk, {"mcpServers": {}})
    def snap():
        out = {}
        for p in sorted(Path(sb.env["CLAUDE_SYNC_HOME"]).parent.rglob("*")):
            if p.is_file():
                out[str(p)] = p.read_bytes()
        return out
    before = snap()
    out = sb.run(["--help"])
    check(sb.rc == 0, "help: exit 0 (got %d)" % sb.rc)
    check("usage" in out.lower(), "help: prints usage")
    check(snap() == before, "help: zero filesystem writes (no state, no lock, no config)")
    check("fs" not in read(sb.desk)["mcpServers"], "help: did NOT run a sync")


def test_unknown_flag_exits_2_no_side_effects(sb):
    print("\n[unknown flag exits 2 with usage on stderr, writes NOTHING]")
    write(sb.code, {"mcpServers": {}})
    write(sb.desk, {"mcpServers": {}})
    syncdir = Path(sb.env["CLAUDE_SYNC_HOME"])
    out = sb.run(["--bogus"])
    check(sb.rc == 2, "bogus: exit 2 (got %d)" % sb.rc)
    check("usage" in out.lower(), "bogus: usage on stderr")
    check(not syncdir.exists(), "bogus: no sync state written")


def test_installers_help_and_bad_flag_no_writes(sb):
    print("\n[install.py / uninstall.py: --help exits 0 with zero writes; unknown flag exits 2]")
    repo = REPO
    for script in ("install.py", "uninstall.py"):
        for args, want_rc in ((["--help"], 0), (["--bogus"], 2)):
            env = dict(sb.env)
            before = set(Path(env["HOME"]).rglob("*")) if Path(env["HOME"]).exists() else set()
            r = subprocess.run([sys.executable, str(repo / script)] + args, capture_output=True,
                               encoding="utf-8", errors="replace", env=env)
            check(r.returncode == want_rc, "%s %s: exit %d (got %d)" % (script, args[0], want_rc, r.returncode))
            check("usage" in (r.stdout + r.stderr).lower(), "%s %s: prints usage" % (script, args[0]))
            after = set(Path(env["HOME"]).rglob("*")) if Path(env["HOME"]).exists() else set()
            check(after == before, "%s %s: zero writes under HOME" % (script, args[0]))


def test_hook_route_still_no_arg():
    print("\n[SessionStart hook route unchanged: bare no-arg engine (hook contract)]")
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    args = hooks["hooks"]["SessionStart"][0]["hooks"][0].get("args", [])
    check(args == ["${CLAUDE_PLUGIN_ROOT}/bin/sync_code_desktop.py"],
          "hook still invokes the engine with no args, got %r" % args)


def main():
    for fn in (test_push_pull_remove, test_first_run_clash, test_two_sided_conflict,
               test_command_change_detected, test_remote_in_desktop, test_malformed_mcpservers,
               test_same_relative_launcher, test_type_conflict_preserves_connector, test_bom_tolerant,
               test_home_isolation, test_path_isolation):
        scenario(fn)
    for fn in (test_help_flag_no_side_effects, test_unknown_flag_exits_2_no_side_effects,
               test_installers_help_and_bad_flag_no_writes):
        scenario(fn)
    test_hook_route_still_no_arg()
    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
