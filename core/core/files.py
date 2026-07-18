"""
core.files
──────────
Filesystem helpers shared by the dispatcher (delete-after-upload) and the
archiver (disk-full purge). One definition of "what counts as this media
file's sidecars," so the two delete paths can't drift on which extras they
remove.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ── Media-type buckets (album grouping) ───────────────────────────────────────
#
# Telegram albums must be homogeneous in practice: photos group with photos,
# videos with videos. GIFs and anything unrecognized are sent individually
# (the old archiver.telegram did the same — gifs/other never went in an album).
# These sets are the ONE definition; the dispatcher's batch claim and any
# future caller share them so "what counts as a photo" can't drift.

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# Video containers Telegram plays INLINE inside a media group.
INLINE_VIDEO_EXTS = {".mp4", ".mov", ".webm"}

# Full-quality / legacy containers Telegram will NOT play inline. They are
# still valid videos this suite manages (a scanner must not skip them), but
# they ship as DOWNLOADABLE documents — beside an inline preview when one
# exists (.mkv), or on their own. .wmv/.ogm/.avi/.flv/.mpg/… were previously
# unlisted, so every scanner silently ignored them; now they are ingested.
DOCUMENT_VIDEO_EXTS = {
    ".mkv", ".wmv", ".ogm", ".ogv", ".avi", ".flv", ".mpg", ".mpeg",
    ".m4v", ".ts", ".mts", ".m2ts", ".3gp", ".vob",
}

VIDEO_EXTS = INLINE_VIDEO_EXTS | DOCUMENT_VIDEO_EXTS

# THE definition of "a media file this suite manages". Every scanner —
# archiver reconcile, recorder startup sweep, orphaned ingest, dedup, sorter —
# imports this set. It was previously copy-pasted into four packages; any
# drift meant one worker ignoring a file another worker had enqueued.
MEDIA_EXTENSIONS = PHOTO_EXTS | VIDEO_EXTS | {".gif"}

ALBUM_MAX = 10  # Telegram's hard limit on items per album

# THE definition of the orphaned (chat_id-folder) source tag. It lives here —
# the low-level leaf — because album_bucket below needs it and core.orphaned
# imports core.store which imports THIS module (importing back would be
# circular). core.orphaned re-exports it under its canonical name
# ORPHANED_SOURCE; import either, they are the same object.
ORPHANED_SOURCE_NAME = "orphaned"


def media_bucket(file_path: str) -> str:
    """Classify a file for album grouping: 'photo', 'video', or 'single'.

    'single' (gifs + anything unrecognized) is the catch-all that the drain
    loop sends one-at-a-time rather than batching — matching the old
    uploader, which only ever albumed photos and videos.
    """
    ext = Path(file_path).suffix.lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return "single"


def orphaned_kind(file_path: str) -> str:
    """Album grouping bucket for a chat_id-folder (orphaned) file: 'media' or
    'document'.

    'media' — photos and inline-playable videos (.mp4/.mov/.webm). These share
    ONE mixed Telegram media group (photo+video are groupable together).
    'document' — .mkv full-quality originals, gifs, and anything else. These
    ship as documents and group with EACH OTHER (multiple .mkv → one document
    album), but never in the same group as inline media.

    Unlike media_bucket there is no 'single': every orphaned file is
    album-eligible with its own kind, so two same-subfolder documents group.
    A file that must stay solo (loose in a chat_id/#hashtag root) is already
    forced individual upstream by a unique batch key, not by this bucket.
    """
    ext = Path(file_path).suffix.lower()
    if ext in PHOTO_EXTS or ext in INLINE_VIDEO_EXTS:
        return "media"
    return "document"


def album_bucket(source: str, file_path: str) -> str:
    """The grouping bucket used to assemble an album: source-aware so chat_id
    folders (orphaned) get the mixed-media/document split (orphaned_kind) while
    every other producer keeps the historical photo/video/single split
    (media_bucket) byte-for-byte unchanged."""
    if source == ORPHANED_SOURCE_NAME:
        return orphaned_kind(file_path)
    return media_bucket(file_path)


# Folders whose name starts with this marker are user-curated category buckets
# (hashtag roots). They are KEPT even when empty — files are routed INTO them,
# so an empty one is a waiting bucket, not an incidental leftover.
KEEP_EMPTY_DIR_PREFIX = "#"


def prune_empty_dirs(root: str | Path) -> int:
    """Delete every empty directory under ``root`` (not ``root`` itself),
    EXCEPT folders whose name starts with '#' (hashtag category buckets, kept
    even when empty). Deepest-first so a parent emptied only by removing its
    now-empty children is caught in the same pass. Best-effort: a dir that is
    non-empty (or vanishes/locks mid-pass) is silently left. Returns the count
    removed.

    THE one definition of the empty-folder auto-cleaner — the recorder startup
    sweep and the archiver's periodic ingest sweep both call it, so the two
    can't drift on which folders they spare.
    """
    root = Path(root)
    removed = 0
    for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        if d.name.startswith(KEEP_EMPTY_DIR_PREFIX):
            continue
        try:
            d.rmdir()  # raises OSError if not empty — exactly what we want
            removed += 1
        except OSError:
            pass
    return removed


def cleanup_sidecars(file_path: str) -> None:
    """Delete a media file plus its known metadata sidecars. UNGATED —
    callers are responsible for checking delivery status / policy first.

    Sidecar shapes covered:
      yt-dlp:     <stem>.info.json   and  <stem>.json
      gallery-dl: <full_name>.json   (e.g. clip.mp4.json)
      recorder:   <stem>_ytdlp.log   (live-capture diagnostic log)
    """
    p = Path(file_path)
    try:
        p.unlink(missing_ok=True)
    except OSError as e:
        log.warning("cleanup: unlink %s failed: %s", p.name, e)
        return
    for suffix in (".json", ".info.json"):
        try:
            p.with_suffix(suffix).unlink(missing_ok=True)
        except OSError:
            pass
    try:
        (p.parent / (p.name + ".json")).unlink(missing_ok=True)
    except OSError:
        pass
    # recorder.capture pairs each live recording with a <stem>_ytdlp.log;
    # drop it with the media so capture logs don't accumulate after upload.
    try:
        (p.parent / (p.stem + "_ytdlp.log")).unlink(missing_ok=True)
    except OSError:
        pass
