"""core.hostguard — the host-management boundary (plan §5.3).

A process may inventory/apply HOST configuration only under a verified
multi-signal grant. The default context is UNKNOWN, and unknown context cannot
perform host-management writes. Detection never relies on HOME, the OS name,
config-file-name lookalikes, or any single spoofable variable: a code-host
grant requires ALL of (code-session marker, plugin-root marker, desktop
platform, interactive TTY, and an existing ~/.claude.json under the home).

A cloud sandbox holding plausible .claude.json / Desktop config files with
CI/no-TTY signals therefore detects as NOT a host, and every write attempt is
refused. A hand-built grant without evidence (a deliberate bypass) is refused
the same way — the check lives at the single choke point, HostManager.write_config.

Stdlib only; Python 3.8 compatible.
"""

import json
import os
from pathlib import Path

CODE_HOST = "code-host"

# Every signal that must be present for a code-host grant. A tuple so a forged
# grant cannot smuggle evidence in as a truthy non-empty value.
REQUIRED_SIGNALS = ("code_session", "plugin_root", "desktop_platform", "tty", "config_present")

DESKTOP_PLATFORMS = ("darwin", "linux", "win32")


class HostGuardError(RuntimeError):
    pass


def detect_context(env, platform, home, tty):
    """Pure multi-signal context detection. Returns (kind, evidence dict of
    booleans). kind is CODE_HOST only when EVERY required signal is truthy;
    otherwise "unknown" (or "cloud" when CI/no-TTY dominate) — neither can
    manage host config. There is deliberately no way to skip the config-file
    signal: ~/.claude.json must exist.
    """
    evidence = {
        "code_session": bool(env.get("CLAUDE_CODE_SESSION_ID")),
        "plugin_root": bool(env.get("CLAUDE_PLUGIN_ROOT")),
        "desktop_platform": platform in DESKTOP_PLATFORMS,
        "tty": bool(tty),
        "config_present": (Path(home) / ".claude.json").is_file(),
    }
    if env.get("CI") and not tty:
        kind = "cloud"
    elif all(evidence[s] for s in REQUIRED_SIGNALS):
        kind = CODE_HOST
    else:
        kind = "unknown"
    return kind, evidence


class HostGrant:
    """A verified grant. `evidence` must be the boolean dict produced by
    detect_context for a code-host context, with every signal truthy —
    a name-only tuple (or an all-False dict) is NOT verified."""

    def __init__(self, kind, root, evidence):
        self.kind = kind
        self.root = str(Path(root).resolve())
        self.evidence = evidence

    def verified(self):
        if self.kind != CODE_HOST or not isinstance(self.evidence, dict):
            return False
        return all(self.evidence.get(s) is True for s in REQUIRED_SIGNALS)


class HostManager:
    """The only path to host-config writes. Refuses anything not covered by a
    verified grant whose root matches the target's root."""

    def __init__(self, grant):
        if not isinstance(grant, HostGrant) or not grant.verified():
            raise HostGuardError("host-management refused: grant is not verified "
                                 "(kind=%r, evidence=%r)" % (getattr(grant, "kind", None),
                                                            getattr(grant, "evidence", None)))
        self.grant = grant

    @classmethod
    def for_context(cls, kind, evidence, root):
        if kind != CODE_HOST or not isinstance(evidence, dict) \
                or not all(evidence.get(s) is True for s in REQUIRED_SIGNALS):
            raise HostGuardError("host-management refused: context %r is not a verified "
                                 "code host (evidence=%r)" % (kind, evidence))
        return cls(HostGrant(kind, root, evidence))

    def write_config(self, path, data):
        """Atomically write a JSON config — only inside the verified root.
        M3 (review): an existing secrets-bearing config keeps its current mode
        (the temp is chmodded to it before replace); a new file gets 0600."""
        target = Path(path).resolve()
        root = Path(self.grant.root)
        if root not in target.parents and target != root:
            raise HostGuardError("host-management refused: %s escapes verified root %s"
                                 % (target, root))
        tmp = target.with_suffix(target.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            mode = os.stat(target).st_mode & 0o777 if target.exists() else 0o600
            os.chmod(tmp, mode)
        except OSError:
            pass
        os.replace(tmp, target)
        return str(target)
