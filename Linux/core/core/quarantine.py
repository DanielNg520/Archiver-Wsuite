"""
core.quarantine
───────────────
Reversible on-disk quarantine for banned/suspended/deleted users. When a user
is banned, the roster entry (PolicyStore) is only half the job — their media
folder must also leave the active tree so folder-scan discovery stops
re-adopting it. This moves it, it does NOT delete it:

    {output_dir}/{platform}/{username}/  →  {output_dir}/{platform}/.deleted/{username}/

(`platform=""` drops the platform segment — the recorder's recordings tree has
none: `.records/{username}/` → `.records/.deleted/{username}/`.)

The `.deleted/` prefix is required: every folder-scan consumer skips dot-dirs
(LocalPlatform.users, core.orphaned, core.sorter, archiver.reconcile), so a
quarantined user simply disappears from discovery without any per-consumer ban
lookup. `restore_user` is the exact inverse, used by `unban`.

Queued uploads survive the move: pass the suite `db` and every DB row whose
file_path lives under the moved folder is repointed to its new location, so
the dispatcher keeps delivering the user's already-queued files from inside
`.deleted/` (and delete-after-upload cleans them there). Without the repoint
those rows would go "file missing" → failed → GC'd, silently losing uploads.

Same-drive guarantee: quarantine lives under `output_dir`, so the move is always
an atomic `os.rename` on one volume — never a cross-drive copy. This stays true
after the two-root storage split (Refactor 2): quarantine tracks `output_dir`,
which stays on the internal drive; only the chat_id route folders move to D:.

Live-recording safety: TikTok's actively-recorded user must never be moved out
from under a running capture (memory: live-recording-sweep-protection). We gate
on core.recorder_lock and skip (not fail) if the target is being recorded; the
roster entry still stands. A rename refused by the OS (Windows: any open handle
inside the folder — e.g. the dispatcher mid-upload — blocks a dir rename) is
treated the same way: deferred, never a crash.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from . import recorder_lock

log = logging.getLogger(__name__)

DELETED_DIRNAME = ".deleted"

# Distinct sentinel so callers can tell "nothing to move" (None) apart from
# "deliberately not moved" (live recording holds it, or the OS refused the
# rename because a file inside is open) — deferred, not an error.
LOCKED_SKIPPED = object()


def _base(output_dir: str | Path, platform: str) -> Path:
    return Path(output_dir) / platform if platform else Path(output_dir)


def _recording_holds(platform: str, username: str) -> bool:
    """True only for the tiktok user currently held by a live recorder. An
    unknown-username lock (predates the username stamp) holds EVERY tiktok user,
    because "unknown" must never degrade into "move the live recording"."""
    if platform != "tiktok":
        return False
    lock_held, recording_user = recorder_lock.live_recording_user()
    return lock_held and recording_user in (None, username)


def _repoint_rows(db, username: str, src: Path, dest: Path) -> int:
    """Re-root every items row of `username` whose file_path lives under `src`
    onto `dest`, so queued uploads keep delivering after the folder move.
    Slash-direction agnostic (the DB holds both styles); rewritten paths use
    the OS-native form. Best-effort: a failure logs and returns 0 — the move
    already happened, and a later unban/restore repoints back."""
    if db is None:
        return 0
    try:
        src_n = str(src).replace("\\", "/").rstrip("/") + "/"
        rows = db.conn.execute(
            "SELECT id, file_path FROM items WHERE username=?", (username,),
        ).fetchall()
        updates = []
        for r in rows:
            p_n = r["file_path"].replace("\\", "/")
            if p_n.startswith(src_n):
                updates.append((str(dest / p_n[len(src_n):]), r["id"]))
        if updates:
            db.conn.executemany(
                "UPDATE items SET file_path=? WHERE id=?", updates)
            db.conn.commit()
        return len(updates)
    except Exception:                            # pragma: no cover — defensive
        log.exception("quarantine: row repoint %s -> %s failed", src, dest)
        return 0


def quarantine_user(output_dir: str | Path, platform: str, username: str,
                    *, lock_platform: str | None = None,
                    db=None) -> "Path | object | None":
    """Move {output_dir}/{platform}/{username}/ into .deleted/ (platform=""
    drops the segment — the recorder's recordings tree).

    Returns:
      - the destination Path on a successful move,
      - None if the source folder does not exist (nothing to do),
      - LOCKED_SKIPPED when the move is deliberately deferred: a live recorder
        holds the user, or the OS refused the rename (open handle inside).

    `lock_platform` names the platform for the live-recording gate when it
    differs from the path segment (the recorder passes platform="" but must
    still gate on "tiktok"). `db` (an ItemStore) repoints the user's queued
    rows onto the new location so their uploads still deliver.

    Idempotent-ish: a second call with no source is a no-op (None). If the dest
    already exists (a re-ban after a prior restore left a stale bucket), the
    destination is suffixed with a UTC stamp so nothing is clobbered.
    """
    base = _base(output_dir, platform)
    src = base / username
    if not src.exists():
        return None

    if _recording_holds(lock_platform or platform, username):
        log.info("quarantine: skipping %s/%s — a live recorder holds it; "
                 "roster entry stands, folder swept next cycle",
                 platform or output_dir, username)
        return LOCKED_SKIPPED

    deleted_root = base / DELETED_DIRNAME
    deleted_root.mkdir(parents=True, exist_ok=True)

    dest = deleted_root / username
    if dest.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = deleted_root / f"{username}__{stamp}"

    try:
        src.rename(dest)  # same-drive, atomic
    except OSError as e:
        # Windows refuses a dir rename while ANY file inside has an open
        # handle (dispatcher mid-upload is the expected case). Defer — the
        # roster entry stands; a later re-detection retries the move.
        log.warning("quarantine: could not move %s (%s) — deferred", src, e)
        return LOCKED_SKIPPED
    n = _repoint_rows(db, username, src, dest)
    log.warning("quarantine: moved %s → %s%s", src, dest,
                f" ({n} queued row(s) repointed)" if n else "")
    return dest


def restore_user(output_dir: str | Path, platform: str, username: str,
                 *, db=None) -> "Path | None":
    """Inverse of quarantine_user: move .deleted/{username}/ back to the active
    tree (and repoint the user's rows back when `db` is given). Used by `unban`.

    Returns the restored Path, or None if the user was not quarantined. Refuses
    to overwrite a live folder — if the active destination already exists, logs
    and returns None rather than clobbering.
    """
    base = _base(output_dir, platform)
    src = base / DELETED_DIRNAME / username
    if not src.exists():
        return None

    dest = base / username
    if dest.exists():
        log.warning("restore: refusing to overwrite live folder %s — "
                    "quarantined copy left in place at %s", dest, src)
        return None

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dest)  # same-drive, atomic
    except OSError as e:
        log.warning("restore: could not move %s (%s) — left quarantined", src, e)
        return None
    n = _repoint_rows(db, username, src, dest)
    log.info("restore: moved %s → %s%s", src, dest,
             f" ({n} row(s) repointed)" if n else "")
    return dest
