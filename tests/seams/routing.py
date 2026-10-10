"""Seams: Telegram routing, topics, rosters, identity, sanitizer, burner account."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ._harness import (
    _FakeSend,
    _drain_once,
    _fresh_db,
    _write_media,
    ok,
    section,
)


# ══════════════════════════════════════════════════════════════════════════════
# Seam 8 — dispatcher.tg_router resolution chain
# ══════════════════════════════════════════════════════════════════════════════

def test_routing_seam() -> None:
    section("Seam 8: tg_router resolution chain")
    from dispatcher.tg_router import TelegramRouter, RouteError
    from core.models import Item

    router = TelegramRouter(default_chat_id="-1000000000001")

    def _item(**kw):
        base = dict(id=1, source="archiver", platform="x", username="al",
                    identifier="i", file_path="/f.jpg", upload_date=None,
                    file_size_bytes=None, title="", discovered_at="t",
                    status="pending", priority=10, caption=None, attempts=0,
                    claimed_at=None, sent_at=None, last_error=None,
                    tg_message_id=None, content_hash=None, chat_id=None,
                    group_key=None)
        base.update(kw)
        return Item(**base)

    for k in list(os.environ):
        if k.startswith("TELEGRAM_CHAT_ID"):
            del os.environ[k]

    ok(router.chat_id_for_item(_item()) == "-1000000000001",
       "falls back to the global default chat id")

    os.environ["TELEGRAM_CHAT_ID_X"] = "-1001"
    ok(router.chat_id_for_item(_item()) == "-1001", "per-platform override wins over default")
    os.environ["TELEGRAM_CHAT_ID_X_AL"] = "-1002"
    ok(router.chat_id_for_item(_item()) == "-1002", "per-user override wins over per-platform")

    os.environ["TELEGRAM_CHAT_ID_TIKTOK_LIVE"] = "-9001"
    live = _item(platform="tiktok", username="streamer", source="recorder")
    ok(router.chat_id_for_item(live) == "-9001",
       "tiktok recorder/live routes to the LIVE channel")

    # Explicit chat_id on the row (orphaned folders) overrides everything.
    orphan = _item(source="orphaned", chat_id="-1009999")
    ok(router.chat_id_for_item(orphan) == "-1009999",
       "explicit row chat_id (orphaned) overrides env resolution")

    # Regression: a DASH-FREE numeric channel id on the row (a legacy row, or one
    # from `archiver ingest --chat 100…`) must re-sign to -100… and resolve as a
    # PeerChannel — NOT a PeerUser, which Telegram can't find an input entity for.
    from telethon.tl.types import PeerChannel, PeerUser
    bare = _item(source="orphaned", chat_id="1001733273713")
    ok(router.chat_id_for_item(bare) == "-1001733273713",
       "dash-free channel id re-signed to its canonical -100… form")
    peer = router.peer_for_item(bare)
    ok(isinstance(peer, PeerChannel) and peer.channel_id == 1733273713,
       "dash-free channel id resolves to PeerChannel (not PeerUser → entity error)")
    ok(not isinstance(router.peer_for_item(orphan), PeerUser),
       "a -100… channel id never resolves to PeerUser")

    bad = _item(source="orphaned", chat_id="not-a-chat-id")
    try:
        router.chat_id_for_item(bad)
        raised = False
    except RouteError:
        raised = True
    ok(raised, "an invalid explicit chat_id raises RouteError (fail fast, not mid-send)")

    for k in ("TELEGRAM_CHAT_ID_X", "TELEGRAM_CHAT_ID_X_AL",
              "TELEGRAM_CHAT_ID_TIKTOK_LIVE"):
        os.environ.pop(k, None)

# ══════════════════════════════════════════════════════════════════════════════
# Seam 9 — PolicyStore banned roster ↔ active user list (the new feature)
# ══════════════════════════════════════════════════════════════════════════════

def test_banned_roster_seam() -> None:
    section("Seam 9: banned roster ↔ active users (mutual exclusivity)")
    from core import PolicyStore

    ps = PolicyStore()
    ps.add_user("x", "alice")
    ps.add_user("x", "bob")
    ps.set("delete_after_upload", True, platform="x", username="bob")

    newly = ps.ban_user("x", "bob", reason="account is suspended",
                         detected_at="2026-06-07T00:00:00+00:00")
    ok(newly, "first ban returns newly=True")
    ok("bob" not in ps.list_users("x"), "banned user removed from active list")
    ok("bob" in ps.list_banned("x"), "banned user appears on the banned roster")
    ok(list(ps.iter_user_overrides()) == [],
       "per-user overrides dropped on ban (no stale config)")
    ok(not ps.ban_user("x", "bob"), "re-ban is idempotent (newly=False)")

    # config add un-bans (operator asserting the account is back) — exclusivity.
    ps.unban_user("x", "bob")
    ok("bob" not in ps.list_banned("x"), "unban removes from the roster")
    ok("bob" not in ps.list_users("x"), "unban does NOT silently re-add to active")
    ps.add_user("x", "bob")
    ok("bob" in ps.list_users("x") and "bob" not in ps.list_banned("x"),
       "the two lists stay mutually exclusive")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 11 — identity.resolve gives renamed-account re-downloads ONE identifier
# (so UNIQUE(platform, identifier) dedups them even when bytes/folder differ)
# ══════════════════════════════════════════════════════════════════════════════

def test_identity_ig_pk_dedup_seam(tmp: Path) -> None:
    section("Seam 11: IG media-pk identity → dedup across rename/re-encode")
    from core import identity, ItemStore

    # Same post (media pk 3540317000569885880), two usernames (account renamed),
    # different bytes → historically two manual_ ids → two uploads. The fix:
    # both resolve to the media PK, so the second insert is rejected.
    a = identity.resolve(Path("/o/fit_miness_1736258696_3540317000569885880_50348444507.jpg"))
    b = identity.resolve(Path("/o/gym__ln_1736258696_3540317000569885880_50348444507.jpg"))
    ok(a.identifier == "3540317000569885880", "IG filename → media PK identifier")
    ok(not a.is_manual, "media-pk identity is not a manual hash fallback")
    ok(a.identifier == b.identifier,
       "renamed-account copies resolve to the SAME identifier")

    # Our OWN download naming is untouched (regression guard).
    ours = identity.resolve(Path("/o/20240101_C1a2b3_0.jpg"))
    ok(ours.identifier == "C1a2b3_0" and not ours.is_manual,
       "our YYYYMMDD_<shortcode>_<num> scheme is unchanged")
    rnd = identity.resolve(Path("/o/some_random_clip.mp4"))
    ok(rnd.is_manual, "a non-matching name still falls back to manual_")
    ok(identity.archive_entry_for("instagram", a) is None,
       "numeric IG media-pk is NOT seeded into gallery-dl's shortcode archive")

    # TikTok: same video from yt-dlp (<id>.mp4) and gallery-dl (<id>_0.mp4)
    # must resolve to ONE identifier; photo carousels must stay distinct.
    yt = identity.resolve(Path("/o/20250317_7482670428511538440.mp4")).identifier
    gd = identity.resolve(Path("/o/20250317_7482670428511538440_0.mp4")).identifier
    ok(yt == gd == "7482670428511538440",
       "TikTok <id>.mp4 and <id>_0.mp4 collapse to one identifier")
    c1 = identity.resolve(Path("/o/20250402_7488614368540757303_1.jpg")).identifier
    c2 = identity.resolve(Path("/o/20250402_7488614368540757303_2.jpg")).identifier
    ok(c1 != c2, "TikTok photo carousel _1/_2 stay distinct (not collapsed)")
    img0 = identity.resolve(Path("/o/20250402_555_0.jpg")).identifier
    ok(img0.endswith("_0"), "a non-video _0 is NOT stripped (only videos)")

    # End-to-end at the table seam: the two copies → exactly one row.
    db = _fresh_db()
    try:
        fa = _write_media(tmp / "instagram" / "fit_miness" /
                          "fit_miness_1736258696_3540317000569885880_50348444507.jpg",
                          b"BYTES-V1")
        fb = _write_media(tmp / "instagram" / "gym__ln" /
                          "gym__ln_1736258696_3540317000569885880_50348444507.jpg",
                          b"BYTES-V2-REENCODED")  # different bytes on purpose
        for f in (fa, fb):
            mi = identity.resolve(f)
            db.add_item(source="archiver", platform="instagram",
                        username=f.parent.name, identifier=mi.identifier,
                        file_path=str(f), upload_date=mi.upload_date)
        rows = db.conn.execute(
            "SELECT COUNT(*) n FROM items WHERE platform='instagram'").fetchone()["n"]
        ok(rows == 1,
           "same post under two handles + different bytes → ONE row (no dup upload)")
    finally:
        db.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 22 — forum-topic routing end-to-end: a `<chat>.t<topic>` folder name →
# core.parse_route → add_item(topic_id) → claim_batch's destination discriminator
# (chat_id + topic_id) → drain dest resolution → fake send's reply_to. The trap
# this guards: two folders for the SAME chat but DIFFERENT topics whose subfolders
# share a name produce the SAME group_key ('<chat>/<sub>'), so ONLY the topic_id
# discriminator keeps them from wrongly albuming into one cross-topic message.
# ══════════════════════════════════════════════════════════════════════════════

def test_topic_routing_seam(tmp: Path) -> None:
    section("Seam 22: forum-topic routing (.t<topic> folder → reply_to)")
    from core import ItemStore, PolicyStore, BatchPolicy
    from core.orphaned import ingest_chat_id_dirs

    # Same chat, two topics, identically-named subfolders ('g') → colliding
    # group_key '<chat>/g'. Topic is the only thing that separates them.
    _write_media(tmp / "-100222.t77" / "g" / "a.jpg", b"AA")
    _write_media(tmp / "-100222.t77" / "g" / "b.jpg", b"BB")
    _write_media(tmp / "-100222.t88" / "g" / "c.jpg", b"CC")
    _write_media(tmp / "-100222.t88" / "g" / "d.jpg", b"DD")

    db_file = str(tmp / "seam22.db")
    store = ItemStore.open(db_file)
    reports = ingest_chat_id_dirs(store, tmp, known_platforms=set())
    inserted = sum(r.inserted for r in reports)
    ok(inserted == 4, "4 loose files ingested from two .t<topic> folders")
    paths = [tmp / "-100222.t77" / "g" / "a.jpg",
             tmp / "-100222.t77" / "g" / "b.jpg",
             tmp / "-100222.t88" / "g" / "c.jpg",
             tmp / "-100222.t88" / "g" / "d.jpg"]
    rows = [store.get(store.id_of(str(p))) for p in paths]
    topics = sorted(r.topic_id for r in rows)
    ok(topics == [77, 77, 88, 88],
       "parse_route carried each file's topic_id onto its row (77/77, 88/88)")
    ok(all(r.chat_id == "-100222" for r in rows),
       "the .t<topic> suffix is stripped from chat_id (dest is the bare chat)")
    store.close()

    ps = PolicyStore()
    ps.set(BatchPolicy.SIZE_KEY, 1)
    fake = _FakeSend()
    _drain_once(db_file, ps, fake, default_chat_id="-100999")

    ok(len(fake.sent_albums) == 2,
       "two albums sent — the shared group_key did NOT merge across topics")
    by_topic = {t: sorted(Path(p).name for p in a)
                for t, a in zip(fake.album_topics, fake.sent_albums)}
    ok(by_topic.get(77) == ["a.jpg", "b.jpg"],
       "topic 77 album = a.jpg+b.jpg, sent with reply_to=77")
    ok(by_topic.get(88) == ["c.jpg", "d.jpg"],
       "topic 88 album = c.jpg+d.jpg, sent with reply_to=88")
    ok(None not in fake.album_topics,
       "every topic-routed album carried a non-None reply_to (no General leak)")

# ══════════════════════════════════════════════════════════════════════════════
# Seam 24 — banned-word sanitizer at send: a configured Sanitizer on the
# dispatcher config strips banned words from the caption the drain builds, before
# the send sees it — while the on-disk file is left untouched. Guards the contract
# between core.sanitize and dispatcher.drain's caption path.
# ══════════════════════════════════════════════════════════════════════════════

def test_banned_word_sanitizer_seam(tmp: Path) -> None:
    section("Seam 24: banned-word sanitizer strips caption at send")
    from core import ItemStore, PolicyStore, Sanitizer, ProtectionPolicy
    from core.hashing import full_hash

    db_file = str(tmp / "seam24.db")
    store = ItemStore.open(db_file)
    # An orphaned single → caption is its own stem (drain's orphaned_caption).
    photo = _write_media(tmp / "-100777" / "meetup badword tonight.jpg", b"PH")
    store.add_item(source="orphaned", platform="orphaned", username="-100777",
                   identifier="orph_1", file_path=str(photo), priority=6,
                   caption="meetup badword tonight", chat_id="-100777",
                   content_hash=full_hash(photo))
    store.close()

    ps = PolicyStore()
    # Protect this orphaned scope so the orphaned ship-and-delete (file + row)
    # is suppressed — this seam is about the sanitizer NOT mutating disk, which
    # is orthogonal to the post-send cleanup. With the safebrake on, the file
    # persists, so the `photo.exists()` check still genuinely catches a sanitizer
    # that would rename or rewrite the source.
    ps.set(ProtectionPolicy.KEY, True, platform="orphaned", username="-100777")
    fake = _FakeSend()
    _drain_once(db_file, ps, fake, default_chat_id="-100777",
                sanitizer=Sanitizer(["badword"]))

    ok(len(fake.single_captions) == 1, "the orphaned photo sent as one single")
    cap = fake.single_captions[0]
    ok("badword" not in cap,
       "the banned word was stripped from the caption before the send")
    ok("meetup" in cap and "tonight" in cap,
       "only the banned token was removed; the rest of the caption survives")
    ok(photo.exists(),
       "the on-disk file is untouched (sanitizer rewrites the message, not disk)")

def test_burner_account_seam() -> None:
    section("Seam 29: optional burner account (config → routing → send seam)")
    from dispatcher.config import BurnerCreds, TelegramCreds
    from dispatcher.send import TelethonSendStrategy
    from dispatcher import tg_router

    # ── config seam: env round-trip through BurnerCreds.from_env ───────────
    primary = TelegramCreds(api_id=111, api_hash="ph", phone="+1",
                            session_name="/tmp/claude-seam-primary")
    for k in ("BURNER_CHAT_IDS", "TELEGRAM_BURNER_SESSION",
              "TELEGRAM_BURNER_PHONE", "TELEGRAM_BURNER_API_ID",
              "TELEGRAM_BURNER_API_HASH"):
        os.environ.pop(k, None)
    ok(BurnerCreds.from_env(primary) is None,
       "no burner env → None (pipeline untouched when unconfigured)")

    os.environ["BURNER_CHAT_IDS"] = "-100555, 777"     # dash-free 777 → -777
    os.environ["TELEGRAM_BURNER_SESSION"] = "/tmp/claude-seam-burner"
    os.environ["TELEGRAM_BURNER_PHONE"] = "+2"
    burner = BurnerCreds.from_env(primary)
    ok(burner is not None and burner.chat_ids == frozenset({"-100555", "-777"}),
       "configured burner normalizes its dedicated chat set")
    ok(burner.api_id == 111 and burner.api_hash == "ph",
       "burner inherits the primary's api creds when its own are unset")

    # ── routing seam: send()/check_destination pick the right client ──────
    class _FakeClient:
        def __init__(self, tag):
            self.tag = tag
            self.entities = 0
            self.disconnects = 0
            self.connects = 0
        async def get_entity(self, peer):
            self.entities += 1
            return type("E", (), {"title": self.tag, "id": 1})()
        async def disconnect(self):
            self.disconnects += 1
        async def connect(self):
            self.connects += 1

    primary_client = _FakeClient("primary")
    burner_client = _FakeClient("burner")

    strat = TelethonSendStrategy(
        api_id=111, api_hash="ph", phone="+1",
        session_name="/tmp/claude-seam-primary", burner=burner)
    strat._client = primary_client
    # stub the burner build+connect so no real Telethon/network is touched
    strat._build_client = lambda *a, **k: burner_client
    async def _noconnect(client, session, phone):
        await client.connect()
    strat._connect_authorized = _noconnect

    # dedicated chat → burner comes up lazily and answers
    res_ok, _ = asyncio.run(
        strat.check_destination(peer=tg_router.Destination("-100555").peer))
    ok(res_ok and burner_client.entities == 1 and primary_client.entities == 0,
       "dedicated chat routes through the burner client")
    ok(strat._burner_client is burner_client and burner_client.connects == 1,
       "burner built + connected lazily on first dedicated use")

    # non-dedicated chat → primary, burner untouched by the new call
    res_ok, _ = asyncio.run(
        strat.check_destination(peer=tg_router.Destination("-100999").peer))
    ok(res_ok and primary_client.entities == 1 and burner_client.entities == 1,
       "non-dedicated chat stays on the primary client")

    # _force_reconnect re-homes whichever account the in-flight send uses
    strat._active_client = burner_client
    asyncio.run(strat._force_reconnect())
    ok(burner_client.disconnects == 1 and primary_client.disconnects == 0,
       "reconnect during a burner-routed send recycles the burner socket")

    # ── fallback seam: a burner that can't authorize falls back to primary ─
    strat2 = TelethonSendStrategy(
        api_id=111, api_hash="ph", phone="+1",
        session_name="/tmp/claude-seam-primary", burner=burner)
    strat2._client = primary_client
    strat2._build_client = lambda *a, **k: _FakeClient("deadburner")
    async def _fail(client, session, phone):
        raise RuntimeError("unauthorized")
    strat2._connect_authorized = _fail
    chosen = asyncio.run(strat2._client_for(tg_router.Destination("-100555").peer))
    ok(chosen is primary_client and strat2._burner_client is None,
       "unauthorized burner falls back to the primary (delivery never blocked)")

    for k in ("BURNER_CHAT_IDS", "TELEGRAM_BURNER_SESSION",
              "TELEGRAM_BURNER_PHONE"):
        os.environ.pop(k, None)

def test_labeled_route_folder_seam(tmp: Path) -> None:
    section("Seam 31b: `<label>~<chat_id>` folder routes on the BARE chat_id")
    from core import ItemStore, ingest_chat_id_dirs, parse_route

    # The label is cosmetic: it is stripped by parse_route and must NEVER reach
    # items.chat_id, so the dispatcher keeps routing on the canonical bare id.
    r = parse_route("family-chat~-1001234567890.t42")
    ok(r is not None and r.chat_id == "-1001234567890"
       and r.topic_id == 42 and r.name == "family-chat",
       "parse_route splits label off, keeps chat_id/topic canonical")
    ok(parse_route("1009999~-100999") is not None
       and parse_route("a~b~-100").chat_id == "-100"
       and parse_route("a~b~-100").name == "a~b",
       "split is on the LAST `~`; a label may itself contain `~`")
    ok(parse_route("library") is None and parse_route("foo~bar") is None,
       "a non-route (no valid chat_id after the last `~`) still matches nothing")

    out = tmp / "out"
    _write_media(out / "family-chat~-100999" / "a.mp4", b"LABELED-ROUTE-BYTES")
    db = ItemStore.open(str(tmp / "lbl.db"))
    reports = ingest_chat_id_dirs(db, out, known_platforms=["x"])
    ok(any(rep.chat_id == "-100999" for rep in reports),
       "labeled folder is ingested as a route (reported on the bare chat_id)")
    row = db.conn.execute(
        "SELECT chat_id FROM items WHERE source='orphaned'").fetchone()
    ok(row is not None and row["chat_id"] == "-100999",
       "the stored items.chat_id is bare canonical — label never leaks in")
    db.close()
