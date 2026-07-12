"""
core.quarantine
───────────────
Reversible on-disk quarantine for banned/suspended/deleted users. When a user
is banned, the roster entry (PolicyStore) is only half the job — their media
folder must also leave the active tree so folder-scan discovery stops
re-adopting it. This moves it, it does NOT delete it:

    {output_dir}/{platform}/{username}/  →  {output_dir}/{platform}/.deleted/{username}/

The `.deleted/` prefix is required: every folder-scan consumer skips dot-dirs
(LocalPlatform.users, core.orphaned, core.sorter, archiver.reconcile), so a
quarantined user simply disappears from discovery without any per-consumer ban
lookup. `restore_user` is the exact inverse, used by `unban`.

Same-drive guarantee: quarantine lives under `output_dir`, so the move is always
an atomic `os.rename` on one volume — never a cross-drive copy. This stays true
after the two-root storage split (Refactor 2): quarantine tracks `output_dir`,
which stays on the internal drive; only the chat_id route folders move to D:.

Live-recording safety: TikTok's actively-recorded user must never be moved out
from under a running capture (memory: live-recording-sweep-protection). We gate
the tiktok platform on core.recorder_lock and skip (not fail) if the target is
being recorded; the roster entry still stands and the folder gets swept the next
cycle once the capture ends.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from . import recorder_lock

log = logging.getLogger(__name__)

DELETED_DIRNAME = ".deleted"

# Distinct sentinel so callers can tell "nothing to move" (None) apart from
# "deliberately not moved because a live recording holds it" (this object).
LOCKED_SKIPPED = object()


def _deleted_root(output_dir: str | Path, platform: str) -> Path:
    return Path(output_dir) / platform / DELETED_DIRNAME


def _recording_holds(platform: str, username: str) -> bool:
    """True only for the tiktok user currently held by a live recorder. An
    unknown-username lock (predates the username stamp) holds EVERY tiktok user,
    because "unknown" must never degrade into "move the live recording"."""
    if platform != "tiktok":
        return False
    lock_held, recording_user = recorder_lock.live_recording_user()
    return lock_held and recording_user in (None, username)


def quarantine_user(output_dir: str | Path, platform: str,
                    username: str) -> "Path | object | None":
    """Move {output_dir}/{platform}/{username}/ into .deleted/.

    Returns:
      - the destination Path on a successful move,
      - None if the source folder does not exist (nothing to do),
      - LOCKED_SKIPPED if a live recorder holds the user (tiktok only) — the
        move is deliberately deferred, not an error.

    Idempotent-ish: a second call with no source is a no-op (None). If the dest
    already exists (a re-ban after a prior restore left a stale bucket), the
    destination is suffixed with a UTC stamp so nothing is clobbered.
    """
    src = Path(output_dir) / platform / username
    if not src.exists():
        return None

    if _recording_holds(platform, username):
        log.info("quarantine: skipping %s/%s — a live recorder holds it; "
                 "roster entry stands, folder swept next cycle",
                 platform, username)
        return LOCKED_SKIPPED

    deleted_root = _deleted_root(output_dir, platform)
    deleted_root.mkdir(parents=True, exist_ok=True)

    dest = deleted_root / username
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = deleted_root / f"{username}__{stamp}"

    src.rename(dest)  # same-drive, atomic
    log.warning("quarantine: moved %s/%s → %s", platform, username, dest)
    return dest


def restore_user(output_dir: str | Path, platform: str,
                 username: str) -> "Path | None":
    """Inverse of quarantine_user: move .deleted/{username}/ back to the active
    tree. Used by `unban`.

    Returns the restored Path, or None if the user was not quarantined. Refuses
    to overwrite a live folder — if the active destination already exists, logs
    and returns None rather than clobbering.
    """
    src = _deleted_root(output_dir, platform) / username
    if not src.exists():
        return None

    dest = Path(output_dir) / platform / username
    if dest.exists():
        log.warning("restore: refusing to overwrite live folder %s/%s — "
                    "quarantined copy left in place at %s",
                    platform, username, src)
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dest)  # same-drive, atomic
    log.info("restore: moved %s → %s/%s", src, platform, username)
    return dest
