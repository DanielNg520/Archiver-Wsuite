"""
recorder.ban_check
──────────────────
Stage 2 of the recorder's auto-ban gate: a profile-page confirmation that an
unstartable TikTok user is actually GONE (banned/deleted), not merely
unrecordable from this box.

Design bias: NEVER ban on ambiguity. The only verdict that authorizes a ban is
GONE, and GONE requires an explicit marker on the profile page. Everything
else — network errors, bot-walls, layout changes, private accounts — degrades
to ALIVE/PRIVATE/UNKNOWN, all of which keep the user in the ordinary cooldown
loop. A false GONE silently stops recording a live account; a false UNKNOWN
just re-checks next escalation.

⚠️ Live-data caveat (from the refactor plan): the TikTok page markers below
must be confirmed against a real known-banned handle before Stage 2 is
trusted. Until then treat this module as "wired but unverified".

Fetch is a plain HTTPS GET via stdlib urllib (the recorder has no HTTP client
dependency), optionally sending the recorder's Netscape-format cookies —
profile existence is public, so this works even with no/expired cookies.
"""
from __future__ import annotations

import gzip
import logging
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path

from core.account_gone import match_account_gone

log = logging.getLogger(__name__)

_TIMEOUT_S = 20.0
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# TikTok-specific page markers, lowercased. The shared core.account_gone
# signals cover extractor error strings; the profile PAGE uses its own wording,
# so both lists are consulted for GONE.
_TIKTOK_GONE_MARKERS = (
    "couldn't find this account",
    "couldn’t find this account",     # curly apostrophe variant
    "could not find this account",
)
_TIKTOK_PRIVATE_MARKERS = (
    "this account is private",
)
# Markers that prove the profile page actually RENDERED a real account —
# TikTok's hydration JSON carries the user object only for existing profiles.
_TIKTOK_ALIVE_MARKERS = (
    '"uniqueid":"{u}"',
    '"@id":"https://www.tiktok.com/@{u}"',
)


class ProfileStatus(Enum):
    GONE    = "gone"       # explicit banned/deleted marker → ban authorized
    ALIVE   = "alive"      # profile renders normally
    PRIVATE = "private"    # exists but private — NOT gone
    UNKNOWN = "unknown"    # network error / ambiguous page — never bans


def _cookie_header(cookies_file: str | None) -> str:
    """Netscape cookies.txt → a single `Cookie:` header value for .tiktok.com.
    Best-effort: unreadable/malformed input yields '' (public fetch)."""
    if not cookies_file:
        return ""
    pairs: list[str] = []
    try:
        lines = Path(cookies_file).expanduser().read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        s = line.strip()
        if s.startswith("#HttpOnly_"):
            s = s[len("#HttpOnly_"):]
        elif not s or s.startswith("#"):
            continue
        parts = s.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, _expiry, name, value = parts[:7]
        if "tiktok.com" in domain:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _fetch_profile_html(username: str, cookies_file: str | None) -> str | None:
    """GET https://www.tiktok.com/@<username>; returns the (decoded) body, or
    None on any network/HTTP failure. A 404 status still returns its body —
    the classification is marker-based, not status-based."""
    url = f"https://www.tiktok.com/@{username}"
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9",
               "Accept-Encoding": "gzip"}
    cookie = _cookie_header(cookies_file)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        try:
            resp = urllib.request.urlopen(req, timeout=_TIMEOUT_S)
        except urllib.error.HTTPError as e:
            resp = e                     # 404 pages carry the marker text
        with resp:
            body = resp.read()
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return body.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("ban_check: fetch @%s failed: %s", username, e)
        return None


def classify_profile_html(username: str, html: str) -> ProfileStatus:
    """Marker-based classification of a profile page body. Split from the
    fetch so the selftest can drive it with canned pages."""
    low = html.lower()
    if match_account_gone(low) or any(m in low for m in _TIKTOK_GONE_MARKERS):
        return ProfileStatus.GONE
    if any(m in low for m in _TIKTOK_PRIVATE_MARKERS):
        return ProfileStatus.PRIVATE
    u = username.lower()
    if any(m.format(u=u) in low for m in _TIKTOK_ALIVE_MARKERS):
        return ProfileStatus.ALIVE
    # Page fetched but matched nothing we recognize (bot-wall, layout change,
    # region gate…) — ambiguous, so it must not authorize a ban.
    return ProfileStatus.UNKNOWN


def profile_check(username: str, cookies_file: str | None = None) -> ProfileStatus:
    """Classify @username's profile page. UNKNOWN on any fetch failure."""
    html = _fetch_profile_html(username, cookies_file)
    if html is None:
        return ProfileStatus.UNKNOWN
    status = classify_profile_html(username, html)
    log.info("ban_check: @%s → %s", username, status.value)
    return status
