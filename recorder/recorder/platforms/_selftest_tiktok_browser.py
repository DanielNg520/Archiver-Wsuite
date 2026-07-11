"""
Validation harness (not shipped) for recorder.platforms.tiktok_browser.

Covers the pure helpers (cookie parsing, pull-URL detection, HLS-over-FLV
preference) AND the full async resolve flow against a FAKE Playwright — so
the capture/prompt/timeout/early-return behavior is exercised without a real
browser or network. Run:

    python recorder/recorder/platforms/_selftest_tiktok_browser.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import json  # noqa: E402

from recorder.platforms import tiktok_browser as tb   # noqa: E402
from recorder.platforms import tiktok as tk           # noqa: E402

OK = "✓"
_passed = 0


def check(cond: bool, msg: str) -> None:
    global _passed
    if not cond:
        raise AssertionError(msg)
    _passed += 1
    print(f"  {OK} {msg}")


def expect_raises(exc, fn, msg: str) -> None:
    try:
        fn()
    except exc:
        global _passed
        _passed += 1
        print(f"  {OK} {msg}")
        return
    raise AssertionError(f"expected {exc.__name__}: {msg}")


# ── pure helper: _is_pull_url ─────────────────────────────────────────────

def test_is_pull_url():
    print("_is_pull_url:")
    pos = [
        "https://pull-o5-sg01.tiktokcdn-us.com/stage/stream-184.flv?_session_id=x",
        "https://pull-hls-f16-sg01.tiktokcdn.com/stage/stream-156_hd/index.m3u8?sign=a",
        "https://x.tiktokcdn.com/game/stream-9.m3u8",
    ]
    neg = [
        "https://www.tiktok.com/@user/live",            # no media ext
        "https://sf16.tiktokcdn.com/avatar/thumb.jpeg", # image, no hint
        "https://pull-x.tiktokcdn.com/banner.png",      # hint but not media
        "https://example.com/video.m3u8",               # media but no pull hint
        "",                                             # empty
    ]
    for u in pos:
        check(tb._is_pull_url(u), f"accepts pull URL: …{u[-32:]}")
    for u in neg:
        check(not tb._is_pull_url(u), f"rejects non-pull: {u[:40]!r}")


# ── pure helper: _pick_best ───────────────────────────────────────────────

def test_pick_best():
    print("_pick_best:")
    flv = "https://pull-a.tiktokcdn.com/stream-1.flv?s=1"
    flv2 = "https://pull-b.tiktokcdn.com/stream-2.flv?s=2"
    hls = "https://pull-c.tiktokcdn.com/stream-1.m3u8?s=3"
    check(tb._pick_best([hls, flv]) == flv, "prefers FLV even when HLS seen first")
    check(tb._pick_best([flv, hls]) == flv, "keeps FLV when FLV seen first")
    check(tb._pick_best([flv, flv2]) == flv, "FLV-only: first-seen wins")
    check(tb._pick_best([hls]) == hls, "single HLS returned (fallback)")


# ── quality selection: _quality_rank / _extract_pull_url ──────────────────

def test_quality_rank():
    print("_quality_rank (highest-possible ordering):")
    r = tk._quality_rank
    check(r("origin") > r("uhd") > r("hd") > r("sd") > r("ld"),
          "origin > uhd > hd > sd > ld")
    check(r("HD1") == r("hd"), "trailing index stripped (HD1 == hd)")
    check(r("FULL_HD1") > r("hd"), "full_hd outranks hd")
    check(r("SD2") == r("sd"), "SD2 == sd")
    check(r("weird") == 0, "unknown label scores 0")


def _sdk_room_info(data: dict, qualities=None, flat_flv=None) -> dict:
    """Build a room_info whose stream_url carries a live_core_sdk_data blob
    (and optionally a competing flat flv_pull_url map, which must LOSE)."""
    pull: dict = {"stream_data": json.dumps({"data": data})}
    if qualities is not None:
        pull["options"] = {"qualities": qualities}
    stream = {"live_core_sdk_data": {"pull_data": pull}}
    if flat_flv is not None:
        stream["flv_pull_url"] = flat_flv
    return {"stream_url": stream}


def test_extract_pull_url():
    print("_extract_pull_url (highest-possible + FLV tiebreak):")
    ex = tk._extract_pull_url

    # 1) sdk blob with authoritative levels → highest level, FLV preferred,
    #    beating a flat flv_pull_url map that only offers lower qualities.
    ri = _sdk_room_info(
        data={
            "sd":     {"main": {"flv": "flv_sd", "hls": "hls_sd"}},
            "origin": {"main": {"flv": "flv_origin", "hls": "hls_origin"}},
            "hd":     {"main": {"flv": "flv_hd", "hls": "hls_hd"}},
        },
        qualities=[{"name": "origin", "level": 5},
                   {"name": "hd", "level": 3},
                   {"name": "sd", "level": 1}],
        flat_flv={"HD1": "flat_hd", "SD1": "flat_sd"},
    )
    check(ex(ri) == "flv_origin", "sdk: picks highest level, FLV, over flat map")

    # 2) highest quality only offers HLS → quality beats the FLV edge-pref.
    ri2 = _sdk_room_info(
        data={
            "hd":     {"main": {"flv": "flv_hd"}},
            "origin": {"main": {"hls": "hls_origin"}},
        },
        qualities=[{"name": "origin", "level": 5}, {"name": "hd", "level": 3}],
    )
    check(ex(ri2) == "hls_origin", "sdk: origin-HLS beats hd-FLV (quality first)")

    # 3) no options.levels → falls back to name-rank inside the sdk data.
    ri3 = _sdk_room_info(data={
        "sd":  {"main": {"flv": "flv_sd"}},
        "uhd": {"main": {"flv": "flv_uhd"}},
    })
    check(ex(ri3) == "flv_uhd", "sdk without levels: name-rank picks uhd")

    # 4) no sdk blob → flat flv_pull_url map ranked by name (FULL_HD1 wins).
    ri4 = {"stream_url": {"flv_pull_url": {
        "SD2": "u_sd2", "HD1": "u_hd1", "FULL_HD1": "u_fhd"}}}
    check(ex(ri4) == "u_fhd", "flat flv map: highest-named quality wins")

    # 5) no flv → multi-quality HLS map ranked by name.
    ri5 = {"stream_url": {"hls_pull_url_map": {"SD1": "h_sd", "HD1": "h_hd"}}}
    check(ex(ri5) == "h_hd", "hls map: highest-named quality wins")

    # 6) last resort: single hls_pull_url string.
    ri6 = {"stream_url": {"hls_pull_url": "only_hls"}}
    check(ex(ri6) == "only_hls", "single hls_pull_url used as last resort")

    # 7) garbage / empty → None (no raise).
    check(ex({"stream_url": {}}) is None, "empty stream_url → None")
    check(ex({}) is None, "no stream_url → None")
    check(ex({"stream_url": {"live_core_sdk_data":
              {"pull_data": {"stream_data": "not json"}}}}) is None,
          "unparseable stream_data → None")


def test_find_stream_url_obj():
    print("_find_stream_url_obj (browser-path room-info dig):")
    f = tb._find_stream_url_obj
    inner = {"flv_pull_url": {"HD1": "x"}}
    check(f({"data": {"stream_url": inner}}) is inner,
          "digs stream_url out of data.stream_url wrapper")
    check(f({"data": [{"stream_url": inner}]}) is inner,
          "digs through a list wrapper (data[0].stream_url)")
    check(f({"live_core_sdk_data": {"pull_data": {}}}).get("live_core_sdk_data"),
          "matches on live_core_sdk_data too")
    check(f({"nope": 1}) is None, "no stream_url present → None")


# ── pure helper: _netscape_to_playwright ──────────────────────────────────

def test_cookie_parse():
    print("_netscape_to_playwright:")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "tiktok.txt"
        p.write_text(
            "# Netscape HTTP Cookie File\n"
            "\n"
            "# a normal comment\n"
            ".tiktok.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\tABC123\n"
            "#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t1999999999\tsid_tt\tDEF456\n"
            "tiktok.com\tFALSE\t/path\tFALSE\t0\tplain\tval\n"
            "broken line without enough fields\n"
            "\t\t\t\t\t\t\n"
        )
        cookies = tb._netscape_to_playwright(str(p))
        by = {c["name"]: c for c in cookies}

        check("sessionid" in by, "parses normal sessionid cookie")
        check(by["sessionid"]["value"] == "ABC123", "sessionid value correct")
        check(by["sessionid"]["secure"] is True, "secure flag TRUE parsed")
        check("sid_tt" in by, "parses #HttpOnly_ cookie (not dropped as comment)")
        check(by["sid_tt"]["httpOnly"] is True, "HttpOnly flag set on sid_tt")
        check(by["sid_tt"]["value"] == "DEF456", "sid_tt value correct")
        check(by["plain"]["domain"] == ".tiktok.com",
              "bare domain normalized to leading-dot")
        check(by["plain"]["secure"] is False, "secure FALSE parsed")
        check(by["plain"]["path"] == "/path", "custom path preserved")
        # malformed/empty rows produced nothing extra
        check(set(by) == {"sessionid", "sid_tt", "plain"},
              "malformed/blank/comment lines skipped")

    # missing file → empty list, no raise
    check(tb._netscape_to_playwright("/no/such/file.txt") == [],
          "missing cookie file → [] (no raise)")


# ── fake Playwright harness ───────────────────────────────────────────────

class _FakeLocator:
    def __init__(self, count): self._count = count
    async def count(self): return self._count
    @property
    def first(self): return self
    async def click(self, timeout=None): self.clicked = True


class _FakePage:
    """Records handlers, lets the test fire fake network events, and tracks
    goto/prompt interactions."""
    def __init__(self, emit_urls, prompt_label=None, goto_exc=None):
        self._emit = emit_urls
        self._prompt_label = prompt_label
        self._goto_exc = goto_exc
        self._handlers = {"request": [], "response": []}
        self.goto_url = None
        self.dismissed = None

    def on(self, event, fn): self._handlers[event].append(fn)

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_url = url
        if self._goto_exc:
            raise self._goto_exc
        # Fire the queued network URLs as 'request' events.
        for u in self._emit:
            for fn in self._handlers["request"]:
                fn(type("R", (), {"url": u}))

    def get_by_text(self, label, exact=False):
        if label == self._prompt_label:
            self.dismissed = label
            return _FakeLocator(1)
        return _FakeLocator(0)

    async def wait_for_timeout(self, ms): pass


class _FakeContext:
    def __init__(self, page): self._page = page; self.added = None
    async def add_cookies(self, cookies): self.added = cookies
    async def new_page(self): return self._page


class _FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.last_context = None
    async def new_context(self, **kw):
        self.last_context = _FakeContext(self._page)
        return self.last_context
    async def close(self): self.closed = True


class _FakeChromium:
    def __init__(self, browser): self._browser = browser; self.launched = False
    async def launch(self, headless=True):
        self.launched = True
        return self._browser


class _FakePW:
    def __init__(self, chromium): self.chromium = chromium
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _install_fake_pw(monkey_page):
    """Patch tiktok_browser's lazy `from playwright.async_api import
    async_playwright` by injecting a fake module into sys.modules."""
    import types
    browser = _FakeBrowser(monkey_page)
    chromium = _FakeChromium(browser)

    def async_playwright():
        return _FakePW(chromium)

    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = async_playwright
    pkg = types.ModuleType("playwright")
    pkg.async_api = mod
    sys.modules["playwright"] = pkg
    sys.modules["playwright.async_api"] = mod
    return browser


def _cookie_file(tmp: Path) -> str:
    p = tmp / "c.txt"
    p.write_text(".tiktok.com\tTRUE\t/\tTRUE\t1999999999\tsessionid\tABC\n")
    return str(p)


def test_resolve_flow():
    print("_resolve_async (fake browser):")
    with tempfile.TemporaryDirectory() as d:
        cf = _cookie_file(Path(d))

        # 1) FLV-only stream, with an age prompt to dismiss
        flv = "https://pull-o5.tiktokcdn-us.com/stage/stream-1.flv?_session_id=x"
        page = _FakePage([flv], prompt_label="Continue")
        browser = _install_fake_pw(page)
        url = asyncio.run(tb._resolve_async("ingyongcuong", cf, timeout_s=3))
        check(url == flv, "captures FLV pull URL from network events")
        check(page.goto_url.endswith("/@ingyongcuong/live"), "navigated to live page")
        check(page.dismissed == "Continue", "dismissed the age prompt")
        injected = {c["name"] for c in browser.last_context.added}
        check("sessionid" in injected, "session cookies injected into context")
        check(browser.closed, "browser closed after resolve")

        # 2) Both FLV and HLS present → FLV wins (preference + grace)
        hls = "https://pull-c.tiktokcdn.com/stage/stream-1.m3u8?sign=a"
        page2 = _FakePage([hls, flv])
        _install_fake_pw(page2)
        url2 = asyncio.run(tb._resolve_async("u", cf, timeout_s=3))
        check(url2 == flv, "prefers FLV when both FLV and HLS captured")

        # 3) No pull URLs emitted → RuntimeError after timeout
        page3 = _FakePage(["https://www.tiktok.com/@u/live"])
        _install_fake_pw(page3)
        expect_raises(
            RuntimeError,
            lambda: asyncio.run(tb._resolve_async("u", cf, timeout_s=1)),
            "no pull URL captured → RuntimeError",
        )

        # 4) prompt absent → still resolves (dismissal is best-effort)
        page4 = _FakePage([flv], prompt_label=None)
        _install_fake_pw(page4)
        url4 = asyncio.run(tb._resolve_async("u", cf, timeout_s=3))
        check(url4 == flv and page4.dismissed is None,
              "resolves even when no age prompt is present")


def test_resolve_no_cookies():
    print("_resolve_async guards:")
    with tempfile.TemporaryDirectory() as d:
        empty = Path(d) / "empty.txt"
        empty.write_text("# only a comment\n")
        page = _FakePage([])
        _install_fake_pw(page)
        expect_raises(
            RuntimeError,
            lambda: asyncio.run(tb._resolve_async("u", str(empty), timeout_s=1)),
            "empty cookie file → RuntimeError (no session)",
        )
        expect_raises(
            RuntimeError,
            lambda: asyncio.run(tb._resolve_async("u", None, timeout_s=1)),
            "None cookie file → RuntimeError",
        )


def test_resolve_pw_missing():
    print("_resolve_async without playwright installed:")
    with tempfile.TemporaryDirectory() as d:
        cf = _cookie_file(Path(d))
        # Remove playwright from sys.modules AND block re-import.
        import builtins
        saved = {k: sys.modules.pop(k) for k in list(sys.modules)
                 if k == "playwright" or k.startswith("playwright.")}
        real_import = builtins.__import__

        def blocked(name, *a, **k):
            if name == "playwright" or name.startswith("playwright."):
                raise ImportError("blocked for test")
            return real_import(name, *a, **k)

        builtins.__import__ = blocked
        try:
            expect_raises(
                RuntimeError,
                lambda: asyncio.run(tb._resolve_async("u", cf, timeout_s=1)),
                "missing playwright → RuntimeError with install hint",
            )
        finally:
            builtins.__import__ = real_import
            sys.modules.update(saved)


def main():
    print("tiktok_browser selftest\n" + "─" * 40)
    test_is_pull_url()
    test_pick_best()
    test_quality_rank()
    test_extract_pull_url()
    test_find_stream_url_obj()
    test_cookie_parse()
    test_resolve_flow()
    test_resolve_no_cookies()
    test_resolve_pw_missing()
    print("─" * 40)
    print(f"ALL PASSED — {_passed} assertions")


if __name__ == "__main__":
    main()
