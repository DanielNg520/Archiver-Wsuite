"""
core.recorder_lock
──────────────────
Read side of the recorder's TikTok soft-lock, shared by every worker that
must NOT touch an in-progress recording.

The recorder writes a pid-stamped JSON heartbeat (recorder.lock) naming the
user it is currently capturing. Two sweepers scan the recordings tree while a
capture may be running — archiver.reconcile_recordings (periodic loop) and
recorder.startup_sweep (every recorder start, incl. crash-restarts) — and both
previously treated a growing recording as sweepable the moment it looked
stable. An ffmpeg reconnect stall makes a LIVE recording look stable for
seconds at a time, and the sweep then converts it and retires (deletes) the
original out from under the recording session (observed 2026-07-09: a manual
`recorder record` of @ntuan.297 lost its .flv to the archiver loop mid-stall —
"vanished before enqueue"). Gate on this module before sweeping a user dir.

Liveness comes from core.heartbeat (writer pid must be alive), so a recorder
SIGKILLed mid-recording never blocks sweeps forever.
"""

from __future__ import annotations

from . import heartbeat, paths


def live_recording_user() -> "tuple[bool, str | None]":
    """(lock_held, username). `lock_held` is True only while a LIVE recorder
    process holds the TikTok soft-lock. `username` is the user it is
    recording, or None if the lock predates the username stamp — callers must
    then treat EVERY user dir as potentially active (skip them all), because
    "unknown" must never degrade into "sweep the live recording"."""
    data = heartbeat.read_live(paths.tiktok_lock())
    if data is None:
        return (False, None)
    u = data.get("username")
    return (True, u if isinstance(u, str) and u else None)
