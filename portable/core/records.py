"""core.records — selected records: allowlisted snapshot/export/propose/import
with one authoritative writer (plan §8 Phase 9, COORDINATION C4).

Rules: explicit allowlist only; stable revision IDs (sha256 of content) and
expected-prior checks; provenance on every record; explicit tombstones;
duplicate proposal idempotent; concurrent conflict held; OFFLINE or MISSING
transport never deletes the authoritative record; unknown export schema is
refused loudly (never all-defaults). Explicit export/import is the mandatory
fallback — native folder transport is only used when Phase 6 qualifies it
(it is not qualified on this machine, fact 5).

Stdlib only; Python 3.8 compatible.
"""

import hashlib
import json
from pathlib import Path

RECORD_SCHEMA = "cds-record/1"


class RecordError(Exception):
    pass


def _revision(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


class RecordStore:
    """One authoritative writer per record name, under data_dir/records/."""

    def __init__(self, data_dir, allowlist):
        self.data_dir = Path(data_dir)
        self.allowlist = tuple(allowlist)
        (self.data_dir / "records").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "proposals").mkdir(parents=True, exist_ok=True)

    def _check(self, name):
        if name not in self.allowlist:
            raise RecordError("record %r is not in the allowlist %r" % (name, self.allowlist))

    def _path(self, name):
        return self.data_dir / "records" / (name + ".json")

    def write_current(self, name, content, origin):
        self._check(name)
        doc = {"schema": RECORD_SCHEMA, "name": name, "revision": _revision(content),
               "content": content, "provenance": {"origin": origin, "writer": "cds"}}
        tmp = self._path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self._path(name))
        return doc

    def snapshot(self, name):
        self._check(name)
        p = self._path(name)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8-sig"))

    def export_record(self, name, dest):
        """Explicit export (mandatory fallback transport). Returns metadata."""
        doc = self.snapshot(name)
        if doc is None:
            raise RecordError("nothing to export for %r" % name)
        export = {"schema": RECORD_SCHEMA, "exported_revision": doc["revision"],
                  "name": name, "content": doc["content"], "provenance": doc.get("provenance"),
                  "tombstone": doc.get("tombstone", False)}
        Path(dest).write_text(json.dumps(export, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        return {"revision": doc["revision"], "provenance": doc.get("provenance"),
                "dest": str(dest)}

    def import_record(self, name, src, expected_prior):
        """Apply an export if the schema is known and expected_prior matches the
        current revision (None = accept any). Offline/missing source NEVER
        deletes; stale prior is refused."""
        self._check(name)
        src = Path(src)
        if not src.exists():
            return {"status": "offline-missing", "revision": None}
        try:
            doc = json.loads(src.read_text(encoding="utf-8-sig"))
        except Exception as e:
            return {"status": "offline-missing", "reason": str(e)}
        if doc.get("schema") != RECORD_SCHEMA:
            raise RecordError("unknown export schema %r (this build understands %s)"
                              % (doc.get("schema"), RECORD_SCHEMA))
        current = self.snapshot(name)
        cur_rev = current["revision"] if current else None
        if expected_prior is not None and cur_rev != expected_prior:
            raise RecordError("stale expected_prior: current revision %r != expected %r"
                              % (cur_rev, expected_prior))
        if doc.get("tombstone"):
            self.write_current(name, doc.get("content"), origin="tombstone:" + str(doc.get("provenance")))
            d = self.snapshot(name)
            d["tombstone"] = True
            self._path(name).write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
            return {"status": "tombstoned", "revision": d["revision"]}
        self.write_current(name, doc["content"], origin="import:" + str(doc.get("provenance")))
        return {"status": "applied", "revision": self.snapshot(name)["revision"]}

    def propose(self, name, content, expected_prior, author):
        """Revision-protected proposal. Duplicate (same content+prior+author) is
        idempotent; a different content on the same prior is a held conflict."""
        self._check(name)
        current = self.snapshot(name)
        cur_rev = current["revision"] if current else None
        if expected_prior != cur_rev:
            raise RecordError("stale expected_prior: current %r != expected %r"
                              % (cur_rev, expected_prior))
        pid = hashlib.sha256(json.dumps(
            {"name": name, "revision": _revision(content), "prior": expected_prior,
             "author": author}, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        ppath = self.data_dir / "proposals" / (pid + ".json")
        if ppath.exists():
            return {"proposal_id": pid, "status": "duplicate-idempotent"}
        existing = list((self.data_dir / "proposals").glob("*.json"))
        for other in existing:
            o = json.loads(other.read_text(encoding="utf-8"))
            if o.get("name") == name and o.get("prior") == expected_prior \
                    and o.get("revision") != _revision(content):
                return {"proposal_id": pid, "status": "conflict-held",
                        "held_against": o.get("proposal_id")}
        ppath.write_text(json.dumps(
            {"schema": RECORD_SCHEMA, "proposal_id": pid, "name": name,
             "revision": _revision(content), "prior": expected_prior, "author": author,
             "content": content}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"proposal_id": pid, "status": "proposed"}

    def tombstone(self, name, reason):
        self._check(name)
        doc = self.snapshot(name)
        if doc is None:
            raise RecordError("cannot tombstone absent record %r" % name)
        doc["tombstone"] = True
        doc["tombstone_reason"] = reason
        self._path(name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                                    encoding="utf-8")
        return doc
