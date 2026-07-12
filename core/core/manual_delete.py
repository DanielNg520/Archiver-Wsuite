"""
core.manual_delete
──────────────────
Deferred-trash sweeper behind `archiver delete` — the MANUAL, terminal user
deletion, distinct from the auto-ban quarantine (core.quarantine, which moves
to an in-archive .deleted/ and is reversible via `unban`).

Lifecycle (one entry in the PolicyStore deletion roster drives all three):
  1. request   — `archiver delete` marks the roster + drops the user from the
                 active list. NO files, NO rows touched yet.
  2. trash     — each sweep, a roster user with every DB row `sent` has their
                 folder sent to the WINDOWS RECYCLE BIN (send2trash) and
                 `trashed_at` stamped. Any pending/sending/failed row defers
                 to the next cycle: un-uploaded work is never dropped.
  3. row GC    — RETENTION_DAYS after trashed_at, the user's DB rows (and
                 checkpoint) are deleted and the roster entry evicted.

Cautions encoded here:
  - core.recorder_lock is respected for tiktok — never trash the folder of a
    user being actively recorded; defer to the next cycle.
  - A missing folder still stamps trashed_at (idempotent — e.g. re-running
    after a crash between trash and stamp).
  - Row GC drops the user's content_hash dedup memory: re-adding the user
    later could re-upload old bytes. Accepted for an intentional delete.
  - The Recycle Bin only exists on a local volume that has one; on a volume
    without one, send2trash degrades to a PERMANENT delete (documented
    tradeoff — output_dir lives on C:, which has a bin).
  - The retention clock is wall-clock ISO timestamps (`now - trashed_at`),
    never process uptime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .quarantine import _recording_holds

log = logging.getLogger(__name__)

RETENTION_DAYS = 30

# Statuses that permit the trash step. Anything else (pending/sending/failed,
# or a status this code doesn't know) defers — unknown must never mean "drop".
_TRASHABLE_STATUSES = frozenset({"sent"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_trash(path: str) -> None:
    """Send a path to the Recycle Bin. Imported lazily so the suite runs
    (and every non-delete selftest passes) without Send2Trash installed."""
    from send2trash import send2trash
    send2trash(path)


@dataclass
class DeletionSweepReport:
    trashed:          list[str] = field(default_factory=list)  # "platform/user"
    deferred_uploads: list[str] = field(default_factory=list)
    deferred_lock:    list[str] = field(default_factory=list)
    gc_users:         list[str] = field(default_factory=list)
    gc_rows:          int = 0
    errors:           list[str] = field(default_factory=list)

    def __bool__(self) -> bool:   # "did anything happen?"
        return bool(self.trashed or self.deferred_uploads or
                    self.deferred_lock or self.gc_users or self.errors)

    def __str__(self) -> str:
        return (f"trashed={len(self.trashed)} "
                f"deferred(uploads)={len(self.deferred_uploads)} "
                f"deferred(lock)={len(self.deferred_lock)} "
                f"gc={len(self.gc_users)} users/{self.gc_rows} rows "
                f"errors={len(self.errors)}")


def process_pending_deletions(
    db,
    policy_store,
    output_dir: str | Path,
    *,
    now: datetime | None = None,
    trash_fn: Callable[[str], None] | None = None,
) -> DeletionSweepReport:
    """One sweep over every platform's deletion roster. Never raises — a
    failure on one user is recorded and the sweep continues (it retries next
    cycle anyway)."""
    now = now or datetime.now(timezone.utc)
    trash = trash_fn or _default_trash
    rep = DeletionSweepReport()

    for platform in policy_store.platforms_with_deletions():
        for username, meta in policy_store.deleting_details(platform).items():
            key = f"{platform}/{username}"
            try:
                trashed_at = meta.get("trashed_at", "")

                if not trashed_at:
                    # ── step 2: deferred trash ──────────────────────────────
                    counts = db.user_status_counts(platform, username)
                    blockers = {s: n for s, n in counts.items()
                                if s not in _TRASHABLE_STATUSES and n}
                    if blockers:
                        rep.deferred_uploads.append(key)
                        log.info("manual-delete: %s deferred — un-sent rows "
                                 "remain: %s", key, blockers)
                        continue
                    if _recording_holds(platform, username):
                        rep.deferred_lock.append(key)
                        log.info("manual-delete: %s deferred — live recording "
                                 "holds the user", key)
                        continue
                    folder = Path(output_dir) / platform / username
                    if folder.exists():
                        trash(str(folder))
                        log.warning("manual-delete: %s → Recycle Bin (%s)",
                                    key, folder, extra={"ev": "delete"})
                    else:
                        log.info("manual-delete: %s has no folder — stamping "
                                 "trashed_at anyway (idempotent)", key)
                    policy_store.set_deleting_field(
                        platform, username, "trashed_at", _now_iso())
                    rep.trashed.append(key)
                    continue

                # ── step 3: row GC after the retention window ───────────────
                try:
                    t0 = datetime.fromisoformat(trashed_at)
                except ValueError:
                    rep.errors.append(f"{key}: bad trashed_at {trashed_at!r}")
                    continue
                if (now - t0).total_seconds() < RETENTION_DAYS * 86400:
                    continue                    # still inside the window
                n = db.reset_user(platform, username)
                policy_store.unmark_deleting(platform, username)
                rep.gc_users.append(key)
                rep.gc_rows += n
                log.warning("manual-delete: %s rows GC'd (%d) — deletion "
                            "complete, roster entry evicted", key, n,
                            extra={"ev": "delete"})
            except Exception as e:
                rep.errors.append(f"{key}: {e}")
                log.error("manual-delete: %s failed: %s — will retry next "
                          "cycle", key, e)
    return rep
