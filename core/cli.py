"""core.cli — the v2 CLI entry (bin/desktop-sync) wiring every core module.

Contract (frozen in docs/PACKAGE-MAP.md): bare invocation on a TTY opens the
numbered menu; bare with no TTY exits 2 with a useful message (no hang); any
subcommand/flag/--help/--json is direct CLI and NEVER opens the menu. Exit
mapping (core/exitcodes): 0 ok · 1 failure · 2 invalid · 3 prereq · 4
conflict/stale · 5 partial/pending. JSON and human output agree on
scope/verdicts/status.

Read-only subcommands (status/doctor/plan/menu-spec) write nothing. Effectful
ones (build/apply) write only under explicitly given output/state roots.
claude_home resolves from HOME (sandboxed in tests).

Stdlib only; Python 3.8 compatible.
"""

import json
import os
import sys
from pathlib import Path

from . import builder, coexistence, distribution, exitcodes, inventory
from . import lifecycle, mcp, menu as menu_mod, planner, records, state as state_mod

USAGE = """desktop-sync — claude-code-desktop-sync v2 CLI

Usage:
  desktop-sync                 bare on a TTY opens the numbered menu (no-TTY: exit 2)
  desktop-sync <command> [flags]

Commands (never open the menu):
  status [--json]            read-only inventory + state summary
  doctor [--json]            read-only environment/config checks
  plan [--json]              pure plan preview (writes nothing)
  build --source DIR --out DIR   build a portable artifact (writes under --out only)
  apply --plan FILE [--yes]  apply a plan via lifecycle (requires --yes)
  verify [--json]            artifact readiness + active-version checks
  recover [--json]           inspect-first recovery report
  disable                    refuse overlay activation without explicit approval
  records export|import NAME [--file F]
  mcp [--preview]            legacy-MCP sync (env-overridden configs in tests)
  menu [--plain]             the dependency-free numbered menu (TTY)
  menu-spec                  pure menu-spec v1 JSON on stdout
  --help                     this text

Environment variables read: HOME (claude home), CLAUDE_CODE_CONFIG /
CLAUDE_DESKTOP_CONFIG / CLAUDE_SYNC_HOME (legacy engine overrides, used by the
mcp subcommand). Exit codes: 0 ok, 1 failure, 2 invalid, 3 prereq, 4
conflict/stale, 5 partial/pending.
"""


def _claude_home(env):
    return Path(env.get("HOME", str(Path.home()))) / ".claude"


def _emit(args, human, doc, out):
    if "--json" in args:
        out.write(json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        out.write(human + "\n")


def cmd_status(args, env, out, err, stdin=None):
    home = _claude_home(env)
    inv = inventory.Inventory({"claude_home": home})
    recs = inv.collect()
    # explicit dict build — no PEP 584 `|` (must run on Python 3.8; review H2)
    doc = {"status": "ok",
           "records": [{"name": r.name, "source": r.source, "kind": r.kind,
                        "version": r.installed_version or r.declared_version,
                        "loaded_observable": r.loaded_observable, "notes": r.notes}
                       for r in recs],
           "notes": inv.notes}
    human = "status: ok — %d item(s)" % len(recs) + ("" if not inv.notes else "  notes: " + ", ".join(inv.notes))
    for r in recs:
        human += "\n  %s (%s, %s)" % (r.name, r.source, r.kind)
    _emit(args, human, doc, out)
    return exitcodes.EXIT_OK


def cmd_doctor(args, env, out, err, stdin=None):
    home = _claude_home(env)
    checks = {
        "python": sys.version.split()[0],
        "claude_home": str(home),
        "claude_json_present": (home / ".claude.json").is_file(),
        "plugins_dir_present": (home / "plugins").is_dir(),
        "synced_dir_present": (home / "plugins" / "synced").is_dir(),
    }
    doc = {"status": "ok", "checks": checks}
    _emit(args, "doctor: " + json.dumps(checks, sort_keys=True), doc, out)
    return exitcodes.EXIT_OK


def cmd_plan(args, env, out, err, stdin=None):
    home = _claude_home(env)
    inv = inventory.Inventory({"claude_home": home})
    recs = inv.collect()
    inputs = {"sources": [{"name": r.name, "revision": r.installed_version or r.declared_version or "?",
                           "kind": r.kind} for r in recs],
              "target": {"surface": "code"}}
    p = planner.build_plan(inputs, baseline=None, first_use=state_mod.FRESH)
    doc = p.to_dict()
    _emit(args, p.to_human(), doc, out)
    return p.exit_code


def cmd_build(args, env, out, err, stdin=None):
    src = _flag(args, "--source")
    outdir = _flag(args, "--out") or "dist/artifact"
    if not src:
        err.write("build: --source is required\n")
        return exitcodes.EXIT_INVALID
    try:
        art = builder.build(Path(src), Path(outdir))
    except builder.BuilderError as e:
        err.write("build refused: %s\n" % e)
        return exitcodes.EXIT_PREREQ
    doc = {"status": "ok", "files": len(art["files"]), "out": outdir}
    _emit(args, "built %d files into %s" % (len(art["files"]), outdir), doc, out)
    return exitcodes.EXIT_OK


def cmd_apply(args, env, out, err, stdin=None):
    plan_file = _flag(args, "--plan")
    if not plan_file:
        err.write("apply: --plan is required\n")
        return exitcodes.EXIT_INVALID
    if "--yes" not in args:
        err.write("apply: refused — confirmation defaults to No; pass --yes\n")
        return exitcodes.EXIT_CONFLICT_STALE
    pdoc = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    import base64
    from .planner import Plan, Action
    actions = []
    for a in pdoc["actions"]:
        act = Action(a["action_id"], a["kind"], a["destination"], a["revision"], a["side_effects"])
        act.content = base64.b64decode(a["content_b64"]) if a.get("content_b64") else None
        act.base_digest = a.get("base_sha256")
        actions.append(act)
    plan = Plan(pdoc["plan_id"], pdoc["inputs_revision"], actions, pdoc["status"],
                pdoc.get("exit_code", 0), pdoc.get("notes", ""))
    home = _claude_home(env)
    # host-management boundary (H3): no write without a verified grant
    from . import hostguard
    kind, ev = hostguard.detect_context(env, sys.platform, home, _isatty(stdin))
    try:
        hostguard.HostManager.for_context(kind, ev, root=home)
    except hostguard.HostGuardError as e:
        err.write("apply refused: %s\n" % e)
        return exitcodes.EXIT_PREREQ
    try:
        r = lifecycle.apply_plan(plan, home, home / "desktop-sync-state")
    except lifecycle.ConflictError as e:
        err.write("conflict: %s\n" % e)
        return exitcodes.EXIT_CONFLICT_STALE
    except lifecycle.LifecycleError as e:
        err.write("failed: %s\n" % e)
        return exitcodes.EXIT_FAILURE
    doc = {"status": r.status, "written": r.written, "idempotent": r.idempotent}
    _emit(args, "apply: %s (written: %s)" % (r.status, ", ".join(r.written) or "none"), doc, out)
    return exitcodes.EXIT_OK


def cmd_verify(args, env, out, err, stdin=None):
    zips = sorted((Path("dist")).glob("*-portable.zip")) if Path("dist").is_dir() else []
    results = {}
    for z in zips:
        ok, problems = distribution.check_artifact_ready(z)
        results[z.name] = {"ready": ok, "problems": problems}
    steps = distribution.pending_steps(zips[0] if zips else "dist/<name>-portable.zip")
    doc = {"status": "ok" if all(v["ready"] for v in results.values()) or not zips else "partial",
           "artifacts": results, "pending_live_steps": steps}
    _emit(args, "verify: " + json.dumps({k: v["ready"] for k, v in results.items()}) +
          " — %d live steps pending (claude.ai, user-run)" % len(steps), doc, out)
    return exitcodes.EXIT_OK if not zips or all(v["ready"] for v in results.values()) else exitcodes.EXIT_PARTIAL


def cmd_recover(args, env, out, err, stdin=None):
    home = _claude_home(env)
    rep = lifecycle.recover(home / "desktop-sync-state")
    doc = {"status": "ok", "partial": rep["partial"], "note": rep["note"]}
    _emit(args, "recover: %d partial entr(ies) — inspect-first, nothing overwritten"
          % len(rep["partial"]), doc, out)
    return exitcodes.EXIT_OK


def cmd_disable(args, env, out, err, stdin=None):
    v = coexistence.coexistence_verdict("claude-code-desktop-sync", "claude-code-desktop-sync-copy")
    try:
        coexistence.may_activate(v, explicit_approval=("--yes" in args))
    except coexistence.CoexistenceError as e:
        err.write("disable/activate refused: %s\n" % e)
        return exitcodes.EXIT_PREREQ
    _emit(args, "coexistence check passed", {"status": "ok"}, out)
    return exitcodes.EXIT_OK


def cmd_records(args, env, out, err, stdin=None):
    home = _claude_home(env)
    store = records.RecordStore(home / "desktop-sync-state" / "data", allowlist=("waypoints",))
    op = args[0] if args else ""
    name = _flag(args, "--name") or (args[1] if len(args) > 1 else None)
    if op not in ("export", "import") or not name:
        err.write("records: usage: records export|import --name NAME [--file F]\n")
        return exitcodes.EXIT_INVALID
    f = _flag(args, "--file") or (home / "desktop-sync-state" / (name + ".export.json"))
    if op == "export":
        meta = store.export_record(name, f)
        doc = {"status": "ok", "exported": meta}
        _emit(args, "exported %s -> %s (rev %s)" % (name, f, meta["revision"][:8]), doc, out)
        return exitcodes.EXIT_OK
    r = store.import_record(name, f, expected_prior=None)
    _emit(args, "import %s: %s" % (name, r["status"]), {"status": r["status"]}, out)
    return exitcodes.EXIT_OK if r["status"] == "applied" else exitcodes.EXIT_PARTIAL


def cmd_mcp(args, env, out, err, stdin=None):
    code = Path(env.get("CLAUDE_CODE_CONFIG") or (Path(env.get("HOME", str(Path.home()))) / ".claude.json"))
    desk = Path(env.get("CLAUDE_DESKTOP_CONFIG") or "")
    sync_home = Path(env.get("CLAUDE_SYNC_HOME") or (Path(env.get("HOME", str(Path.home()))) / ".claude" / "desktop-sync"))
    if not desk or not desk.is_file() or not code.is_file():
        err.write("mcp: both configs must exist (set CLAUDE_CODE_CONFIG / CLAUDE_DESKTOP_CONFIG)\n")
        return exitcodes.EXIT_PREREQ
    if "--preview" not in args:
        # host-management boundary (H3): a real sync writes host config — needs a
        # verified code-host grant, never a cloud/unknown context
        from . import hostguard
        home = _claude_home(env)
        kind, ev = hostguard.detect_context(env, sys.platform, home, _isatty(stdin))
        try:
            hostguard.HostManager.for_context(kind, ev, root=home)
        except hostguard.HostGuardError as e:
            err.write("mcp refused: %s\n" % e)
            return exitcodes.EXIT_PREREQ
    st = mcp.SyncState(sync_home / "last-sync-state-v2.json",
                       backup_dir=sync_home / "backups", journal=sync_home / "journal.jsonl")
    if "--preview" in args:
        out.write("mcp preview: would run sync between %s and %s (no writes in preview)\n" % (code, desk))
        return exitcodes.EXIT_OK
    o = st.run(code, desk)
    doc = {"status": "ok", "changed": o.changed, "conflicts": o.conflicts,
           "type_conflicts": o.type_conflicts, "notes": o.notes}
    _emit(args, "mcp: changed=%s conflicts=%s" % (o.changed, o.conflicts), doc, out)
    return exitcodes.EXIT_OK


_COMMANDS = {
    "status": cmd_status, "doctor": cmd_doctor, "plan": cmd_plan, "build": cmd_build,
    "apply": cmd_apply, "verify": cmd_verify, "recover": cmd_recover, "disable": cmd_disable,
    "records": cmd_records, "mcp": cmd_mcp,
}


def _isatty(stream):
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:
        return False


def _flag(args, name):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def main(argv, stdin=None, stdout=None, stderr=None, env=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = env or dict(os.environ)
    # Windows consoles default to cp1252 and crash on emoji (UnicodeEncodeError,
    # CI run 43359234140) — reconfigure to utf-8 with replacement, never crash
    for stream in (stdout, stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    argv = list(argv)
    if "--help" in argv or "-h" in argv or argv == ["help"]:
        stdout.write(USAGE)
        return exitcodes.EXIT_OK
    if not argv:
        if stdin.isatty():
            return cmd_menu(["--plain"], env, stdout, stderr, stdin=stdin)
        stderr.write("desktop-sync: the menu is interactive and needs a terminal. "
                     "Try: desktop-sync status | doctor | plan | build | apply | verify "
                     "| recover | menu-spec | --help\n")
        return exitcodes.EXIT_INVALID
    if argv[0] == "menu-spec":
        spec = menu_mod.build_spec()
        errs = menu_mod.validate_spec(spec)
        if errs:
            stderr.write("menu-spec invalid: %s\n" % errs)
            return exitcodes.EXIT_FAILURE
        # ensure_ascii=True: valid JSON that decodes identically and cannot
        # crash a non-UTF-8 consumer pipe
        stdout.write(json.dumps(spec, indent=2, ensure_ascii=True) + "\n")
        return exitcodes.EXIT_OK
    if argv[0] == "menu":
        return cmd_menu(argv[1:], env, stdout, stderr, stdin=stdin)
    if argv[0] == "exit":
        return exitcodes.EXIT_OK
    cmd = _COMMANDS.get(argv[0])
    if cmd is None:
        stderr.write("desktop-sync: unknown command %r\n%s" % (argv[0], USAGE))
        return exitcodes.EXIT_INVALID
    try:
        return cmd(argv[1:], env, stdout, stderr, stdin)
    except Exception as e:
        stderr.write("desktop-sync: %s: %s\n" % (type(e).__name__, e))
        return exitcodes.EXIT_FAILURE


def cmd_menu(args, env, stdout, stderr, stdin=None):
    spec = menu_mod.build_spec()
    ran = []

    # 2.0.1 (user report 2026-11-13): run_plain_menu calls print_fn like print() — one line per
    # call, no "\n" — and the menu then blocks in readline(). Passing bare stdout.write left the
    # whole menu unflushed in the terminal's line buffer: the window looked dead, and on input it
    # dumped as one run-on line. Every menu line ends with a newline and is flushed, and every
    # read flushes first, so nothing the user must see can sit buffered while we wait for them.
    def say(text):
        stdout.write(text + "\n")
        stdout.flush()

    def readline():
        stdout.flush()
        return stdin.readline()

    def run_action(action):
        # M2 (review): the menu MUST ask the spec's prompts before running —
        # placeholders never pass through literally into an effectful command
        answers = {}
        for pr in action.get("prompts", []):
            if pr.get("type") == "choice":
                choices = pr.get("choices") or []
                for i, c in enumerate(choices, 1):
                    stdout.write("    %d) %s\n" % (i, c.get("label", c.get("value"))))
                default = pr.get("default")
                ans = None
                while ans is None:
                    line = readline()
                    if line == "":
                        raise EOFError
                    line = line.strip()
                    if line == "" and default is not None:
                        ans = default
                    elif line.isdigit() and 1 <= int(line) <= len(choices):
                        ans = choices[int(line) - 1]["value"]
                    else:
                        stdout.write("    invalid choice\n")
                answers[pr["id"]] = ans
            elif pr.get("type") == "confirm":
                default = pr.get("default") or ""
                line = readline()
                if line == "":
                    raise EOFError
                ans = line.strip().lower()
                answers[pr["id"]] = default if ans == "" else ("--yes" if ans in ("y", "yes") else "--no")
            else:
                stdout.write("  %s " % pr.get("question", pr["id"]))
                line = readline()
                if line == "":
                    raise EOFError
                answers[pr["id"]] = line.rstrip("\n") or (pr.get("default") or "")
        argv = menu_mod.build_argv(action, answers)
        rc = main([a for a in argv[1:] if a], stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        ran.append((action["label"], rc))

    def input_fn():
        line = readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    menu_mod.run_plain_menu(spec, input_fn, say, run_action)
    return exitcodes.EXIT_OK
