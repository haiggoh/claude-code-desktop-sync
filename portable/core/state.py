"""core.state — versioned typed state schema (plan §5.5) and first-use states.

The v2 state file (owned data dir, never inside caches) records: source/
manifest/install identities and scopes, content revision, component closure,
transform version, output hashes, target qualification, ownership, journal,
proposal revisions. Private inventory stays separate from any public manifest.

Validation is loud: valid JSON of the wrong shape, or an unknown schema
version, is REFUSED with named errors — never silently defaulted to empty state.

First-use kinds are distinguished explicitly:
  FRESH            no baseline file — approved safe union, never deletions
  EXISTING         readable baseline — normal operation
  LOST_BASELINE    baseline exists but is unreadable/corrupt — recovery
                   required; writes refused until the user resolves it
  INVALID_BASELINE baseline present, readable, but wrong shape — refuse

Stdlib only; Python 3.8 compatible.
"""

import json

STATE_SCHEMA = "cds-state/1"

FRESH = "fresh"
EXISTING = "existing"
LOST_BASELINE = "lost-baseline"
INVALID_BASELINE = "invalid-baseline"

_REQUIRED_KEYS = {
    "schema": str,
    "sources": list,
    "installs": dict,
    "revisions": dict,
    "transform_version": str,
    "output_hashes": dict,
    "target_qualification": dict,
    "ownership": dict,
    "journal": list,
    "proposals": dict,
}


def new_state(source, install_name):
    """A fresh, valid, empty state document."""
    return {
        "schema": STATE_SCHEMA,
        "sources": [{"name": install_name, "source": source, "scope": "user"}],
        "installs": {install_name: {"identity": install_name, "scope": "user"}},
        "revisions": {},
        "transform_version": "1",
        "output_hashes": {},
        "target_qualification": {},
        "ownership": {},
        "journal": [],
        "proposals": {},
    }


def validate_state(doc):
    """Returns (ok, errors). Loud refusal on wrong shape or schema drift."""
    if not isinstance(doc, dict):
        return False, ["state is not a JSON object"]
    errors = []
    schema = doc.get("schema")
    if schema != STATE_SCHEMA:
        errors.append("unknown state schema %r (this build understands %s)"
                      % (schema, STATE_SCHEMA))
        return False, errors
    for key, typ in _REQUIRED_KEYS.items():
        if key not in doc:
            errors.append("missing key %r" % key)
        elif not isinstance(doc[key], typ):
            errors.append("key %r must be %s" % (key, typ.__name__))
    for src in doc.get("sources", []):
        if not isinstance(src, dict) or not isinstance(src.get("name"), str):
            errors.append("sources entries must be objects with a string 'name'")
    return (not errors), errors


def load_state_file(path):
    """Returns (doc|None, first_use_kind). Never raises.

    absent file -> (None, FRESH); unparseable -> (None, LOST_BASELINE);
    parseable but wrong shape/schema -> (None, INVALID_BASELINE); valid ->
    (doc, EXISTING).
    """
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return None, first_use_kind(absent=True, corrupt=False, valid_shape=False)
    try:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None, first_use_kind(absent=False, corrupt=True, valid_shape=False)
    ok, _errors = validate_state(doc)
    if not ok:
        return None, first_use_kind(absent=False, corrupt=False, valid_shape=False)
    return doc, first_use_kind(absent=False, corrupt=False, valid_shape=True)


def first_use_kind(absent, corrupt, valid_shape):
    """Pure mapping used by load_state_file and tests.

    absent -> FRESH · corrupt/unparseable -> LOST_BASELINE (recovery required)
    · readable but invalid shape/schema -> INVALID_BASELINE (refuse) · valid ->
    EXISTING.
    """
    if absent:
        return FRESH
    if corrupt:
        return LOST_BASELINE
    if not valid_shape:
        return INVALID_BASELINE
    return EXISTING
