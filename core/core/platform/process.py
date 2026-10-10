"""
core.platform.process
──────────────────────
Process-liveness probe and process inspection helpers.

``os.kill(pid, 0)`` is the POSIX idiom for "does this process exist?":
ProcessLookupError ⇒ dead, PermissionError ⇒ alive-but-another-user.

  pid_alive(pid) -> bool

Also here (used by ops health):

  proc_stats(pid) -> str | None            # "up 1:10:15, cpu 10.6%, mem 110MB"
  find_worker_pid(command, action) -> int  # locate a worker by its argv
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # exists, owned by another user
    except (OSError, ValueError, TypeError):
        return False
    return True


# ── process inspection (ops health) ────────────────────────────────────────

def proc_stats(pid: int) -> "str | None":
    try:
        out = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "etime=,%cpu=,rss="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    fields = out.stdout.split()
    if out.returncode != 0 or len(fields) != 3:
        return None
    etime, pcpu, rss_kb = fields
    try:
        mem_mb = int(rss_kb) // 1024
    except ValueError:
        return None
    return f"up {etime}, cpu {pcpu}%, mem {mem_mb}MB"


def find_worker_pid(command: str, action: str) -> "int | None":
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            pid = int(fields[0])
            argv = shlex.split(fields[1])
        except (ValueError, IndexError):
            continue
        for index, token in enumerate(argv[:-1]):
            if Path(token).name == command and argv[index + 1] == action:
                return pid
    return None
