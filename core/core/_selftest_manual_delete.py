"""
Focused validation for the manual-delete lifecycle (Phase 4): deletion roster,
deferred Recycle-Bin trash, recorder-lock deferral, 30-day row GC, cancel.

Run: python core/core/_selftest_manual_delete.py

Standalone (no pytest). Real ItemStore + PolicyStore on temp paths; the trash
call is mocked (no real Recycle Bin involved) and the clock is injected.
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

import core.quarantine as q                                   # noqa: E402
from core import (                                            # noqa: E402
    ItemStore, PolicyStore, process_pending_deletions, RETENTION_DAYS,
)

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "archive"
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    q.recorder_lock.live_recording_user = lambda: (False, None)

    trashed: list[str] = []
    trash = trashed.append
    now0 = datetime.now(timezone.utc)

    def _add_row(platform, user, ident, *, sent):
        p = out / platform / user / f"{ident}.mp4"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 128)
        db.add_item(source="archiver", platform=platform, username=user,
                    identifier=ident, file_path=str(p))
        if sent:
            claimed = db.claim_next()
            assert claimed is not None
            db.mark_sent(claimed.id)

    # ── request: roster + active drop; files/rows untouched ─────────────────
    store.add_user("x", "alice")
    _add_row("x", "alice", "a1", sent=True)
    _add_row("x", "alice", "a2", sent=False)      # still pending

    check(store.mark_deleting("x", "alice",
                              requested_at=now0.isoformat(timespec="seconds")),
          "mark_deleting returns True on first request")
    check(not store.mark_deleting("x", "alice"),
          "repeat request is a no-op (original requested_at kept)")
    store.remove_user("x", "alice")
    check("alice" not in store.list_users("x"), "dropped from active list")
    check("alice" in store.list_deleting("x"), "on the deletion roster")
    check((out / "x" / "alice").is_dir(), "files untouched at request time")

    # ── deferred: un-sent rows block the trash ──────────────────────────────
    rep = process_pending_deletions(db, store, out, now=now0, trash_fn=trash)
    check(rep.deferred_uploads == ["x/alice"] and not trashed,
          "pending row defers the trash")
    check(not store.deleting_details("x")["alice"].get("trashed_at"),
          "no trashed_at while deferred")

    # ── all sent → trashed (mocked), trashed_at stamped ─────────────────────
    claimed = db.claim_next()
    db.mark_sent(claimed.id)
    rep = process_pending_deletions(db, store, out, now=now0, trash_fn=trash)
    check(rep.trashed == ["x/alice"] and trashed == [str(out / "x" / "alice")],
          "all-sent user trashed via trash_fn")
    check(bool(store.deleting_details("x")["alice"].get("trashed_at")),
          "trashed_at stamped")
    rep = process_pending_deletions(db, store, out, now=now0, trash_fn=trash)
    check(len(trashed) == 1, "second sweep does not re-trash")

    # ── row GC: 29d → kept, 31d → purged + roster evicted ───────────────────
    rep = process_pending_deletions(
        db, store, out, now=now0 + timedelta(days=RETENTION_DAYS - 1),
        trash_fn=trash)
    check(not rep.gc_users and db.user_status_counts("x", "alice"),
          f"{RETENTION_DAYS - 1}d after trash: rows kept")
    rep = process_pending_deletions(
        db, store, out, now=now0 + timedelta(days=RETENTION_DAYS + 1),
        trash_fn=trash)
    check(rep.gc_users == ["x/alice"] and rep.gc_rows == 2,
          f"{RETENTION_DAYS + 1}d after trash: rows purged")
    check(not db.user_status_counts("x", "alice"), "no rows remain")
    check("alice" not in store.list_deleting("x"), "roster entry evicted")

    # ── recorder lock defers a tiktok trash ─────────────────────────────────
    store.add_user("tiktok", "streamer")
    _add_row("tiktok", "streamer", "s1", sent=True)
    store.mark_deleting("tiktok", "streamer")
    store.remove_user("tiktok", "streamer")
    q.recorder_lock.live_recording_user = lambda: (True, "streamer")
    rep = process_pending_deletions(db, store, out, now=now0, trash_fn=trash)
    check(rep.deferred_lock == ["tiktok/streamer"] and len(trashed) == 1,
          "live recording defers the trash")
    q.recorder_lock.live_recording_user = lambda: (False, None)

    # ── cancel before trash → restored to active, nothing moved ─────────────
    # (streamer is still un-trashed thanks to the lock deferral)
    check("streamer" in store.list_deleting("tiktok"), "still on roster")
    store.unmark_deleting("tiktok", "streamer")
    store.add_user("tiktok", "streamer")
    check("streamer" in store.list_users("tiktok")
          and (out / "tiktok" / "streamer").is_dir(),
          "cancel-before-trash restores active user, folder intact")
    rep = process_pending_deletions(db, store, out, now=now0, trash_fn=trash)
    check(len(trashed) == 1, "cancelled user is not swept")

    # ── missing folder still stamps trashed_at (idempotent) ─────────────────
    store.mark_deleting("x", "ghost")
    rep = process_pending_deletions(db, store, out, now=now0, trash_fn=trash)
    check(rep.trashed == ["x/ghost"] and len(trashed) == 1,
          "no-folder user stamps trashed_at without a trash call")

    db.close()
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
