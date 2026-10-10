"""Seams: dispatcher drain loop, quarantine, circuit breaker, backoff, housekeeping."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ._harness import _FakeSend, _db_file, _fresh_db, _write_media, ok, section


# ══════════════════════════════════════════════════════════════════════════════
# Seam 10 — the FULL dispatcher drain loop against a fake Telegram sender
# ══════════════════════════════════════════════════════════════════════════════

def test_full_drain_seam(tmp: Path) -> None:
    section("Seam 10: full dispatcher drain (claim→send→mark→delete)")
    from core import (ItemStore, PolicyStore, DeletePolicy, RecorderDeletePolicy,
                      BatchPolicy, DeletionGuard)
    from core.hashing import full_hash
    from dispatcher.drain import drain_forever
    from dispatcher.config import DispatcherConfig
    from dispatcher.tg_router import TelegramRouter

    db = _fresh_db()
    db_path = _db_file(db)
    try:
        # Two archiver photos (album) + one recorder single. delete-after-upload
        # ON globally so the drain's delete gate fires after a successful send.
        ps = PolicyStore()
        ps.set("delete_after_upload", True)
        ps.set(RecorderDeletePolicy.KEY, True)
        # Disable the min-batch gate so the small album sends within the test
        # (the gate itself is covered by Seam 5). Default size is 10.
        ps.set(BatchPolicy.SIZE_KEY, 1)

        p1 = _write_media(tmp / "x" / "al" / "p1.jpg", b"P1")
        p2 = _write_media(tmp / "x" / "al" / "p2.jpg", b"P2")
        for f, ident in ((p1, "p1"), (p2, "p2")):
            db.add_item(source="archiver", platform="x", username="al",
                        identifier=ident, file_path=str(f), priority=10,
                        caption="A", content_hash=full_hash(f))
        rec = _write_media(tmp / "rec" / "bo" / "bo_1.mp4", b"REC")
        db.add_item(source="recorder", platform="tiktok", username="bo",
                    identifier="rec_bo_1", file_path=str(rec), priority=5,
                    content_hash=full_hash(rec))

        # An orphaned single (already prepped at ingest). It must send with the
        # streamable net DISABLED — proves source-keyed net gating end-to-end.
        orph = _write_media(tmp / "orph" / "o1.mp4", b"ORPH")
        db.add_item(source="orphaned", platform="orphaned", username="-100999",
                    identifier="orph_o1", file_path=str(orph), priority=6,
                    caption="o1.mp4", chat_id="-100999",
                    content_hash=full_hash(orph))

        # A byte-duplicate of p1 that must be SUPPRESSED + its copy deleted.
        dup = _write_media(tmp / "x" / "al" / "p1_dup.jpg", b"P1")
        db.add_item(source="archiver", platform="x", username="al",
                    identifier="p1_dup", file_path=str(dup), priority=10,
                    caption="A", content_hash=full_hash(dup))
        db.close()

        cfg = DispatcherConfig(
            telegram=None, default_chat_id="-100123", db_path=db_path,
            policy_store=ps, poll_interval_s=0.01, max_retries=3,
            inter_album_sleep=0.0, stuck_claim_min=10, failed_retention_days=0,
        )
        store = ItemStore.open(db_path)
        fake = _FakeSend()
        router = TelegramRouter(default_chat_id="-100123")
        stop = asyncio.Event()

        async def _run():
            task = asyncio.create_task(drain_forever(
                cfg, store, fake, router,
                DeletePolicy(ps), RecorderDeletePolicy(ps), BatchPolicy(ps),
                DeletionGuard(ps), stop_event=stop,
            ))
            # Poll until everything is terminal (sent/deduped) or timeout.
            for _ in range(400):
                await asyncio.sleep(0.01)
                c = store.counts_by_status()
                if c.get("pending", 0) == 0 and c.get("sending", 0) == 0:
                    break
            stop.set()
            await task

        asyncio.run(_run())

        counts = store.counts_by_status()
        ok(counts.get("pending", 0) == 0, "drain emptied the pending queue")
        ok(counts.get("sent", 0) == 4,
           "4 rows terminal as 'sent' (2 album + recorder single + dedup-suppressed)")
        ok(store.id_of(str(orph)) is None and not orph.exists(),
           "orphaned chat_id-folder item left NO trace: row deleted AND file removed "
           "(ship-and-delete)")
        ok(fake.sent_albums and sorted(Path(p).name for p in fake.sent_albums[0])
           == ["p1.jpg", "p2.jpg"],
           "the two photos went up as ONE album (homogeneous batch)")
        ok(sorted(Path(p).name for p in fake.sent_singles) == ["bo_1.mp4", "o1.mp4"],
           "recorder + orphaned files each sent as singles (never albumed)")
        # Source-keyed streamable-net gating: the recorder (fail-soft producer)
        # asks for the net; the orphaned row (prepped at ingest) opts out.
        net = dict(zip((Path(p).name for p in fake.sent_singles),
                       fake.sent_ensure_streamable))
        ok(net.get("bo_1.mp4") is True,
           "recorder single requests the send-time streamable net")
        ok(net.get("o1.mp4") is False,
           "orphaned single (already prepped at ingest) opts out of the net")
        ok(not p1.exists() and not p2.exists() and not rec.exists(),
           "delete-after-upload removed the originals post-send")
        ok(not dup.exists(),
           "dedup-suppressed duplicate's on-disk copy was removed unconditionally")
        dup_row = store.get(store.id_of(str(dup)))
        ok(dup_row.status == "sent" and dup_row.tg_message_id is None
           and "deduped" in (dup_row.last_error or ""),
           "suppressed dup recorded as sent-by-twin (no real send, audited)")
    finally:
        try:
            store.close()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# Seam 11 — MediaEmptyError ↔ fail-fast quarantine (queue can't head-of-line jam)
# Telegram occasionally rejects uploaded media for a destination (MediaEmptyError),
# often transiently. The send envelope must NOT retry it 4x (a backoff storm), and
# the drain must NOT let it cycle attempts at the head of the queue — one poison
# album would otherwise starve the whole drain (the real 3-hour outage). Contract:
# media_empty ⇒ terminal 'failed' on the FIRST hit, no CANCELLED_MARKER, so the
# drain moves on AND `reset failed` can recover it once the cause clears.
# ══════════════════════════════════════════════════════════════════════════════

class _MediaEmptySend:
    """Fake strategy modeling the real poison: an ALBUM send hits MediaEmptyError
    (album atomicity — one bad item fails all). The per-item fallback then sends
    each single: a file whose name contains 'bad' stays MediaEmptyError (a truly
    undeliverable clip media_prep can't fix); everything else delivers (a good
    H.264 item, or a VP9 the net re-encoded). Proves: deliver the good, isolate
    the bad — never write off the whole album."""
    def __init__(self):
        self.album_attempts = 0
        self.single_sends: list[str] = []

    async def send(self, *, peer, file_path, caption, ensure_streamable=True,
                   filetype_tag=False, topic_id=None):
        from dispatcher.send import SendResult
        self.single_sends.append(file_path)
        if "bad" in Path(file_path).name:
            return SendResult(ok=False, error="MediaEmptyError: rejected",
                              media_empty=True)
        return SendResult(ok=True)

    async def send_album(self, *, peer, file_paths, caption, topic_id=None, as_documents=False):
        from dispatcher.send import SendResult
        self.album_attempts += 1
        return SendResult(ok=False, error="MediaEmptyError: rejected",
                          media_empty=True)

def test_media_empty_quarantine_seam(tmp: Path) -> None:
    section("Seam 11: MediaEmptyError ↔ per-item fallback (deliver good, isolate bad)")
    from core import (ItemStore, PolicyStore, DeletePolicy, RecorderDeletePolicy,
                      BatchPolicy, DeletionGuard, CANCELLED_MARKER)
    from core.hashing import full_hash
    from dispatcher.drain import drain_forever
    from dispatcher.config import DispatcherConfig
    from dispatcher.tg_router import TelegramRouter

    db = _fresh_db()
    db_path = _db_file(db)
    try:
        ps = PolicyStore()
        ps.set(BatchPolicy.SIZE_KEY, 1)   # let the small album send immediately
        # A 3-photo album whose send hits MediaEmptyError: two good items + one
        # undeliverable ('bad'). Plus a recorder single that must still flow.
        good1 = _write_media(tmp / "x" / "al" / "good1.jpg", b"G1")
        good2 = _write_media(tmp / "x" / "al" / "good2.jpg", b"G2")
        bad   = _write_media(tmp / "x" / "al" / "bad_vp9.jpg", b"BAD")
        for f, ident in ((good1, "g1"), (good2, "g2"), (bad, "b1")):
            db.add_item(source="archiver", platform="x", username="al",
                        identifier=ident, file_path=str(f), priority=10,
                        content_hash=full_hash(f))
        rec = _write_media(tmp / "rec" / "bo" / "bo_1.mp4", b"REC")
        db.add_item(source="recorder", platform="tiktok", username="bo",
                    identifier="rec_bo_1", file_path=str(rec), priority=5,
                    content_hash=full_hash(rec))
        db.close()

        cfg = DispatcherConfig(
            telegram=None, default_chat_id="-100123", db_path=db_path,
            policy_store=ps, poll_interval_s=0.01, max_retries=4,
            inter_album_sleep=0.0, stuck_claim_min=10, failed_retention_days=0)
        store = ItemStore.open(db_path)
        fake = _MediaEmptySend()
        stop = asyncio.Event()

        async def _run():
            task = asyncio.create_task(drain_forever(
                cfg, store, fake, TelegramRouter(default_chat_id="-100123"),
                DeletePolicy(ps), RecorderDeletePolicy(ps), BatchPolicy(ps),
                DeletionGuard(ps), stop_event=stop))
            for _ in range(400):
                await asyncio.sleep(0.01)
                c = store.counts_by_status()
                if c.get("pending", 0) == 0 and c.get("sending", 0) == 0:
                    break
            stop.set(); await task

        asyncio.run(_run())

        ok(fake.album_attempts == 1,
           "album attempted once, then fell back to per-item (no retry storm)")
        ok(store.get(store.id_of(str(good1))).status == "sent" and
           store.get(store.id_of(str(good2))).status == "sent",
           "good album items DELIVERED individually (not lost with the bad one)")
        bad_row = store.get(store.id_of(str(bad)))
        ok(bad_row.status == "failed" and bad_row.attempts <= 1,
           "only the undeliverable item quarantined (no retry-budget churn)")
        ok(not (bad_row.last_error or "").startswith(CANCELLED_MARKER),
           "quarantine leaves a plain failure (reset failed can recover it)")
        ok(store.get(store.id_of(str(rec))).status == "sent",
           "deliverable recorder single still sent — poison didn't block it")
        n = store.reset_failed(None, None)
        ok(n == 1 and store.get(store.id_of(str(bad))).status == "pending",
           "reset failed re-arms only the quarantined item (recovery path)")
    finally:
        try: store.close()
        except Exception: pass

class _AlwaysFailSend:
    """Every send fails with a SYSTEMIC (network) error — models Telegram down."""
    def __init__(self):
        self.calls = 0

    async def send(self, *, peer, file_path, caption, ensure_streamable=True,
                   filetype_tag=False, topic_id=None):
        from dispatcher.send import SendResult
        self.calls += 1
        return SendResult(ok=False, error="network down")

    async def send_album(self, *, peer, file_paths, caption, topic_id=None, as_documents=False):
        from dispatcher.send import SendResult
        self.calls += 1
        return SendResult(ok=False, error="network down")

def test_circuit_breaker_seam(tmp: Path) -> None:
    section("Seam 11b: dispatcher circuit breaker pauses on systemic failure")
    import dispatcher.drain as drain_mod
    from core import (ItemStore, PolicyStore, DeletePolicy, RecorderDeletePolicy,
                      BatchPolicy, DeletionGuard)
    from core.hashing import full_hash
    from dispatcher.config import DispatcherConfig
    from dispatcher.tg_router import TelegramRouter

    orig_trip, orig_cd = drain_mod._CIRCUIT_TRIP_AT, drain_mod._CIRCUIT_COOLDOWN_S
    drain_mod._CIRCUIT_TRIP_AT = 3
    drain_mod._CIRCUIT_COOLDOWN_S = 30.0   # long: we catch the drain mid-cooldown
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        ps = PolicyStore(); ps.set(BatchPolicy.SIZE_KEY, 1)
        for i in range(10):
            f = _write_media(tmp / "rec" / "u" / f"u_{i}.mp4", bytes([i]) * 50)
            db.add_item(source="recorder", platform="tiktok", username="u",
                        identifier=f"rec_{i}", file_path=str(f), priority=5,
                        content_hash=full_hash(f))
        db.close()
        cfg = DispatcherConfig(
            telegram=None, default_chat_id="-100123", db_path=db_path,
            policy_store=ps, poll_interval_s=0.01, max_retries=4,
            inter_album_sleep=0.0, stuck_claim_min=10, failed_retention_days=0,
            stall_backoff_s=0)  # no backoff: breaker needs back-to-back reclaims
        store = ItemStore.open(db_path)
        fake = _AlwaysFailSend()
        stop = asyncio.Event()

        async def _run():
            task = asyncio.create_task(drain_mod.drain_forever(
                cfg, store, fake, TelegramRouter(default_chat_id="-100123"),
                DeletePolicy(ps), RecorderDeletePolicy(ps), BatchPolicy(ps),
                DeletionGuard(ps), stop_event=stop))
            for _ in range(500):
                await asyncio.sleep(0.01)
                if fake.calls >= 3:
                    break
            await asyncio.sleep(0.15)   # let it reach the cooldown gate
            stop.set(); await task

        asyncio.run(_run())
        ok(fake.calls == 3,
           f"breaker tripped at threshold: {fake.calls} sends then paused, not "
           f"all 10 churned through")
    finally:
        drain_mod._CIRCUIT_TRIP_AT = orig_trip
        drain_mod._CIRCUIT_COOLDOWN_S = orig_cd
        try: store.close()
        except Exception: pass

def test_drain_backoff_seam(tmp: Path) -> None:
    section("Seam 11c: dispatcher drain backs off failed rows instead of reclaiming them")
    import dispatcher.drain as drain_mod
    from core import (ItemStore, PolicyStore, DeletePolicy, RecorderDeletePolicy,
                      BatchPolicy, DeletionGuard)
    from core.hashing import full_hash
    from dispatcher.config import DispatcherConfig
    from dispatcher.tg_router import TelegramRouter

    db = _fresh_db()
    db_path = _db_file(db)
    store = None
    try:
        ps = PolicyStore(); ps.set(BatchPolicy.SIZE_KEY, 1)
        paths = []
        for i in range(2):
            f = _write_media(tmp / "rec" / "u" / f"u_{i}.mp4", bytes([i]) * 50)
            paths.append(str(f))
            db.add_item(source="recorder", platform="tiktok", username="u",
                        identifier=f"rec_{i}", file_path=str(f), priority=5,
                        content_hash=full_hash(f))
        db.close()
        cfg = DispatcherConfig(
            telegram=None, default_chat_id="-100123", db_path=db_path,
            policy_store=ps, poll_interval_s=0.01, max_retries=4,
            inter_album_sleep=0.0, stuck_claim_min=10, failed_retention_days=0)
        store = ItemStore.open(db_path)
        fake = _AlwaysFailSend()
        stop = asyncio.Event()

        async def _run():
            task = asyncio.create_task(drain_mod.drain_forever(
                cfg, store, fake, TelegramRouter(default_chat_id="-100123"),
                DeletePolicy(ps), RecorderDeletePolicy(ps), BatchPolicy(ps),
                DeletionGuard(ps), stop_event=stop))
            for _ in range(500):
                await asyncio.sleep(0.01)
                if fake.calls >= 1:
                    break
            await asyncio.sleep(0.3)   # any wrongful reclaim would happen here
            stop.set(); await task

        asyncio.run(_run())
        ok(fake.calls == 1, f"album sent once, never reclaimed: {fake.calls} calls")
        for path in paths:
            row = store.get(store.id_of(path))
            ok(row.status == 'pending', f"row {path} pending after failure")
            ok(row.attempts == 1, f"row {path} claimed once")
            ok(row.retry_after is not None, f"row {path} has backoff stamp")
    finally:
        if store is not None:
            store.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 15 — in-batch dedup must not suppress before its twin DELIVERS.
# Two byte-identical pending files claimed in one batch: the dupe is held back
# from the send. If the send FAILS, the dupe's bytes/file must be untouched
# (its twin never delivered); only after a successful send may it be
# suppressed and its redundant copy removed. Regression guard for the
# file-integrity bug where a dupe was marked 'sent' + deleted pre-send.
# ══════════════════════════════════════════════════════════════════════════════

class _FlakySend(_FakeSend):
    """Fails the first N album/single sends, then succeeds. `on_failure` (if
    set) runs at the moment of each failure — the deterministic point to
    assert what the world looks like while the twin has NOT delivered."""
    def __init__(self, fail_first: int, on_failure=None):
        super().__init__()
        self._failures_left = fail_first
        self._on_failure = on_failure

    def _maybe_fail(self):
        from dispatcher.send import SendResult
        if self._failures_left > 0:
            self._failures_left -= 1
            if self._on_failure:
                self._on_failure()
            return SendResult(ok=False, error="simulated network failure")
        return None

    async def send(self, *, peer, file_path, caption, ensure_streamable=True,
                   filetype_tag=False, topic_id=None):
        return self._maybe_fail() or await super().send(
            peer=peer, file_path=file_path, caption=caption,
            ensure_streamable=ensure_streamable, filetype_tag=filetype_tag,
            topic_id=topic_id)

    async def send_album(self, *, peer, file_paths, caption, topic_id=None, as_documents=False):
        return self._maybe_fail() or await super().send_album(
            peer=peer, file_paths=file_paths, caption=caption, topic_id=topic_id)

def test_in_batch_dedup_integrity_seam(tmp: Path) -> None:
    section("Seam 15: in-batch dup survives a failed twin send")
    from core import (ItemStore, PolicyStore, DeletePolicy, RecorderDeletePolicy,
                      BatchPolicy, DeletionGuard)
    from core.hashing import full_hash
    from dispatcher.drain import drain_forever
    from dispatcher.config import DispatcherConfig
    from dispatcher.tg_router import TelegramRouter

    db = _fresh_db()
    db_path = _db_file(db)
    store = None
    try:
        ps = PolicyStore()
        ps.set(BatchPolicy.SIZE_KEY, 1)

        # Two byte-identical photos in ONE album group → claimed together.
        a = _write_media(tmp / "x" / "al" / "a.jpg", b"SAME")
        b = _write_media(tmp / "x" / "al" / "b.jpg", b"SAME")
        for f, ident in ((a, "a"), (b, "b")):
            db.add_item(source="archiver", platform="x", username="al",
                        identifier=ident, file_path=str(f), priority=10,
                        caption="A", content_hash=full_hash(f))
        db.close()

        cfg = DispatcherConfig(
            telegram=None, default_chat_id="-100123", db_path=db_path,
            policy_store=ps, poll_interval_s=0.01, max_retries=5,
            inter_album_sleep=0.0, stuck_claim_min=10, failed_retention_days=0,
            stall_backoff_s=0,  # no backoff: test needs an immediate retry
        )
        store = ItemStore.open(db_path)
        # At each failure instant the twin has NOT delivered — both files must
        # still be on disk and no row may be terminal 'sent'. Captured inside
        # the sender so the check is deterministic, not poll-timing-dependent.
        failure_snapshots: list[bool] = []

        def _at_failure():
            c = store.counts_by_status()
            failure_snapshots.append(
                a.exists() and b.exists() and c.get("sent", 0) == 0)

        fake = _FlakySend(fail_first=2, on_failure=_at_failure)
        router = TelegramRouter(default_chat_id="-100123")
        stop = asyncio.Event()

        async def _run():
            task = asyncio.create_task(drain_forever(
                cfg, store, fake, router,
                DeletePolicy(ps), RecorderDeletePolicy(ps), BatchPolicy(ps),
                DeletionGuard(ps), stop_event=stop,
            ))
            for _ in range(600):
                await asyncio.sleep(0.01)
                c = store.counts_by_status()
                if c.get("pending", 0) == 0 and c.get("sending", 0) == 0 \
                        and c.get("sent", 0) == 2:
                    break
            stop.set()
            await task

        asyncio.run(_run())

        ok(len(failure_snapshots) == 2 and all(failure_snapshots),
           "during failed sends, no file was deleted and nothing marked sent")
        counts = store.counts_by_status()
        ok(counts.get("sent", 0) == 2 and counts.get("failed", 0) == 0,
           "after the sender recovered, both rows are terminal 'sent'")
        sent_files = [Path(p).name for batch in fake.sent_albums for p in batch] \
            + [Path(p).name for p in fake.sent_singles]
        ok(len(sent_files) == 1,
           "exactly ONE physical upload happened (the dupe never re-sent)")
        dup_row = store.get(store.id_of(str(b)))
        ok("deduped" in (dup_row.last_error or ""),
           "held-back dupe was suppressed only AFTER its twin delivered")
        ok(not b.exists(),
           "redundant copy removed once (and only once) the bytes shipped")
    finally:
        for s in (store, db):
            try:
                s.close()
            except Exception:
                pass

# ══════════════════════════════════════════════════════════════════════════════
# Seam 25 — dispatcher housekeeping owns failed-queue maintenance
# Failed-upload lifecycle (retire dead tombstones + re-queue the rest) lives
# ENTIRELY in the dispatcher's periodic housekeeping (drain.run_housekeeping),
# the queue owner — not in the archiver loop. The contract, three ways:
#   • a 'failed' row whose file is GONE is deleted every pass (always, no policy)
#   • the REMAINING (present-file) failed rows are re-queued only when
#     auto_retry_failed is on (default OFF — opt-in, so a poison row can't
#     re-arm itself into a perpetual re-upload storm)
#   • delete-first ordering ⇒ a missing-file row is never re-armed to 'pending'
#     and so never costs a wasted send attempt on a vanished path
# Plus the retention backstop still caps present-file rows stuck past the window.
# A regression here either resurrects vanished files into wasted sends or
# silently stops failed uploads from ever retrying.
# ══════════════════════════════════════════════════════════════════════════════

def test_failed_housekeeping_seam(tmp: Path) -> None:
    section("Seam 25: dispatcher housekeeping ↔ failed-queue maintenance")
    from core import PolicyStore, FailedRetryPolicy
    from dispatcher.drain import run_housekeeping
    from dispatcher.config import DispatcherConfig

    def _cfg(db_path: str, ps: PolicyStore, retention: float):
        # Only telegram/default_chat_id/db_path/policy_store are required; the
        # rest default. failed_retention_days drives the retention backstop.
        return DispatcherConfig(
            telegram=None, default_chat_id="-1", db_path=db_path,
            policy_store=ps, failed_retention_days=retention)

    def _make_failed(db, where: Path, *, present: bool, ident: str) -> str:
        fp = where / f"{ident}.mp4"
        if present:
            _write_media(fp, ident.encode())
        db.add_item(source="archiver", platform="x", username="al",
                    identifier=ident, file_path=str(fp), priority=10)
        db.conn.execute("UPDATE items SET status='failed', attempts=3 "
                        "WHERE id=?", (db.id_of(str(fp)),))
        db.conn.commit()
        return str(fp)

    def _status(db, fp):
        r = db.get(db.id_of(fp)) if db.id_of(fp) is not None else None
        return r.status if r else None

    # ── auto_retry ON (opt-in): present → re-queued, missing → deleted ──
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        present = _make_failed(db, tmp / "on", present=True, ident="keep")
        missing = _make_failed(db, tmp / "on", present=False, ident="gone")
        ps = PolicyStore(); ps.set(FailedRetryPolicy.KEY, True)
        run_housekeeping(db, _cfg(db_path, ps, retention=0))  # ON, prune off
        ok(_status(db, present) == "pending",
           "auto_retry ON: present-file failed row re-queued to pending")
        ok(db.id_of(missing) is None,
           "missing-file failed row DELETED (can never deliver), not re-queued")
    finally:
        db.close()

    # ── auto_retry OFF: present stays failed, missing still deleted ──
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        present = _make_failed(db, tmp / "off", present=True, ident="keep")
        missing = _make_failed(db, tmp / "off", present=False, ident="gone")
        ps = PolicyStore(); ps.set(FailedRetryPolicy.KEY, False)
        run_housekeeping(db, _cfg(db_path, ps, retention=0))
        ok(_status(db, present) == "failed",
           "auto_retry OFF: present-file failed row left for a manual reset")
        ok(db.id_of(missing) is None,
           "missing-file cleanup runs even with auto_retry OFF (unconditional)")
    finally:
        db.close()

    # ── retention backstop: an old present-file failed row is pruned ──
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        old = _make_failed(db, tmp / "ret", present=True, ident="stale")
        # Backdate discovered_at past the window so prune_failed catches it;
        # auto_retry OFF keeps it 'failed' (else it'd be re-queued, not pruned).
        db.conn.execute("UPDATE items SET discovered_at='2000-01-01T00:00:00Z' "
                        "WHERE id=?", (db.id_of(old),))
        db.conn.commit()
        ps = PolicyStore(); ps.set(FailedRetryPolicy.KEY, False)
        run_housekeeping(db, _cfg(db_path, ps, retention=7))
        ok(db.id_of(old) is None,
           "retention backstop prunes a present-file row stuck past the window")
    finally:
        db.close()

    # ── backstop wins over auto_retry: an old row is PRUNED, not resurrected ──
    # The conflict guard. With auto_retry ON, prune MUST run before the re-queue;
    # otherwise reset_failed would move the row to pending first and the cap
    # would never fire (a permanent failure cycling forever — the storm).
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        old = _make_failed(db, tmp / "storm", present=True, ident="stale")
        db.conn.execute("UPDATE items SET discovered_at='2000-01-01T00:00:00Z' "
                        "WHERE id=?", (db.id_of(old),))
        db.conn.commit()
        ps = PolicyStore(); ps.set(FailedRetryPolicy.KEY, True)
        run_housekeeping(db, _cfg(db_path, ps, retention=7))
        ok(db.id_of(old) is None,
           "auto_retry ON: a row failing past the window is pruned, not re-armed "
           "(prune-before-reorder prevents the perpetual-retry storm)")
    finally:
        db.close()

    # ── a manually-cancelled row survives auto_retry; retry(id) forces it back ──
    # cancel() parks the row in 'failed' with CANCELLED_MARKER. A deliberate
    # abort must NOT be resurrected by auto_retry's bulk reset_failed — only the
    # targeted retry(id) override (which clears last_error) brings it back.
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        fp = tmp / "cancel" / "abort.mp4"
        _write_media(fp, b"abort")
        db.add_item(source="archiver", platform="x", username="al",
                    identifier="abort", file_path=str(fp), priority=10)
        cid = db.id_of(str(fp))
        ok(db.cancel(cid), "cancel: pending row parked as a manual abort")
        ps = PolicyStore(); ps.set(FailedRetryPolicy.KEY, True)
        run_housekeeping(db, _cfg(db_path, ps, retention=0))  # auto_retry ON
        ok(db.get(cid).status == "failed",
           "auto_retry ON does NOT resurrect a cancelled row (CANCELLED_MARKER)")
        ok(db.retry(cid) and db.get(cid).status == "pending",
           "targeted retry(id) overrides cancel and re-arms the row")
    finally:
        db.close()

    # ── TRANSIENT auto-recovery (default ON, storm-safe) ──────────────────────
    # A failure with a transient cause (network / upload corruption) heals on the
    # ~15-min cadence with NO opt-in, while a PERMANENT one (Telegram rejecting
    # the media) stays quarantined — so the poison-row storm that forced
    # auto_retry_failed off never happens. This is the safe-by-default self-heal.
    from core import is_transient_failure, CANCELLED_MARKER
    ok(is_transient_failure("ConnectionError: Connection to Telegram failed 5 time(s)"),
       "classifier: ConnectionError is transient")
    ok(not is_transient_failure("FilePartsInvalidError: The number of file parts is invalid"),
       "classifier: FilePartsInvalidError is PERMANENT (removed from transient "
       "signatures — auto-retry was storming; see upload-ceiling fix)")
    ok(not is_transient_failure("ImageProcessFailedError: Failure while processing image"),
       "classifier: ImageProcessFailedError is PERMANENT (poison — never auto-armed)")
    ok(not is_transient_failure("file missing on disk: /x/y.mp4"),
       "classifier: missing-file is PERMANENT")
    ok(not is_transient_failure(None) and not is_transient_failure(""),
       "classifier: unknown/empty defaults to PERMANENT (conservative)")
    ok(not is_transient_failure(CANCELLED_MARKER + " by user"),
       "classifier: a manual abort is never transient")

    def _fail_with(db, where: Path, ident: str, err: str) -> str:
        fp = where / f"{ident}.mp4"
        _write_media(fp, ident.encode())
        db.add_item(source="archiver", platform="x", username="al",
                    identifier=ident, file_path=str(fp), priority=10)
        db.conn.execute("UPDATE items SET status='failed', attempts=3, last_error=? "
                        "WHERE id=?", (err, db.id_of(str(fp))))
        db.conn.commit()
        return str(fp)

    db = _fresh_db()
    db_path = _db_file(db)
    try:
        trans = _fail_with(db, tmp / "tr", "neterr",
                           "ConnectionError: Connection to Telegram failed 5 time(s)")
        perm  = _fail_with(db, tmp / "tr", "imgerr",
                           "ImageProcessFailedError: Failure while processing image")
        ps = PolicyStore(); ps.set(FailedRetryPolicy.KEY, False)   # opt-in OFF
        run_housekeeping(db, _cfg(db_path, ps, retention=7))
        ok(_status(db, trans) == "pending",
           "auto_retry OFF: a TRANSIENT failure self-heals to pending (default on)")
        ok(_status(db, perm) == "failed",
           "auto_retry OFF: a PERMANENT failure stays quarantined (no poison storm)")
    finally:
        db.close()

    # The cancelled row must survive the transient sweep too (belt-and-braces:
    # CANCELLED_MARKER is excluded by the classifier, not just _reset_to_pending).
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        fp = tmp / "trc" / "abort.mp4"
        _write_media(fp, b"abort")
        db.add_item(source="archiver", platform="x", username="al",
                    identifier="abort2", file_path=str(fp), priority=10)
        cid = db.id_of(str(fp))
        db.cancel(cid)
        ps = PolicyStore(); ps.set(FailedRetryPolicy.KEY, False)
        run_housekeeping(db, _cfg(db_path, ps, retention=0))
        ok(db.get(cid).status == "failed",
           "transient sweep does NOT resurrect a cancelled row")
    finally:
        db.close()
