"""Seams: recorder startup sweep, reconcile, live enqueue, recording roots."""

from __future__ import annotations

import os
import time
from pathlib import Path

from ._harness import _db_file, _fresh_db, _write_media, ok, section


# ══════════════════════════════════════════════════════════════════════════════
# Seam 6 — recorder.startup_sweep reconciles the shared table with disk
# ══════════════════════════════════════════════════════════════════════════════

def test_startup_sweep_seam(tmp: Path) -> None:
    section("Seam 6: recorder.startup_sweep over the shared table")
    from recorder import startup_sweep
    from core.hashing import full_hash

    out = tmp / "recout"
    db = _fresh_db()
    db_path = _db_file(db)
    try:
        # (a) a SENT-but-not-deleted file → sweep deletes it (policy ON below).
        sent_f = _write_media(out / "alice" / "alice_sent.mp4", b"SENT-BYTES")
        db.add_item(source="recorder", platform="tiktok", username="alice",
                    identifier="rec_sent", file_path=str(sent_f),
                    content_hash=full_hash(sent_f))
        db.mark_sent(db.claim_next().id)   # only row so far → it's this one

        # (b) a FAILED file → sweep re-arms it (failed → pending).
        failed_f = _write_media(out / "alice" / "alice_failed.mp4", b"FAILED-BYTES")
        db.add_item(source="recorder", platform="tiktok", username="alice",
                    identifier="rec_failed", file_path=str(failed_f),
                    content_hash=full_hash(failed_f))
        fid = db.id_of(str(failed_f))
        # A REAL terminal failure (burn the retry budget), NOT a manual cancel:
        # cancel is now durable and must never be swept back to pending, so using
        # it here would wrongly assert the sweep re-arms an abort. claim_next is
        # this row (only pending left after (a) was sent).
        db.mark_failed(db.claim_next().id, error="send failed", max_retries=0)
        ok(db.get(fid).status == "failed", "  precondition: row is failed")

        # (c) a brand-NEW file with no row → sweep registers it.
        _write_media(out / "carol" / "carol_new.mp4", b"NEW-RECORDING-BYTES")

        # (d) a per-recording .log → sweep deletes it.
        (out / "alice" / "alice_sent_ytdlp.log").write_text("yt-dlp log\n")

        # (e) an orphaned RAW .flv: a capture that crashed before its live remux
        #     ran, leaving a non-canonical container with no DB row. It is NOT in
        #     MEDIA_EXTENSIONS, so the sweep must recognise it via the convertible
        #     set or it is stranded forever. Recovered raw here; the dispatcher's
        #     send-time net (Seam 20) makes it streamable at upload.
        orphan_flv = _write_media(out / "dave" / "dave_crash.flv",
                                  b"ORPHANED-RAW-FLV-NEVER-ENQUEUED")

        db.close()   # sweep opens its own ItemStore on the same file

        # Policy ON so the sent leftover is actually removed (uses a temp config).
        from core import PolicyStore, RecorderDeletePolicy
        ps = PolicyStore()   # ARCHIVER_SUITE_CONFIG points at a temp file
        ps.set(RecorderDeletePolicy.KEY, True)

        rep = startup_sweep.sweep(str(out), db_path, policy_store=ps)
        ok(rep.deleted_sent == 1 and not sent_f.exists(),
           "sent-but-present file deleted (delete-after-upload honored)")
        ok(rep.requeued >= 2, "failed re-armed AND new file registered (requeued≥2)")
        ok(rep.logs_deleted == 1, "per-recording .log cleaned up")

        db2 = __import__("core").ItemStore.open(db_path)
        try:
            ok(db2.get(fid).status == "pending", "failed recording re-armed to pending")
            ok(db2.has_file_path(str(out / "carol" / "carol_new.mp4")),
               "brand-new recording registered into the shared table")
            ok(db2.has_file_path(str(orphan_flv)),
               "orphaned raw .flv recovered by the sweep (not stranded on disk)")
        finally:
            db2.close()
    finally:
        pass

# ══════════════════════════════════════════════════════════════════════════════
# Seam 7 — archiver.reconcile_recordings identifier matches live enqueue
# ══════════════════════════════════════════════════════════════════════════════

def test_recordings_reconcile_seam(tmp: Path) -> None:
    section("Seam 7: reconcile_recordings ↔ recorder identifier/priority")
    from archiver.reconcile import (
        reconcile_recordings, _recorder_identifier as arch_ident,
        _RECORDER_PRIORITY,
    )
    from recorder.enqueue import (
        _recorder_identifier as rec_ident, RECORDER_PRIORITY,
    )

    # The two packages MUST agree on the recorder identity + priority, or a
    # live-enqueued recording and the same file reconciled by the archiver
    # would not collide on UNIQUE(platform, identifier).
    probe = "/x/y/bob_1700.mp4"
    ok(arch_ident(Path(probe)) == rec_ident(probe),
       "archiver and recorder derive the SAME recorder identifier")
    ok(_RECORDER_PRIORITY == RECORDER_PRIORITY,
       "archiver and recorder agree on recorder upload priority")

    out = tmp / "recorder-out"
    _write_media(out / "dave" / "dave_42.mp4", b"RECONCILE-RECORDING-BYTES")
    db = _fresh_db()
    try:
        reports = reconcile_recordings(db, str(out))
        total = sum(r.inserted for r in reports)
        ok(total == 1, "reconcile_recordings queued the loose recording")
        row = db.get(db.id_of(str(out / "dave" / "dave_42.mp4")))
        ok(row.source == "recorder" and row.priority == RECORDER_PRIORITY,
           "reconciled recording carries source=recorder + recorder priority")
        ok(row.identifier == rec_ident(str(out / "dave" / "dave_42.mp4")),
           "reconciled identifier == the live-enqueue identifier")
    finally:
        db.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 17 — recorder live enqueue goes through core.ingest with the recorder's
# identifier scheme intact, and inherits ingest's dedup-collapse: bytes already
# tracked under another path never become a second row.
# ══════════════════════════════════════════════════════════════════════════════

def test_recorder_enqueue_ingest_seam(tmp: Path) -> None:
    section("Seam 17: recorder enqueue ↔ core.ingest")
    from recorder.enqueue import EnqueueClient

    db = _fresh_db()
    db_path = _db_file(db)
    try:
        rec = _write_media(tmp / "alice" / "alice_live.mp4", b"LIVE")
        # Age the mtime past the stability quiescent window so the test
        # doesn't pay the 1.5s probe sleep.
        old = __import__("time").time() - 60
        os.utime(rec, (old, old))

        client = EnqueueClient(db_path)
        ok(client.enqueue(platform="tiktok", username="alice",
                          file_path=str(rec), caption="c"),
           "live enqueue registers a fresh recording")
        row = db.get(db.id_of(str(rec)))
        ok(row.identifier == f"recorder_{rec.stem}",
           "recorder identifier scheme preserved through core.ingest")
        ok(row.content_hash is not None,
           "live enqueue stamps content_hash (dedup guarantee intact)")

        # Same bytes under a second path → collapsed, never a second row.
        twin = _write_media(tmp / "alice" / "alice_live_copy.mp4", b"LIVE")
        os.utime(twin, (old, old))
        inserted = client.enqueue(platform="tiktok", username="alice",
                                  file_path=str(twin), caption="c")
        ok(not inserted, "byte-identical second path does not insert")
        ok(db.id_of(str(twin)) is None or db.id_of(str(twin)) == row.id,
           "no second row for identical bytes (dedup-collapse applied)")
    finally:
        try:
            db.close()
        except Exception:
            pass

def test_recording_roots_seam(tmp: Path) -> None:
    section("Seam 36: recordings reconcile covers the recorder's fallback root")
    from archiver import reconcile as archiver_reconcile
    from core import ingest, paths as core_paths
    from core.media_prep import PrepResult

    _osp = archiver_reconcile._osp

    # 1. recording_roots ordering + dedupe
    primary = tmp / "primary"
    fallback = tmp / "fallback"
    roots = core_paths.recording_roots(primary, fallback)
    ok(roots == (primary, fallback),
       "recording_roots returns (primary, fallback) in order")
    same = core_paths.recording_roots(primary, primary)
    ok(same == (primary,), "recording_roots dedupes equal roots to one path")

    orig_prep = ingest.media_prep.prepare
    ingest.media_prep.prepare = (                        # type: ignore
        lambda p, split_threshold_bytes=None: PrepResult.passthrough(p))
    try:
        # 2. both roots hold a recording for different users
        primary2 = tmp / "primary2"
        fallback2 = tmp / "fallback2"
        db = _fresh_db()
        try:
            p_alice = _write_media(primary2 / "alice" / "alice_1700.mp4",
                                   b"PRIMARY-ALICE")
            f_bob = _write_media(fallback2 / "bob" / "bob_1600.mp4",
                                 b"FALLBACK-BOB")
            old = time.time() - 3600
            for f in (p_alice, f_bob):
                os.utime(f, (old, old))

            orig_rd = archiver_reconcile._recorder_output_dir
            orig_rs = archiver_reconcile._recorder_state_dir
            archiver_reconcile._recorder_output_dir = lambda: primary2
            archiver_reconcile._recorder_state_dir = lambda: fallback2
            try:
                reports = archiver_reconcile.reconcile_recordings(db)
                names = {r.username for r in reports}
                ok(names == {"alice", "bob"},
                   "reconcile cover both roots' users")
                ok(db.id_of(str(p_alice)) is not None,
                   "primary recording got a row")
                ok(db.id_of(str(f_bob)) is not None,
                   "fallback recording got a row")
            finally:
                archiver_reconcile._recorder_output_dir = orig_rd
                archiver_reconcile._recorder_state_dir = orig_rs
        finally:
            db.close()

        # 3. primary root does not exist → fallback still registered
        primary3 = tmp / "primary3"
        fallback3 = tmp / "fallback3"
        db = _fresh_db()
        try:
            f_carol = _write_media(fallback3 / "carol" / "carol_1600.mp4",
                                   b"FALLBACK-ONLY")
            old = time.time() - 3600
            os.utime(f_carol, (old, old))

            orig_rd = archiver_reconcile._recorder_output_dir
            orig_rs = archiver_reconcile._recorder_state_dir
            archiver_reconcile._recorder_output_dir = lambda: primary3
            archiver_reconcile._recorder_state_dir = lambda: fallback3
            try:
                reports = archiver_reconcile.reconcile_recordings(db)
                ok({r.username for r in reports} == {"carol"},
                   "missing primary root is skipped, fallback still runs")
                ok(db.id_of(str(f_carol)) is not None,
                   "fallback-only recording got a row")
            finally:
                archiver_reconcile._recorder_output_dir = orig_rd
                archiver_reconcile._recorder_state_dir = orig_rs
        finally:
            db.close()

        # 4. primary "root" is a regular file → iter dir raises OSError
        primary4 = tmp / "primary4"
        fallback4 = tmp / "fallback4"
        db = _fresh_db()
        try:
            file_as_root = primary4
            file_as_root.write_bytes(b"not a directory")
            f_dave = _write_media(fallback4 / "dave" / "dave_1600.mp4",
                                  b"FALLBACK-DAVE")
            old = time.time() - 3600
            os.utime(f_dave, (old, old))

            orig_rd = archiver_reconcile._recorder_output_dir
            orig_rs = archiver_reconcile._recorder_state_dir
            archiver_reconcile._recorder_output_dir = lambda: file_as_root
            archiver_reconcile._recorder_state_dir = lambda: fallback4
            try:
                reports = archiver_reconcile.reconcile_recordings(db)
                ok({r.username for r in reports} == {"dave"},
                   "unreadable primary root is skipped, fallback still runs")
                ok(db.id_of(str(f_dave)) is not None,
                   "fallback recording got a row despite primary OSError")
            finally:
                archiver_reconcile._recorder_output_dir = orig_rd
                archiver_reconcile._recorder_state_dir = orig_rs
        finally:
            db.close()

        # 5. _recorder_state_dir env parsing
        env_dir = tmp / "recorder_env"
        env_dir.mkdir()
        env_file = env_dir / ".env"
        env_file.write_text("STATE_DIR =   /tmp/spaced_state_dir   \n")
        env_path = Path("/tmp/spaced_state_dir")
        orig_config_dir = _osp.config_dir
        _osp.config_dir = lambda _kind: env_dir
        try:
            ok(archiver_reconcile._recorder_state_dir() == env_path,
               "STATE_DIR is stripped from .env")
        finally:
            _osp.config_dir = orig_config_dir

        env_file.write_text("OTHER=1\n")
        orig_config_dir = _osp.config_dir
        _osp.config_dir = lambda _kind: env_dir
        try:
            ok(archiver_reconcile._recorder_state_dir()
               == core_paths.recorder_state_dir(),
               "missing STATE_DIR falls back to core default")
        finally:
            _osp.config_dir = orig_config_dir
    finally:
        ingest.media_prep.prepare = orig_prep            # type: ignore
