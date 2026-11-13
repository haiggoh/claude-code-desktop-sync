#!/usr/bin/env python3
"""build-portable.py — deterministic builder for the portable (Chat/Cowork-safe)
package of claude-code-desktop-sync, per COORDINATION C1 Option B.

Host package  = repo root, unchanged (keeps bin/, hooks, install.py, tests).
Portable pkg  = committed `portable/` directory, generated ONLY by this script:
  - same manifest `name` (a same-name account copy is shadowed, not double-run;
    a distinct name would load both — COORDINATION §2 fact 4)
  - NO top-level bin/ (top-level bin makes Chat/Cowork refuse the whole plugin —
    fact 1)
  - hook-invoked scripts  -> scripts/            (referenced via ${CLAUDE_PLUGIN_ROOT})
  - skill-invoked scripts -> skills/<s>/scripts/ (referenced via ${CLAUDE_SKILL_DIR};
    ${CLAUDE_PLUGIN_ROOT} is NOT substituted inside skill bodies)
  - Python import closure copied, never symlinked (fact 7)
  - portable/PORTABLE-BUILD.json, schema haiggoh-portable-build/1 (COORDINATION C2)
  - dist/<name>-portable.zip (gitignored), manifest at zip root (fact 6)

Usage:
  python3 scripts/build-portable.py            # regenerate portable/ + dist/
  python3 scripts/build-portable.py --check    # verify committed portable/ == fresh build
  python3 scripts/build-portable.py --out DIR --dist DIR
  python3 scripts/build-portable.py --help

Exit codes: 0 ok / check passed · 1 check failed (stale portable/) · 2 bad args.
Environment variables read: CDS_SOURCE_COMMIT (optional fallback for the recorded
source_commit when git is unavailable; the git commit from `git rev-parse HEAD`
wins when present). Note: --check masks source_commit when comparing (the recorded
commit is always the parent of the commit carrying portable/) and verifies only
that it exists in repo history — content freshness is the byte comparison.
Stdlib only; Python 3.8 compatible.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCHEMA = "haiggoh-portable-build/1"
BUILDER = "scripts/build-portable.py"
BUILDER_VERSION = "1.3.0"
TRANSFORM_VERSION = "1"
SOURCE_REPO = "https://github.com/haiggoh/claude-code-desktop-sync"

# dest-in-portable <- source-in-repo, verbatim copies
COPIES = [
    (".claude-plugin/plugin.json", ".claude-plugin/plugin.json"),
    ("scripts/sync_code_desktop.py", "bin/sync_code_desktop.py"),               # hook-invoked
    ("skills/desktop-sync/scripts/sync_code_desktop.py", "bin/sync_code_desktop.py"),  # skill-invoked
    ("docs/SYNC-REFERENCE.md", "docs/SYNC-REFERENCE.md"),
    ("README.md", "README.md"),
    ("CHANGELOG.md", "CHANGELOG.md"),
    ("LICENSE", "LICENSE"),
]

HOOK_OLD = "${CLAUDE_PLUGIN_ROOT}/bin/sync_code_desktop.py"
HOOK_NEW = "${CLAUDE_PLUGIN_ROOT}/scripts/sync_code_desktop.py"
SKILL_OLD = "${CLAUDE_SKILL_DIR}/../../bin/sync_code_desktop.py"
SKILL_NEW = "${CLAUDE_SKILL_DIR}/scripts/sync_code_desktop.py"


def _rewrite_hooks(text):
    doc = json.loads(text)
    n = 0
    for group in doc.get("hooks", {}).get("SessionStart", []) or []:
        for hook in group.get("hooks", []) or []:
            args = hook.get("args") or []
            hook["args"] = [HOOK_NEW if a == HOOK_OLD else a for a in args]
            n += 1
    return json.dumps(doc, indent=2) + "\n", n


def _rewrite_skill(text):
    if SKILL_OLD not in text:
        raise SystemExit("build-portable: SKILL.md no longer references %s — update the transform" % SKILL_OLD)
    return text.replace(SKILL_OLD, SKILL_NEW), 1


# dest-in-portable -> (source-in-repo, transform)
REWRITES = {
    "hooks/hooks.json": ("hooks/hooks.json", _rewrite_hooks),
    "skills/desktop-sync/SKILL.md": ("skills/desktop-sync/SKILL.md", _rewrite_skill),
}

# Component annotations, COORDINATION C2/C3 vocabulary. Evidence: source-inspected
# (COORDINATION §2 facts, official platform docs fetched 2026-11-12).
COMPONENTS = [
    {"path": "hooks/hooks.json", "kind": "hooks", "adaptation_class": "qualified-adapter",
     "evidence_level": "source-inspected",
     "surfaces": {"chat": False, "cowork": True, "code": True},
     "notes": "SessionStart hook relocated bin->scripts; Chat ignores hooks, Cowork loads them (fact 1)"},
    {"path": "scripts/sync_code_desktop.py", "kind": "script", "adaptation_class": "mechanical",
     "evidence_level": "source-inspected",
     "surfaces": {"chat": False, "cowork": True, "code": True},
     "notes": "byte-identical engine copy; hook-invoked via ${CLAUDE_PLUGIN_ROOT}; hooks not loaded in Chat"},
    {"path": "skills/desktop-sync/SKILL.md", "kind": "skill", "adaptation_class": "mechanical",
     "evidence_level": "source-inspected",
     "surfaces": {"chat": True, "cowork": True, "code": True},
     "notes": "engine reference rewritten to ${CLAUDE_SKILL_DIR}/scripts/ (plugin root is not substituted in skill bodies)"},
    {"path": "skills/desktop-sync/scripts/sync_code_desktop.py", "kind": "script", "adaptation_class": "mechanical",
     "evidence_level": "source-inspected",
     "surfaces": {"chat": True, "cowork": True, "code": True},
     "notes": "byte-identical engine copy for skill invocation; Chat can execute skill scripts (dated observation, §2)"},
    {"path": "bin/", "kind": "bin", "adaptation_class": "unsupported",
     "evidence_level": "source-inspected",
     "surfaces": {"chat": False, "cowork": False, "code": True},
     "notes": "EXCLUDED from portable: a top-level bin/ makes Chat/Cowork refuse the whole plugin (fact 1); host root keeps it"},
    {"path": "install.py", "kind": "installer", "adaptation_class": "unsupported",
     "evidence_level": "source-inspected",
     "surfaces": {"chat": False, "cowork": False, "code": True},
     "notes": "EXCLUDED: standalone host installer writes ~/.claude — host-only, stays in the host root"},
]


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _git(args):
    # -c safe.directory=* : CI containers run as root over a runner-owned
    # checkout, which git otherwise refuses as "dubious ownership".
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(REPO)] + args,
                          capture_output=True, text=True)


def source_commit(explicit=None):
    """The commit the portable tree was built from.

    Explicit flag wins (tests run the builder under a restricted PATH where git
    may not exist, e.g. Windows sandboxes). Otherwise git rev-parse HEAD; if git
    is unavailable, fall back to CDS_SOURCE_COMMIT, else "unknown" with a
    stderr warning (never crash the build over a provenance lookup).
    """
    if explicit:
        return explicit
    if shutil.which("git"):
        r = _git(["rev-parse", "HEAD"])
        if r.returncode == 0:
            return r.stdout.strip()
    env_val = os.environ.get("CDS_SOURCE_COMMIT")
    if env_val:
        return env_val
    print("build-portable: warning: git not found and CDS_SOURCE_COMMIT unset; "
          "recording source_commit as 'unknown'", file=sys.stderr)
    return "unknown"


def build(out_dir, make_zip=True, dist_dir=None, commit=None):
    """Generate the portable tree under out_dir. Returns the file map."""
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    files = {}
    for dest, src in COPIES:
        data = (REPO / src).read_bytes()
        files[dest] = data
    for dest, (src, fn) in REWRITES.items():
        text = (REPO / src).read_text(encoding="utf-8")
        new_text, _n = fn(text)
        files[dest] = new_text.encode("utf-8")
    # sanity: manifest name must stay identical (fact 4)
    root_name = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
    portable_name = json.loads(files[".claude-plugin/plugin.json"].decode("utf-8"))["name"]
    if root_name != portable_name:
        raise SystemExit("build-portable: manifest name drifted: %r vs %r" % (root_name, portable_name))
    if any(d == "bin" or d.startswith("bin/") for d in files):
        raise SystemExit("build-portable: top-level bin/ leaked into portable output")
    for dest, data in sorted(files.items()):
        p = out / dest
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    build_meta = {
        "schema": SCHEMA,
        "source_repo": SOURCE_REPO,
        "source_commit": source_commit(commit),
        "builder": BUILDER,
        "builder_version": BUILDER_VERSION,
        "transform_version": TRANSFORM_VERSION,
        "files": {dest: sha256_bytes(data) for dest, data in sorted(files.items())},
        "components": COMPONENTS,
    }
    meta = json.dumps(build_meta, indent=2, ensure_ascii=False) + "\n"
    (out / "PORTABLE-BUILD.json").write_text(meta, encoding="utf-8")
    if make_zip and dist_dir is not None:
        dist = Path(dist_dir)
        dist.mkdir(parents=True, exist_ok=True)
        zip_path = dist / ("%s-portable.zip" % root_name)
        # deterministic zip: sorted names, fixed timestamp, manifest at zip root
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
            all_files = sorted(files.keys()) + ["PORTABLE-BUILD.json"]
            for rel in all_files:
                info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, (out / rel).read_bytes())
        return files, str(zip_path)
    return files, None


def _tree(root):
    root = Path(root)
    if not root.exists():
        return None
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _mask_commit(meta_bytes):
    """PORTABLE-BUILD.json records the commit the tree was built FROM, which
    legitimately differs from the commit carrying the file (it is always the
    parent). Freshness compares CONTENT, so mask source_commit before comparing."""
    meta = json.loads(meta_bytes.decode("utf-8"))
    meta["source_commit"] = "<commit>"
    return json.dumps(meta, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")


def check(commit=None):
    """Rebuild into a temp dir and compare with committed portable/.

    All files are compared byte-for-byte except PORTABLE-BUILD.json, whose
    source_commit is masked (see _mask_commit). When git is available, the
    recorded source_commit must also exist in this repo's history.
    """
    with tempfile.TemporaryDirectory() as tmp:
        build(Path(tmp) / "portable", make_zip=False, commit=commit)
        fresh = _tree(Path(tmp) / "portable")
    committed = _tree(REPO / "portable")
    if committed is None:
        print("STALE: portable/ does not exist in the repo — run build-portable.py")
        return 1
    problems = []
    for rel in sorted(set(fresh) | set(committed)):
        if rel not in committed:
            problems.append("extra in fresh build: %s" % rel)
        elif rel not in fresh:
            problems.append("missing in fresh build (stale): %s" % rel)
        elif rel == "PORTABLE-BUILD.json":
            if _mask_commit(fresh[rel]) != _mask_commit(committed[rel]):
                problems.append("differs (besides source_commit): %s" % rel)
        elif fresh[rel] != committed[rel]:
            problems.append("differs: %s" % rel)
    # provenance: the recorded source_commit must be a real commit of this repo.
    # Skipped (with a note) when git cannot read the repo at all (missing binary,
    # root container without safe.directory) or the checkout is SHALLOW (CI used
    # to default to fetch-depth=1, where the parent commit is absent). Only a
    # healthy, non-shallow repo with a genuinely missing commit is a failure.
    recorded = json.loads(committed["PORTABLE-BUILD.json"].decode("utf-8")).get("source_commit", "")
    if shutil.which("git") and recorded and recorded != "unknown":
        if _git(["rev-parse", "HEAD"]).returncode != 0:
            print("build-portable: note: git cannot read this repo; skipping "
                  "source_commit provenance check", file=sys.stderr)
        else:
            is_shallow = _git(["rev-parse", "--is-shallow-repository"]).stdout.strip() == "true"
            if _git(["cat-file", "-e", recorded + "^{commit}"]).returncode != 0 and not is_shallow:
                problems.append("PORTABLE-BUILD.json source_commit %s not found in repo history" % recorded)
    if problems:
        print("STALE: committed portable/ differs from a fresh build:")
        for p in problems:
            print("  " + p)
        print("run: python3 scripts/build-portable.py")
        return 1
    print("OK: portable/ matches a fresh build (%d files)" % len(fresh))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(
        prog="build-portable.py",
        description="Generate the portable (Chat/Cowork-safe) package of this repo "
                    "into portable/ (committed) plus dist/<name>-portable.zip (gitignored). "
                    "--check verifies the committed portable/ equals a fresh build. "
                    "Reads no environment variables; source commit comes from git HEAD.")
    ap.add_argument("--check", action="store_true", help="verify committed portable/ is fresh; exit 1 if stale")
    ap.add_argument("--out", default=None, help="output dir for the portable tree (default: <repo>/portable)")
    ap.add_argument("--dist", default=None, help="dir for the zip (default: <repo>/dist); zip written unless --no-zip")
    ap.add_argument("--no-zip", action="store_true", help="do not write dist zip")
    ap.add_argument("--source-commit", default=None,
                    help="record this commit as source_commit instead of git HEAD "
                         "(for sandboxed runs without git); env CDS_SOURCE_COMMIT is the fallback")
    args = ap.parse_args(argv)
    if args.check:
        return check(commit=args.source_commit)
    out = Path(args.out) if args.out else REPO / "portable"
    dist = Path(args.dist) if args.dist else REPO / "dist"
    files, zip_path = build(out, make_zip=not args.no_zip, dist_dir=dist if not args.no_zip else None,
                            commit=args.source_commit)
    print("built %d files into %s" % (len(files), out))
    if zip_path:
        print("zip: %s" % zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
