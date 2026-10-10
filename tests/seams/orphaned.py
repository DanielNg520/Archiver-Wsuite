"""Seams: orphaned-folder ingest (keep-original, hashtag roots, noname, mixed albums)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ._harness import (
    _FakeSend,
    _drain_once,
    _ffmpeg_present,
    _make_video,
    _write_media,
    ok,
    section,
)


# ══════════════════════════════════════════════════════════════════════════════
# Seam 21 — keep-original documents end-to-end: orphaned.ingest_folder (core)
# → claim_batch grouping → the full drain → fake send. Proves the cross-worker
# contract for a mixed folder: a non-streamable original ships as its OWN single
# (so send() documents it) while its converted preview albums with the sibling
# streamable videos, and an excluded .flv contributes only its converted copy.
# ══════════════════════════════════════════════════════════════════════════════

def test_keep_original_document_seam(tmp: Path) -> None:
    section("Seam 21: keep-original documents (ingest → drain → send)")
    if not _ffmpeg_present():
        ok(True, "ffmpeg/ffprobe absent — keep-original seam skipped")
        return
    from core import (ItemStore, PolicyStore, DeletePolicy,
                      RecorderDeletePolicy, BatchPolicy, DeletionGuard)
    from core.orphaned import ingest_folder
    from dispatcher.drain import drain_forever
    from dispatcher.config import DispatcherConfig
    from dispatcher.tg_router import TelegramRouter

    chat_id = "-100555"
    folder = tmp / chat_id
    album = folder / "album"
    album.mkdir(parents=True)
    # A subfolder so the streamable copies album together. Three sources:
    #   keep.mkv  — non-streamable → converted (album) + kept as a DOCUMENT
    #   plain.mp4 — already streamable → album as-is
    #   raw.flv   — non-streamable but EXCLUDED → only its converted copy ships
    _make_video(album / "keep.mkv", container="matroska")
    _make_video(album / "plain.mp4", container="mp4")
    _make_video(album / "raw.flv", container="flv")

    db_file = str(tmp / "seam21.db")
    store = ItemStore.open(db_file)
    rep = ingest_folder(store, folder, chat_id=chat_id, guard=None)
    ok(rep.inserted == 4,
       "4 rows: keep.mp4 + plain.mp4 + raw.mp4 (album) + keep.mkv (document)")
    ok((album / "keep.mkv").exists() and not (album / "raw.flv").exists(),
       "kept .mkv stays on disk; excluded .flv original is deleted")
    store.close()

    ps = PolicyStore()
    ps.set("delete_after_upload", False)        # keep originals; we assert sends
    ps.set(BatchPolicy.SIZE_KEY, 1)             # don't defer the small album
    cfg = DispatcherConfig(
        telegram=None, default_chat_id=chat_id, db_path=db_file,
        policy_store=ps, poll_interval_s=0.01, max_retries=3,
        inter_album_sleep=0.0, stuck_claim_min=10, failed_retention_days=0,
    )
    store = ItemStore.open(db_file)
    fake = _FakeSend()
    router = TelegramRouter(default_chat_id=chat_id)
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

    asyncio.run(_run())

    # The converted previews (keep/plain/raw → .mp4) went up as ONE album.
    album_names = sorted(Path(p).name for p in fake.sent_albums[0]) \
        if fake.sent_albums else []
    ok(album_names == ["keep.mp4", "plain.mp4", "raw.mp4"],
       "the three streamable copies ship as one album (converted + native)")
    # The kept .mkv shipped as its OWN single with the streamable net DISABLED,
    # so send() takes the force_document branch — never albumed with its preview.
    singles = {Path(p).name: net for p, net in
               zip(fake.sent_singles, fake.sent_ensure_streamable)}
    ok(singles == {"keep.mkv": False},
       "only the kept .mkv sent as a single, net off (→ document at send)")
    ok(all("raw.flv" != Path(p).name for p in
            fake.sent_singles + [f for a in fake.sent_albums for f in a]),
       "the excluded .flv original is never sent (convert-only)")
    store.close()

# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# Seam 28 — `#hashtag` folders are VIRTUAL ROOTS: a file directly inside a
# `chat_id/#tag/` folder uploads INDIVIDUALLY (like a file in the chat_id root),
# captioned `#tag file_name`; a deeper subfolder `chat_id/#tag/sub/` is still an
# album (`#tag sub` header). A non-hashtag subfolder is unchanged (albums).
# Guards the contract between core.orphaned._route_for and drain.orphaned_caption.
# ══════════════════════════════════════════════════════════════════════════════

def test_hashtag_root_seam(tmp: Path) -> None:
    section("Seam 28: #hashtag folders are virtual roots (individual vs album)")
    from core import ItemStore, PolicyStore, BatchPolicy
    from core.orphaned import ingest_chat_id_dirs

    # Directly under #JAV → individual; under #Asian/Eli Shaw → album; under a
    # plain (non-hashtag) Nainoi folder → album (existing behavior, unchanged).
    _write_media(tmp / "-100333" / "#JAV" / "one.jpg", b"J1")
    _write_media(tmp / "-100333" / "#JAV" / "two.jpg", b"J2")
    _write_media(tmp / "-100333" / "#Asian" / "Eli Shaw" / "a.jpg", b"AA")
    _write_media(tmp / "-100333" / "#Asian" / "Eli Shaw" / "b.jpg", b"BB")
    _write_media(tmp / "-100333" / "Nainoi" / "x.jpg", b"XX")
    _write_media(tmp / "-100333" / "Nainoi" / "y.jpg", b"YY")

    db_file = str(tmp / "seam28.db")
    store = ItemStore.open(db_file)
    ingest_chat_id_dirs(store, tmp, known_platforms=set())
    jav = [store.get(store.id_of(str(tmp / "-100333" / "#JAV" / n)))
           for n in ("one.jpg", "two.jpg")]
    ok(all(r.group_key is None for r in jav),
       "files directly in #JAV have NO group_key (upload individually)")
    eli = store.get(store.id_of(str(tmp / "-100333" / "#Asian" / "Eli Shaw" / "a.jpg")))
    ok(eli.group_key == "-100333/#Asian/Eli Shaw",
       "a file under #Asian/Eli Shaw albums by its full subpath")
    nai = store.get(store.id_of(str(tmp / "-100333" / "Nainoi" / "x.jpg")))
    ok(nai.group_key == "-100333/Nainoi",
       "a plain (non-hashtag) subfolder still albums — behavior unchanged")
    store.close()

    ps = PolicyStore()
    ps.set(BatchPolicy.SIZE_KEY, 1)
    fake = _FakeSend()
    _drain_once(db_file, ps, fake, default_chat_id="-100999")

    ok(sorted(Path(p).name for p in fake.sent_singles) == ["one.jpg", "two.jpg"],
       "the two #JAV files were sent as individual singles, not an album")
    ok(sorted(fake.single_captions) == ["#JAV one", "#JAV two"],
       "each individual caption is '#JAV <file>' (hashtag stays clickable)")
    album_caps = sorted(fake.album_captions)
    ok(any(c.startswith("#Asian Eli Shaw") for c in album_caps),
       "#Asian/Eli Shaw album header is space-joined '#Asian Eli Shaw'")
    ok(any(c.startswith("Nainoi") for c in album_caps),
       "the plain Nainoi album header is just 'Nainoi'")

def test_noname_folder_seam(tmp: Path) -> None:
    section("Seam 29: [noname] album folders drop per-file names from the caption")
    from core import ItemStore, PolicyStore, BatchPolicy
    from core.orphaned import ingest_chat_id_dirs

    # An album folder tagged `[noname]` → caption is the folder's own text with
    # the marker stripped, NO filenames. A sibling plain folder is unaffected.
    _write_media(tmp / "-100444" / "[noname] Day at the beach" / "IMG_2201.jpg", b"B1")
    _write_media(tmp / "-100444" / "[noname] Day at the beach" / "IMG_2202.jpg", b"B2")
    _write_media(tmp / "-100444" / "Nainoi" / "x.jpg", b"XX")
    _write_media(tmp / "-100444" / "Nainoi" / "y.jpg", b"YY")

    db_file = str(tmp / "seam29.db")
    store = ItemStore.open(db_file)
    ingest_chat_id_dirs(store, tmp, known_platforms=set())
    beach = store.get(store.id_of(
        str(tmp / "-100444" / "[noname] Day at the beach" / "IMG_2201.jpg")))
    ok(beach.group_key == "-100444/[noname] Day at the beach",
       "a `[noname]` folder still albums by its subpath (plain, not a #root)")
    store.close()

    ps = PolicyStore()
    ps.set(BatchPolicy.SIZE_KEY, 1)
    fake = _FakeSend()
    _drain_once(db_file, ps, fake, default_chat_id="-100999")

    album_caps = sorted(fake.album_captions)
    ok("Day at the beach" in album_caps,
       "the `[noname]` album caption is just 'Day at the beach' — marker & names dropped")
    ok(not any("IMG_2201" in c or "IMG_2202" in c for c in album_caps),
       "no filename leaked into the `[noname]` album caption")
    ok(any(c.startswith("Nainoi") and ("x" in c or "y" in c) for c in album_caps),
       "the sibling plain Nainoi album still lists its filenames (unchanged)")

def test_orphaned_mixed_album_seam(tmp: Path) -> None:
    section("Seam 30: chat_id folders — mixed photo+video album, grouped docs, media-first")
    from core import ItemStore, PolicyStore, BatchPolicy

    db_file = str(tmp / "seam30.db")
    db = ItemStore.open(db_file)
    gk = "-100777/trip"

    def _add(name: str, payload: bytes) -> None:
        f = _write_media(tmp / "-100777" / "trip" / name, payload)
        stem = Path(name).stem
        db.add_item(source="orphaned", platform="orphaned", username="orphaned",
                    identifier=f"trip_{stem}", file_path=str(f),
                    chat_id="-100777", group_key=gk, priority=50)

    # Documents (.mkv) enqueued FIRST → earlier discovered_at. The media-first
    # ordering must still hold them behind the subfolder's inline media.
    _add("orig0.mkv", b"MKV0")
    _add("orig1.mkv", b"MKV1")
    # Inline media: 3 photos + 2 videos → must collapse into ONE mixed album
    # (NOT split into a photo album + a video album).
    for i in range(3):
        _add(f"p{i}.jpg", f"PH{i}".encode())
    for i in range(2):
        _add(f"v{i}.mp4", f"VID{i}".encode())

    ps = PolicyStore()
    ps.set(BatchPolicy.SIZE_KEY, 1)
    fake = _FakeSend()
    _drain_once(db_file, ps, fake, default_chat_id="-100999")
    db.close()

    ok(len(fake.sent_albums) == 2,
       "exactly two albums: one mixed media album + one document album (not 3)")
    media_suffixes = {Path(p).suffix for p in fake.sent_albums[0]}
    ok(len(fake.sent_albums[0]) == 5 and media_suffixes == {".jpg", ".mp4"},
       "media album mixes 3 photos + 2 videos in ONE group of 5")
    ok(all(Path(p).suffix == ".mkv" for p in fake.sent_albums[1])
       and len(fake.sent_albums[1]) == 2,
       "the two .mkv documents are grouped into one document album")
    ok(fake.album_as_documents == [False, True],
       "media shipped inline FIRST, documents shipped as_documents SECOND "
       "(even though the .mkv were enqueued earlier)")

def test_orphaned_no_trace_and_pseudo_platform_seam(tmp: Path) -> None:
    section("Seam 31: chat_id drop-zone leaves no trace; pseudo-platform keeps dedup")
    from core import ItemStore, ingest_chat_id_dirs
    from core.ingest import register_file, IngestOutcome
    from core.hashing import full_hash
    from archiver.reconcile import reconcile_pseudo_platform

    out = tmp / "out"
    same = b"IDENTICAL-DROP-ZONE-BYTES-XXXXXXXXXXXXXXXXX"

    # ── chat_id drop-zone: byte-identical files BOTH upload (dedup bypassed) ──
    db = ItemStore.open(str(tmp / "nt.db"))
    a = _write_media(out / "-100123" / "a.mp4", same)
    b = _write_media(out / "-100123" / "b.mp4", same)   # byte-identical copy
    r1 = register_file(db, a, source="orphaned", platform="orphaned",
                       username="-100123", chat_id="-100123")
    r2 = register_file(db, b, source="orphaned", platform="orphaned",
                       username="-100123", chat_id="-100123")
    ok(r1.outcome == IngestOutcome.INSERTED
       and r2.outcome == IngestOutcome.INSERTED,
       "chat_id drop-zone bypasses dedup: identical files BOTH enqueue")

    # Re-add after a prior sent+deleted (leave-no-trace) → uploads AGAIN.
    aid = db.id_of(str(a))
    while (it := db.claim_next()) is not None:
        db.mark_sent(it.id)
    db.delete(aid)                                       # maybe_delete: row gone
    a2 = _write_media(out / "-100123" / "a.mp4", same)   # user re-drops it
    r3 = register_file(db, a2, source="orphaned", platform="orphaned",
                       username="-100123", chat_id="-100123")
    ok(r3.outcome == IngestOutcome.INSERTED,
       "a chat_id file re-added after send+delete RE-UPLOADS (the reported bug)")

    # An orphaned 'sent' row must never suppress another item as a twin.
    ok(db.sent_twin(full_hash(a2), exclude_id=-1) is None,
       "sent_twin excludes orphaned rows (a drop-zone copy never gates others)")
    db.close()

    # ── archiver source is UNCHANGED: byte-dup still collapses ──
    db = ItemStore.open(str(tmp / "arch.db"))
    pa = _write_media(out / "x" / "u" / "a.jpg", same)
    pb = _write_media(out / "x" / "u" / "b.jpg", same)
    register_file(db, pa, source="archiver", platform="x", username="u")
    rr = register_file(db, pb, source="archiver", platform="x", username="u")
    ok(rr.outcome == IngestOutcome.DEDUP_DROPPED,
       "real-source (archiver) byte-dup still dedup_dropped (global dedup kept)")
    db.close()

    # ── pseudo-platform: a non-chat_id folder ingests upload-only, dedup kept ──
    db = ItemStore.open(str(tmp / "ps.db"))
    _write_media(out / "xiaohongshu" / "set" / "20240101_p1_0.jpg", b"XHS-1")
    _write_media(out / "xiaohongshu" / "set" / "20240102_p2_0.jpg", b"XHS-2")
    seen: list[str] = []
    reports = ingest_chat_id_dirs(
        db, out, known_platforms=["x", "tiktok", "instagram"],
        pseudo_ingest=lambda name, sd: (
            seen.append(name), reconcile_pseudo_platform(name, sd, db))[-1])
    ok(seen == ["xiaohongshu"],
       "a folder that is neither platform nor chat_id → pseudo-platform ingest")
    ok(any(r.pseudo_dir and r.chat_id == "xiaohongshu" for r in reports),
       "the pseudo-platform folder is reported as pseudo_dir")
    row = db.conn.execute(
        "SELECT COUNT(*) c, SUM(source='archiver') a, SUM(chat_id IS NULL) nc "
        "FROM items WHERE platform='xiaohongshu'").fetchone()
    ok(row["c"] == 2 and row["a"] == 2 and row["nc"] == 2,
       "pseudo-platform rows: source='archiver', chat_id NULL (env-routed)")

    # ── RESERVED folders are NEVER pseudo-ingested ────────────────────────────
    # Regression guard (2026-07-18): the `unsorted/` drop folder is owned by the
    # sort sweep, and a built-in platform folder whose download is disabled this
    # run still belongs to its extractor — neither must fall into the pseudo
    # branch and upload raw as `@<name> · <name>`. Both are absent from
    # known_platforms here, so only the reserved_names guard keeps them out.
    _write_media(out / "unsorted" / "alice_1780000000_1.mp4", b"LOOSE")
    _write_media(out / "tiktok" / "loose.mp4", b"DISABLED-BUILTIN")
    seen2: list[str] = []
    reports2 = ingest_chat_id_dirs(
        db, out, known_platforms=["x", "instagram"],   # tiktok "disabled"
        reserved_names={"x", "tiktok", "instagram"},
        pseudo_ingest=lambda name, sd: (
            seen2.append(name), reconcile_pseudo_platform(name, sd, db))[-1])
    ok("unsorted" not in seen2,
       "the unsorted/ drop folder is NOT pseudo-ingested (owned by sort sweep)")
    ok("tiktok" not in seen2,
       "a disabled built-in platform folder is NOT pseudo-ingested")
    ok(db.conn.execute(
        "SELECT COUNT(*) c FROM items WHERE platform IN ('unsorted','tiktok')"
    ).fetchone()["c"] == 0,
       "no rows created for reserved folders (no @unsorted·unsorted uploads)")

    # Re-introduction dedup KEPT: a re-added already-sent copy is NOT re-uploaded.
    while (it := db.claim_next()) is not None:
        db.mark_sent(it.id)
    reintro = _write_media(out / "xiaohongshu" / "set" / "reintro.jpg", b"XHS-1")
    rep = reconcile_pseudo_platform("xiaohongshu", out / "xiaohongshu", db)
    ok(rep.deleted_dupes == 1 and not reintro.exists(),
       "pseudo-platform re-added already-SENT bytes are NOT re-uploaded (dedup kept)")
    db.close()
