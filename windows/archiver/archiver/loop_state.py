"""
archiver.loop_state
───────────────────
Cross-process phase heartbeat for `archiver loop`. The loop writes a tiny JSON
file at each phase transition — "running" while a scan cycle executes, and
"sleeping" between cycles (carrying the wake time) — so `ops health` /
`ops watch` can show whether the archiver is actively working or resting
between loops, instead of just "process alive".

A FILE, not the DB — mirrors dispatcher.progress: the phase is ephemeral
status, ops deliberately reads only on-disk artifacts (it imports no worker
package), and hammering the shared SQLite for a status line would be backwards.
Validity is gated on the writer pid being alive, so a crashed loop can never
leave a lying "running"/"sleeping" status behind. The path is fixed (not
derived from config) so the standalone ops reader can find it without importing
the archiver.

Concurrency: a run now scans several platforms at once (see
Orchestrator.max_concurrent_platforms), each announcing its current user
through the same `on_user` hook. A single overwrite would make the status flip
between platforms and hide the concurrency. So the "running" heartbeat carries
a `scans` LIST — one (platform, user, since) entry per platform actively
scanning — maintained in an in-process registry that scan_start/scan_done keep
current. Legacy single `platform`/`user` fields mirror the most-recent scan so
an older ops reader still shows something sane.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from core import heartbeat, paths

DEFAULT_PATH = paths.archiver_loop()
STORIES_PATH = paths.archiver_stories()

_PHASES = ("running", "sleeping")

# In-process registry of platforms currently scanning: platform -> {user, since}.
# The heavy run drives platforms concurrently via asyncio (single thread), but
# the stories sweeper writes its own file from a separate thread — the lock is
# cheap insurance so the two never race this dict. At most one user per platform
# is active at a time (a platform walks its users sequentially), so `platform`
# is the natural key.
_active: "dict[str, dict]" = {}
_active_lock = threading.Lock()


def write_running(run_n: int, *, platform: str | None = None,
                  user: str | None = None, path: Path = DEFAULT_PATH) -> None:
    """Mark a scan cycle as in progress.

    Called once generically at cycle start with no platform/user (the pre/post
    phases — reconcile, ingest — and as a registry RESET for the new cycle), and
    again per user via the `on_user` hook as each platform reaches a new user.
    Per-user calls register that platform's current scan so the heartbeat names
    every concurrent scan, not just the last one to write."""
    now = time.time()
    with _active_lock:
        if platform is None:
            # Cycle start / pre-post phase: clear the previous cycle's scans so
            # a finished platform can't linger into the next run.
            _active.clear()
        else:
            _active[platform] = {"platform": platform, "user": user,
                                 "since": now}
        _emit_running(run_n, now, path)


def scan_done(run_n: int, platform: str, *, path: Path = DEFAULT_PATH) -> None:
    """Drop a platform from the active registry once it finishes all its users,
    so a completed platform stops reading as 'still scanning' while slower
    platforms in the same run keep going."""
    now = time.time()
    with _active_lock:
        _active.pop(platform, None)
        _emit_running(run_n, now, path)


def _emit_running(run_n: int, now: float, path: Path) -> None:
    """Write the 'running' heartbeat from the current registry. Caller holds
    _active_lock."""
    state = {"pid": os.getpid(), "phase": "running", "run_n": run_n,
             "since": now, "updated_at": now}
    scans = list(_active.values())
    if scans:
        state["scans"] = scans
        # Legacy single-scan mirror (most recently started) for old ops readers.
        latest = max(scans, key=lambda s: s.get("since", 0.0))
        state["platform"] = latest["platform"]
        state["user"] = latest["user"]
    _write(state, path)


def write_sleeping(run_n: int, wake_at: float, *,
                   path: Path = DEFAULT_PATH) -> None:
    """Mark the loop as resting until `wake_at` (epoch secs)."""
    now = time.time()
    with _active_lock:
        _active.clear()   # nothing is scanning while we sleep
    _write({"pid": os.getpid(), "phase": "sleeping", "run_n": run_n,
            "since": now, "wake_at": wake_at, "updated_at": now}, path)


def clear(path: Path = DEFAULT_PATH) -> None:
    """Remove the heartbeat — call when the loop exits, so a stopped loop
    doesn't read back as forever 'sleeping' (belt-and-suspenders with the
    pid-liveness check on the reader)."""
    with _active_lock:
        _active.clear()
    heartbeat.clear(path)


# ── stories fast-lane heartbeat ─────────────────────────────────────────────
# The Instagram stories sweeper runs on its OWN cadence in a separate daemon
# thread, so it gets its own file — a stories pass and a heavy scan can both be
# 'running' at once. The reader (ops.health) treats an absent/stale file as
# "lane idle"; the pid-liveness gate voids it if the loop process dies.

def write_stories(run_n: int, *, user: str | None = None,
                  path: Path = STORIES_PATH) -> None:
    """Mark a stories pass as in progress; `user` names the account currently
    being fetched (None between accounts / during health check)."""
    now = time.time()
    state = {"pid": os.getpid(), "phase": "running", "run_n": run_n,
             "since": now, "updated_at": now}
    if user:
        state["user"] = user
    _write(state, path)


def stories_idle(run_n: int, wake_at: float, *,
                 last_new: int | None = None,
                 path: Path = STORIES_PATH) -> None:
    """Mark the stories lane as resting until its next pass at `wake_at`,
    optionally carrying how many new story files the last pass pulled."""
    now = time.time()
    state = {"pid": os.getpid(), "phase": "sleeping", "run_n": run_n,
             "since": now, "wake_at": wake_at, "updated_at": now}
    if last_new is not None:
        state["last_new"] = last_new
    _write(state, path)


def clear_stories(path: Path = STORIES_PATH) -> None:
    """Remove the stories heartbeat when the sweeper exits."""
    heartbeat.clear(path)


def _write(state: dict, path: Path) -> None:
    heartbeat.write_atomic(path, state)


def read(path: Path = DEFAULT_PATH) -> dict | None:
    """Current loop phase, or None if absent / malformed / writer gone."""
    return heartbeat.read_live(path, validate=lambda d: d.get("phase") in _PHASES)


def read_stories(path: Path = STORIES_PATH) -> dict | None:
    """Current stories-lane phase, or None if idle-absent / malformed / gone."""
    return heartbeat.read_live(path, validate=lambda d: d.get("phase") in _PHASES)
