"""Seams: producer enqueue into the items table and claim_batch grouping/gating/order."""

from __future__ import annotations

from pathlib import Path

from ._harness import (
    _FakeSend,
    _db_file,
    _drain_once,
    _fresh_db,
    _write_media,
    ok,
    section,
)


# ══════════════════════════════════════════════════════════════════════════════
# Seam 2 — every producer writes the ONE items table; priority + content_hash
# ══════════════════════════════════════════════════════════════════════════════

def test_producer_table_seam(tmp: Path) -> None:
    section("Seam 2: producers → one items table (priority + content_hash)")
    from recorder.enqueue import EnqueueClient, RECORDER_PRIORITY, _recorder_identifier
    from core import CHAT_ID_PRIORITY

    db = _fresh_db()
    try:
        # Archiver-style enqueue (priority 10, content_hash stamped by producer).
        from core.hashing import full_hash
        af = _write_media(tmp / "x" / "alice" / "20240101_1_0.jpg", b"ARCHIVER-BYTES")
        db.add_item(source="archiver", platform="x", username="alice",
                    identifier="x_1", file_path=str(af), priority=10,
                    content_hash=full_hash(af))

        # chat_id-folder files are urgent, but live recordings still win.
        of = _write_media(tmp / "-100123" / "loose.mp4", b"CHAT-ID-BYTES")
        db.add_item(source="orphaned", platform="orphaned", username="-100123",
                    identifier="orphaned_1", file_path=str(of),
                    priority=CHAT_ID_PRIORITY, chat_id="-100123",
                    content_hash=full_hash(of))

        # Recorder LIVE enqueue (priority 5). This is the seam the fix touched:
        # the recorder must now stamp content_hash like every other producer.
        rf = _write_media(tmp / "rec" / "bob" / "bob_1700.mp4", b"RECORDING-BYTES")
        # EnqueueClient opens its OWN ItemStore on the same file → use db_path.
        client = EnqueueClient(_db_file(db))
        inserted = client.enqueue(platform="tiktok", username="bob",
                                  file_path=str(rf), caption="@bob · tiktok · live")
        ok(inserted, "recorder live enqueue inserted a row")

        rec = db.get(db.id_of(str(rf)))
        ok(rec is not None, "recorder row is in the shared table")
        ok(rec.content_hash is not None,
           "recorder live enqueue now STAMPS content_hash (seam fix)")
        ok(rec.content_hash == full_hash(rf),
           "stamped hash equals core.hashing.full_hash (one definition of bytes)")
        ok(rec.identifier == _recorder_identifier(str(rf)),
           "recorder identifier scheme is recorder_<stem>")
        ok(RECORDER_PRIORITY < CHAT_ID_PRIORITY < 10,
           "priority order is recorder, chat_id folder, archiver")

        # The dispatcher claims lowest-priority-number first.
        first = db.claim_next()
        ok(first.source == "recorder",
           "claim_next picks the recorder row first")
        second = db.claim_next()
        ok(second.source == "orphaned", "then the chat_id-folder row")
        third = db.claim_next()
        ok(third.source == "archiver", "then the archiver row")
        ok(db.claim_next() is None, "queue drained — nothing left to claim")
    finally:
        db.close()

def test_local_platform_discovery_seam(tmp: Path) -> None:
    section("Seam 13: local-platform discovery excludes reserved routes")
    from types import SimpleNamespace
    from archiver.orchestrator import _local_platform_names

    # '-100123.t42' is a TOPIC-suffixed route: it must be excluded too. Using
    # is_chat_id here (bare-only) instead of parse_route would MISS the `.t`
    # suffix, auto-adopt it as a platform, and upload to the default chat.
    for name in ("x", "tiktok", "instagram", "unsorted",
                 "-100123", "-100123.t42", "1003547920321.t41478", "library"):
        (tmp / name).mkdir(parents=True, exist_ok=True)
    config = SimpleNamespace(output_dir=str(tmp), local_platforms=())

    names = _local_platform_names(config)
    ok(names == ["library"],
       "only a genuine local platform is auto-discovered "
       "(bare AND topic-suffixed routes excluded)")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 3 — add_item → claim_batch album grouping (media bucket + group key)
# ══════════════════════════════════════════════════════════════════════════════

def test_album_batching_seam(tmp: Path) -> None:
    section("Seam 3: claim_batch album grouping by bucket + group")
    from core.files import ALBUM_MAX

    db = _fresh_db()
    try:
        # 12 photos, same (platform,user,source,caption) → one album capped at
        # ALBUM_MAX; a video in the same group must NOT mix in.
        for i in range(12):
            f = _write_media(tmp / "x" / "al" / f"p{i}.jpg", f"PH{i}".encode())
            db.add_item(source="archiver", platform="x", username="al",
                        identifier=f"p{i}", file_path=str(f), priority=10,
                        caption="album-A")
        vf = _write_media(tmp / "x" / "al" / "v.mp4", b"VID")
        db.add_item(source="archiver", platform="x", username="al",
                    identifier="v", file_path=str(vf), priority=10,
                    caption="album-A")

        batch = db.claim_batch()
        ok(len(batch) == ALBUM_MAX, f"photo album capped at ALBUM_MAX={ALBUM_MAX}")
        ok(all(Path(it.file_path).suffix == ".jpg" for it in batch),
           "video did not mix into the photo album (bucket-homogeneous)")

        # A 'single'-bucket item (gif) is always sent alone.
        gf = _write_media(tmp / "x" / "al" / "g.gif", b"GIF")
        db.add_item(source="archiver", platform="x", username="al",
                    identifier="g", file_path=str(gf), priority=1,
                    caption="album-A")
        solo = db.claim_batch()
        ok(len(solo) == 1 and solo[0].identifier == "g",
           "gif (single bucket) is claimed alone, never albumed")
    finally:
        db.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 3b — claim_batch per-album BYTE cap (oversize album can't wedge the queue)
# ══════════════════════════════════════════════════════════════════════════════

def test_album_byte_cap_seam(tmp: Path) -> None:
    section("Seam 3b: claim_batch caps an album at max_album_bytes")

    db = _fresh_db()
    try:
        # 5 sized videos in one group; cap=2000, each 800B → 2 fit per album
        # (800+800=1600 ok, +800=2400 > 2000). file_size_bytes drives the cap;
        # the on-disk file is tiny (claim reads the column, not the disk).
        for i in range(5):
            f = _write_media(tmp / "o" / "sub" / f"v{i}.mp4", b"V")
            db.add_item(source="orphaned", platform="orphaned", username="orphaned",
                        identifier=f"v{i}", file_path=str(f), priority=10,
                        chat_id="-100999", group_key="-100999/sub",
                        file_size_bytes=800)

        b1 = db.claim_batch(max_album_bytes=2000)
        ok(len(b1) == 2, f"first album byte-capped to 2 items (got {len(b1)})")
        b2 = db.claim_batch(max_album_bytes=2000)
        ok(len(b2) == 2, "second album also 2 items")
        b3 = db.claim_batch(max_album_bytes=2000)
        ok(len(b3) == 1, "trailing item ships alone")
        for it in (*b1, *b2, *b3):
            db.mark_sent(it.id)

        # A lone item bigger than the whole cap still ships (anchor always in).
        big = _write_media(tmp / "o" / "big" / "huge.mp4", b"H")
        db.add_item(source="orphaned", platform="orphaned", username="orphaned",
                    identifier="huge", file_path=str(big), priority=10,
                    chat_id="-100999", group_key="-100999/big",
                    file_size_bytes=9999)
        solo = db.claim_batch(max_album_bytes=2000)
        ok(len(solo) == 1 and solo[0].identifier == "huge",
           "an item larger than the cap ships by itself (never dropped)")
        db.mark_sent(solo[0].id)

        # Legacy NULL sizes count as 0 → still album (uncapped), as before.
        for i in range(3):
            f = _write_media(tmp / "o" / "leg" / f"n{i}.jpg", f"N{i}".encode())
            db.add_item(source="orphaned", platform="orphaned", username="orphaned",
                        identifier=f"n{i}", file_path=str(f), priority=10,
                        chat_id="-100999", group_key="-100999/leg")
        nul = db.claim_batch(max_album_bytes=2000)
        ok(len(nul) == 3, "NULL-size rows count as 0 → album unchanged (no regression)")

        # A truncated group flushes despite a high min-batch gate (no 7-day stall).
        for i in range(4):
            f = _write_media(tmp / "a" / "u" / f"c{i}.mp4", b"C")
            db.add_item(source="archiver", platform="x", username="u",
                        identifier=f"c{i}", file_path=str(f), priority=10,
                        caption="vidgrp", file_size_bytes=1500)
        capped = db.claim_batch(max_album_bytes=2000,
                                min_batch=lambda a: 10, flush_age_s=lambda a: None)
        ok(0 < len(capped) < 10,
           "byte-capped group flushes now, not held behind min_batch=10")
    finally:
        db.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 4 — core.ingest content_hash → dispatcher global-dedup guarantee
# ══════════════════════════════════════════════════════════════════════════════

def test_content_hash_dedup_seam(tmp: Path) -> None:
    section("Seam 4: global content_hash dedup (ingest ↔ dispatcher)")
    from core import register_file
    from core.hashing import full_hash

    db = _fresh_db()
    try:
        same = b"IDENTICAL-MEDIA-CONTENT-FOR-DEDUP-XXXXXXXXXX"
        a = _write_media(tmp / "d" / "a.jpg", same)
        b = _write_media(tmp / "d" / "b.jpg", same)   # byte-identical copy

        r1 = register_file(db, a, source="archiver", platform="x", username="u")
        ok(r1.inserted, "first copy ingested → new row")
        r2 = register_file(db, b, source="archiver", platform="x", username="u")
        ok(not r2.inserted, "byte-identical second copy did NOT create a row")
        ok(r2.outcome.value == "dedup_dropped", "second copy reported dedup_dropped")
        ok(not b.exists(), "redundant on-disk copy was removed (as if never there)")

        # Dispatcher's sent_twin: once one row ships, a DIFFERENT row with the
        # same bytes is suppressed (the guarantee). Add a same-hash row directly.
        c = _write_media(tmp / "d" / "c.jpg", same)
        cid_inserted = db.add_item(source="recorder", platform="tiktok",
                                   username="z", identifier="rec_c",
                                   file_path=str(c), content_hash=full_hash(c))
        ok(cid_inserted, "a same-bytes row from a DIFFERENT (platform,identifier) inserts")
        row1 = db.id_of(str(a))
        # Drive row1 → 'sent' through the real state machine to simulate prior
        # delivery. Claim every pending row ONCE into a list (claim flips them to
        # 'sending'); mark the target sent; requeue the rest exactly once. (Never
        # requeue mid-claim — that resurrects the row and loops forever.)
        claimed_ids = []
        while (it := db.claim_next()) is not None:
            claimed_ids.append(it.id)
        for cid in claimed_ids:
            if cid == row1:
                db.mark_sent(cid)
            else:
                db.requeue(cid)
        ok(row1 in claimed_ids and db.get(row1).status == "sent",
           "row1 marked sent (simulating prior delivery)")
        twin = db.sent_twin(full_hash(c), exclude_id=db.id_of(str(c)))
        ok(twin is not None and twin.id == row1,
           "sent_twin finds the already-delivered bytes (O(log n) index hit)")
        ok(db.sent_twin(None, exclude_id=1) is None,
           "NULL content_hash never matches a twin (never wrongly suppressed)")
    finally:
        db.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 5 — BatchPolicy → claim_batch min-batch gate (defer + flush-age)
# ══════════════════════════════════════════════════════════════════════════════

def test_min_batch_gate_seam(tmp: Path) -> None:
    section("Seam 5: min-batch gate + anti-starvation flush")
    db = _fresh_db()
    try:
        # 3 photos in a group; require min_batch=5 → group is DEFERRED.
        for i in range(3):
            f = _write_media(tmp / "x" / "g" / f"p{i}.jpg", f"q{i}".encode())
            db.add_item(source="archiver", platform="x", username="g",
                        identifier=f"q{i}", file_path=str(f), priority=10,
                        caption="grp")
        got = db.claim_batch(min_batch=lambda a: 5, flush_age_s=lambda a: None)
        ok(got == [], "under-threshold group is deferred (nothing claimed yet)")

        # Same group, flush-age 0-ish → anti-starvation flush claims the partial.
        flushed = db.claim_batch(min_batch=lambda a: 5,
                                 flush_age_s=lambda a: 0.0001)
        ok(len(flushed) == 3,
           "aged partial is flushed despite being below min_batch")

        # Recorder/orphaned exemption is enforced by the dispatcher's closures
        # (source=='archiver' gate only); verify a recorder anchor bypasses it.
        rf = _write_media(tmp / "rec" / "u" / "u_1.mp4", b"RR")
        db.add_item(source="recorder", platform="tiktok", username="u",
                    identifier="rec_u_1", file_path=str(rf), priority=5)

        def _min(anchor):
            return 9 if anchor["source"] == "archiver" else 1

        claimed = db.claim_batch(min_batch=_min, flush_age_s=lambda a: None)
        ok(claimed and claimed[0].source == "recorder",
           "recorder anchor bypasses the min-batch gate (sends immediately)")
    finally:
        db.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 26 — producer enqueue order ↔ dispatcher claim_batch SEND ORDER
# A user's media must drain CONTIGUOUSLY, not interleave with other users'.
# The queue's raw (priority, discovered_at) order scatters a user whose files
# arrive across multiple runs: files downloaded later get a later discovered_at,
# so another user who appeared in between would sort ahead of them. claim_batch
# instead anchors each (platform, username) cluster on its FIRST-APPEARANCE time
# (core.store._CLUSTER_COLS), so later-enqueued files join the user's existing
# block. The contract holds across the cases that actually broke — single-bucket
# items (one send each) and >ALBUM_MAX runs (multiple albums) — while priority
# still dominates (a live recorder cluster drains before an archiver backlog).
# A regression here re-scatters a user's uploads across the timeline.
# ══════════════════════════════════════════════════════════════════════════════

def test_send_order_clustering_seam(tmp: Path) -> None:
    section("Seam 26: enqueue order ↔ claim_batch send-order clustering")
    from core import ItemStore

    def _seed(plan, ext: str) -> ItemStore:
        """plan: [(username, count, discovered_at)] — simulates same-user files
        arriving in separate runs, interleaved with another user's run."""
        db = _fresh_db()
        k = 0
        for user, count, ts in plan:
            for _ in range(count):
                f = _write_media(tmp / f"{ext[1:]}/{user}/{user}_{k}{ext}",
                                 f"{user}{k}".encode())
                db.add_item(source="archiver", platform="x", username=user,
                            identifier=f"{user}_{k}", file_path=str(f), priority=10)
                db.conn.execute("UPDATE items SET discovered_at=? WHERE id=?",
                                (ts, db.id_of(str(f))))
                k += 1
        db.conn.commit()
        return db

    def _drain(db) -> list:
        out = []
        while (b := db.claim_batch(max_items=10)):
            out.append((b[0].username, len(b)))
        return out

    # alice appears first (run @:01), bob runs in between (@:02), alice runs
    # again (@:03). Without clustering, alice's :03 files sort after bob's :02.
    interleaved = [("alice", 2, "2024-01-01T00:00:01Z"),
                   ("bob",   2, "2024-01-01T00:00:02Z"),
                   ("alice", 2, "2024-01-01T00:00:03Z")]

    # Single-bucket (.gif): each file is its own send; the recompute-prone case.
    db = _seed(interleaved, ".gif")
    try:
        ok(_drain(db) == [("alice", 1)] * 4 + [("bob", 1)] * 2,
           "single-bucket: all of alice's files drain before bob's (contiguous)")
    finally:
        db.close()

    # Album bucket, over the 10-item cap: alice 8@:01 + 5@:03 = 13 photos, bob
    # 3@:02 between. Alice's two albums must stay adjacent, ahead of bob.
    db = _seed([("alice", 8, "2024-01-01T00:00:01Z"),
                ("bob",   3, "2024-01-01T00:00:02Z"),
                ("alice", 5, "2024-01-01T00:00:03Z")], ".jpg")
    try:
        ok(_drain(db) == [("alice", 10), ("alice", 3), ("bob", 3)],
           "over-cap: alice's two albums drain back-to-back, then bob")
    finally:
        db.close()

    # Priority still dominates the cluster anchor: a recorder single (priority 5)
    # for a user who appeared LATE (@09:00) must still precede alice's pri-10
    # backlog that appeared at :01.
    db = _seed([("alice", 2, "2024-01-01T00:00:01Z")], ".jpg")
    try:
        rec = _write_media(tmp / "rec" / "zoe.mp4", b"REC")
        db.add_item(source="recorder", platform="tiktok", username="zoe",
                    identifier="rec_1", file_path=str(rec), priority=5)
        db.conn.execute("UPDATE items SET discovered_at='2024-01-01T09:00:00Z' "
                        "WHERE id=?", (db.id_of(str(rec)),))
        db.conn.commit()
        ok(_drain(db) == [("zoe", 1), ("alice", 2)],
           "priority wins over first-appearance: recorder pri-5 cluster drains first")
    finally:
        db.close()

def test_name_cluster_batch_seam(tmp: Path) -> None:
    section("Seam 32: loose files sharing a ≥4-char name run batch into one album")
    from core import ItemStore, PolicyStore, BatchPolicy
    from core.orphaned import ingest_chat_id_dirs, NAME_CLUSTER_PREFIX

    # A small chat_id root of LOOSE files (no subfolders). Three share the run
    # 'sunset'; 'random.jpg' shares nothing → stays an individual send. A photo
    # and a "video" (.mp4, fake bytes are fine — photos/videos both cluster by
    # name; no ffmpeg needed since prepare passes tiny non-probed files through
    # for images and the mp4 here is only asserted at the grouping layer).
    _write_media(tmp / "-100888" / "sunset_01.jpg", b"S1")
    _write_media(tmp / "-100888" / "sunset_02.jpg", b"S2")
    _write_media(tmp / "-100888" / "sunset_03.png", b"S3")
    _write_media(tmp / "-100888" / "random.jpg", b"RR")

    db_file = str(tmp / "seam32.db")
    store = ItemStore.open(db_file)
    ingest_chat_id_dirs(store, tmp, known_platforms=set())

    rows = {n: store.get(store.id_of(str(tmp / "-100888" / n)))
            for n in ("sunset_01.jpg", "sunset_02.jpg", "sunset_03.png",
                      "random.jpg")}
    cluster_keys = {rows[n].group_key for n in
                    ("sunset_01.jpg", "sunset_02.jpg", "sunset_03.png")}
    ok(len(cluster_keys) == 1 and next(iter(cluster_keys)) is not None
       and next(iter(cluster_keys)).startswith(NAME_CLUSTER_PREFIX),
       "the three 'sunset*' files share ONE synthetic cluster group_key")
    ok(rows["random.jpg"].group_key is None,
       "the unrelated 'random.jpg' keeps NO group_key (still an individual send)")
    store.close()

    ps = PolicyStore()
    ps.set(BatchPolicy.SIZE_KEY, 1)
    fake = _FakeSend()
    _drain_once(db_file, ps, fake, default_chat_id="-100999")

    ok(len(fake.sent_albums) == 1
       and sorted(Path(p).name for p in fake.sent_albums[0])
           == ["sunset_01.jpg", "sunset_02.jpg", "sunset_03.png"],
       "the three 'sunset*' files ship as ONE album")
    ok(sorted(Path(p).name for p in fake.sent_singles) == ["random.jpg"],
       "the unrelated file still ships as its own single")
    ok(any("sunset_01" in c and "sunset_02" in c and "sunset_03" in c
           for c in fake.album_captions),
       "the cluster album caption lists every filename (one per line)")

def test_name_cluster_threshold_seam(tmp: Path) -> None:
    section("Seam 33: a folder with >=10 loose files is NOT name-clustered")
    from core import ItemStore
    from core.orphaned import ingest_chat_id_dirs

    # 10 files that all share 'shot' — but the folder is at the cap, so the
    # batching rule stands down and every file sends individually (unchanged).
    for i in range(10):
        _write_media(tmp / "-100111" / f"shot_{i:02d}.jpg", f"S{i}".encode())

    db_file = str(tmp / "seam33.db")
    store = ItemStore.open(db_file)
    ingest_chat_id_dirs(store, tmp, known_platforms=set())
    keys = [store.get(store.id_of(str(tmp / "-100111" / f"shot_{i:02d}.jpg")))
            .group_key for i in range(10)]
    ok(all(k is None for k in keys),
       "10 loose files (== cap) → none clustered, all individual sends")
    store.close()
