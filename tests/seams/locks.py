"""Seams: TikTok soft-lock, dispatcher instance lock, live-recording protection."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from ._harness import _db_file, _dead_pid, _fresh_db, _write_media, ok, section


# ══════════════════════════════════════════════════════════════════════════════
# Seam 1 — the TikTok soft-lock: recorder writes, archiver reads
# ══════════════════════════════════════════════════════════════════════════════

def test_lock_seam(tmp: Path) -> None:
    section("Seam 1: recorder.lock ←→ archiver.lock_reader")
    import archiver.lock_reader as lr
    from recorder.lock import TikTokLock

    lock_path = tmp / "locks" / "tiktok.lock"
    # Point the reader at the same path the writer will use (the production
    # contract is a shared absolute path; here we redirect both to tmp).
    orig = lr.LOCK_PATH
    lr.LOCK_PATH = lock_path
    try:
        ok(not lr.tiktok_lock_held(), "no lock initially → archiver downloads")
        # Default pid = this live test process, so the lock reads as held.
        with TikTokLock(str(lock_path)):
            ok(lock_path.exists(), "recorder __enter__ wrote the lockfile")
            ok(lr.tiktok_lock_held(), "archiver SEES the lock while recording")
        ok(not lr.tiktok_lock_held(), "recorder __exit__ removed the lock")
        # Stale lock (recorder SIGKILLed without cleanup): file persists but its
        # pid is dead → the reader's liveness gate SELF-HEALS it to not-held, so
        # TikTok archiving resumes instead of starving forever.
        lock_path.write_text(f'{{"pid": {_dead_pid()}}}')
        ok(not lr.tiktok_lock_held(),
           "stale lock (dead writer pid) self-heals to not-held")
        # A lock owned by a LIVE process still blocks (no false resume).
        lock_path.write_text(f'{{"pid": {os.getpid()}}}')
        ok(lr.tiktok_lock_held(), "lock with a live writer pid still reads held")
    finally:
        lr.LOCK_PATH = orig

def test_dispatcher_instance_lock_seam(tmp: Path) -> None:
    section("Seam 14: one dispatcher owns each Telethon session")
    from dispatcher.instance_lock import DispatcherInstanceLock

    session = str(tmp / "telegram-session")
    # The child prints its own os.getpid(), which is the pid the lock file
    # records — assert against that.
    code = (
        "import os,sys,time;"
        "sys.path.insert(0,'dispatcher');"
        "from dispatcher.instance_lock import DispatcherInstanceLock;"
        f"lock=DispatcherInstanceLock({session!r});"
        "lock.__enter__();print(f'locked {os.getpid()}',flush=True);time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        first = child.stdout.readline().split()
        ok(first and first[0] == "locked",
           "first dispatcher process acquires the session lock")
        holder = int(first[1])
        probe = DispatcherInstanceLock(session)
        ok(probe.holder_pid() == holder,
           "holder_pid() probe names the owning process")
        err = ""
        try:
            with DispatcherInstanceLock(session):
                acquired = True
        except RuntimeError as e:
            acquired, err = False, str(e)
        ok(not acquired, "second dispatcher process is rejected")
        ok(str(holder) in err,
           "rejection message names the holding pid (diagnosable, not opaque)")
    finally:
        child.terminate()
        child.wait(timeout=5)

    # The kernel frees the lock when the holder dies, but process teardown is
    # asynchronous — poll briefly instead of asserting on a race.
    deadline = time.time() + 5
    while (DispatcherInstanceLock(session).holder_pid() is not None
           and time.time() < deadline):
        time.sleep(0.1)
    ok(DispatcherInstanceLock(session).holder_pid() is None,
       "holder_pid() reports no owner once the process is gone")
    with DispatcherInstanceLock(session):
        ok(True, "lock is recoverable after the owner exits")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 16 — instance lock is CWD-independent. A bare session name must resolve
# to the SAME lock file no matter where the process was started (launchd CWD=/
# vs manual CWD=~ previously took two different locks and both ran).
# ══════════════════════════════════════════════════════════════════════════════

def test_lock_cwd_independence_seam(tmp: Path) -> None:
    section("Seam 16: instance lock path is CWD-independent")
    from dispatcher.instance_lock import DispatcherInstanceLock

    tmp.mkdir(parents=True, exist_ok=True)
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        lock_a = DispatcherInstanceLock("bare-session-name")
        os.chdir("/")
        lock_b = DispatcherInstanceLock("bare-session-name")
    finally:
        os.chdir(cwd)
    ok(lock_a.path == lock_b.path and lock_a.path.is_absolute(),
       "bare session name → one absolute lock path from any CWD")

    abs_session = tmp / "explicit" / "session"
    lock_c = DispatcherInstanceLock(str(abs_session))
    ok(lock_c.path.parent == abs_session.parent,
       "path-style session name keeps the lock beside the session file")

def test_live_recording_protection_seam(tmp: Path) -> None:
    section("Seam 32: sweepers skip the user a live recorder is recording")
    from recorder.lock import TikTokLock
    from recorder import startup_sweep
    from archiver.reconcile import reconcile_recordings
    from core import ingest, paths as core_paths
    from core.media_prep import PrepResult

    root = tmp / "records"
    active = _write_media(root / "alice" / "alice_1700.mp4", b"LIVE-RECORDING")
    active_log = root / "alice" / "alice_1700_ytdlp.log"
    active_log.write_text("live capture log")
    other = _write_media(root / "bob" / "bob_1600.mp4", b"FINISHED-RECORDING")
    old = time.time() - 3600
    for f in (active, other):
        os.utime(f, (old, old))

    db = _fresh_db()
    lock_path = tmp / "locks" / "tiktok.lock"
    orig_lockfn = core_paths.tiktok_lock
    orig_prep = ingest.media_prep.prepare
    core_paths.tiktok_lock = lambda: lock_path          # type: ignore
    ingest.media_prep.prepare = (                        # type: ignore
        lambda p, split_threshold_bytes=None: PrepResult.passthrough(p))
    try:
        lock = TikTokLock(str(lock_path))
        lock.username = "alice"
        with lock:
            reports = reconcile_recordings(db, root)
            names = {r.username for r in reports}
            ok("alice" not in names,
               "reconcile SKIPS the actively-recorded user's dir")
            ok("bob" in names, "reconcile still processes other users")
            ok(db.id_of(str(active)) is None,
               "no row registered for the live recording")
            ok(db.id_of(str(other)) is not None,
               "the finished recording DID get a row")

            startup_sweep.sweep(str(root), _db_file(db))
            ok(active.exists() and active_log.exists(),
               "startup sweep leaves the live recording AND its log alone")
            ok(db.id_of(str(active)) is None,
               "sweep registered nothing from the protected dir")
        # Lock released → the next reconcile picks alice up again.
        reconcile_recordings(db, root)
        ok(db.id_of(str(active)) is not None,
           "after the lock clears, the recording IS reconciled")
    finally:
        core_paths.tiktok_lock = orig_lockfn             # type: ignore
        ingest.media_prep.prepare = orig_prep            # type: ignore
        db.close()
