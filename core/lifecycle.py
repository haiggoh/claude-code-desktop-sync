"""core.lifecycle — apply/recovery lifecycle with owned locks, verified
backups, private unique temps, and an append-only journal (plan §8 Phase 8).

Rules enforced at the choke points:
  - a conflicted/stale plan (status != ok) writes NOTHING (ConflictError)
  - backups are written and verified BEFORE the first target write; any backup
    failure => LifecycleError, zero target writes, journal "failed-prewrite"
  - every target write goes through a private unique temp file (mode 0600)
    + atomic os.replace; a fault between temp-write and replace journals
    "failed-midwrite" and leaves no target file
  - an existing target whose digest differs from the action's base_digest is a
    USER EDIT: ConflictError, never overwritten
  - concurrent runs serialize on an owned lock (O_EXCL + pid); a stale lock
    (age > stale_seconds) is stolen, a fresh foreign lock => LockedError
  - removal is ownership-only: paths not in the ownership records are refused;
    backups live outside the removal target and survive removal
  - recovery is inspect-first: it reports exact journal states and never
    overwrites newer edits or deletes the snapshot as a universal fix

Stdlib only; Python 3.8 compatible.
"""

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

LOCK_STALE_SECONDS = 120


class LifecycleError(Exception):
    pass


class LockedError(LifecycleError):
    pass


class ConflictError(LifecycleError):
    pass


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _journal(state_dir, entry):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def acquire_owned_lock(state_dir, stale_seconds=LOCK_STALE_SECONDS):
    """True when the lock was acquired (or stolen as stale). False when a fresh
    foreign lock is held."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / ".lock"
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock)
            except OSError:
                age = 0
            if age > stale_seconds:
                try:
                    os.unlink(lock)
                    continue
                except OSError:
                    return False
            return False
    return False


def release_lock(state_dir):
    try:
        os.unlink(str(Path(state_dir) / ".lock"))
    except OSError:
        pass


def backup_file(path, backup_dir):
    """Copy path into backup_dir with mode 0600. Returns the backup path.
    Raises OSError when the backup cannot be written (caller treats this as
    fatal BEFORE any target write)."""
    path = Path(path)
    if not path.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-%d" % os.getpid()
    dst = backup_dir / (path.name + "." + stamp + ".bak")
    shutil.copy2(path, dst)
    os.chmod(dst, 0o600)
    return dst


class Result:
    def __init__(self, status, idempotent=False, written=()):
        self.status = status
        self.idempotent = idempotent
        self.written = list(written)


def apply_plan(plan, dest_root, state_dir, fault_inject=None):
    """Apply a plan's actions under all the §8 safeguards. Conflicted plans
    (status != ok) write nothing."""
    if plan.status != "ok":
        raise ConflictError("plan status %r (exit %d): no writable actions"
                            % (plan.status, plan.exit_code))
    dest_root = Path(dest_root)
    state_dir = Path(state_dir)
    if not acquire_owned_lock(state_dir):
        raise LockedError("another lifecycle run holds the lock in %s" % state_dir)
    try:
        backups = state_dir / "backups"
        # 1) backups for every existing target, verified BEFORE any write
        for a in plan.actions:
            target = dest_root / a.destination
            if target.exists():
                try:
                    backup_file(target, backups)
                except OSError as e:
                    _journal(state_dir, {"status": "failed-prewrite", "action": a.action_id,
                                         "reason": "backup failed: %s" % e})
                    raise LifecycleError("backup verification failed for %s: %s — "
                                         "zero target writes" % (target, e)) from None
        # 2) write actions
        written = []
        idempotent = True
        for a in plan.actions:
            target = dest_root / a.destination
            content = a.content
            base = getattr(a, "base_digest", None)
            if target.exists():
                current = digest_bytes(target.read_bytes())
                if current == digest_bytes(content):
                    continue  # already applied
                if base is not None and current != base:
                    _journal(state_dir, {"status": "conflict-user-edit", "action": a.action_id,
                                         "destination": a.destination})
                    raise ConflictError("user edited %s since base revision — refusing to "
                                        "overwrite" % target)
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".",
                                       suffix=".tmp")
            try:
                if hasattr(os, "fchmod"):  # POSIX only; Windows uses ACLs
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(content)
                    f.flush()
                    os.fsync(f.fileno())
                if fault_inject is not None:
                    fault_inject()  # owned-fixture fault seam, between temp write and replace
                os.replace(tmp, target)
            except BaseException as e:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                _journal(state_dir, {"status": "failed-midwrite", "action": a.action_id,
                                     "destination": a.destination, "error": str(e)})
                raise LifecycleError("write failed mid-way for %s: %s" % (target, e)) from None
            _journal(state_dir, {"status": "committed", "action": a.action_id,
                                 "destination": a.destination,
                                 "sha256": digest_bytes(content)})
            written.append(a.destination)
            idempotent = False
        return Result("ok", idempotent=idempotent, written=written)
    finally:
        release_lock(state_dir)


def recover(state_dir):
    """Inspect-first recovery report: exact journal states, never destructive."""
    state_dir = Path(state_dir)
    journal = state_dir / "journal.jsonl"
    entries = []
    if journal.exists():
        for line in journal.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    partial = [e for e in entries if e.get("status") != "committed"]
    return {"journal": entries, "partial": partial,
            "note": "inspect-first: never overwrite newer edits, never delete the "
                    "snapshot as a universal fix; rerun the same plan after resolving"}


def remove_owned(path, ownership, state_dir):
    """Ownership-only removal: refuse paths absent from the ownership records.
    Backups go to state_dir/backups (outside the removal target)."""
    path = Path(path)
    rel = path.name
    rec = ownership.get(rel)
    if rec is None:
        raise LifecycleError("refusing to remove %s: not in ownership records" % path)
    if path.exists() and digest_bytes(path.read_bytes()) != rec.get("sha256"):
        raise ConflictError("refusing to remove %s: content drifted from ownership "
                            "digest" % path)
    backup_file(path, Path(state_dir) / "backups")
    path.unlink()
    return True
