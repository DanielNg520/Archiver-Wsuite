"""commands
────
Telegram bot command parsing and CLI execution for archiver-suite.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import typing

_CMD_PATTERN = (
    r"^/\s*(record|ar_add|ar-add|re_add|re-add|remove|list|recstatus)"
    r"(?:@[a-zA-Z0-9_]+)?(?:\s+(.*))?$"
)
_CMD_RE = re.compile(_CMD_PATTERN, re.IGNORECASE)


_VALID_HANDLE_RE = re.compile(r"^[A-Za-z0-9_.]{1,64}$")


def parse_command(text: str) -> tuple[str, str | None] | None:
    match = _CMD_RE.match(text.strip())
    if not match:
        return None
    cmd, args = match.group(1).lower(), match.group(2)

    if cmd == "ar-add":
        cmd = "ar_add"
    elif cmd == "re-add":
        cmd = "re_add"

    if cmd in ("record", "ar_add", "re_add", "remove"):
        if not args:
            return None
        handle = args.strip()
        if handle.startswith("@"):
            handle = handle[1:]
        if not _VALID_HANDLE_RE.match(handle):
            return None
        return (cmd, handle)

    if cmd == "list":
        return ("list", None)
    if cmd == "recstatus":
        return ("status", None)

    return None


def run_cli(
    argv: list[str], timeout_s: float = 30.0
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return -1, f"command timed out after {timeout_s}s"
    except OSError as err:
        return -1, str(err)


def start_record(
    recorder_bin: str, handle: str
) -> tuple[subprocess.Popen, typing.IO[bytes]]:
    log = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(
            [recorder_bin, "record", "--user", handle],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return proc, log
    except BaseException:
        log.close()
        raise


def wait_record(
    proc: subprocess.Popen, log: typing.IO[bytes]
) -> tuple[int, str]:
    try:
        proc.wait()
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
        return proc.returncode, output
    finally:
        log.close()
