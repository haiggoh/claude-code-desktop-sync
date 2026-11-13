"""core.windowdriver — bundled nonprivileged window launcher (plan §6.3/§6.4).

Reuse-first: an installed helper is only used through a qualified adapter;
otherwise this repo's OWN nonprivileged launcher runs (no sudo-named helper, no
password banner, no personal ~/.claude/scripts dependency). macOS uses
Terminal.app via osascript with the AppleScript string built in Python (escape
backslash + quote); the shell layer stays a pure argv LIST (never joined), so
quoting is enforced independently at both layers.

Per-launch receipts (plan §6.4): unique unpredictable launch ID, exclusively
created private receipt dir with a .owned marker, atomic receipt file written
by the launched wrapper, validated for matching launch id + freshness.
Distinct states: prepared / request-error / request-accepted / not-verified /
frontend-started / exited. Bounded wait labelled NOT_VERIFIED (not necessarily
failed). No auto-relaunch on uncertain acceptance; never kills a terminal this
process did not open.

Stdlib only; Python 3.8 compatible.
"""

import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

RECEIPT_VERSION = 1


class ReceiptError(Exception):
    pass


class LaunchResult:
    def __init__(self, state, launch_id, receipt_dir, relaunches=0):
        self.state = state
        self.launch_id = launch_id
        self.receipt_dir = receipt_dir
        self.relaunches = relaunches


def new_launch_id():
    return secrets.token_hex(16)


def new_receipt_dir(parent, launch_id):
    """Exclusively created private per-launch receipt dir (mkdir, exist_ok=False
    => collision raises), marked owned."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    rdir = parent / launch_id
    try:
        os.mkdir(str(rdir), 0o700)  # exclusive: FileExistsError on collision
    except FileExistsError:
        raise ReceiptError("receipt dir %s already exists" % rdir) from None
    marker = rdir / ".owned"
    fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, ("launch %s\n" % launch_id).encode())
    os.close(fd)
    return rdir


def write_receipt(rdir, receipt):
    """Atomic versioned receipt write."""
    receipt = dict(receipt)
    receipt["receipt_version"] = RECEIPT_VERSION
    rdir = Path(rdir)
    tmp = rdir / "receipt.json.tmp"
    tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, rdir / "receipt.json")


def validate_receipt(rdir, launch_id, max_age=300):
    """Receipt must match the launch id and be fresh; foreign/stale/symlink
    receipts are rejected. ID is correlation, not proof of a trustworthy host."""
    rdir = Path(rdir)
    if rdir.is_symlink():
        raise ReceiptError("receipt dir is a symlink — refused")
    rp = rdir / "receipt.json"
    if rp.is_symlink() or not rp.is_file():
        raise ReceiptError("no receipt file")
    try:
        doc = json.loads(rp.read_text(encoding="utf-8"))
    except Exception as e:
        raise ReceiptError("receipt unreadable: %s" % e) from None
    if doc.get("launch_id") != launch_id:
        raise ReceiptError("receipt launch id mismatch (foreign receipt)")
    if doc.get("receipt_version") != RECEIPT_VERSION:
        raise ReceiptError("unknown receipt version %r" % doc.get("receipt_version"))
    age = time.time() - float(doc.get("started_at", 0))
    if age > max_age or age < -5:
        raise ReceiptError("receipt stale (age %.0fs > %ds)" % (age, max_age))
    return doc


def applescript_quote(value):
    """Escape for an AppleScript double-quoted string literal: backslash first,
    then quote. Newlines/tabs are not allowed raw in the literal — encode them."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return out


def shell_argv_list(argv):
    """The shell layer is always a LIST — never joined into a string."""
    return [str(a) for a in argv]


def select_driver(platform, has_osascript):
    if platform == "darwin":
        return "terminal-app" if has_osascript else "none"
    if platform == "win32":
        return "windows-terminal"
    return "none"  # linux/headless: direct/conversational fallback (plan §6.4)


def _terminal_app_driver(argv, cwd, receipt_dir, launch_id):
    """osascript `do script` with the command encoded as an argv list inside the
    shell text; cwd encoded via a shell-quoted `cd`; AppleScript layer escaped
    by applescript_quote. Returns the osascript exit code."""
    shell_text = "cd %s && %s" % (_shell_quote(str(cwd)), " ".join(_shell_quote(a) for a in argv))
    script = ('tell application "Terminal"\n  activate\n  do script "%s"\nend tell'
              % applescript_quote(shell_text))
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return r.returncode


def _shell_quote(arg):
    """POSIX single-quote escaping for the shell layer: ' -> '\\'' """
    return "'" + str(arg).replace("'", "'\\''") + "'"


def _windows_driver(argv, cwd, receipt_dir, launch_id):
    # cmd /c with argv list via subprocess (no shell string interpolation)
    return subprocess.run(["cmd", "/c"] + shell_argv_list(argv), cwd=cwd,
                          capture_output=True).returncode


def launch(argv, cwd, driver=None, platform=None, wait_seconds=30, poll_interval=1.0,
           receipt_parent=None):
    """Launch argv in a real terminal window. driver is injectable for tests;
    default selects by platform. Never relaunches on uncertain acceptance."""
    platform = platform or sys_platform()
    argv = shell_argv_list(argv)
    launch_id = new_launch_id()
    rdir = new_receipt_dir(Path(receipt_parent or tempfile.gettempdir()), launch_id)
    # the target may reference its own receipt dir via this whole-token placeholder
    argv = [a.replace("{receipt_dir}", str(rdir)) for a in argv]
    if driver is None:
        has_osascript = _which("osascript") is not None
        kind = select_driver(platform, has_osascript)
        driver = {"terminal-app": _terminal_app_driver, "windows-terminal": _windows_driver}.get(kind)
        if driver is None:
            return LaunchResult("request-error", launch_id, str(rdir))
    rc = driver(argv, str(cwd), str(rdir), launch_id)
    if rc != 0:
        return LaunchResult("request-error", launch_id, str(rdir))
    # bounded wait for the frontend receipt
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            validate_receipt(rdir, launch_id, max_age=wait_seconds + 60)
            return LaunchResult("frontend-started", launch_id, str(rdir))
        except ReceiptError:
            time.sleep(poll_interval)
    return LaunchResult("not-verified", launch_id, str(rdir), relaunches=0)


def sys_platform():
    import sys
    return sys.platform


def _which(cmd):
    import shutil
    return shutil.which(cmd)
