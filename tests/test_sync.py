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


# ---------- multi-target discovery (2.0.3: standard + 3P Desktop) ----------

def discovered_targets(sb):
    """The two Desktop config paths the engine must discover under the sandbox (no override).
    Mirrors docs claude.com/docs/third-party/claude-desktop/data-storage.md (app-data table)."""
    tmp = Path(sb.env["CLAUDE_SYNC_HOME"]).parent
    home = tmp / "home"
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        return base / "Claude" / "claude_desktop_config.json", base / "Claude-3p" / "claude_desktop_config.json"
    if os.name == "nt":
        return (tmp / "appdata" / "Claude" / "claude_desktop_config.json",
                tmp / "localappdata" / "Claude-3p" / "claude_desktop_config.json")
    base = home / ".config"
    return base / "Claude" / "claude_desktop_config.json", base / "Claude-3p" / "claude_desktop_config.json"


def discovery_mode(sb):
    """Drop the single-file override so the engine discovers targets itself."""
    tmp = Path(sb.env["CLAUDE_SYNC_HOME"]).parent
    del sb.env["CLAUDE_DESKTOP_CONFIG"]
    sb.env["APPDATA"] = str(tmp / "appdata")
    sb.env["LOCALAPPDATA"] = str(tmp / "localappdata")
    std, threep = discovered_targets(sb)
    std.parent.mkdir(parents=True, exist_ok=True)
    return std, threep


def test_3p_first_sync_is_additive_never_deletes_code(sb):
    print("\n[3P target appearing next to a legacy snapshot: purely additive, Code config untouched]")
    std, threep = discovery_mode(sb)
    write(sb.code, {"mcpServers": {
        "fs": {"command": "/usr/bin/true", "args": ["-x"]},
        "gh": {"type": "http", "url": "https://ex/mcp", "headers": {"Authorization": "Bearer t"}},
    }})
    write(std, {"mcpServers": {}})
    sb.run()                                      # legacy-style baseline against the standard target
    check("fs" in read(std)["mcpServers"], "baseline: standard Desktop target got fs")
    code_before = sb.code.read_bytes()
    threep.parent.mkdir(parents=True, exist_ok=True)
    write(threep, {"preferences": {"keep": True}, "mcpServers": {}})   # empty 3P config, as on a real 3P Mac
    out = sb.run()
    check(sb.code.read_bytes() == code_before,
          "hazard: ~/.claude.json byte-identical after first 3P sync (no 'deleted in Desktop' false positive)")
    check("fs" in read(threep)["mcpServers"], "3P target received Code's stdio server")
    check("gh" not in read(threep)["mcpServers"], "3P target: remote server NOT written")
    check(read(threep).get("preferences", {}).get("keep") is True, "3P target: unrelated keys preserved")
    check("3P" in out, "output names the 3P target (got %r)" % out[:200])


def test_delete_in_code_reaches_both_targets(sb):
    print("\n[two targets: a deletion in Code is removed from BOTH Desktop configs]")
    std, threep = discovery_mode(sb)
    threep.parent.mkdir(parents=True, exist_ok=True)
    write(sb.code, {"mcpServers": {"fs": {"command": "/usr/bin/true"}, "keep": {"command": "/bin/sh"}}})
    write(std, {"mcpServers": {}})
    write(threep, {"mcpServers": {}})
    sb.run()
    check("fs" in read(std)["mcpServers"] and "fs" in read(threep)["mcpServers"], "setup: fs on both targets")
    code = read(sb.code)
    del code["mcpServers"]["fs"]
    write(sb.code, code)
    sb.run()
    check("fs" not in read(std)["mcpServers"], "delete: gone from standard target")
    check("fs" not in read(threep)["mcpServers"], "delete: gone from 3P target")
    check("keep" in read(std)["mcpServers"] and "keep" in read(threep)["mcpServers"], "delete: others kept")
    check(sb.run() == "", "converged: next run is silent")


def test_desktop_add_converges_across_targets(sb):
    print("\n[a server added in the 3P Desktop reaches Code, then the standard target, within 2 runs]")
    std, threep = discovery_mode(sb)
    threep.parent.mkdir(parents=True, exist_ok=True)
    write(sb.code, {"mcpServers": {}})
    write(std, {"mcpServers": {}})
    write(threep, {"mcpServers": {"only3p": {"command": "/usr/bin/true"}}})
    sb.run()
    check("only3p" in read(sb.code)["mcpServers"], "pull: 3P-added server reached Code in run 1")
    sb.run()
    check("only3p" in read(std)["mcpServers"], "converge: reached the standard target by run 2")
    check(sb.run() == "", "converged: run 3 is silent")


def test_override_is_single_target(sb):
    print("\n[CLAUDE_DESKTOP_CONFIG override = exactly one target; discovered paths untouched]")
    tmp = Path(sb.env["CLAUDE_SYNC_HOME"]).parent
    sb.env["APPDATA"] = str(tmp / "appdata")
    sb.env["LOCALAPPDATA"] = str(tmp / "localappdata")
    std, threep = discovered_targets(sb)
    for p in (std, threep):
        p.parent.mkdir(parents=True, exist_ok=True)
        write(p, {"mcpServers": {}})
    before = {p: p.read_bytes() for p in (std, threep)}
    write(sb.code, {"mcpServers": {"fs": {"command": "/usr/bin/true"}}})
    write(sb.desk, {"mcpServers": {}})
    sb.run()
    check("fs" in read(sb.desk)["mcpServers"], "override target synced")
    check(all(p.read_bytes() == b for p, b in before.items()), "discovered targets untouched under override")


def test_per_target_state_files(sb):
    print("\n[state: standard target keeps last-sync-state.json (no migration); 3P has its own]")
    std, threep = discovery_mode(sb)
    threep.parent.mkdir(parents=True, exist_ok=True)
    write(sb.code, {"mcpServers": {"fs": {"command": "/usr/bin/true"}}})
    write(std, {"mcpServers": {}})
    write(threep, {"mcpServers": {}})
    sb.run()
    sync = Path(sb.env["CLAUDE_SYNC_HOME"])
    legacy_state, threep_state = sync / "last-sync-state.json", sync / "last-sync-state-3p.json"
    check(legacy_state.exists() and "fs" in read(legacy_state).get("servers", {}),
          "standard target state = legacy file")
    check(threep_state.exists() and "fs" in read(threep_state).get("servers", {}),
          "3P target state = its own file (existence checked before reading)")


def main():
    for fn in (test_3p_first_sync_is_additive_never_deletes_code, test_delete_in_code_reaches_both_targets,
               test_desktop_add_converges_across_targets, test_override_is_single_target,
               test_per_target_state_files):
        scenario(fn)
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
