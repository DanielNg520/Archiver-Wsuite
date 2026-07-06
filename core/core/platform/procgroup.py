"""
core.platform.procgroup
────────────────────────
Spawn a child in its own process group and later kill the WHOLE group — the
child and every process it spawned — as one unit.

Why this is a data-integrity guard, not a nicety: the recorder runs yt-dlp,
which does the actual live download via a **child ffmpeg**. If we terminate only
the yt-dlp pid, that ffmpeg is orphaned; it keeps the recording file open and
writing, and a remux that then unlinks the source drains live footage into a
deleted inode — silent data loss, observed in prod. The invariant both backends
must uphold: **killing the group guarantees the child ffmpeg dies too.**

API — all operate on a ``subprocess.Popen`` (``proc``):

  popen_kwargs() -> dict     # spread into Popen(...) to make its own group
  terminate(proc) -> bool    # graceful stop of the whole group; False = unreachable
  kill(proc)      -> bool     # forceful kill of the whole tree;  False = unreachable

Mapping:

  POSIX    spawn  start_new_session=True (new session ⇒ new process group)
           term   SIGTERM to the group   (os.killpg(getpgid(pid), …))
           kill   SIGKILL to the group
  Windows  spawn  CREATE_NEW_PROCESS_GROUP
           term   CTRL_BREAK_EVENT to the group (only deliverable to a child in
                  its own group; lets ffmpeg flush and close the file cleanly)
           kill   taskkill /PID <pid> /T /F  (/T = whole descendant tree ⇒ the
                  child ffmpeg cannot survive; /F = force)

A False return means the group could not be signalled (already exited, not
permitted); the caller falls back to acting on the bare pid.
"""

from __future__ import annotations

import os
import signal
import subprocess

if os.name == "nt":                                   # ── Windows backend ──

    def popen_kwargs() -> dict:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    def terminate(proc: "subprocess.Popen | None") -> bool:
        # Ctrl-Break to the child's own group. Popen.send_signal maps
        # CTRL_BREAK_EVENT onto GenerateConsoleCtrlEvent for the group.
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
            return True
        except (OSError, ValueError):
            return False

    def kill(proc: "subprocess.Popen | None") -> bool:
        # Terminate the whole descendant tree so the child ffmpeg can't orphan.
        if proc is None:
            return False
        try:
            res = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
            )
            return res.returncode == 0
        except OSError:
            return False

else:                                                 # ── POSIX backend ──

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
