"""core.distribution — native distribution readiness + honest pending steps
(plan §8 Phase 7).

The offline-checkable parts live here: is the artifact actually upload-ready
(manifest at zip root, no top-level bin/, within the platform limits of
COORDINATION §2 fact 2, PORTABLE-BUILD hashes match the zip contents), and what
the exact remaining account steps are — always recorded as PENDING until
observed in the user-run live gate (COORDINATION §5). Nothing here fabricates
installation: no private API, no upload, no enable.

Active-version verification separates installed-row from actually-loaded:
an installed row whose observed session marker is older fails (stale), a
same-name installed plugin shadowing the synced copy reports shadowed (fact 4).

Stdlib only; Python 3.8 compatible. Read-only: no writes, no subprocess.
"""

import hashlib
import json
import zipfile
from pathlib import Path

# Platform upload constraints, COORDINATION §2 fact 2 (source-inspected 2026-11-12).
PLATFORM_LIMITS = {"max_files": 5000, "max_bytes": 200 * 1024 * 1024, "max_zip_bytes": 200 * 1024 * 1024}


def check_artifact_ready(zip_path, limits=None):
    """(ok, problems) for a candidate upload zip. Read-only."""
    limits = dict(PLATFORM_LIMITS, **(limits or {}))
    problems = []
    p = Path(zip_path)
    if not p.is_file():
        return False, ["zip missing: %s" % zip_path]
    if p.stat().st_size > limits["max_zip_bytes"]:
        problems.append("limits: zip %d bytes exceeds %d" % (p.stat().st_size, limits["max_zip_bytes"]))
    try:
        z = zipfile.ZipFile(p)
    except Exception as e:
        return False, ["not a zip: %s" % e]
    with z:
        names = z.namelist()
        if len(names) > limits["max_files"]:
            problems.append("limits: %d entries exceeds %d files" % (len(names), limits["max_files"]))
        total = sum(i.file_size for i in z.infolist())
        if total > limits["max_bytes"]:
            problems.append("limits: expanded %d bytes exceeds %d" % (total, limits["max_bytes"]))
        if ".claude-plugin/plugin.json" not in names:
            problems.append("manifest not at zip root (fact 6): .claude-plugin/plugin.json missing")
        if any(n == "bin" or n.startswith("bin/") for n in names):
            problems.append("top-level bin/ present — Chat/Cowork would refuse the whole plugin (fact 1)")
        if any(n.startswith("/") or ".." in n.split("/") for n in names):
            problems.append("unsafe entry (absolute or traversal) in zip")
        # PORTABLE-BUILD.json hashes must match the zip contents
        if "PORTABLE-BUILD.json" in names:
            try:
                meta = json.loads(z.read("PORTABLE-BUILD.json").decode("utf-8"))
                for rel, digest in meta.get("files", {}).items():
                    if rel not in names:
                        problems.append("PORTABLE-BUILD lists %s but zip lacks it" % rel)
                    elif hashlib.sha256(z.read(rel)).hexdigest() != digest:
                        problems.append("hash mismatch for %s" % rel)
            except Exception as e:
                problems.append("PORTABLE-BUILD.json unreadable: %s" % e)
        else:
            problems.append("PORTABLE-BUILD.json missing from zip")
    return (not problems), problems


def pending_steps(zip_path):
    """The exact supported account steps, all PENDING until observed live.
    Never fabricated: no step here is ever marked done by this module."""
    return [
        {"step": "upload the artifact zip in the claude.ai plugin UI (200 MB upload cap, fact 2)",
         "where": "claude.ai → plugins → upload", "status": "pending",
         "expected": "plugin appears in the account plugin list with the manifest name+version"},
        {"step": "enable the uploaded plugin for the account",
         "where": "claude.ai → plugins → enable", "status": "pending",
         "expected": "account-installed plugin visible to eligible Code sessions as <name>@synced (fact 3)"},
        {"step": "open a FRESH Code session signed in with claude.ai (not token/helper — fact 5) and confirm the synced copy loads or is shadowed (fact 4)",
         "where": "Claude Code / Desktop app", "status": "pending",
         "expected": "session marker shows the deployed version; same-name Code-installed plugin shadows the synced copy"},
        {"step": "record artifact path + sha256, observed marker, and report back into COORDINATION.md §5",
         "where": "this repo's coordination doc", "status": "pending",
         "expected": "live-gate entry with artifact sha256 and observed behavior"},
    ]


def verify_active_version(installed_row, session_observation):
    """'ok' | 'stale' | 'shadowed'. An installed row is not proof of the active
    version: the marker observed in a fresh session decides."""
    if session_observation.get("shadowed_by"):
        return "shadowed"
    seen = session_observation.get("seen_version")
    if seen is None:
        return "stale"  # no marker observed -> cannot claim active
    if seen != installed_row.get("version"):
        return "stale"
    return "ok"
