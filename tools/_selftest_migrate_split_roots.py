"""
Focused validation for tools/migrate_split_roots.py (Phase 6): dry-run touches
nothing, --apply moves only chat_id route folders, rewrites exactly their rows,
refuses to clobber, and is resumable.

Run: python tools/_selftest_migrate_split_roots.py

Standalone (no pytest). Synthetic .archive tree + temp suite.db (ARCHIVER_DB
override); the script is executed as a subprocess exactly as the rollout will.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo / "core"))

from core import ItemStore  # noqa: E402

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def _run(*extra, env):
    return subprocess.run(
        [sys.executable, str(_repo / "tools" / "migrate_split_roots.py"),
         *extra],
        capture_output=True, text=True, env=env)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    src = tmp / "archive"
    dest = tmp / "routes"
    dbp = tmp / "suite.db"

    # Synthetic unified root: platforms + .records stay, routes move.
    for p in ("x/alice", "tiktok/bob", ".records/run1", "unsorted"):
        (src / p).mkdir(parents=True)
    routes = ["-1001111111111", "-1002222222222.t42", "@mychannel"]
    for r in routes:
        d = src / r
        d.mkdir()
        (d / "drop.mp4").write_bytes(b"x" * 64)

    db = ItemStore.open(str(dbp))
    db.add_item(source="orphaned", platform="orphaned",
                username="-1001111111111", identifier="o1",
                file_path=str(src / "-1001111111111" / "drop.mp4"))
    db.add_item(source="orphaned", platform="orphaned",
                username="-1002222222222", identifier="o2",
                file_path=str(src / "-1002222222222.t42" / "drop.mp4"))
    db.add_item(source="archiver", platform="x", username="alice",
                identifier="a1", file_path=str(src / "x" / "alice" / "a.mp4"))
    db.close()

    env = dict(os.environ, ARCHIVER_DB=str(dbp), PYTHONUTF8="1")

    # ── dry run: correct candidate set, nothing changes ─────────────────────
    r = _run("--src", str(src), "--dest", str(dest), env=env)
    check(r.returncode == 0, "dry run exits 0")
    check(all(name in r.stdout for name in routes),
          "dry run lists every chat_id route folder")
    check("x" != "" and " x " not in r.stdout.replace("x/alice", "")
          and ".records" not in r.stdout.split("chat_id")[1].split("total")[0],
          "platforms/.records are not candidates")
    check(all((src / rname).is_dir() for rname in routes) and not dest.exists(),
          "dry run moved nothing")

    # ── apply: routes move, their rows rewritten, platform tree untouched ───
    # Pre-create one destination to prove the no-clobber skip.
    (dest / "@mychannel").mkdir(parents=True)
    r = _run("--src", str(src), "--dest", str(dest), "--apply", env=env)
    check(r.returncode == 0, "--apply exits 0")
    check((dest / "-1001111111111" / "drop.mp4").exists()
          and (dest / "-1002222222222.t42" / "drop.mp4").exists(),
          "route folders moved to the destination root")
    check(not (src / "-1001111111111").exists(), "source route folders gone")
    check((src / "@mychannel").is_dir() and "SKIP @mychannel" in r.stdout,
          "existing destination refused, source left in place")
    check((src / "x" / "alice").is_dir() and (src / ".records" / "run1").is_dir(),
          "platform tree and .records untouched")

    db = ItemStore.open(str(dbp))
    paths = {r_.identifier: r_.file_path for r_ in db.list_items(limit=10)}
    check(paths["o1"] == str(dest / "-1001111111111" / "drop.mp4").replace("\\", "/"),
          "moved folder's row rewritten to the new root")
    check(paths["o2"] == str(dest / "-1002222222222.t42" / "drop.mp4").replace("\\", "/"),
          "topic-suffixed folder's row rewritten too")
    check(paths["a1"] == str(src / "x" / "alice" / "a.mp4"),
          "platform row untouched")
    db.close()

    check(any(p.name.startswith("suite.db.pre-split-") for p in tmp.iterdir()),
          "timestamped DB backup created")

    # ── resumable: re-run only re-reports the skipped clash ─────────────────
    r = _run("--src", str(src), "--dest", str(dest), "--apply", env=env)
    check(r.returncode == 0 and "moved=0" in r.stdout,
          "re-run is a no-op for already-moved folders")

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
