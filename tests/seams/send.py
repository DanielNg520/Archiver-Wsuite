"""Seams: send strategy (stall watchdog, progress, streamable net, split/fast albums)."""

from __future__ import annotations

import asyncio
import sys
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
# Seam 18 — the stall watchdog. A silent TCP freeze raises nothing, so without
# a per-attempt deadline the serial drain loop awaits forever (observed: a
# whole night of zero uploads, one row wedged in 'sending'). The retry
# envelope must convert "no progress" into a counted, retryable failure and
# recycle the presumed-wedged connection between attempts.
# ══════════════════════════════════════════════════════════════════════════════

def test_send_stall_watchdog_seam() -> None:
    section("Seam 18: send stall watchdog (deadline + reconnect)")
    from dispatcher.send import TelethonSendStrategy

    strategy = TelethonSendStrategy(
        api_id=0, api_hash="", phone="", session_name="stub",
        max_retries=2, retry_base_delay=0.01,
        stall_base_timeout_s=0.05, stall_min_rate_kib_s=128.0,
    )

    class _StubClient:
        def __init__(self):
            self.disconnects = 0
            self.connects = 0
        async def disconnect(self):
            self.disconnects += 1
        async def connect(self):
            self.connects += 1

    stub = _StubClient()
    strategy._client = stub  # bypass __aenter__: no network in tests

    # deadline math: fixed grace + payload at the floor rate
    ok(strategy._stall_timeout(0) == 0.05,
       "empty payload → base timeout only")
    ok(abs(strategy._stall_timeout(128 * 1024 * 10) - (0.05 + 10.0)) < 1e-6,
       "payload timeout scales by the floor-rate assumption")

    # a send that never completes must fail after max_retries, not hang
    calls = {"n": 0}
    async def _hang():
        calls["n"] += 1
        await asyncio.sleep(60)

    result = asyncio.run(
        strategy._send_with_retries(_hang, what="stub", payload_bytes=0))
    ok(not result.ok and "stalled" in (result.error or ""),
       "eternal stall becomes a counted failure, not an eternal await")
    ok(calls["n"] == 2, "each retry got its own deadline")
    ok(stub.disconnects == 2 and stub.connects == 2,
       "wedged connection is recycled before every retry")

    # first attempt stalls, second succeeds → retry actually recovers
    state = {"n": 0}
    async def _flaky():
        state["n"] += 1
        if state["n"] == 1:
            await asyncio.sleep(60)

    result = asyncio.run(
        strategy._send_with_retries(_flaky, what="stub", payload_bytes=0))
    ok(result.ok, "one stalled attempt then success → SendResult.ok")

    # THE FIX: a huge payload must NOT hide a hang behind a payload-scaled total
    # deadline (10 GB → ~42 h). With NO progress ticks, the no-progress watchdog
    # trips at ~grace regardless of payload size.
    import time as _t
    async def _hang_big():
        await asyncio.sleep(60)
    t0 = _t.monotonic()
    result = asyncio.run(strategy._send_with_retries(
        _hang_big, what="big", payload_bytes=10 * 1024**3))   # ceiling ≈ 22 h
    elapsed = _t.monotonic() - t0
    ok(not result.ok and "stalled" in (result.error or ""),
       "huge-payload hang still fails via no-progress (not hidden by the ceiling)")
    ok(elapsed < 5.0,
       f"caught in ~grace, not the payload-scaled ceiling ({elapsed:.2f}s < 5s)")

    # NO false stall: steady progress ticks keep a long-but-live upload alive
    # even though it runs longer than the grace window.
    live = {"n": 0}
    async def _live():
        live["n"] += 1
        for _ in range(6):
            await asyncio.sleep(0.03)
            strategy._last_progress_ts = _t.monotonic()   # simulate a progress tick
    # payload sized so the absolute ceiling (~10s) isn't the binding limit —
    # only the no-progress grace is, which the ticks keep resetting.
    result = asyncio.run(strategy._send_with_retries(
        _live, what="live", payload_bytes=128 * 1024 * 10))
    ok(result.ok and live["n"] == 1,
       "steady progress ticks prevent a false stall (runs > grace, still ok)")

    # A NETWORK error must trigger an explicit reconnect. Telethon's own
    # auto_reconnect is OFF (it raced our _force_reconnect → 'NoneType'.connect
    # hangs), so the dispatcher is the SOLE reconnect authority: without this,
    # every retry would hit the same dead socket. First attempt errors → reconnect
    # → second succeeds.
    stub2 = _StubClient()
    strategy._client = stub2
    netstate = {"n": 0}
    async def _neterr():
        netstate["n"] += 1
        if netstate["n"] == 1:
            raise ConnectionError("Connection to Telegram failed 5 time(s)")

    result = asyncio.run(
        strategy._send_with_retries(_neterr, what="stub", payload_bytes=0))
    ok(result.ok, "network error then success → SendResult.ok")
    ok(stub2.disconnects == 1 and stub2.connects == 1,
       "ConnectionError triggers ONE explicit _force_reconnect before the retry "
       "(sole reconnect authority; Telethon auto_reconnect is off)")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 19 — upload-progress heartbeat. The drain's send strategy WRITES a JSON
# heartbeat; `dispatcher status` and `ops health` READ it from other processes.
# The seam contract: atomic, throttled-but-never-misses-the-final-tick, and
# self-expiring (stale timestamp or dead writer pid reads as "idle", so a
# crashed dispatcher can't leave a lying status line behind).
# ══════════════════════════════════════════════════════════════════════════════

def test_upload_progress_seam(tmp: Path) -> None:
    section("Seam 19: upload progress heartbeat (writer ↔ readers)")
    import json
    import subprocess as sp
    from dispatcher.progress import ProgressReporter, read_progress, describe

    tmp.mkdir(parents=True, exist_ok=True)
    pf = tmp / "progress.json"

    rep = ProgressReporter(path=pf, min_interval_s=0.0)
    cb = rep.callback("/x/video.mp4", batch_pos=3, batch_total=10)
    cb(52_428_800, 140_826_032)
    p = read_progress(pf)
    ok(p is not None and p["file"] == "/x/video.mp4" and p["sent"] == 52_428_800,
       "heartbeat written and readable cross-call")
    desc = describe(p)
    ok("video.mp4" in desc and "[file 3/10]" in desc and "37%" in desc,
       f"describe() is human-readable ({desc})")

    # rate + ETA derive from byte/timestamp deltas
    fake = {"file": "/x/a.mp4", "sent": 50, "total": 100,
            "started_at": 0.0, "updated_at": 50.0}
    ok("1.0KB" not in describe(fake) and "ETA 50s" in describe(fake),
       "describe() derives rate and ETA from the heartbeat")

    # throttle: mid ticks suppressed, final tick never dropped
    rep2 = ProgressReporter(path=pf, min_interval_s=9999)
    cb2 = rep2.callback("/x/video.mp4")
    cb2(1, 100)            # first write (throttle window opens)
    cb2(2, 100)            # suppressed
    ok(read_progress(pf)["sent"] == 1, "mid-upload ticks are throttled")
    cb2(100, 100)          # sent == total bypasses the throttle
    ok(read_progress(pf)["sent"] == 100, "final tick always lands (100%)")

    # staleness self-expiry
    data = json.loads(pf.read_text())
    data["updated_at"] -= 3600
    pf.write_text(json.dumps(data))
    ok(read_progress(pf) is None, "stale heartbeat reads as idle")

    # dead-writer self-expiry: a just-exited child's pid is guaranteed dead
    dead_pid = int(sp.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True, text=True).stdout.strip())
    data["updated_at"] = __import__("time").time()
    data["pid"] = dead_pid
    pf.write_text(json.dumps(data))
    ok(read_progress(pf) is None, "dead writer pid reads as idle")

    # clear() removes the artifact entirely
    cb(1, 2)
    rep.clear()
    ok(read_progress(pf) is None, "clear() leaves no heartbeat behind")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 20 — the send-time streamable net. A recording whose recorder remux fell
# back to the raw container (.flv/.ts), or any video that bypassed ingest-time
# prep, reaches the dispatcher non-streamable. The send strategy must convert it
# to a streamable .mp4 BEFORE handing it to Telegram, send the converted bytes,
# and clean the temp up — while leaving an already-streamable file untouched
# (no needless re-encode) and never mutating the on-disk original.
# ══════════════════════════════════════════════════════════════════════════════

def test_send_streamable_net_seam(tmp: Path) -> None:
    section("Seam 20: send-time streamable net (non-streamable video → mp4)")
    if not _ffmpeg_present():
        ok(True, "ffmpeg/ffprobe absent — net seam skipped (toolchain missing)")
        return
    from dispatcher.send import TelethonSendStrategy

    strategy = TelethonSendStrategy(
        api_id=0, api_hash="", phone="", session_name="stub")

    from telethon.tl import types as tg_types

    # Video sends now funnel through the parallel uploader: the strategy
    # upload_file()s the bytes (→ a handle) and send_file()s an
    # InputMediaUploadedDocument. The fake records every upload and the media so
    # we can introspect what actually went to Telegram.
    class _Handle:
        def __init__(self, path): self.path = str(path)

    class _CaptureClient:
        def __init__(self):
            self.uploaded: list[str] = []   # every upload_file path (file+thumbs)
            self.sent: list = []            # InputMedia objects → send_file
        async def upload_file(self, file, **kw):
            self.uploaded.append(str(file))
            return _Handle(file)
        async def send_file(self, peer, file, **kw):
            self.sent.append(file)
        async def disconnect(self): ...
        async def connect(self): ...

    def media_file(m) -> str | None:        # the uploaded bytes behind a media
        return getattr(getattr(m, "file", None), "path", None)

    def media_name(m) -> str | None:        # explicit DocumentAttributeFilename
        for a in (getattr(m, "attributes", None) or []):
            if isinstance(a, tg_types.DocumentAttributeFilename):
                return a.file_name
        return None

    def is_document(m) -> bool:             # a document has NO video attribute
        attrs = getattr(m, "attributes", None) or []
        return not any(isinstance(a, tg_types.DocumentAttributeVideo)
                       for a in attrs)

    # (a) a non-streamable .flv → Telegram receives a CONVERTED .mp4.
    flv = _make_video(tmp / "u" / "clip.flv", container="flv")
    cap = _CaptureClient()
    strategy._client = cap                      # bypass __aenter__: no network
    res = asyncio.run(strategy.send(peer="p", file_path=str(flv), caption="c"))
    ok(res.ok and len(cap.sent) == 1, "non-streamable .flv send succeeded")
    wire = media_file(cap.sent[0])
    ok(wire and wire.endswith(".mp4") and wire != str(flv),
       "dispatcher converted .flv → streamable .mp4 before the Telegram send")
    eff_name = media_name(cap.sent[0]) or Path(wire).name
    ok(eff_name == "clip.mp4",
       "upload filename is the clean original stem + .mp4 (no .tgprep tag)")
    ok(flv.exists() and flv.suffix == ".flv",
       "the on-disk original recording is left untouched (never lose bytes)")
    ok(not (tmp / "u" / "clip.mp4").exists(),
       "the converted temp was cleaned up after the send")

    # (b) an already-streamable .mp4 → passthrough: sent untouched, no temp.
    mp4 = _make_video(tmp / "u" / "ok.mp4", container="mp4")
    cap2 = _CaptureClient()
    strategy._client = cap2
    res2 = asyncio.run(strategy.send(peer="p", file_path=str(mp4), caption="c"))
    ok(res2.ok and media_file(cap2.sent[0]) == str(mp4),
       "already-streamable .mp4 is sent as-is (no needless re-encode)")
    ok(sorted(p.name for p in (tmp / "u").iterdir()) == ["clip.flv", "ok.mp4"],
       "no temp artifacts left behind by either send")

    # (c) ensure_streamable=False (a source that prepped at ingest, e.g. an
    # orphaned .mkv kept as a document) → the net is skipped, raw bytes ship.
    mkv = _make_video(tmp / "u" / "keep.mkv", container="matroska")
    cap3 = _CaptureClient()
    strategy._client = cap3
    res3 = asyncio.run(strategy.send(
        peer="p", file_path=str(mkv), caption="c", ensure_streamable=False))
    ok(res3.ok and media_file(cap3.sent[0]) == str(mkv),
       "ensure_streamable=False ships the original .mkv as-is (no conversion)")
    ok(not (tmp / "u" / "keep.mp4").exists(),
       "no conversion temp created when the net is skipped")
    ok(is_document(cap3.sent[0]),
       "the non-streamable kept .mkv is sent as a DOCUMENT, not a 2nd video "
       "(otherwise Telegram shows the recording twice)")

    # (d) ensure_streamable=False on an ALREADY-streamable .mp4 (the common
    # prepped-at-ingest case) keeps the normal streaming-video path — only
    # non-streamable kept originals become documents.
    mp4b = _make_video(tmp / "u" / "ingested.mp4", container="mp4")
    cap4 = _CaptureClient()
    strategy._client = cap4
    res4 = asyncio.run(strategy.send(
        peer="p", file_path=str(mp4b), caption="c", ensure_streamable=False))
    ok(res4.ok and not is_document(cap4.sent[0]),
       "a streamable as-is .mp4 still ships as a normal video, not a document")

    # (e) an as-is streamable file stored with the internal ".tgprep" marker
    # (an incompatible-codec .mp4 converted in place at ingest) must upload with
    # a CLEAN name — the tag never reaches Telegram, even on the as-is path.
    tagged = _make_video(tmp / "u" / "show.tgprep.mp4", container="mp4")
    cap5 = _CaptureClient()
    strategy._client = cap5
    res5 = asyncio.run(strategy.send(
        peer="p", file_path=str(tagged), caption="c", ensure_streamable=False))
    ok(res5.ok and media_file(cap5.sent[0]) == str(tagged),
       "the real .tgprep file on disk is what gets uploaded")
    ok(media_name(cap5.sent[0]) == "show.mp4",
       "but Telegram is told the clean name 'show.mp4' (no .tgprep leak)")

    # (f) ALBUM path: _send_video_album_fast uploads each item with
    # attributes=None (an explicit DocumentAttributeFilename would break Telegram
    # grouping), so the ".tgprep" strip must happen on the DERIVED filename attr
    # inside _upload_document — otherwise a split album's ".tgprep" part leaks its
    # on-disk name to the chat. Drive _upload_document directly with attributes=
    # None (exactly how the album path calls it) and confirm the wire name is clean.
    cap6 = _CaptureClient()
    strategy._client = cap6
    album_item = asyncio.run(strategy._upload_document(
        str(tagged), attributes=None, thumb_path=None,
        supports_streaming=True, force_document=False,
        progress_cb=None))
    ok(media_name(album_item) == "show.mp4",
       "album item (attributes=None) also gets the clean name — no .tgprep leak")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 23 — split-part albums: an oversize original split into parts shares ONE
# core.split_group_key, so claim_batch albums the parts together (despite each
# part carrying a different per-part caption), and the drain's min-batch gate is
# EXEMPT for a split group (flush the complete unit immediately, never hold it
# waiting for the archiver batch size). Guards the contract between core.grouping,
# core.store.claim_batch, and dispatcher.drain.
# ══════════════════════════════════════════════════════════════════════════════

def test_split_album_seam(tmp: Path) -> None:
    section("Seam 23: split-part albums (shared group_key, gate-exempt flush)")
    from core import ItemStore, PolicyStore, BatchPolicy, split_group_key
    from core.hashing import full_hash

    gk = split_group_key("x", "alice", "bigvideo")
    db_file = str(tmp / "seam23.db")
    store = ItemStore.open(db_file)
    parts = []
    for n in range(3):
        # Part 1 carries the internal ".tgprep" marker on disk (a remuxed copy):
        # it must ship as that real file but be NAMED cleanly in the caption.
        stem = f"bigvideo_part{n:03d}" + (".tgprep" if n == 1 else "")
        f = _write_media(tmp / "x" / "alice" / f"{stem}.mp4", bytes([n]) * 300)
        parts.append(f)
        # Distinct per-part captions on purpose: only the shared group_key may
        # bind them, never a coincidentally-equal caption.
        store.add_item(source="archiver", platform="x", username="alice",
                       identifier=f"bigvideo_p{n}", file_path=str(f),
                       priority=10, group_key=gk,
                       caption=f"@alice · tiktok · live · bigvideo_part{n:03d} #live",
                       content_hash=full_hash(f))
    store.close()

    ps = PolicyStore()
    # Archiver min-batch gate set HIGH: a non-split group of 3 would be deferred.
    # The split exemption is the only reason these flush.
    ps.set(BatchPolicy.SIZE_KEY, 10)
    fake = _FakeSend()
    _drain_once(db_file, ps, fake, default_chat_id="-100123")

    ok(len(fake.sent_albums) == 1 and len(fake.sent_albums[0]) == 3,
       "all 3 split parts shipped as ONE album despite min_batch=10 (gate-exempt)")
    ok(sorted(Path(p).name for p in fake.sent_albums[0]) ==
       ["bigvideo_part000.mp4", "bigvideo_part001.tgprep.mp4",
        "bigvideo_part002.mp4"],
       "the album ships the real files incl. the .tgprep-marked part")
    cap = fake.album_captions[0]
    ok(cap == "@alice · tiktok · live · bigvideo #live",
       "split album caption is the recorder format with the _partNNN token stripped, "
       "named once — not a list of raw part filenames")
    store = ItemStore.open(db_file)
    ok(store.counts_by_status().get("sent", 0) == 3,
       "all three part rows are terminal 'sent'")
    store.close()

def test_video_metadata_backend_seam(tmp: Path) -> None:
    section("Seam 27: video-metadata backend (album videos must NOT ship as 1x1 images)")
    # ROOT-CAUSE REGRESSION GUARD. The native video-ALBUM send (send.send_album)
    # passes NO explicit attributes and relies on Telethon to derive each item's
    # width/height/duration itself. Telethon can only do that with `hachoir`
    # installed; without it it emits DocumentAttributeVideo(w=1, h=1, duration=0)
    # and Telegram renders every album video as a 1x1 static IMAGE. That bug
    # shipped silently for days because no test exercised Telethon's own metadata
    # path — single sends attach explicit ffprobe attributes and so masked it.
    from telethon import utils
    from telethon.tl import types as tg_types

    # 1. The dispatcher refuses to start without the backend (integrity-first).
    from dispatcher.cli import _assert_video_metadata_backend
    _assert_video_metadata_backend()
    ok(True, "startup guard passes when the video-metadata backend is present")

    import importlib.util
    real_find_spec = importlib.util.find_spec
    importlib.util.find_spec = lambda n, *a, **k: (
        None if n == "hachoir" else real_find_spec(n, *a, **k))
    try:
        raised = False
        try:
            _assert_video_metadata_backend()
        except RuntimeError:
            raised = True
        ok(raised, "startup guard FAILS FAST when the backend is missing")
    finally:
        importlib.util.find_spec = real_find_spec

    # 2. End-to-end proof: Telethon derives REAL geometry for a real mp4 — the
    #    exact call the album path leans on. A 160x120/1s clip must come back as
    #    a video attribute with those non-degenerate dims, never the 1x1/0s stub.
    if not _ffmpeg_present():
        ok(True, "ffmpeg absent — real-geometry probe skipped (toolchain missing)")
        return
    mp4 = _make_video(tmp / "clip.mp4", container="mp4")
    attrs, mime = utils.get_attributes(str(mp4))
    vattr = next((a for a in attrs
                  if isinstance(a, tg_types.DocumentAttributeVideo)), None)
    ok(vattr is not None, "Telethon attaches a DocumentAttributeVideo to the mp4")
    ok(mime == "video/mp4", "mime resolves to video/mp4 (sent as video, not photo)")
    ok(not (vattr.w <= 1 and vattr.h <= 1 and vattr.duration <= 0),
       f"geometry is real, not the 1x1/0s stub (w={vattr.w} h={vattr.h} "
       f"dur={vattr.duration}) — album videos render as videos")
    ok(vattr.w == 160 and vattr.h == 120,
       f"derived dimensions match the source (160x120, got {vattr.w}x{vattr.h})")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 33 — fast video album → native list-send fallback. The fast path's group
# call (SendMultiMedia over materialized documents) is intermittently rejected
# with MediaInvalid even when every item is individually fine (observed live:
# >half of platform video albums, with the per-item fallback then delivering
# 10/10). send_album must retry the SAME batch once via the native list send —
# which demonstrably groups these files — before surfacing media_empty and
# letting the drain degroup the album into singles. Every other outcome of the
# fast path (success, flood-wait, real errors) must pass through untouched.
# ══════════════════════════════════════════════════════════════════════════════

def test_fast_album_native_fallback_seam() -> None:
    section("Seam 33: fast album rejection → ONE native retry before degrouping")
    from dispatcher import send as send_mod
    from dispatcher.send import SendResult, TelethonSendStrategy

    def _strategy(fast_album: bool = True) -> TelethonSendStrategy:
        s = TelethonSendStrategy(
            api_id=0, api_hash="", phone="", session_name="stub",
            fast_album=fast_album)
        s._client = object()   # bypass __aenter__: no network in tests
        return s

    def _wire(s, fast_results):
        """Stub both album paths; record the dispatch order + arguments."""
        calls: list = []
        async def fake_fast(peer, fps, caps, *, topic_id=None):
            calls.append(("fast", list(fps), list(caps), topic_id))
            return fast_results.pop(0)
        async def fake_native(peer, fps, caps, *, topic_id=None):
            calls.append(("native", list(fps), list(caps), topic_id))
            return SendResult(ok=True)
        s._send_video_album_fast = fake_fast
        s._send_video_album_native = fake_native
        return calls

    files = ["a.mp4", "b.mp4", "c.mp4"]
    orig_present = send_mod.fast_upload._internals_present
    send_mod.fast_upload._internals_present = lambda c: True
    try:
        # 1. fast succeeds → native never runs (happy path unchanged).
        s = _strategy()
        calls = _wire(s, [SendResult(ok=True)])
        r = asyncio.run(s.send_album(peer="p", file_paths=files, caption="cap"))
        ok(r.ok and [c[0] for c in calls] == ["fast"],
           "fast success → delivered, no native retry")

        # 2. fast group-rejected → ONE native retry of the SAME batch wins.
        s = _strategy()
        calls = _wire(s, [SendResult(ok=False, error="MediaInvalidError",
                                     media_empty=True)])
        r = asyncio.run(s.send_album(
            peer="p", file_paths=files, caption="cap", topic_id=7))
        ok(r.ok and [c[0] for c in calls] == ["fast", "native"],
           "fast media-rejection → exactly one native retry, album delivered")
        ok(calls[1][1] == files and calls[1][3] == 7,
           "native retry carries the SAME files and topic_id")
        ok(calls[1][2] == ["cap", None, None],
           "native retry keeps A1 caption semantics (caption on first item only)")

        # 3. native also rejects → media_empty surfaces so the drain's
        #    per-item recover_media_empty ladder runs exactly as before.
        s = _strategy()
        calls = _wire(s, [SendResult(ok=False, media_empty=True)])
        async def native_reject(peer, fps, caps, *, topic_id=None):
            calls.append(("native", list(fps), list(caps), topic_id))
            return SendResult(ok=False, error="MediaInvalidError",
                              media_empty=True)
        s._send_video_album_native = native_reject
        r = asyncio.run(s.send_album(peer="p", file_paths=files, caption=None))
        ok(not r.ok and r.media_empty
           and [c[0] for c in calls] == ["fast", "native"],
           "double rejection surfaces media_empty → drain degroups as before")

        # 4. a NON-media fast failure (flood-wait, stall, network) passes
        #    through untouched — the native retry is for group rejection only.
        s = _strategy()
        calls = _wire(s, [SendResult(ok=False, flood_wait_s=30)])
        r = asyncio.run(s.send_album(peer="p", file_paths=files, caption=None))
        ok(not r.ok and r.flood_wait_s == 30
           and [c[0] for c in calls] == ["fast"],
           "non-media failure (flood-wait) propagates with NO native retry")

        # 5. FAST_ALBUM=0 pins the native path; the fast path never runs.
        s = _strategy(fast_album=False)
        calls = _wire(s, [])
        r = asyncio.run(s.send_album(peer="p", file_paths=files, caption="cap"))
        ok(r.ok and [c[0] for c in calls] == ["native"],
           "FAST_ALBUM=0 → native directly, fast path never invoked")

        # 6. internals absent → native directly (structural fallback intact).
        send_mod.fast_upload._internals_present = lambda c: False
        s = _strategy()
        calls = _wire(s, [])
        r = asyncio.run(s.send_album(peer="p", file_paths=files, caption="cap"))
        ok(r.ok and [c[0] for c in calls] == ["native"],
           "missing Telethon internals → native directly (unchanged)")
    finally:
        send_mod.fast_upload._internals_present = orig_present
