"""
core.notify
───────────
A small recorder→dispatcher event queue for one-off alert messages (e.g. "a
live recording finished") that are NOT media uploads. CLAUDE.md's "the
dispatcher is the ONLY Telegram sender" rule means these still have to route
through the dispatcher rather than a second bot/client — but they don't fit
the upload `items` table (no file, no platform/user chat routing; they go to
one fixed admin chat regardless of which platform/user they're about).

Shape: one small JSON file per pending notification under
core.paths.notify_outbox_dir() — atomic write (tmp + os.replace, same
contract as core.heartbeat.write_atomic) so a reader never sees a
half-written file, and one file per event so the producer (recorder) and
consumer (dispatcher) never have to coordinate an append/truncate race the
way a single shared log file would need. The dispatcher's poll loop lists the
directory, sends each file's text, and calls ack() to delete it on success;
anything that fails to send is simply left in place and retried next cycle.

CONTRACT: queue() never raises — a notification must never break the
recording/upload it's reporting on (same swallow-on-failure contract as
core.heartbeat).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from . import paths as _paths


def _outbox_dir() -> Path:
    d = _paths.notify_outbox_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def queue_event(kind: str, text: str, **fields) -> None:
    """Queue a plain-text notification for the dispatcher to deliver.
    `kind` is a short machine tag (e.g. "recorder_done"); `text` is the
    ready-to-send message body; any extra `fields` are carried along for a
    consumer that wants structured detail instead of just the text."""
    d = _outbox_dir()
    name = f"{time.time():.6f}-{uuid.uuid4().hex[:8]}.json"
    payload = {"kind": kind, "text": text, "queued_at": time.time(), **fields}
    try:
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, d / name)
    except OSError:
        pass


def drain_events() -> "list[tuple[Path, dict]]":
    """Pending notifications, oldest first, as (path, payload) pairs. A
    malformed file is skipped (left in place for manual inspection) rather
    than raising. Caller acks each path after a successful send."""
    d = _outbox_dir()
    out: "list[tuple[Path, dict]]" = []
    try:
        names = sorted(p for p in d.iterdir() if p.suffix == ".json")
    except OSError:
        return out
    for p in names:
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return out


def ack_event(path: Path) -> None:
    """Remove a delivered notification. A delivery that can't clean up its
    own file just gets resent next cycle — not a correctness bug, so errors
    are swallowed here too."""
    try:
        path.unlink()
    except OSError:
        pass
