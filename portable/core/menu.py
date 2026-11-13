"""core.menu — the mandatory dependency-free numbered menu + pure menu-spec v1
(plan §6.1, §6.5; family contract = get-haiggoh docs/menu-spec.md v1 as observed
at 0227eb5).

Rules enforced here:
  - menu-spec is PURE data on stdout, exit 0; validate_spec implements the
    consumer-side rules (menu_spec version, title, actions; per action
    label/argv/output; prompts id/question/type; choice requires choices;
    every {placeholder} needs a prompt and vice versa)
  - argv is NEVER shell-joined: build_argv substitutes WHOLE tokens only, so an
    answer with ';'/spaces stays one argv element
  - blank Enter defaults to the read-only preview action; confirmation defaults
    to No; empty bulk selection is not all
  - the menu itself never mutates: it dispatches to the same CLI/core handlers

Stdlib only; Python 3.8 compatible.
"""

import re

MENU_SPEC_VERSION = 1
_PROMPT_TYPES = ("choice", "text", "confirm")
_OUTPUT_MODES = ("panel", "terminal")
_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")

TITLE = "🖇️ claude-code-desktop-sync"


def validate_spec(spec):
    """Error list for a menu-spec dict (empty = valid) — mirrors
    get_haiggoh_tui.menuspec.validate at 0227eb5."""
    errors = []
    if not isinstance(spec, dict):
        return ["menu-spec is not a JSON object"]
    if spec.get("menu_spec") != MENU_SPEC_VERSION:
        return ["unknown menu_spec version %r (this build understands %d)"
                % (spec.get("menu_spec"), MENU_SPEC_VERSION)]
    if not isinstance(spec.get("title"), str) or not spec.get("title"):
        errors.append("title must be a non-empty string")
    actions = spec.get("actions")
    if not isinstance(actions, list):
        return errors + ["actions must be a list"]
    for i, action in enumerate(actions):
        where = "action[%d]" % i
        if not isinstance(action, dict):
            errors.append("%s: not an object" % where)
            continue
        if not isinstance(action.get("label"), str) or not action.get("label"):
            errors.append("%s: label must be a non-empty string" % where)
        argv = action.get("argv")
        if not isinstance(argv, list) or not all(isinstance(t, str) for t in argv):
            errors.append("%s: argv must be a list of strings" % where)
            argv = []
        if action.get("output") not in _OUTPUT_MODES:
            errors.append("%s: output must be one of %s" % (where, list(_OUTPUT_MODES)))
        prompts = action.get("prompts", [])
        if not isinstance(prompts, list):
            errors.append("%s: prompts must be a list" % where)
            prompts = []
        prompt_ids = set()
        for j, prompt in enumerate(prompts):
            pwhere = "%s.prompts[%d]" % (where, j)
            if not isinstance(prompt, dict):
                errors.append("%s: not an object" % pwhere)
                continue
            pid = prompt.get("id")
            if not isinstance(pid, str) or not pid:
                errors.append("%s: id must be a non-empty string" % pwhere)
                continue
            prompt_ids.add(pid)
            if not isinstance(prompt.get("question"), str):
                errors.append("%s: question must be a string" % pwhere)
            ptype = prompt.get("type")
            if ptype not in _PROMPT_TYPES:
                errors.append("%s: type must be one of %s" % (pwhere, list(_PROMPT_TYPES)))
            if ptype == "choice" and not isinstance(prompt.get("choices"), list):
                errors.append("%s: type 'choice' requires a choices list" % pwhere)
        placeholders = set()
        for tok in argv:
            m = _PLACEHOLDER_RE.match(tok)
            if m:
                placeholders.add(m.group(1))
        for ph in sorted(placeholders - prompt_ids):
            errors.append("%s: placeholder {%s} has no prompt with that id" % (where, ph))
        for pid in sorted(prompt_ids - placeholders):
            errors.append("%s: prompt %r is not referenced by any argv placeholder"
                          % (where, pid))
    return errors


def build_argv(action, answers):
    """Whole-token {placeholder} substitution; result is always a list — never
    shell-joined."""
    out = []
    for tok in action.get("argv", []):
        m = _PLACEHOLDER_RE.match(tok)
        out.append(answers.get(m.group(1), tok) if m else tok)
    return out


def build_spec():
    """This product's menu-spec v1 document (pure data)."""
    def act(label, argv, output="panel", prompts=None):
        return {"label": label, "argv": argv, "output": output, "prompts": prompts or []}
    return {
        "menu_spec": MENU_SPEC_VERSION,
        "title": TITLE,
        "actions": [
            act("📊 Status / coverage", ["desktop-sync", "status"]),
            act("🩺 Doctor (read-only checks)", ["desktop-sync", "doctor"]),
            act("🗺️ Plan (pure preview, no writes)", ["desktop-sync", "plan"]),
            act("📦 Build portable artifact for plugin…",
                ["desktop-sync", "build", "--source", "{plugin}", "--out", "{out}"],
                prompts=[
                    {"id": "plugin", "question": "Source plugin directory?", "type": "text",
                     "default": None, "pattern": None},
                    {"id": "out", "question": "Output directory?", "type": "text",
                     "default": "dist/artifact", "pattern": None},
                ]),
            act("✅ Apply plan… (requires confirmation)",
                ["desktop-sync", "apply", "{go}"],
                prompts=[{"id": "go", "question": "Apply the pending plan? This writes.",
                          "type": "confirm",
                          "choices": [{"value": "--yes", "label": "Yes, apply"},
                                      {"value": "--no", "label": "No (default)"}],
                          "default": "--no", "pattern": None}]),
            act("🔍 Verify active version / artifact", ["desktop-sync", "verify"]),
            act("🧯 Recover (inspect-first)", ["desktop-sync", "recover"]),
            act("🧹 Disable / remove (owned only)", ["desktop-sync", "disable"]),
            act("📤 Records: export / import…",
                ["desktop-sync", "records", "{op}", "{name}"],
                prompts=[
                    {"id": "op", "question": "Operation?", "type": "choice",
                     "choices": [{"value": "export", "label": "export"},
                                 {"value": "import", "label": "import"}],
                     "default": "export", "pattern": None},
                    {"id": "name", "question": "Record name (allowlisted)?", "type": "text",
                     "default": "waypoints", "pattern": None},
                ]),
            act("🔌 MCP sync preview / safe apply", ["desktop-sync", "mcp", "--preview"]),
            act("ℹ️ Help", ["desktop-sync", "--help"]),
            act("🚪 Exit", ["desktop-sync", "exit"]),
        ],
    }


def run_plain_menu(spec, input_fn, print_fn, run_action_fn):
    """The mandatory dependency-free numbered menu.

    input_fn() -> raw input line (raises EOFError at end); print_fn(text);
    run_action_fn(action) executes the chosen action via the SAME CLI handlers.
    Blank Enter runs the FIRST action (read-only preview default). 'q' exits.
    Returns the number of actions run."""
    actions = spec["actions"]
    print_fn(spec["title"])
    print_fn("=" * 60)
    for i, a in enumerate(actions, 1):
        print_fn("  %d) %s" % (i, a["label"]))
    print_fn("  q) Quit")
    print_fn("Choose [1-%d, blank=preview, q=quit]: " % len(actions))
    ran = 0
    while True:
        try:
            line = input_fn()
        except EOFError:
            print_fn("(EOF) exiting menu — no action applied")
            return ran
        line = (line or "").strip()
        if line.lower() in ("q", "quit", "exit"):
            return ran
        if line == "":
            run_action_fn(actions[0])  # blank Enter = read-only preview default
            ran += 1
            continue
        if not line.isdigit() or not (1 <= int(line) <= len(actions)):
            print_fn("invalid choice %r — nothing applied" % line)
            continue
        run_action_fn(actions[int(line) - 1])
        ran += 1
