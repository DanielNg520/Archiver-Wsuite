"""
core.platform.filelock
───────────────────────
Advisory whole-file locking for Linux (systemd) and macOS (launchd).

The suite's singleton locks (core.InstanceLock, the dispatcher session lock) and
the per-file media-prep lock all depend on ONE guarantee: **the kernel releases
the lock automatically when the holder exits or crashes** — even on SIGKILL or
power loss — so there is never a stale lock to clean up and never a PID-liveness
heuristic in the hot path. The fcntl backend preserves exactly that guarantee:

  fcntl  → ``fcntl.flock`` (whole-file BSD lock; freed on close/exit)

API — all operate on an open file object (``handle``) and are non-blocking:

  try_acquire_exclusive(handle) -> bool   # True = we now hold it exclusively
  try_acquire_shared(handle)    -> bool   # True = we hold a shared/read lock
  release(handle)               -> None
"""

from __future__ import annotations

import fcntl


def try_acquire_exclusive(handle) -> bool:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:            # includes BlockingIOError (held elsewhere)
        return False


def try_acquire_shared(handle) -> bool:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def release(handle) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
