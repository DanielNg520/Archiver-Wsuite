"""
dispatcher.progress
───────────────────
Cross-process upload progress. The drain's send strategy writes a tiny JSON
heartbeat (atomic tmp+rename, throttled to ~1/s) while Telethon uploads;
`dispatcher status` and `ops health` read it from their own processes.

A FILE, not the DB: progress is ephemeral telemetry that changes every
second — hammering the shared SQLite (and its writers' lock) for a status
line would be backwards. The file is small, atomic to read, and self-expires:
a reader treats it as absent when the heartbeat is stale or the writer pid
is gone, so a crashed dispatcher can never leave a lying status behind.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from core import heartbeat, paths

DEFAULT_PATH = paths.dispatcher_progress()

# Heartbeat older than this is a dead upload (writer throttles to 1s, so a
# live upload refreshes far more often even on a crawling link).
STALE_AFTER_S = 30.0


class ProgressReporter:
    """Writer side. One instance per drain run, reused across sends."""

    def __init__(self, path: Path = DEFAULT_PATH, min_interval_s: float = 1.0):
        self.path = path
        self._min_interval_s = min_interval_s
        self._last_write = 0.0

    def callback(self, file_path: str, *,
                 batch_pos: int | None = None,
                 batch_total: int | None = None):
        """A Telethon progress_callback(sent, total) for one file upload.
        batch_pos/batch_total give album context ('file 3/10').

        UNIT of (sent, total): single sends and the fast album's per-item
        uploads report BYTES. But Telethon's NATIVE list send — the album-level
        callback we attach for photo/mixed/native-video/document albums
        (batch_pos is None, batch_total > 1) — reports (files_completed,
        total_files), NOT bytes. Tag the heartbeat so readers render '3/9 files'
        instead of the byte formatter turning it into a nonsense '3B/9B · 0 B/s'.
        """
        started_at = time.time()
        unit = ("files"
                if batch_pos is None and batch_total and batch_total > 1
                else "bytes")

        def _cb(sent: int, total: int) -> None:
            now = time.time()
            # Always record the final tick so 100% is never skipped.
            if sent < total and now - self._last_write < self._min_interval_s:
                return
            self._last_write = now
            self._write({
                "pid":         os.getpid(),
                "file":        file_path,
                "sent":        int(sent),
                "total":       int(total),
                "unit":        unit,
                "batch_pos":   batch_pos,
                "batch_total": batch_total,
                "started_at":  started_at,
                "updated_at":  now,
            })

        return _cb

    def clear(self) -> None:
        """Remove the heartbeat — call when a send finishes either way."""
        heartbeat.clear(self.path)

    def _write(self, state: dict) -> None:
        heartbeat.write_atomic(self.path, state)


# ── reader side ────────────────────────────────────────────────────────────

def read_progress(path: Path = DEFAULT_PATH) -> dict | None:
    """Current upload state, or None if idle / stale / writer gone."""
    return heartbeat.read_live(
        path, stale_after_s=STALE_AFTER_S,
        validate=lambda d: "sent" in d and "total" in d,
    )


def _human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _human_secs(s: float) -> str:
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def describe(p: dict) -> str:
    """'name.mp4 [file 3/10]  37% (52.3MB/140.8MB, 890.1KB/s, ETA 1m40s)'

    A native-album heartbeat (unit='files') counts completed files, not bytes,
    so it renders '3/9 files' with a file-paced ETA instead of the byte form."""
    name = Path(p["file"]).name
    sent, total = p["sent"], p["total"]
    pct = (100 * sent / total) if total else 0
    files = p.get("unit") == "files"
    # Sub-second elapsed yields absurd rates ("25TB/s") on the first tick;
    # wait a full second of signal before showing rate/ETA.
    elapsed = p.get("updated_at", 0) - p.get("started_at", 0)
    if files:
        parts = []
        if elapsed >= 1.0 and sent > 0 and total > sent:
            rate = sent / elapsed  # files/s
            if rate > 0:
                parts.append(f"ETA {_human_secs((total - sent) / rate)}")
    else:
        parts = [f"{_human_bytes(sent)}/{_human_bytes(total)}"]
        if elapsed >= 1.0 and sent > 0:
            rate = sent / elapsed
            parts.append(f"{_human_bytes(rate)}/s")
            if rate > 0 and total >= sent:
                parts.append(f"ETA {_human_secs((total - sent) / rate)}")
    batch = _batch_tag(p)
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{name}{batch}  {pct:.0f}%{detail}"


def _batch_tag(p: dict) -> str:
    """Album context tag. A native-album heartbeat (unit='files') counts
    COMPLETED files, so the in-flight item is sent+1 — surface it as
    '[file 4/9]' instead of a positionless '[album of 9]'. The fast album's
    per-item callback carries an explicit batch_pos; a single send has none."""
    total = p.get("batch_total")
    if not total or total <= 1:
        return ""
    if p.get("unit") == "files":
        pos = min(p["sent"] + 1, total) if p.get("sent", 0) < total else total
        return f" [file {pos}/{total}]"
    if p.get("batch_pos"):
        return f" [file {p['batch_pos']}/{total}]"
    return f" [album of {total}]"
