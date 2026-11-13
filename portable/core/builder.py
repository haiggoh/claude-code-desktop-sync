"""core.builder — deterministic legacy-artifact builder (plan §8 Phase 5, §4
mandatory envelope).

Builds a target-compatible artifact from an unchanged third-party/local plugin
source, with explicit preconditions and NO execution of the plugin's code (no
running scripts, no dependency fetch to infer closure — the import closure is
derived from static sibling-import analysis only; unresolvable dynamic
dependencies are disclosed as `unknown`, never fabricated).

Recognized transforms (deterministic, hashed):
  - native-unchanged: verbatim copy (manifest, docs, resources)
  - mechanical: hook bin/ -> scripts/ (${CLAUDE_PLUGIN_ROOT}), skill bin/ ->
    skills/<s>/scripts/ (${CLAUDE_SKILL_DIR}), python sibling imports preserved

Safety limits & refusals (BuilderError): archive/path traversal, absolute
paths, symlink escapes, duplicate normalized (case-folded) names, private
markers (scanner detects risk — it does not prove a file secret-free),
excessive file count / expanded bytes / single-file size, and any source change
detected between the pre-build snapshot and the post-build verification (a
source that changes mid-build invalidates the artifact).

Stdlib only; Python 3.8 compatible.
"""

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

TRANSFORM_VERSION = "1"

DEFAULT_LIMITS = {
    "max_files": 5000,                 # platform upload constraint (fact 2)
    "max_bytes": 200 * 1024 * 1024,    # 200 MB per plugin (fact 2)
    "max_single_file_bytes": 20 * 1024 * 1024,
    "max_path_len": 200,
}

# Basenames/suffixes that make an entry private by default (excluded).
PRIVATE_BASENAMES = {".env", ".git", ".DS_Store", "Thumbs.db", "id_rsa", "id_ed25519"}
PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".env")
PRIVATE_NAME_RE = re.compile(r"(token|secret|credential|password|api[_-]?key)", re.IGNORECASE)
# Synthetic marker used by tests to plant a private-looking payload.
PRIVATE_CONTENT_MARKER = "PRIVATE-MARKER"

HOOK_OLD = "${CLAUDE_PLUGIN_ROOT}/bin/"
HOOK_NEW = "${CLAUDE_PLUGIN_ROOT}/scripts/"
SKILL_OLD = "${CLAUDE_SKILL_DIR}/../../bin/"
SKILL_NEW = "${CLAUDE_SKILL_DIR}/scripts/"

_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][A-Za-z0-9_]*)\s+import|import\s+([A-Za-z_][A-Za-z0-9_]*))",
                        re.MULTILINE)


class BuilderError(Exception):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _is_private(rel, data):
    name = os.path.basename(rel)
    lower = name.lower()
    if lower in PRIVATE_BASENAMES:
        return "private basename %r" % name
    if lower.endswith(PRIVATE_SUFFIXES):
        return "private suffix %r" % name
    if PRIVATE_NAME_RE.search(name):
        return "private-looking name %r" % name
    if len(data) <= 1024 * 1024 and PRIVATE_CONTENT_MARKER.encode("utf-8") in data:
        return "private content marker in %r" % rel
    return None


def check_duplicate_names(rels):
    """Raise on duplicate case-folded names. (On a case-insensitive FS two such
    files cannot coexist, so this is tested directly on a name list — and it
    also guards zip inputs where both CAN coexist.)"""
    seen = {}
    for rel in rels:
        norm = rel.casefold()
        if norm in seen and seen[norm] != rel:
            raise BuilderError("duplicate normalized path: %s vs %s" % (seen[norm], rel))
        seen[norm] = rel


def snapshot_source(source):
    """rel -> sha256 for every regular file under source (symlinks not followed)."""
    source = Path(source)
    out = {}
    for p in sorted(source.rglob("*")):
        if p.is_symlink():
            out[str(p.relative_to(source))] = "symlink:" + os.readlink(str(p))
        elif p.is_file():
            out[str(p.relative_to(source))] = sha256_bytes(p.read_bytes())
    return out


def verify_source_stable(before, after):
    if before != after:
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        raise BuilderError("source changed during build; artifact invalidated. changed: %s"
                           % ", ".join(changed[:5]))


_REL_IMPORT_RE = re.compile(
    r"^\s*from\s+(\.+)([A-Za-z_][A-Za-z0-9_.]*)?\s+import\s+([A-Za-z_][A-Za-z0-9_,\s]*)",
    re.MULTILINE)


def _is_stdlib(mod):
    """3.8-safe stdlib check: find_spec and require the origin to live in the
    interpreter's own stdlib dir (never site-/dist-packages)."""
    import importlib.util
    import sysconfig
    try:
        spec = importlib.util.find_spec(mod)
    except Exception:
        return False
    if spec is None or not spec.origin:
        return False
    stdlib_dir = os.path.realpath(sysconfig.get_path("stdlib"))
    origin = os.path.realpath(spec.origin)
    return origin.startswith(stdlib_dir + os.sep) and "site-packages" not in origin \
        and "dist-packages" not in origin


def _sibling_imports(py_path, source_root):
    """Static import classification of one .py file:
    found    — sibling modules that exist next to the file (preserved)
    refused  — relative imports (from . import x) that do NOT resolve inside
               the package: a missing resource refuses the component
    unknown  — absolute imports that are neither sibling nor stdlib: dynamic
               third-party dependency, disclosed as unknown (never fabricated)
    """
    text = py_path.read_text(encoding="utf-8", errors="replace")
    found, refused, unknown = set(), set(), set()
    base = py_path.parent
    for m in _REL_IMPORT_RE.finditer(text):
        dots, mod, names = m.group(1), m.group(2) or "", m.group(3)
        # relative import: resolve against the file's package dir
        pkg = base
        for _ in range(len(dots) - 1):
            pkg = pkg.parent
        prefix = mod.replace(".", os.sep) if mod else ""
        for name in [n.strip() for n in names.split(",") if n.strip()]:
            target = pkg / (prefix + os.sep + name if prefix else name)
            if not (target.with_suffix(".py")).is_file() and not (target / "__init__.py").is_file():
                refused.add("from %s%s import %s" % (dots, mod, name))
    for m in _IMPORT_RE.finditer(text):
        mod = m.group(1) or m.group(2)
        if (base / (mod + ".py")).is_file() or (base / mod / "__init__.py").is_file():
            found.add(mod)
        elif _is_stdlib(mod):
            pass  # stdlib: fine on every supported version
        elif "." not in mod and (Path(source_root) / (mod + ".py")).is_file():
            found.add(mod)
        else:
            unknown.add(mod)
    return found, refused, unknown


def build(source, dest, limits=None, external_on_path=None, fault_inject=None):
    """Build the artifact. Returns the artifact dict (also written to
    dest/ARTIFACT.json). Raises BuilderError on any refusal.

    fault_inject: optional callable run right before the post-build source
    stability check — the owned-fixture seam for testing mid-build source
    changes (plan §0.11: fault injection belongs only in owned synthetic
    fixtures)."""
    source = Path(source).resolve()
    dest = Path(dest)
    limits = dict(DEFAULT_LIMITS, **(limits or {}))
    external_on_path = set(external_on_path or [])

    before = snapshot_source(source)

    # ---- walk & validate ----
    entries = []  # (rel, abs_path, data)
    total_bytes = 0
    all_rels = [str(p.relative_to(source)) for p in sorted(source.rglob("*"))
                if not (p.is_symlink() and p.is_dir())]
    check_duplicate_names(all_rels)
    for p in sorted(source.rglob("*")):
        rel = str(p.relative_to(source))
        if rel.split(os.sep)[0] == ".git" or rel == ".git":
            continue
        parts = rel.split(os.sep)
        if any(part == ".." for part in parts) or os.path.isabs(rel):
            raise BuilderError("traversal/absolute path in source: %s" % rel)
        if len(rel) > limits["max_path_len"]:
            raise BuilderError("path too long: %s" % rel)
        if p.is_symlink():
            target = Path(os.readlink(str(p)))
            resolved = (p.parent / target) if not target.is_absolute() else target
            resolved = resolved.resolve()
            # component-wise containment, NEVER string prefix: /w/plugin2/notes.txt
            # startswith "/w/plugin" but is outside it
            try:
                resolved.relative_to(source)
            except ValueError:
                raise BuilderError("symlink escapes source root: %s -> %s" % (rel, target)) from None
            if resolved.is_dir():
                raise BuilderError("symlink to directory not supported: %s -> %s" % (rel, target))
            data = resolved.read_bytes()
        elif p.is_file():
            data = p.read_bytes()
        else:
            continue
        if len(entries) + 1 > limits["max_files"]:
            raise BuilderError("limits: more than %d files" % limits["max_files"])
        if len(data) > limits["max_single_file_bytes"]:
            raise BuilderError("limits: single file %s exceeds %d bytes" % (rel, limits["max_single_file_bytes"]))
        total_bytes += len(data)
        if total_bytes > limits["max_bytes"]:
            raise BuilderError("limits: expanded size exceeds %d bytes" % limits["max_bytes"])
        priv = _is_private(rel, data)
        if priv:
            raise BuilderError("refusing private entry: %s" % priv)
        entries.append((rel, p, data))

    # ---- sibling-import closure (static; missing relative resource refuses
    # the component; third-party imports are disclosed unknown) ----
    components = []
    for rel, p, data in entries:
        if rel.endswith(".py"):
            found, refused, unknown_mods = _sibling_imports(p, source)
            if refused:
                raise BuilderError("missing resource: unresolved relative import(s) %s "
                                   "in %s — component refused" % (sorted(refused), rel))
            notes = "sibling imports preserved: %s" % sorted(found)
            if unknown_mods:
                notes += "; third-party imports disclosed unknown: %s" % sorted(unknown_mods)
            components.append({"path": rel, "kind": "script",
                               "adaptation_class": "mechanical",
                               "evidence_level": "fixture-tested",
                               "surfaces": {"chat": True, "cowork": True, "code": True},
                               "notes": notes})

    # ---- dynamic external commands in hooks -> disclosed unknown (ALL events) ----
    unknowns = []
    for rel, p, data in entries:
        if rel == os.path.join("hooks", "hooks.json"):
            try:
                doc = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            for event, groups in (doc.get("hooks") or {}).items():
                for group in groups or []:
                    for hook in group.get("hooks", []) or []:
                        cmd = str(hook.get("command", ""))
                        if cmd and os.sep not in cmd and not os.path.isabs(cmd):
                            base = cmd.split()[0]
                            in_source = any(e[0].startswith(os.path.join("bin", base))
                                            or e[0].startswith(os.path.join("scripts", base))
                                            for e in entries)
                            if not in_source and base not in external_on_path and base not in ("python3", "python", "bash", "sh"):
                                unknowns.append({"path": "hooks/hooks.json", "kind": "hooks",
                                                 "event": event,
                                                 "adaptation_class": "unknown",
                                                 "evidence_level": "source-inspected",
                                                 "surfaces": {"chat": False, "cowork": True, "code": True},
                                                 "notes": "dynamic dependency %r not present in source and not resolvable offline — disclosed unknown, not fabricated" % base})

    # ---- transform & write ----
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    files = {}
    bin_files = []  # (basename, data) — mirrored into each bundled skill below
    for rel, p, data in entries:
        if rel == os.path.join("hooks", "hooks.json") or rel.endswith("SKILL.md"):
            text = data.decode("utf-8")
            text = text.replace(HOOK_OLD, HOOK_NEW).replace(SKILL_OLD, SKILL_NEW)
            data = text.encode("utf-8")
        new_rel = rel
        if rel.startswith("bin" + os.sep):
            new_rel = "scripts" + os.sep + rel[len("bin" + os.sep):]
            bin_files.append((os.path.basename(rel), data))
        out_path = dest / new_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        files[new_rel] = sha256_bytes(data)
    # skill-invoked copies of bin scripts live under each bundled skill's
    # scripts/ dir (skill bodies can only reference ${CLAUDE_SKILL_DIR});
    # done AFTER the main loop so dest/skills/ exists
    for sk in sorted(dest.glob("skills/*/")):
        if not sk.is_dir():
            continue
        sk_scripts = sk / "scripts"
        sk_scripts.mkdir(parents=True, exist_ok=True)
        for basename, data in bin_files:
            existing = sk_scripts / basename
            if existing.exists() and existing.read_bytes() != data:
                # M1 (review): never clobber a skill-owned script of the same
                # basename with the bin copy — refuse loudly instead
                raise BuilderError("name collision: bin/%s would overwrite the "
                                   "skill-owned %s" % (basename, existing.relative_to(dest)))
            if not existing.exists():
                existing.write_bytes(data)
                files[str(existing.relative_to(dest))] = sha256_bytes(data)

    artifact = {
        "schema": "cds-artifact/1",
        "transform_version": TRANSFORM_VERSION,
        "source": str(source),
        "source_hashes": before,
        "files": files,
        "components": components + unknowns,
        "provenance": "generated offline from unchanged source; no execution, no fetch",
    }
    (dest / "ARTIFACT.json").write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n",
                                         encoding="utf-8")

    # ---- source must not have changed during the build ----
    if fault_inject is not None:
        fault_inject()
    try:
        verify_source_stable(before, snapshot_source(source))
    except BuilderError:
        # never leave an invalidated artifact on disk looking valid
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return artifact
