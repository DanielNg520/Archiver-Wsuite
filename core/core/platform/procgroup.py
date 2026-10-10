"""
core.platform.procgroup
────────────────────────
Spawn a child in its own process group and later kill the WHOLE group — the
child and every process it spawned — as one unit.

Why this is a data-integrity guard, not a nicety: the recorder runs yt-dlp,
which does the actual live download via a **child ffmpeg**. If we terminate only
the yt-dlp pid, that ffmpeg is orphaned; it keeps the recording file open and
writing, and a remux that then unlinks the source drains live footage into a
deleted inode — silent data loss, observed in prod. The invariant the POSIX
implementation must uphold: **killing the group guarantees the child ffmpeg dies too.**

API — all operate on a ``subprocess.Popen`` (``proc``):

  popen_kwargs() -> dict     # spread into Popen(...) to make its own group
  terminate(proc) -> bool    # graceful stop of the whole group; False = unreachable
  kill(proc)      -> bool     # forceful kill of the whole tree;  False = unreachable
  terminate_pid(pid) -> bool # stop a worker known only by pid (recorder stop)

Mapping:

  POSIX    spawn  start_new_session=True (new session ⇒ new process group)
           term   SIGTERM to the group   (os.killpg(getpgid(pid), …))
           kill   SIGKILL to the group

A False return means the group could not be signalled (already exited, not
permitted); the caller falls back to acting on the bare pid.
"""

from __future__ import annotations

import os
import signal
import subprocess

def popen_kwargs() -> dict:
    return {"start_new_session": True}


def _signal_group(proc: "subprocess.Popen | None", sig: int) -> bool:
    if proc is None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def terminate(proc: "subprocess.Popen | None") -> bool:
    return _signal_group(proc, signal.SIGTERM)


def kill(proc: "subprocess.Popen | None") -> bool:
    return _signal_group(proc, signal.SIGKILL)


def terminate_pid(pid: int) -> bool:
    # SIGTERM the recorder itself; its own handler does the graceful stop
    # (which group-kills the capture). ProcessLookupError ⇒ already gone.
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        return False
