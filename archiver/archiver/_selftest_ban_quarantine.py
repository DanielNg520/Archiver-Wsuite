"""
Focused validation for the Phase 2 archiver ban wire-in: _ban_account moves the
user folder into .deleted/ alongside the roster write, and `banned unban`
restores it.

Run: python archiver/archiver/_selftest_ban_quarantine.py

Standalone (no pytest). Real PolicyStore on a temp config.toml, real folder
tree on a temp output_dir; the Orchestrator method is exercised unbound on a
duck-typed shim so no worker machinery is constructed.
"""
import sys
import tempfile
import types
from pathlib import Path

_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo / "core"))
sys.path.insert(0, str(_repo / "archiver"))

from core import ItemStore, PolicyStore             # noqa: E402
import core.quarantine as q                          # noqa: E402
from archiver.orchestrator import Archiver           # noqa: E402
from archiver.cli import cmd_banned                  # noqa: E402

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "archive"
    (out / "x" / "alice").mkdir(parents=True)
    (out / "x" / "alice" / "post.mp4").write_bytes(b"x" * 256)

    store = PolicyStore(tmp / "config.toml")
    q.recorder_lock.live_recording_user = lambda: (False, None)

    config = types.SimpleNamespace(policy_store=store, output_dir=str(out))
    db = ItemStore.open(str(tmp / "suite.db"))
    shim = types.SimpleNamespace(config=config, db=db, _banned_this_run=[])

    # A queued (pending) upload for the user about to be banned — its row must
    # follow the folder into .deleted/ so the dispatcher still delivers it.
    db.add_item(source="archiver", platform="x", username="alice",
                identifier="queued1",
                file_path=str(out / "x" / "alice" / "post.mp4"))

    # ── _ban_account: roster entry + folder quarantined + report staged ─────
    Archiver._ban_account(shim, "x", "alice", "account suspended")
    check("alice" in store.list_banned("x"), "ban lands on the roster")
    row = [r for r in db.list_items(limit=10) if r.identifier == "queued1"][0]
    check(row.file_path == str(out / "x" / ".deleted" / "alice" / "post.mp4")
          and Path(row.file_path).exists(),
          "queued row repointed into .deleted (upload still deliverable)")
    check(not (out / "x" / "alice").exists(), "active folder gone")
    check((out / "x" / ".deleted" / "alice" / "post.mp4").exists(),
          "folder quarantined into .deleted/ with files intact")
    check(len(shim._banned_this_run) == 1
          and shim._banned_this_run[0]["quarantined"].endswith("alice"),
          "report line carries the quarantine path")

    # re-detection of an already-banned account: idempotent, folder already gone
    Archiver._ban_account(shim, "x", "alice", "account suspended")
    check(shim._banned_this_run[1]["quarantined"] == "no folder",
          "re-ban with no folder reports 'no folder'")

    # live-recording lock → quarantine deferred, roster still written
    (out / "tiktok" / "streamer").mkdir(parents=True)
    q.recorder_lock.live_recording_user = lambda: (True, "streamer")
    Archiver._ban_account(shim, "tiktok", "streamer", "user has been banned")
    check("streamer" in store.list_banned("tiktok"),
          "locked user still lands on the roster")
    check((out / "tiktok" / "streamer").exists()
          and shim._banned_this_run[2]["quarantined"] == "deferred (live recording)",
          "locked user folder NOT moved; report says deferred")
    q.recorder_lock.live_recording_user = lambda: (False, None)

    # ── cmd_banned unban: roster entry removed + folder restored ────────────
    args = types.SimpleNamespace(banned_cmd="unban", platform="x",
                                 user="@alice", re_add=False)
    rc = cmd_banned(args, config, db=db)
    check(rc == 0, "unban exits 0")
    check("alice" not in store.list_banned("x"), "unban clears the roster entry")
    check((out / "x" / "alice" / "post.mp4").exists()
          and not (out / "x" / ".deleted" / "alice").exists(),
          "unban restores the quarantined folder")
    row = [r for r in db.list_items(limit=10) if r.identifier == "queued1"][0]
    check(row.file_path == str(out / "x" / "alice" / "post.mp4"),
          "unban repoints the queued row back")

    # unban of a user who was never banned → error exit, nothing moved
    args2 = types.SimpleNamespace(banned_cmd="unban", platform="x",
                                  user="ghost", re_add=False)
    check(cmd_banned(args2, config, db=db) == 1,
          "unban of unknown user exits 1")
    db.close()

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
