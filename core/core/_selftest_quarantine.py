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

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
