"""
recorder.platforms.tiktok_browser
──────────────────────────────────
Headless-browser fallback for resolving the HLS/FLV pull URL of an
AGE-RESTRICTED TikTok live.

WHY THIS EXISTS:
  The normal path (recorder.platforms.tiktok) asks TikTok's webcast API
  `room/info` for the stream URL. For 18+ streams TikTok refuses that
  request unless it carries a browser-generated X-Bogus/a-bogus signature
  plus a live msToken — TikTokLive's free signer won't sign room/info, and
  yt-dlp hits the same wall. The API returns `{"prompts": ""}` with
  status_code 4003110 → TikTokLive raises AgeRestrictedError.

  A logged-in browser succeeds because TikTok's own `webmssdk` JS runs in
  the page and signs the XHR that fetches the pull URL. So we do exactly
  what the browser does: load `@user/live` with the session cookies in a
  real (headless) Chromium, let TikTok's JS make its signed request, and
  sniff the resulting pull URL off the network. That URL is then handed to
  yt-dlp like any other.

COST / SCOPE:
  This is a FALLBACK ONLY — it is slow (spawns Chromium, ~5–15s) and is
  invoked solely when the API path age-gates. Non-restricted streams never
  touch this module. Requires `playwright` + a Chromium build
  (`python -m playwright install chromium`).

ISOLATION:
  Like the rest of recorder.platforms.tiktok, the async lifetime is exactly
  one call inside a throwaway event loop — no state shared with recorder
  threads. The browser is always closed in a finally.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# A pull URL must look like a real live stream edge, not an unrelated asset.
# TikTok live edges live on pull-*.tiktokcdn*.com and the path carries a
# stream-<id> segment. We accept both HLS (.m3u8) and FLV (.flv) — yt-dlp's
# ffmpeg downloader ingests either; HLS is preferred when both appear.
_PULL_HINTS = ("pull-", "/stream-", "/game/", "pull_")


class CookiesRequiredError(RuntimeError):
    """The browser fallback was reached but no TikTok session cookies were
    available, so an age-restricted live cannot be resolved. Distinct type so
    the state machine can react specifically (e.g. temporarily skip the user)
    instead of treating it like a generic transient resolve failure."""


def _is_pull_url(url: str) -> bool:
    """True if `url` looks like a live-stream pull edge (HLS or FLV) rather
    than an unrelated page asset. Module-level so it is unit-testable."""
    return (".m3u8" in url or ".flv" in url) and any(h in url for h in _PULL_HINTS)


def resolve_stream_url_via_browser(
    uid: str,
    cookies_file: str | None,
    timeout_s: float = 45.0,
) -> str:
    """Synchronous entry point: return a pull URL for @uid's live, or raise.

    Runs the async sniffer in a throwaway loop so the threaded recorder can
    call it like any other blocking resolve. Raises RuntimeError if nothing
    is captured before timeout (caller treats a raise as 'start failed')."""
    return asyncio.run(_resolve_async(uid.lstrip("@"), cookies_file, timeout_s))


async def _resolve_async(uid: str, cookies_file: str | None, timeout_s: float) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "age-restricted live needs the browser fallback, but playwright "
            "is not installed. Run: python -m playwright install chromium"
        ) from e

    cookies = _netscape_to_playwright(cookies_file) if cookies_file else []
    if not cookies:
        raise CookiesRequiredError(
            "browser fallback needs the TikTok session cookies; none loaded "
            f"from {cookies_file!r}"
        )

    # Two capture channels, best first:
    #   best[]  — HIGHEST-quality URL selected from the room-info JSON the page
    #             fetches (authoritative: carries every quality variant). This
    #             is how we record origin/uhd instead of whatever default the
    #             player happened to load.
    #   found[] — raw pull URLs sniffed off request/response URLs, used only as
    #             a fallback when no room-info JSON could be parsed. This is the
    #             player's default quality, so it's second choice.
    # An Event lets us return promptly once either channel yields something.
    best: list[str] = []
    found: list[str] = []
    got = asyncio.Event()

    def _consider(url: str) -> None:
        if _is_pull_url(url) and url not in found:
            found.append(url)
            got.set()

    async def _consider_json(resp) -> None:
        """Try to read a room-info response body and select its highest-quality
        URL. Best-effort: non-JSON bodies, unretrievable bodies, and unexpected
        shapes are ignored so the media-URL fallback still applies."""
        url = resp.url
        if "webcast" not in url or not any(
            k in url for k in ("room/info", "room/enter", "/enter/", "reflow")
        ):
            return
        try:
            body = await resp.json()
        except Exception:  # noqa: BLE001 - body may be non-JSON or gone
            return
        obj = _find_stream_url_obj(body)
        if not obj:
            return
        from .tiktok import _extract_pull_url  # lazy: avoids import cycle
        picked = _extract_pull_url({"stream_url": obj})
        if picked and picked not in best:
            best.append(picked)
            got.set()

    async with async_playwright() as pw:
        browser = await _launch_chromium(pw)
        try:
            ctx = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/129.0.0.0 Safari/537.36"
                )
            )
            await ctx.add_cookies(cookies)
            page = await ctx.new_page()
            page.on("request", lambda r: _consider(r.url))
            page.on("response", lambda r: _consider(r.url))
            page.on("response", lambda r: asyncio.create_task(_consider_json(r)))

            await page.goto(
                f"https://www.tiktok.com/@{uid}/live",
                wait_until="domcontentloaded",
                timeout=min(45000, int(timeout_s * 1000)),
            )
            await _dismiss_age_prompt(page)

            # Wait for the first candidate, then a short grace so the room-info
            # JSON (highest quality) can land even if a default-quality media
            # URL was sniffed first.
            try:
                await asyncio.wait_for(got.wait(), timeout_s)
            except asyncio.TimeoutError:
                pass
            if found or best:
                await asyncio.sleep(2.0)  # grace for the authoritative JSON
        finally:
            await browser.close()

    # Prefer the authoritative highest-quality pick; fall back to the sniffed
    # default-quality URL only if no room-info JSON was parseable.
    if best:
        return best[0]
    if found:
        return _pick_best(found)
    raise RuntimeError(
        f"browser fallback could not capture a pull URL for @{uid} "
        f"within {timeout_s:.0f}s (stream ended, or TikTok layout changed)"
    )


def _find_stream_url_obj(obj):
    """Recursively locate TikTok's `stream_url` object inside a decoded
    room-info JSON body — the dict that carries `flv_pull_url` /
    `live_core_sdk_data` / `hls_pull_url`. Room-info responses nest it under
    varying wrappers (`data.stream_url`, `data[0].stream_url`, …), so we search
    structurally rather than assume a path. Returns the first match or None.
    Depth-bounded to keep a pathological body from blowing the stack."""
    def _walk(node, depth):
        if depth > 8 or not isinstance(node, (dict, list)):
            return None
        if isinstance(node, dict):
            if any(k in node for k in
                   ("flv_pull_url", "live_core_sdk_data", "hls_pull_url")):
                return node
            children = node.values()
        else:
            children = node
        for child in children:
            hit = _walk(child, depth + 1)
            if hit is not None:
                return hit
        return None
    return _walk(obj, 0)


_install_lock = asyncio.Lock()
_install_attempted = False


async def _launch_chromium(pw):
    """Launch headless Chromium, self-healing a missing/stale browser build.

    Playwright pins an exact browser revision per library version. When pip or
    pipx bumps `playwright` (e.g. to one wanting build 1228), the Chromium
    binaries already on disk no longer match and `launch()` raises
    "Executable doesn't exist at ...", which would otherwise fail EVERY
    age-restricted recording until a human manually runs `playwright install`.

    So on that specific error we run `python -m playwright install chromium`
    for the *current* interpreter (the same venv this code runs in, so it
    lands the revision this Playwright expects) exactly once per process, then
    retry the launch. Any other launch error, or a still-failing retry, is
    re-raised unchanged for the caller's start-failure handling."""
    try:
        return await pw.chromium.launch(headless=True)
    except Exception as e:  # noqa: BLE001 - narrowed by message below
        if "Executable doesn't exist" not in str(e):
            raise
        await _ensure_browser_installed()
        # Retry once; a second failure is real (disk full, no network, etc.).
        return await pw.chromium.launch(headless=True)


async def _ensure_browser_installed() -> None:
    """Run `playwright install chromium` for this interpreter, once per process.

    Guarded by a lock + flag so concurrent age-restricted resolves don't kick
    off duplicate downloads. The download is blocking, so it runs in a thread
    to avoid stalling the event loop."""
    global _install_attempted
    async with _install_lock:
        if _install_attempted:
            return
        _install_attempted = True
        log.warning(
            "tiktok-browser: Chromium build missing/stale for the installed "
            "Playwright; auto-running `playwright install chromium` (one-time)"
        )
        await asyncio.to_thread(_run_playwright_install)


def _run_playwright_install() -> None:
    """Blocking `python -m playwright install chromium`. Logs but never raises
    here — a failure surfaces on the launch retry with Playwright's own error."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode == 0:
            log.warning("tiktok-browser: Chromium install complete")
        else:
            log.error(
                "tiktok-browser: `playwright install chromium` exited %d: %s",
                proc.returncode,
                (proc.stderr or proc.stdout or "").strip()[-500:],
            )
    except Exception as e:  # noqa: BLE001 - launch retry will surface the real state
        log.error("tiktok-browser: `playwright install chromium` failed: %s", e)


async def _dismiss_age_prompt(page) -> None:
    """Best-effort: click through TikTok's 'sensitive/age-restricted content'
    interstitial so its JS fires the signed pull-URL request. Never raises —
    if no prompt is present, or the labels shifted, we just proceed and rely
    on the network sniff."""
    for label in ("Continue", "Watch now", "Watch", "I'm over 18",
                  "View", "Skip", "Got it"):
        try:
            el = page.get_by_text(label, exact=False)
            if await el.count():
                await el.first.click(timeout=2000)
                log.debug("tiktok-browser: dismissed prompt via %r", label)
                return
        except Exception:  # noqa: BLE001 - interstitial is optional
            continue


def _pick_best(urls: list[str]) -> str:
    """Prefer FLV (.flv) over HLS (.m3u8). TikTok's HLS pull edges
    (`pull-hls-*`) stall the response from this deployment while the FLV edges
    (`pull-f5-*`) stream fine, and FLV's single long-lived connection avoids the
    per-segment 404s / m3u8 rotation that split HLS recordings. yt-dlp's ffmpeg
    downloader ingests FLV natively. Kept in sync with tiktok._extract_pull_url.
    Within a kind, first-seen wins; HLS is the fallback when no FLV appears."""
    for u in urls:
        if ".flv" in u:
            return u
    return urls[0]


def _netscape_to_playwright(path: str) -> list[dict]:
    """Netscape cookies.txt → Playwright add_cookies() dicts. Handles the
    `#HttpOnly_` line prefix (which the bare `#` comment skip would otherwise
    drop, losing sid_tt/sessionid). Best-effort; malformed lines skipped."""
    out: list[dict] = []
    try:
        lines = Path(path).expanduser().read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        http_only = False
        if s.startswith("#HttpOnly_"):
            s = s[len("#HttpOnly_"):]
            http_only = True
        elif not s or s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, cpath, secure, _expiry, name, value = parts[:7]
        out.append({
            "name": name,
            "value": value,
            # Playwright wants a leading-dot domain for host-spanning cookies.
            "domain": domain if domain.startswith(".") else "." + domain.lstrip("."),
            "path": cpath or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
        })
    return out
