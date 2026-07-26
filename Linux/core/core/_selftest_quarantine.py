"""
Focused validation for the reversible ban quarantine (core.quarantine),
Phase 1 of the bans/paths refactor.

Run: python core/core/_selftest_quarantine.py

Standalone (no pytest). Builds a real output_dir tree on a temp path, exercises
move / restore / re-ban collision / missing-source, and (with recorder_lock
stubbed) the live-recording skip.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

import core.quarantine as q                                  # noqa: E402
from core import (                                           # noqa: E402
    quarantine_user, restore_user, LOCKED_SKIPPED,
)

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def _make_user(root: Path, platform: str, user: str, *files: str) -> Path:
    d = root / platform / user
    d.mkdir(parents=True, exist_ok=True)
    for f in files or ("clip.mp4",):
        (d / f).write_bytes(b"x" * 512)
    return d


def _no_lock():
    return (False, None)


def main() -> int:
    root = Path(tempfile.mkdtemp())
    # Default: no live recording, so the tiktok gate is a pass-through.
    q.recorder_lock.live_recording_user = _no_lock

    # ── move: creates .deleted/<user>/, source gone, files intact ───────────
    _make_user(root, "x", "alice", "a.mp4", "b.mp4")
    dest = quarantine_user(root, "x", "alice")
    check(dest == root / "x" / ".deleted" / "alice", "quarantine dest is .deleted/<user>")
    check(dest.is_dir() and not (root / "x" / "alice").exists(),
          "source folder moved away")
    check((dest / "a.mp4").exists() and (dest / "b.mp4").exists(),
          "files intact after move")

    # ── no source folder → None, no error ───────────────────────────────────
    check(quarantine_user(root, "x", "ghost") is None,
          "missing source returns None")

    # ── restore round-trips ─────────────────────────────────────────────────
    back = restore_user(root, "x", "alice")
    check(back == root / "x" / "alice" and back.is_dir(),
          "restore moves folder back to active tree")
    check((back / "a.mp4").exists() and not (root / "x" / ".deleted" / "alice").exists(),
          "restore empties the quarantine bucket, files intact")
    check(restore_user(root, "x", "alice") is None,
          "restore of a non-quarantined user returns None")

    # ── second ban after restore → timestamp-suffixed dest, no clobber ──────
    # Simulate a stale bucket already occupying .deleted/alice (e.g. a prior
    # restore left one), then re-ban: the new dest must be suffixed, not merge.
    stale = root / "x" / ".deleted" / "alice"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "OLD.mp4").write_bytes(b"old")
    _make_user(root, "x", "alice", "NEW.mp4")
    dest2 = quarantine_user(root, "x", "alice")
    check(dest2 != stale and dest2.name.startswith("alice__"),
          "re-ban collision gets a UTC-stamped dest")
    check((dest2 / "NEW.mp4").exists() and (stale / "OLD.mp4").exists(),
          "neither the new nor the stale bucket is clobbered")

    # ── restore refuses to overwrite a live folder ──────────────────────────
    _make_user(root, "ig", "bob", "v.mp4")
    quarantine_user(root, "ig", "bob")
    _make_user(root, "ig", "bob", "fresh.mp4")   # a live folder reappears
    check(restore_user(root, "ig", "bob") is None,
          "restore refuses to overwrite a live folder")
    check((root / "ig" / ".deleted" / "bob" / "v.mp4").exists(),
          "quarantined copy left intact when restore refused")

    # ── live-recording lock held for the user → move skipped ────────────────
    _make_user(root, "tiktok", "streamer", "live.flv")
    q.recorder_lock.live_recording_user = lambda: (True, "streamer")
    r = quarantine_user(root, "tiktok", "streamer")
    check(r is LOCKED_SKIPPED, "locked user → LOCKED_SKIPPED sentinel (not a path)")
    check((root / "tiktok" / "streamer").exists()
          and not (root / "tiktok" / ".deleted" / "streamer").exists(),
          "locked user folder NOT moved")

    # unknown-username lock holds EVERY tiktok user
    q.recorder_lock.live_recording_user = lambda: (True, None)
    check(quarantine_user(root, "tiktok", "streamer") is LOCKED_SKIPPED,
          "unknown-username lock holds all tiktok users")

    # a lock never blocks a DIFFERENT platform
    _make_user(root, "x", "carol", "c.mp4")
    check(isinstance(quarantine_user(root, "x", "carol"), Path),
          "tiktok lock does not block other platforms")

    # once the lock clears, the tiktok user moves normally
    q.recorder_lock.live_recording_user = _no_lock
    check(isinstance(quarantine_user(root, "tiktok", "streamer"), Path),
          "tiktok user quarantines once the lock clears")

    # ── no platform segment (recorder .records layout) ──────────────────────
    rec = root / "records"
    (rec / "streamer2").mkdir(parents=True)
    (rec / "streamer2" / "live.flv").write_bytes(b"x" * 128)
    d = quarantine_user(rec, "", "streamer2", lock_platform="tiktok")
    check(d == rec / ".deleted" / "streamer2" and d.is_dir(),
          'platform="" quarantines directly under output_dir')
    q.recorder_lock.live_recording_user = lambda: (True, "streamer2")
    (rec / "streamer2").mkdir()
    check(quarantine_user(rec, "", "streamer2", lock_platform="tiktok")
          is LOCKED_SKIPPED,
          "lock_platform gates the no-segment layout on the tiktok lock")
    q.recorder_lock.live_recording_user = _no_lock
    check(restore_user(rec, "", "streamer2") is None,
          'restore with platform="" refuses to overwrite the live folder')

    # ── db repoint: queued rows follow the moved folder ─────────────────────
    from core import ItemStore
    db = ItemStore.open(str(root / "suite.db"))
    _make_user(root, "ig", "carla", "v1.mp4")
    old = str(root / "ig" / "carla" / "v1.mp4")
    db.add_item(source="archiver", platform="ig", username="carla",
                identifier="c1", file_path=old)
    # a mixed-slash row must repoint too (the DB holds both styles)
    _make_user(root, "ig", "carla", "v2.mp4")
    db.add_item(source="archiver", platform="ig", username="carla",
                identifier="c2",
                file_path=str(root / "ig" / "carla" / "v2.mp4").replace("\\", "/"))
    dest = quarantine_user(root, "ig", "carla", db=db)
    paths = {r.identifier: r.file_path for r in db.list_items(limit=20)
             if r.username == "carla"}
    check(paths["c1"] == str(dest / "v1.mp4"),
          "backslash row repointed into .deleted")
    check(paths["c2"] == str(dest / "v2.mp4"),
          "forward-slash row repointed too")
    check(Path(paths["c1"]).exists(), "repointed path resolves on disk")
    restore_user(root, "ig", "carla", db=db)
    paths = {r.identifier: r.file_path for r in db.list_items(limit=20)
             if r.username == "carla"}
    check(paths["c1"] == old, "restore repoints rows back")
    db.close()

    # ── Windows open-handle rename refusal → deferred, not a crash ──────────
    import os
    _make_user(root, "x", "held", "h.mp4")
    fh = open(root / "x" / "held" / "h.mp4", "rb")
    try:
        r = quarantine_user(root, "x", "held")
        if os.name == "nt":
            check(r is LOCKED_SKIPPED and (root / "x" / "held").exists(),
                  "open handle inside → move deferred (LOCKED_SKIPPED)")
        else:                                    # POSIX renames succeed anyway
            check(r is not None, "open handle: POSIX move proceeds")
    finally:
        fh.close()

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
