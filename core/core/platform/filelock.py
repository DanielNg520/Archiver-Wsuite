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
at a fixed high offset (NOT offset 0): the Windows lock is *mandatory*, so a
locked byte is unreadable, and offset 0 holds the holder PID that holder_pid()
must read back — locking high leaves that readable. A whole-file POSIX flock and
a 1-byte Windows lock give identical mutual exclusion because every participant
locks the same byte.
"""

from __future__ import annotations

import os

if os.name == "nt":                                   # ── Windows backend ──
    import msvcrt

    # Lock one byte at a FIXED HIGH OFFSET, not offset 0. msvcrt.locking() creates
    # a *mandatory* byte-range lock (unlike POSIX fcntl.flock, which is advisory):
    # a locked byte becomes unreadable to every handle, including the holder's own.
    # The lock file's body is the holder's PID (written at offset 0) which the
    # holder_pid() diagnostic must be able to read back. So we lock a byte far past
    # any PID text (1 GiB in, past EOF — msvcrt allows locking beyond EOF), leaving
    # the PID region readable while still giving full mutual exclusion: every
    # participant locks the SAME byte, so exactly one can hold it at a time.
    _NBYTES = 1
    _LOCK_OFFSET = 1 << 30

    def _win_lock(handle, mode) -> bool:
        fd = handle.fileno()
        saved = os.lseek(fd, 0, os.SEEK_CUR)      # preserve the fd position
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, mode, _NBYTES)
            return True
        except OSError:
            return False
        finally:
            os.lseek(fd, saved, os.SEEK_SET)

    def try_acquire_exclusive(handle) -> bool:
        return _win_lock(handle, msvcrt.LK_NBLCK)

    def try_acquire_shared(handle) -> bool:
        # No shared mode on Windows; a non-blocking exclusive attempt answers the
        # only question the shared probe asks ("is it lockable right now?").
        return _win_lock(handle, msvcrt.LK_NBLCK)

    def release(handle) -> None:
        fd = handle.fileno()
        saved = os.lseek(fd, 0, os.SEEK_CUR)
        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _NBYTES)
        except OSError:
            pass
        finally:
            os.lseek(fd, saved, os.SEEK_SET)

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
