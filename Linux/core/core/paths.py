"""
core.paths
──────────
The single source of truth for the suite's shared ON-DISK layout — the files
that are a CROSS-PROCESS CONTRACT: one worker writes them, another reads them.
These locations used to be hard-coded independently in each worker (and again in
ops), so the writer and the reader could silently drift apart. Defining them
once here makes that drift impossible — the same reasoning that already keeps the
DB path in core.schema.

What lives here (writer → reader):
  • tiktok_lock        recorder.lock  →  archiver.lock_reader, ops.health
  • dispatcher_progress dispatcher.progress (writer+reader) → ops.health
  • archiver_loop       archiver.loop_state (writer+reader) → ops.health
  • recorder_pid        recorder.cli  →  ops.health   (DEFAULT; the recorder may
                        relocate its state dir, which only it and ops-via-default
                        can know — so this is the agreed default, not a guarantee)

Functions (not constants) so the value reflects $HOME at call time and stays
consistent with core.schema.db_path()'s style. The DB path itself stays in
core.schema (db_path); import it from there.
"""

from __future__ import annotations

from pathlib import Path

from core.platform import paths as _osp


def locks_dir() -> Path:
    """Directory holding the suite's cross-process lock files."""
    return _osp.locks_dir()


def tiktok_lock() -> Path:
    """Recorder's TikTok soft-lock: present ⇒ a live capture is in flight, so the
    archiver skips its TikTok download step. Written by recorder.lock; read by
    archiver.lock_reader and ops.health."""
    return locks_dir() / "tiktok.lock"


def dispatcher_progress() -> Path:
    """Dispatcher's upload-progress heartbeat (see core.heartbeat)."""
    return _osp.config_dir(_osp.DISPATCHER) / "progress.json"


def archiver_loop() -> Path:
    """Archiver loop's phase heartbeat (running/sleeping; see core.heartbeat)."""
    return _osp.config_dir(_osp.ARCHIVER) / "loop.json"


def archiver_stories() -> Path:
    """Archiver stories-lane heartbeat. The Instagram stories fast-lane runs as
    its own daemon on a tight cadence, independent of the heavy loop, so it gets
    its OWN heartbeat file (written by archiver.loop_state; read by ops.health).
    Separate from loop.json so a stories pass and a heavy scan can both be
    'running' at once without clobbering each other's status."""
    return _osp.config_dir(_osp.ARCHIVER) / "stories.json"


def dispatcher_stop_flag() -> Path:
    """Cooperative 'finish the current batch, then exit cleanly' flag.
    Written by `ops update` before it reinstalls the packages; read by the
    dispatcher drain loop, which returns cleanly BETWEEN batches when it appears
    (never mid-upload). `ops update` removes it again before reloading the
    workers, so a freshly started dispatcher never sees a stale flag."""
    return locks_dir() / "dispatcher.stop"


def recorder_pid() -> Path:
    """Default recorder pid file. The recorder writes it under its configured
    state dir (default ~/.recorder); ops reads this default location."""
    return Path("~/.recorder/pid").expanduser()
