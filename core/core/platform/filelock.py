"""
core.platform.filelock
───────────────────────
Advisory whole-file locking, portable across POSIX and Windows.

The suite's singleton locks (core.InstanceLock, the dispatcher session lock) and
the per-file media-prep lock all depend on ONE guarantee: **the kernel releases
the lock automatically when the holder exits or crashes** — even on SIGKILL or
power loss — so there is never a stale lock to clean up and never a PID-liveness
heuristic in the hot path. Both backends below preserve exactly that guarantee:

  POSIX    → ``fcntl.flock`` (whole-file BSD lock; freed on close/exit)
  Windows  → ``msvcrt.locking`` (mandatory byte-range lock on the fd; the OS
             releases every lock on the fd when the process ends)

API — all operate on an open file object (``handle``) and are non-blocking:

  try_acquire_exclusive(handle) -> bool   # True = we now hold it exclusively
  try_acquire_shared(handle)    -> bool   # True = we hold a shared/read lock
  release(handle)               -> None

Semantic note on the Windows backend: ``msvcrt`` has no shared-lock mode, so
``try_acquire_shared`` degrades to a non-blocking exclusive attempt. That is
correct for the only caller that uses it — the diagnostic ``holder_pid`` probe,
which only needs to answer "can this be locked right now?" (success → nobody
holds it, release immediately; failure → a live holder). It locks a single byte
at offset 0; a whole-file POSIX flock and a 1-byte Windows lock give identical
mutual exclusion because every participant locks the same byte.
"""

from __future__ import annotations

import os

if os.name == "nt":                                   # ── Windows backend ──
    import msvcrt

    # Lock one byte at offset 0. msvcrt.locking() locks `nbytes` from the current
    # file position, and may lock a region past EOF (fine for an empty lock file).
    _NBYTES = 1

    def _win_lock(handle, mode) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), mode, _NBYTES)
            return True
        except OSError:
            return False

    def try_acquire_exclusive(handle) -> bool:
        return _win_lock(handle, msvcrt.LK_NBLCK)

    def try_acquire_shared(handle) -> bool:
        # No shared mode on Windows; a non-blocking exclusive attempt answers the
        # only question the shared probe asks ("is it lockable right now?").
        return _win_lock(handle, msvcrt.LK_NBLCK)

    def release(handle) -> None:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _NBYTES)
        except OSError:
            pass

else:                                                 # ── POSIX backend ──
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
