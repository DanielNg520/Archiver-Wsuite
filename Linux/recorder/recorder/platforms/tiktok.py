"""
recorder.platforms.tiktok
──────────────────────────
TikTok live detection + HLS URL resolution.

IMPLEMENTATION CHOICE (diverges from the guide — see lesson below):
  The guide sketches manual SIGI_STATE scraping + the api-live/detail
  endpoint. As of 2026 that path is brittle: TikTok moved the room blob
  between SIGI_STATE and __UNIVERSAL_DATA_FOR_REHYDRATION__, and the
  detail endpoint shape shifts. The maintained `TikTokLive` library
  (v6.x) absorbs those changes behind a stable `is_live()` / `room_id`
  API. We depend on it rather than re-deriving the scraping each time
  TikTok ships a change. This is the same reasoning as anti-pattern #7
  in the guide ("don't reinvent yt-dlp") applied to live detection.

THREADING vs ASYNC boundary:
  The recorder is threaded by mandate (guide line 44). TikTokLive is
  async. We bridge by running each check in a throwaway event loop via
  asyncio.run() inside the synchronous is_live(). There is no persistent
  loop and no shared state between an async loop and our threads — the
  async lifetime is exactly one function call. This respects anti-pattern
  #2 (don't share state across an asyncio loop and a thread).

ROOM_ID CACHE:
  TikTokLive resolves room_id internally per call. We additionally cache
  the HLS URL resolution path lightly: room_id for a live user is stable
  within a session, so once is_live() has populated client.room_id we can
  reuse it for stream_url() in the same recording attempt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# A liveness check runs inside the recorder's poll loop. It MUST fail fast,
# never hang: a stalled TikTok request with no timeout freezes the whole loop
# (the "recorder stops after running through the list" symptom — it's actually
# blocked on one user's network call, not stopped). Bound every network await
# so a hung check just reads as "not live" and polling continues.
_CHECK_TIMEOUT_S = 30.0
_CLOSE_TIMEOUT_S = 10.0


class TikTokLivePlatform:
    """Satisfies the LivePlatform Protocol structurally (no inheritance)."""

    name = "tiktok"

    def __init__(self, cookies_file: str | None, state_dir: str):
        self._cookies_file = cookies_file
        self._room_cache_path = Path(state_dir).expanduser() / "room_id_cache.json"
        self._room_cache: dict[str, str] = self._load_cache()

    # ── cache ─────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict[str, str]:
        try:
            return json.loads(self._room_cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        try:
            self._room_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._room_cache_path.write_text(json.dumps(self._room_cache))
        except OSError as e:
            log.warning("room_id cache write failed: %s", e)

    # ── Protocol surface ──────────────────────────────────────────────────

    def is_live(self, username: str) -> bool:
        """Synchronous liveness check. Never raises — returns False on any
        error so a poll loop can't be killed by a transient network hiccup
        or a TikTok-side change."""
        uid = username.lstrip("@")
        try:
            return asyncio.run(self._is_live_async(uid))
        except Exception as e:
            log.debug("is_live(%s) failed (treated as not-live): %s", uid, e)
            return False

    def stream_url(self, username: str) -> str:
        """Resolve the current HLS pull URL. Called only after is_live()
        returned True. May raise; caller handles a raise as 'start failed'."""
        uid = username.lstrip("@")
        return asyncio.run(self._stream_url_async(uid))

    # ── async internals ───────────────────────────────────────────────────

    async def _make_client(self, uid: str):
        # Imported lazily so importing this module (e.g. for `recorder
        # config`) doesn't require TikTokLive to be installed.
        from TikTokLive import TikTokLiveClient

        web_kwargs: dict = {}
        cookies: dict[str, str] = {}
        if self._cookies_file:
            # TikTokLive forwards web_kwargs to its httpx client. A cookies
            # file improves detection reliability for age-gated / region-
            # restricted lives.
            cookies = _parse_netscape_cookies(self._cookies_file)
            if cookies:
                web_kwargs["httpx_kwargs"] = {"cookies": cookies}
        client = TikTokLiveClient(unique_id=f"@{uid}", web_kwargs=web_kwargs)
        session_id = cookies.get("sessionid")
        if session_id:
            client.web.set_session(
                session_id=session_id,
                tt_target_idc=cookies.get("tt-target-idc"),
            )
        return client

    async def _is_live_async(self, uid: str) -> bool:
        client = await self._make_client(uid)
        try:
            # Bounded: a hung check times out (→ caught by is_live → "not live")
            # instead of stalling the poll loop forever.
            live = await asyncio.wait_for(client.is_live(), _CHECK_TIMEOUT_S)
            if live:
                rid = getattr(client, "room_id", None)
                if rid:
                    self._room_cache[uid] = str(rid)
                    self._save_cache()
            return bool(live)
        finally:
            await self._safe_close(client)

    async def _stream_url_async(self, uid: str) -> str:
        # Import lazily so this module imports without TikTokLive installed.
        from TikTokLive.client.errors import AgeRestrictedError

        client = await self._make_client(uid)
        try:
            # fetch_room_info() resolves the room from EITHER a room_id or a
            # unique_id. Called with no args it falls back to a stored
            # _web.params["room_id"], which is unset on this fresh client (that
            # state is only populated by is_live(), which ran on a different
            # client instance) — hence the KeyError('room_id') in the logs.
            # Query by unique_id instead: it hits the info_by_user/ endpoint,
            # needs no room_id, and returns the same payload shape.
            info = await asyncio.wait_for(
                client.web.fetch_room_info(unique_id=uid), _CHECK_TIMEOUT_S)
            url = _extract_pull_url(info)
            if not url:
                raise RuntimeError(
                    f"could not extract HLS URL for @{uid} from room info; "
                    f"TikTok response shape may have changed"
                )
            return url
        except AgeRestrictedError:
            # 18+ stream: the unsigned webcast API won't return room info
            # (status_code 4003110). Fall back to driving a logged-in headless
            # browser, which lets TikTok's own JS sign the pull-URL request —
            # exactly what playback in a real browser does. See tiktok_browser.
            log.info(
                "tiktok: @%s is age-restricted — webcast API blocked, "
                "trying headless-browser fallback", uid)
            return await self._stream_url_via_browser(uid)
        finally:
            await self._safe_close(client)

    async def _stream_url_via_browser(self, uid: str) -> str:
        from .tiktok_browser import resolve_stream_url_via_browser
        # The resolver runs its own Playwright loop; we're already on an event
        # loop here, so run it in a thread to avoid nesting asyncio.run().
        return await asyncio.to_thread(
            resolve_stream_url_via_browser, uid, self._cookies_file)

    @staticmethod
    async def _safe_close(client) -> None:
        """Close the client's HTTP sessions (httpx + curl_cffi).

        is_live()/stream_url() build a fresh client on EVERY poll. Without
        this close the sessions leak file descriptors and connections; after
        enough polls (accelerated by the per-recording HANDOFF re-scan)
        is_live() starts failing, is caught as 'not live', and the recorder
        appears to stop listening. Bounded + never raises — a slow or failing
        close must not hang or kill the poll loop."""
        try:
            await asyncio.wait_for(client.close(), _CLOSE_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 — best-effort cleanup
            log.debug("tiktok: client close failed/timed out (ignored): %s", e)


# ── helpers ─────────────────────────────────────────────────────────────────

# Quality ranking for TikTok live variants. TikTok labels each variant with a
# quality name (origin/uhd/hd/sd/ld, sometimes suffixed like HD1/SD1/SD2 or
# FULL_HD1). Higher score = better. Used only when the authoritative sdk
# `options.qualities` levels are absent — otherwise we trust TikTok's own
# `level` int. Unknown names score 0 (below every known quality but still
# selectable if nothing else exists).
_QUALITY_RANK = {
    "origin": 100, "or4": 100,
    "uhd": 90, "4k": 90,
    "full_hd": 80, "fullhd": 80,
    "hd": 70,
    "sd": 50, "md": 55,
    "ld": 30,
}


def _quality_rank(name: str) -> int:
    """Score a TikTok quality label so the highest-possible variant sorts
    first. Case-insensitive; a trailing index (HD1, SD2) is stripped so it
    ranks with its base quality. Unknown labels score 0."""
    key = (name or "").strip().lower().rstrip("0123456789").rstrip("_")
    return _QUALITY_RANK.get(key, 0)


def _urls_from_sdk_stream_data(stream: dict) -> list[tuple[int, str, str]]:
    """Parse TikTok's authoritative multi-quality blob
    `stream_url.live_core_sdk_data.pull_data` into a ranked candidate list of
    (rank, kind, url), best first. `stream_data` is a JSON *string* mapping
    each quality name → {"main": {"flv": ..., "hls": ...}}; `options.qualities`
    (when present) carries a per-quality `level` int we trust over our name
    table. Returns [] if the blob is missing or unparseable."""
    out: list[tuple[int, str, str]] = []
    try:
        pull = (stream.get("live_core_sdk_data") or {}).get("pull_data") or {}
        data = json.loads(pull.get("stream_data") or "{}").get("data") or {}
    except (AttributeError, TypeError, json.JSONDecodeError):
        return out
    if not isinstance(data, dict):
        return out
    # Authoritative levels, if TikTok provided them: {quality_name: level}.
    levels: dict[str, int] = {}
    try:
        for q in (pull.get("options") or {}).get("qualities") or []:
            nm = q.get("name") or q.get("sdk_key")
            if nm is not None and isinstance(q.get("level"), int):
                levels[str(nm).lower()] = q["level"]
    except (AttributeError, TypeError):
        levels = {}
    for name, variant in data.items():
        main = (variant or {}).get("main") if isinstance(variant, dict) else None
        if not isinstance(main, dict):
            continue
        rank = levels.get(str(name).lower(), _quality_rank(str(name)))
        flv, hls = main.get("flv"), main.get("hls")
        # Prefer FLV within a quality (single long-lived connection, sidesteps
        # the HLS-edge stall — see below); HLS is the same-quality fallback.
        if isinstance(flv, str) and flv:
            out.append((rank, "flv", flv))
        if isinstance(hls, str) and hls:
            out.append((rank, "hls", hls))
    # Highest quality first; FLV before HLS at equal quality.
    out.sort(key=lambda t: (t[0], t[1] == "flv"), reverse=True)
    return out


def _extract_pull_url(room_info: dict) -> str | None:
    """Pull the HIGHEST-POSSIBLE-quality stream URL out of TikTok room info.

    Quality: TikTok offers a ladder of variants (origin/uhd/hd/sd/ld). We pick
    the best the source actually exposes, walking down the ladder to the first
    present — never an arbitrary map entry. The authoritative source is the
    `live_core_sdk_data` sdk blob (carries every quality + TikTok's own `level`
    ranking); the flat `flv_pull_url`/`hls_pull_url_map` dicts are the fallback
    when that blob is absent.

    Edge family: PREFER FLV over HLS *at equal quality*. TikTok serves HLS
    playlist edges (`pull-hls-*`) and continuous-FLV edges (`pull-f5-*`).
    Observed in prod (Beelink box, SG-origin streams): the HLS edges complete
    the TLS handshake but then STALL the playlist/segment response — yt-dlp
    hangs until its socket timeout, exits rc=1 with zero bytes, and the state
    machine reads that as a dead stream. The FLV edge for the SAME stream
    returns 200 and streams immediately, over a single long-lived connection
    (no per-segment fetches, no m3u8 rotation), sidestepping the segment-404 /
    URL-rotation reconnect churn. yt-dlp's ffmpeg downloader ingests FLV
    natively. So FLV wins ties, but a strictly-higher quality wins over FLV.
    Returns None if nothing matches."""
    if not isinstance(room_info, dict):
        return None
    stream = room_info.get("stream_url")
    if not isinstance(stream, dict):
        return None

    # 1. Authoritative multi-quality sdk blob — highest quality, FLV-preferred.
    ranked = _urls_from_sdk_stream_data(stream)
    if ranked:
        return ranked[0][2]

    # 2. Fallback: flat flv_pull_url map, ranked by quality name (FLV first).
    flv = stream.get("flv_pull_url")
    if isinstance(flv, dict) and flv:
        return max(flv.items(), key=lambda kv: _quality_rank(kv[0]))[1]
    if isinstance(flv, str) and flv:
        return flv

    # 3. Fallback: multi-quality HLS map, ranked by quality name.
    hls_map = stream.get("hls_pull_url_map")
    if isinstance(hls_map, dict) and hls_map:
        return max(hls_map.items(), key=lambda kv: _quality_rank(kv[0]))[1]

    # 4. Last resort: the single default HLS pull URL.
    hls = stream.get("hls_pull_url")
    if isinstance(hls, str) and hls:
        return hls
    return None


def _parse_netscape_cookies(path: str) -> dict[str, str]:
    """Minimal Netscape cookies.txt → {name: value}. Best-effort; bad
    lines are skipped. We only need the values httpx will send as a cookie
    header, not full attributes."""
    out: dict[str, str] = {}
    try:
        for line in Path(path).expanduser().read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out[parts[5]] = parts[6]
    except OSError:
        pass
    return out
