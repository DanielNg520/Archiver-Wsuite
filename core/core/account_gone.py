"""
core.account_gone
─────────────────
The ONE place that decides "does this extractor error mean the ACCOUNT is
gone?" — banned, suspended, or deleted, as opposed to a single missing item, a
format error, or a transient hiccup.

Lives in core because two sides need the same verdict:
  - the archiver extractors (`archiver.platforms`), which sniff gallery-dl /
    yt-dlp log streams and raise AccountGoneError, and
  - the recorder ban check (`recorder.ban_check`, Phase 3), which classifies a
    TikTok profile page as GONE / ALIVE / PRIVATE / UNKNOWN.

Keeping the signal list here means the two can never disagree on what "gone"
looks like.

Kept CONSERVATIVE on purpose — a gone verdict retires a user from the active
list, so a false positive is worse than a false negative (a missed ban just
wastes one more run's fetch; a wrong ban silently stops archiving a live
account). Per-item 404s are NOT in here; gallery-dl signals a gone profile via
NotFoundError, which callers catch by type instead.
"""
from __future__ import annotations

# Lowercased substrings that, in an extractor's error output, unambiguously mean
# the ACCOUNT is gone.
ACCOUNT_GONE_SIGNALS = (
    "account is suspended",
    "account suspended",
    "has been suspended",
    "user not found",
    "account does not exist",
    "user does not exist",
    "no longer exists",
    "could not find user",
    "unable to find user",
    "no longer available",
    "account is unavailable",
    "account has been banned",
    "user has been banned",
    "account was banned",
)


def match_account_gone(text: str) -> str:
    """Return the first matching gone-signal substring (the human-readable
    reason), or '' if none match. `text` should already be lowercased."""
    for sig in ACCOUNT_GONE_SIGNALS:
        if sig in text:
            return sig
    return ""
