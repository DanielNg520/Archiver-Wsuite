"""Seams: shared harness (ok/section counters, temp DB/media helpers, fake sender, drain runner)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path


# ── tiny test harness ─────────────────────────────────────────────────────────

_checks = 0

def ok(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"✗ {label}")
    _checks += 1
    print(f"✓ {label}")

def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 50 - len(title)))

def _write_media(path: Path, payload: bytes) -> Path:
    """Write a >=200-byte 'media' file (stability + min-size gates both pass)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload + b"\0" * max(0, 256 - len(payload)))
    return path

def _fresh_db() -> "object":
    from core import ItemStore
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return ItemStore.open(p)

def _dead_pid() -> int:
    """A pid guaranteed not to be alive right now (for stale-heartbeat tests).
    Uses the suite's portable liveness primitive."""
    from core.platform import process as _process
    p = 999_999
    while _process.pid_alive(p):
        p += 1
    return p

def _db_file(store) -> str:
    """Pull the on-disk path out of an ItemStore's connection (test helper)."""
    row = store.conn.execute("PRAGMA database_list").fetchone()
    return row["file"]

class _FakeSend:
    """A SendStrategy stand-in: records calls, always succeeds. Lets us drive
    dispatcher.drain.drain_forever end-to-end with zero network. Captures the
    caption and topic_id of every send so seams that assert on the destination
    forum-topic (Seam 22) or the sanitized caption (Seam 24) can read them back."""
    def __init__(self):
        self.sent_singles: list[str] = []
        self.sent_albums: list[list[str]] = []
        self.sent_ensure_streamable: list[bool] = []
        self.single_captions: list[str] = []
        self.album_captions: list[str] = []
        self.single_topics: list[int | None] = []
        self.album_topics: list[int | None] = []
        self.album_as_documents: list[bool] = []

    async def send(self, *, peer, file_path, caption, ensure_streamable=True,
                   filetype_tag=False, topic_id=None):
        from dispatcher.send import SendResult
        self.sent_singles.append(file_path)
        self.sent_ensure_streamable.append(ensure_streamable)
        self.single_captions.append(caption)
        self.single_topics.append(topic_id)
        return SendResult(ok=True)

    async def send_album(self, *, peer, file_paths, caption, topic_id=None, as_documents=False):
        from dispatcher.send import SendResult
        self.sent_albums.append(list(file_paths))
        self.album_captions.append(caption)
        self.album_topics.append(topic_id)
        self.album_as_documents.append(as_documents)
        return SendResult(ok=True)

def _ffmpeg_present() -> bool:
    from shutil import which
    return which("ffmpeg") is not None and which("ffprobe") is not None

def _make_video(path: Path, *, container: str) -> Path:
    """A real, tiny H.264/AAC clip in the requested container. Codecs are always
    Telegram-friendly, so streamability is decided purely by the container —
    .mp4 streams inline, .flv does not (forcing the remux path)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True)
    return path

# ══════════════════════════════════════════════════════════════════════════════
# Shared drain runner for the destination/grouping seams below (22–24). Drives
# the real drain_forever to quiescence against a _FakeSend, same pattern as
# Seam 10/21 but factored out so each new seam reads as just its setup+asserts.
# ══════════════════════════════════════════════════════════════════════════════

def _drain_once(db_file: str, ps, fake: "_FakeSend", *,
                default_chat_id: str, sanitizer=None) -> None:
    from core import (ItemStore, DeletePolicy, RecorderDeletePolicy,
                      BatchPolicy, DeletionGuard)
    from dispatcher.drain import drain_forever
    from dispatcher.config import DispatcherConfig
    from dispatcher.tg_router import TelegramRouter

    cfg_kwargs = dict(
        telegram=None, default_chat_id=default_chat_id, db_path=db_file,
        policy_store=ps, poll_interval_s=0.01, max_retries=3,
        inter_album_sleep=0.0, stuck_claim_min=10, failed_retention_days=0,
    )
    if sanitizer is not None:
        cfg_kwargs["sanitizer"] = sanitizer
    cfg = DispatcherConfig(**cfg_kwargs)
    store = ItemStore.open(db_file)
    router = TelegramRouter(default_chat_id=default_chat_id)
    stop = asyncio.Event()

    async def _run():
        task = asyncio.create_task(drain_forever(
            cfg, store, fake, router,
            DeletePolicy(ps), RecorderDeletePolicy(ps), BatchPolicy(ps),
            DeletionGuard(ps), stop_event=stop,
        ))
        for _ in range(400):
            await asyncio.sleep(0.01)
            c = store.counts_by_status()
            if c.get("pending", 0) == 0 and c.get("sending", 0) == 0:
                break
        stop.set()
        await task

    try:
        asyncio.run(_run())
    finally:
        store.close()

def _reset_config() -> None:
    """Point ARCHIVER_SUITE_CONFIG at a brand-new empty file."""
    fd, path = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    os.environ["ARCHIVER_SUITE_CONFIG"] = path
