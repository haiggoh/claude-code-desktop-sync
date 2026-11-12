"""tests/overhaul harness — isolated environments for the overhaul test suite.

Every tool/engine invocation goes through run_tool(), which REFUSES to launch
unless the environment carries explicit root overrides (HOME and every
CLAUDE_* path pinned as direct children of a test-owned root). A test that
forgets the override fails loudly instead of touching the real machine.

Stdlib only; Python 3.8 compatible (CI runs the legacy 3.8 job).
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
ENGINE = REPO / "bin" / "sync_code_desktop.py"

# Env vars that would leak the real machine into a child process.
# Windows stores some of these in mixed case ("Path", "UserProfile"), so the
# filter below compares case-insensitively (k.upper()) — a case-sensitive
# filter lets the real PATH/USERPROFILE survive alongside the sandbox ones.
LEAKY_ENV = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "PATH",
             "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA", "PATHEXT",
             "ProgramFiles",
             "CLAUDE_CODE_CONFIG", "CLAUDE_DESKTOP_CONFIG", "CLAUDE_SYNC_HOME",
             "PYTHONDONTWRITEBYTECODE")
LEAKY_ENV_UPPER = frozenset(k.upper() for k in LEAKY_ENV)


def clean_env():
    """The real environment minus every leaky key, matched case-insensitively."""
    return {k: v for k, v in os.environ.items() if k.upper() not in LEAKY_ENV_UPPER}

# Env keys that must be pinned inside the test root before any launch.
REQUIRED_ROOT_KEYS = ("HOME", "CLAUDE_CODE_CONFIG", "CLAUDE_DESKTOP_CONFIG",
                      "CLAUDE_SYNC_HOME")


class HarnessError(RuntimeError):
    pass


def build_env(root):
    """Return an isolated child env rooted at `root` (a directory the test owns).

    All writable paths (HOME, both configs, sync home) are direct children of
    root; PATH is the sandbox bin dir plus the bare system dirs.
    """
    root = Path(root).resolve()  # resolve symlinks (macOS /var -> /private/var) so PATH matches the guard's resolve()
    home = root / "home"
    bindir = root / "bin"
    home.mkdir(parents=True, exist_ok=True)
    bindir.mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),  # Windows ntpath.expanduser prefers USERPROFILE over HOME
        "PATH": os.pathsep.join([str(bindir), "/usr/bin", "/bin"]),
        "CLAUDE_CODE_CONFIG": str(root / "code.json"),
        "CLAUDE_DESKTOP_CONFIG": str(root / "desktop.json"),
        "CLAUDE_SYNC_HOME": str(root / "sync"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def require_root_override(env, root):
    """Refuse to launch unless every required writable path sits inside `root`.

    Raises HarnessError naming the offending key — never silently proceeds.
    """
    root = Path(root).resolve()
    for key in REQUIRED_ROOT_KEYS:
        val = env.get(key)
        if not val:
            raise HarnessError(
                "launch refused: %s is not set — an unisolated run could touch "
                "the real machine" % key)
        if Path(val).resolve().parent != root:
            raise HarnessError(
                "launch refused: %s=%s escapes the test root %s" % (key, val, root))
    return True


def run_tool(argv, env, root, cwd=None, timeout=60):
    """Run argv under `env` — but only after require_root_override(env, root)
    passes. The child env is the real env minus LEAKY_ENV, plus `env`."""
    require_root_override(env, root)
    clean = clean_env()
    clean.update(env)
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                          env=clean, cwd=str(cwd or root), timeout=timeout)


def run_engine(env, root, args=()):
    """Run the sync engine under an isolated env (guard enforced)."""
    return run_tool([sys.executable, ENGINE] + list(args), env, root)


def tree_snapshot(root):
    """relpath -> (size, mtime_ns) for every regular file under root."""
    root = Path(root)
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


def changed_files(root, before):
    """Paths whose (size, mtime) changed or that appeared since `before`."""
    after = tree_snapshot(root)
    return {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
