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


def detect_context(env, platform, home, tty, require_configs=True):
    """Pure multi-signal context detection. Returns (kind, evidence dict).

    kind is CODE_HOST only when every required signal holds; otherwise the
    context is "unknown" (or "cloud" when CI/no-TTY signals dominate) —
    neither of which can manage host config.
    """
    evidence = {
        "code_session": bool(env.get("CLAUDE_CODE_SESSION_ID")),
        "plugin_root": bool(env.get("CLAUDE_PLUGIN_ROOT")),
        "desktop_platform": platform in DESKTOP_PLATFORMS,
        "tty": bool(tty),
        "config_present": bool(require_configs is False or (Path(home) / ".claude.json").is_file()),
    }
    if env.get("CI") and not tty:
        kind = "cloud"
    elif all(evidence[s] for s in REQUIRED_SIGNALS):
        kind = CODE_HOST
    else:
        kind = "unknown"
    return kind, evidence


class HostGrant:
    """A verified grant. `evidence` must be exactly the REQUIRED_SIGNALS tuple
    as produced by detect_context for a code-host context."""

    def __init__(self, kind, root, evidence):
        self.kind = kind
        self.root = str(Path(root).resolve())
        self.evidence = tuple(evidence)

    def verified(self):
        return self.kind == CODE_HOST and self.evidence == REQUIRED_SIGNALS


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
        if kind != CODE_HOST or not all(evidence.get(s) for s in REQUIRED_SIGNALS):
            raise HostGuardError("host-management refused: context %r is not a verified "
                                 "code host (evidence=%r)" % (kind, evidence))
        return cls(HostGrant(kind, root, REQUIRED_SIGNALS))

    def write_config(self, path, data):
        """Atomically write a JSON config — only inside the verified root."""
        target = Path(path).resolve()
        root = Path(self.grant.root)
        if root not in target.parents and target != root:
            raise HostGuardError("host-management refused: %s escapes verified root %s"
                                 % (target, root))
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        return str(target)
