"""Seams: archiver full-history gate, concurrent platform loops, stories fast lane."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

from ._harness import _fresh_db, _write_media, ok, section


# ══════════════════════════════════════════════════════════════════════════════
# Seam 12 — full-history gate: core.store flag ↔ archiver._compute_date_min
# The gate (needs_full_history) and the cutoff computation live in different
# packages; the contract is "armed user ⇒ None cutoff ⇒ whole-timeline walk",
# and "marking done ⇒ fall back to the incremental floor". A regression in
# either side silently turns full-history into a no-op (old posts never come
# down) or makes EVERY run re-walk the timeline (slow + rate-limit risk).
# ══════════════════════════════════════════════════════════════════════════════

def test_full_history_gate_seam() -> None:
    section("Seam 12: full-history gate ↔ _compute_date_min cutoff")
    from archiver.platforms import _compute_date_min

    db = _fresh_db()
    try:
        # Brand-new user: no checkpoint row → needs full history → None cutoff,
        # so gallery-dl/yt-dlp walk the ENTIRE timeline on the first run.
        ok(db.needs_full_history("tiktok", "alice"),
           "brand-new user (no checkpoint) needs full history")
        ok(_compute_date_min(db, "tiktok", "alice", slack_days=2) is None,
           "armed user ⇒ None cutoff (extractor walks whole timeline)")

        # A delivered post gives the incremental path something to anchor on.
        f = _write_media(Path(tempfile.mkdtemp()) / "20240115_1_0.mp4", b"V")
        db.add_item(source="archiver", platform="tiktok", username="alice",
                    identifier="tt_1", file_path=str(f),
                    upload_date="20240115")
        # Drive it through the real state machine (pending → sending → sent) so
        # max_sent_upload_date counts it — mark_sent is guarded on 'sending'.
        for item in db.claim_batch():
            db.mark_sent(item.id)
        ok(db.max_sent_upload_date("tiktok", "alice") == "20240115",
           "  precondition: a delivered post exists with a date floor")

        # Still armed (download hasn't completed yet) → full-history WINS over
        # the floor: the cutoff stays None even though a floor now exists.
        ok(_compute_date_min(db, "tiktok", "alice", slack_days=2) is None,
           "armed user overrides the incremental floor (still None)")

        # Orchestrator closes the gate after the first complete walk.
        db.mark_full_history_done("tiktok", "alice")
        ok(not db.needs_full_history("tiktok", "alice"),
           "mark_full_history_done closes the gate")
        from datetime import datetime, timezone
        cutoff = _compute_date_min(db, "tiktok", "alice", slack_days=2)
        cutoff_day = (datetime.fromtimestamp(cutoff, tz=timezone.utc)
                      .strftime("%Y%m%d") if cutoff is not None else None)
        ok(cutoff_day == "20240113",
           "done user ⇒ incremental cutoff = floor − slack_days (fast path)")

        # `run --full-history` re-opens the gate without touching rows/files;
        # the cutoff goes back to None so old posts are re-walked next run.
        db.rearm_full_history("tiktok", "alice")
        ok(db.needs_full_history("tiktok", "alice"),
           "rearm_full_history re-opens the gate on demand")
        ok(_compute_date_min(db, "tiktok", "alice", slack_days=2) is None,
           "re-armed user ⇒ None cutoff again (old posts re-walked)")

        # Migration semantics: an existing user (checkpoint already present from
        # set_last_run) that was never explicitly armed reads as done — the v3
        # migration backfilled full_history_done=1 so upgrades don't re-walk
        # everyone. Here a fresh checkpoint defaults to needing it, so we assert
        # the inverse contract: a marked-done user is never re-walked silently.
        db.mark_full_history_done("tiktok", "alice")
        ok(_compute_date_min(db, "tiktok", "alice", slack_days=2) is not None,
           "a done user never silently reverts to a full walk")
    finally:
        try:
            db.close()
        except Exception:
            pass

# ══════════════════════════════════════════════════════════════════════════════
# Seam 34 — concurrent platform loops each on their OWN db connection.
# The orchestrator now fans fetching platforms out with asyncio.gather so a slow
# platform (Instagram's long pacing) doesn't block the others. The contract:
#   (a) loops actually overlap;  (b) each platform gets a DISTINCT ItemStore
#   connection (never a shared one — that's a corruption footgun);
#   (c) one platform crashing in its loop never sinks the others;
#   (d) ARCHIVER_MAX_CONCURRENT_PLATFORMS=1 restores fully-sequential behavior.
# ══════════════════════════════════════════════════════════════════════════════

def test_concurrent_platform_loops_seam() -> None:
    section("Seam 34: concurrent platform loops (own connection, isolation)")
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from core import ItemStore
    from archiver.orchestrator import Archiver

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    ItemStore.open(db_path).close()                 # init schema once

    def make_arch(max_conc=0):
        a = object.__new__(Archiver)
        a.config = SimpleNamespace(db_path=db_path, max_concurrent_platforms=max_conc,
                                   scan_jitter_s=0.0, priority_reinject_users=8,
                                   priority_reinject_seconds=1800.0)
        a._tripped = set()
        a._fetches = lambda p: True
        a._deleting_users = lambda n: frozenset()
        a.priority_policy = SimpleNamespace(is_priority=lambda p, u: False)
        async def healthy(p): return True
        a._ensure_platform_healthy = healthy
        return a

    def mkplat(name):
        return SimpleNamespace(name=name, users=("u1", "u2"),
                               inter_user_delay=lambda: 0.0)

    rt = datetime.now(timezone.utc)

    # (a)+(b): overlap + distinct connections + real writes committed.
    arch = make_arch()
    conn_ids, timeline = {}, []
    async def au(platform, username, run_time, db):
        conn_ids.setdefault(platform.name, id(db.conn))
        timeline.append((time.perf_counter(), platform.name, "start"))
        await asyncio.sleep(0.15)
        db.add_item(source="archiver", platform=platform.name, username=username,
                    identifier=f"{platform.name}_{username}",
                    file_path=f"/x/{platform.name}/{username}",
                    upload_date="20260101", file_size_bytes=123, title="t",
                    priority=10)
        timeline.append((time.perf_counter(), platform.name, "end"))
        return {"status": "ok"}
    arch._archive_user = au
    results = {}
    t0 = time.perf_counter()
    asyncio.run(Archiver._run_platforms(
        arch, [mkplat("instagram"), mkplat("x")], None, rt, results, None))
    wall = time.perf_counter() - t0
    ok(len(results) == 4 and all(v["status"] == "ok" for v in results.values()),
       "all 4 (platform,user) pairs archived ok")
    ok(wall < 0.45, f"platforms ran concurrently (wall={wall:.2f}s << 0.60s seq)")
    ok(conn_ids["instagram"] != conn_ids["x"],
       "each platform used its OWN db connection")
    first_start = {n: min(t for t, nn, e in timeline if nn == n and e == "start")
                   for n in ("instagram", "x")}
    ok(max(first_start.values()) < min(t for t, n, e in timeline if e == "end"),
       "both loops in flight before either finished — truly overlapped")
    ok(ItemStore.open(db_path).conn.execute(
        "SELECT COUNT(*) FROM items").fetchone()[0] == 4,
       "all 4 rows committed through the separate connections")

    # (c): a platform crashing in its loop must not sink the other.
    arch2 = make_arch()
    async def healthy_crash(p):
        if p.name == "instagram":
            raise RuntimeError("boom")
        return True
    arch2._ensure_platform_healthy = healthy_crash
    done = []
    async def au2(platform, username, run_time, db):
        done.append(platform.name); return {"status": "ok"}
    arch2._archive_user = au2
    asyncio.run(Archiver._run_platforms(
        arch2, [mkplat("instagram"), mkplat("x")], None, rt, {}, None))
    ok(done == ["x", "x"],
       "healthy platform finished despite the other crashing (isolation holds)")

    # (d): max_concurrent=1 → strictly sequential (rollback switch).
    arch3 = make_arch(max_conc=1)
    inflight = {"n": 0, "peak": 0}
    async def au3(platform, username, run_time, db):
        inflight["n"] += 1; inflight["peak"] = max(inflight["peak"], inflight["n"])
        await asyncio.sleep(0.02); inflight["n"] -= 1; return {"status": "ok"}
    arch3._archive_user = au3
    asyncio.run(Archiver._run_platforms(
        arch3, [mkplat("instagram"), mkplat("x")], None, rt, {}, None))
    ok(inflight["peak"] == 1,
       "ARCHIVER_MAX_CONCURRENT_PLATFORMS=1 → never more than one loop in flight")

    # (e): staleness-first order — a never-scanned user leads a stale one, both
    # lead a freshly-scanned one, on a real store with real checkpoints.
    from datetime import timedelta
    s = ItemStore.open(db_path)
    s.set_last_run("x", "u1", rt - timedelta(days=5))   # stale
    s.set_last_run("x", "u2", rt)                        # fresh
    s.close()
    def mkplat3(name, users):
        return SimpleNamespace(name=name, users=users,
                               inter_user_delay=lambda: 0.0)
    arch4 = make_arch()
    order = []
    async def au4(platform, username, run_time, db):
        order.append(username); return {"status": "ok"}
    arch4._archive_user = au4
    asyncio.run(Archiver._run_platforms(
        arch4, [mkplat3("x", ("u2", "u1", "u3"))], None, rt, {}, None))
    # u3 never scanned (front), then u1 (5d stale), then u2 (fresh) last.
    ok(order == ["u3", "u1", "u2"],
       f"staleness-first walk: never > stale > fresh ({order})")

    # (f): priority users lead AND are re-injected mid-walk. Mark u1 priority,
    # re-inject every 1 user, on an 8-user roster → u1 appears repeatedly and
    # first.
    arch5 = make_arch()
    arch5.priority_policy = SimpleNamespace(
        is_priority=lambda p, u: u == "p1")
    arch5.config.priority_reinject_users = 2
    arch5.config.priority_reinject_seconds = 0.0   # count-arm only
    seq = []
    async def au5(platform, username, run_time, db):
        seq.append(username); return {"status": "ok"}
    arch5._archive_user = au5
    roster = ("a", "b", "c", "d", "p1", "e", "f")
    asyncio.run(Archiver._run_platforms(
        arch5, [mkplat3("x", roster)], None, rt, {}, None))
    ok(seq[0] == "p1", f"priority user leads the walk ({seq[:3]})")
    ok(seq.count("p1") >= 3,
       f"priority user re-injected multiple times ({seq.count('p1')}x: {seq})")
    ok(set(u for u in seq) == set(roster),
       "every roster user still scanned at least once")

    # (g): SMART auto-priority — a regular poster (many distinct post dates in
    # the window) is auto-promoted and leads, without any explicit mark.
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    s2 = ItemStore.open(db_path)
    today = _dt.now(_tz.utc)
    for k in range(10):   # 'reg' posted on 10 distinct recent days
        d = (today - _td(days=k)).strftime("%Y%m%d")
        s2.add_item(source="archiver", platform="ap", username="reg",
                    identifier=f"reg_{d}", file_path=f"/ap/reg/{d}",
                    upload_date=d, file_size_bytes=1, title="t", priority=10)
    old = (today - _td(days=400)).strftime("%Y%m%d")   # 'quiet' posted long ago
    s2.add_item(source="archiver", platform="ap", username="quiet",
                identifier="quiet_old", file_path="/ap/quiet/old",
                upload_date=old, file_size_bytes=1, title="t", priority=10)
    s2.close()
    arch6 = make_arch()
    arch6.config.auto_priority = True
    arch6.config.auto_priority_window_days = 30.0
    arch6.config.auto_priority_min_days = 8
    arch6.config.auto_priority_refresh_days = 14.0
    arch6.config.priority_manual_cap = 10
    arch6.config.auto_priority_cap = 10
    arch6.priority_policy = SimpleNamespace(is_priority=lambda p, u: False)
    seq2 = []
    async def au6(platform, username, run_time, db):
        seq2.append(username); return {"status": "ok"}
    arch6._archive_user = au6
    asyncio.run(Archiver._run_platforms(
        arch6, [mkplat3("ap", ("quiet", "reg"))], None, rt, {}, None))
    ok(seq2 and seq2[0] == "reg",
       f"auto-priority: regular poster leads the walk ({seq2})")

    # (h): SEPARATE per-source caps — manual (up to manual_cap) and auto (up to
    # auto_cap) never compete for slots; and the auto ranking is cached (not
    # recomputed) within the refresh interval.
    s3 = ItemStore.open(db_path)
    for name, ndays in [("r1", 20), ("r2", 15), ("r3", 12), ("r4", 10)]:
        for k in range(ndays):
            d = (today - _td(days=k)).strftime("%Y%m%d")
            s3.add_item(source="archiver", platform="cp", username=name,
                        identifier=f"{name}_{d}", file_path=f"/cp/{name}/{d}",
                        upload_date=d, file_size_bytes=1, title="t", priority=10)
    s3.close()
    archc = make_arch()
    archc.config.auto_priority = True
    archc.config.auto_priority_window_days = 30.0
    archc.config.auto_priority_min_days = 8
    archc.config.auto_priority_refresh_days = 14.0
    archc.config.priority_manual_cap = 10            # manual cap (generous)
    archc.config.auto_priority_cap = 2               # auto capped at 2
    # m1 manual (kept regardless of post activity).
    archc.priority_policy = SimpleNamespace(is_priority=lambda p, u: u == "m1")
    dbc = ItemStore.open(db_path)
    roster_c = ("m1", "r1", "r2", "r3", "r4", "z")
    pset = Archiver._priority_users(archc, "cp", roster_c, dbc)
    ok("m1" in pset, f"manual mark always in priority set ({pset})")
    # manual(m1) independent of auto; auto capped at 2 → top-2 regulars r1,r2.
    ok(pset == {"m1", "r1", "r2"},
       f"separate caps: manual + top-2 auto, no slot competition ({pset})")
    # Cache present + reused: mutate the stored ranking and confirm it's honored
    # (proves we didn't recompute from the DB within the interval).
    import json as _json
    dbc.meta_set("auto_priority:cp", _json.dumps(
        {"computed_at": today.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "users": ["r4", "r3"]}))
    pset2 = Archiver._priority_users(archc, "cp", roster_c, dbc)
    ok(pset2 == {"m1", "r4", "r3"},
       f"auto ranking served from cache within interval ({pset2})")
    dbc.close()

# ══════════════════════════════════════════════════════════════════════════════
# Seam 35 — Instagram stories fast lane. Stories expire in 24h, so they get a
# SEPARATE stories-only pass (loop's stories-sweeper) on a tighter cadence than
# the slow posts/reels crawl. Contract:
#   (a) config splits 'stories' out of the heavy include when the lane is on;
#   (b) download_stories fetches include=stories with NO date-min (all <24h);
#   (c) run_stories is a no-op when the lane is off / IG absent;
#   (d) run_stories NEVER advances the posts/reels checkpoints (independent lanes).
# ══════════════════════════════════════════════════════════════════════════════

def test_stories_fast_lane_seam() -> None:
    section("Seam 35: Instagram stories fast lane")
    from types import SimpleNamespace
    from archiver.config import (InstagramConfig, _IG_PACING_DEFAULTS,
                                 _split_stories_from_include)
    from archiver.platforms import InstagramPlatform
    from archiver.orchestrator import Archiver
    import gallery_dl.config, gallery_dl.job

    # (a) include-splitting
    ok(_split_stories_from_include("posts,reels,stories", 10800) == "posts,reels",
       "lane ON strips 'stories' from the heavy include")
    ok(_split_stories_from_include("posts,reels,stories", 0) == "posts,reels,stories",
       "lane OFF leaves the include untouched (legacy behavior)")
    ok(_split_stories_from_include("stories", 10800) == "posts,reels",
       "include='stories' only → heavy falls back to posts,reels")

    def mkcfg(interval=10800.0):
        return InstagramConfig(
            users=("friend",), cookies_file="ig.txt", firefox_profile="",
            cookie_refresh_days=3.0, include="posts,reels",
            pacing=_IG_PACING_DEFAULTS, browser="firefox",
            stories_interval=interval, stories_user_gap_min=1.0,
            stories_user_gap_max=2.0)

    # (b) download_stories → include=stories, NO date-min, in the LIVE gdl config
    import tempfile as _tf
    cfg = SimpleNamespace(instagram=mkcfg(), output_dir=_tf.mkdtemp(),
                          state_dir=_tf.mkdtemp())
    plat = InstagramPlatform(cfg)
    captured = {}
    def fake_run(self):
        captured["include"] = gallery_dl.config.get(("extractor", "instagram"), "include")
        captured["date-min"] = gallery_dl.config.get(("extractor", "instagram"), "date-min")
        captured["browser"] = gallery_dl.config.get(("extractor", "instagram"), "browser")
    orig_run = gallery_dl.job.DownloadJob.run
    gallery_dl.job.DownloadJob.run = fake_run
    try:
        class _DB:
            def needs_full_history(self, *a): return True
            def max_sent_upload_date(self, *a): return None
            def get_last_run(self, *a): return None
        plat.download_stories("friend", _DB())
    finally:
        gallery_dl.job.DownloadJob.run = orig_run
    ok(captured["include"] == "stories", "download_stories fetches include=stories")
    ok(captured["date-min"] is None, "stories pass sets NO date-min (all <24h)")
    ok(captured["browser"] == "firefox", "stories pass keeps the Firefox fingerprint")

    # (c)+(d) run_stories no-op when off; and never advances checkpoints when on.
    checkpoint_calls = []
    class _RecDB:
        def set_last_run(self, *a): checkpoint_calls.append("last_run")
        def set_date_floor(self, *a): checkpoint_calls.append("date_floor")
        def mark_full_history_done(self, *a): checkpoint_calls.append("full_hist")
    arch = object.__new__(Archiver)
    arch.config = SimpleNamespace(instagram=mkcfg(interval=0.0))
    arch.db = _RecDB()
    from core import DownloadPolicy, PolicyStore
    arch.download_policy = DownloadPolicy(PolicyStore())
    res = asyncio.run(Archiver.run_stories(arch))
    ok(res == {}, "run_stories is a no-op when the lane is off (interval=0)")

    # lane ON: stub health + download_stories, assert iteration + no checkpoints
    arch.config = SimpleNamespace(instagram=mkcfg(interval=10800.0))
    async def healthy(p): return True
    arch._ensure_platform_healthy = healthy
    arch._deleting_users = lambda n: frozenset()
    seen = []
    import archiver.platforms as _plat   # run_stories does `from .platforms
    orig_igp = _plat.InstagramPlatform   # import InstagramPlatform` at call time
    class _FakePlat:
        name = "instagram"
        def __init__(self, cfg): self.users = ("a", "b")
        def stories_inter_user_delay(self): return 0.0
        def download_stories(self, u, db): seen.append(u); return 1
    _plat.InstagramPlatform = _FakePlat
    try:
        res = asyncio.run(Archiver.run_stories(arch))
    finally:
        _plat.InstagramPlatform = orig_igp
    ok(seen == ["a", "b"], "run_stories walks every configured user")
    ok(all(v["status"] == "ok" for v in res.values()), "each stories user reports ok")
    ok(checkpoint_calls == [],
       "run_stories NEVER touches posts/reels checkpoints (lanes independent)")
