"""
Focused validation for the upload-drain ETA (ItemStore.drain_eta + the
`stats` display line).

Run: python core/core/_selftest_drain_eta.py

Standalone (no pytest). Real ItemStore on a temp DB; sent_at ages are set
directly so the trailing-window math is exercised without waiting.
"""
import io
import sys
import tempfile
import types
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from core import ItemStore                       # noqa: E402
from core.cli import handle_stats, human_eta     # noqa: E402

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    GB = 1_000_000_000

    def _add(ident, size, *, sent_ago_min=None, platform="x", user="alice"):
        db.add_item(source="archiver", platform=platform, username=user,
                    identifier=ident, file_path=str(tmp / f"{ident}.mp4"),
                    file_size_bytes=size)
        if sent_ago_min is not None:
            # Stamp THIS row sent directly (claim_next would grab the oldest
            # pending row, not necessarily this one).
            db.conn.execute(
                "UPDATE items SET status='sent', sent_at=? WHERE identifier=?",
                (_iso(datetime.now(timezone.utc)
                      - timedelta(minutes=sent_ago_min)), ident))
            db.conn.commit()

    # ── empty queue → done ───────────────────────────────────────────────────
    eta = db.drain_eta()
    check(eta["remaining_files"] == 0 and eta["eta_seconds"] is None,
          "empty queue: nothing remaining")

    # ── pending but nothing sent recently → n/a, never a guess ──────────────
    _add("p1", 2 * GB)
    _add("old", 1 * GB, sent_ago_min=600)        # sent, but outside the window
    eta = db.drain_eta(window_minutes=60)
    check(eta["remaining_files"] == 1 and eta["eta_seconds"] is None,
          "no sends inside the window: eta is None (n/a)")

    # ── observed throughput → sane bytes-based estimate ─────────────────────
    # 3 GB delivered starting 30 min ago → ~1.67 MB/s; 2 GB left → ~20 min.
    _add("s1", 1 * GB, sent_ago_min=30)
    _add("s2", 1 * GB, sent_ago_min=20)
    _add("s3", 1 * GB, sent_ago_min=10)
    eta = db.drain_eta(window_minutes=60)
    check(eta["sent_files"] == 3 and eta["sent_bytes"] == 3 * GB,
          "window tallies the 3 recent sends only")
    check(eta["rate_bps"] and abs(eta["rate_bps"] - 3 * GB / 1800) / (3 * GB / 1800) < 0.05,
          "rate ≈ bytes/window-span")
    expect = 2 * GB / eta["rate_bps"]
    check(abs(eta["eta_seconds"] - expect) < 1, "eta = remaining/rate")

    # ── scoped remaining, global rate ────────────────────────────────────────
    _add("tt1", 4 * GB, platform="tiktok", user="bob")
    scoped = db.drain_eta(window_minutes=60, platform="tiktok")
    check(scoped["remaining_bytes"] == 4 * GB
          and scoped["rate_bps"]
          and abs(scoped["rate_bps"] - eta["rate_bps"]) / eta["rate_bps"] < 0.01,
          "platform scope filters remaining, keeps the global rate")

    # ── NULL-size rows degrade to a files/sec estimate ───────────────────────
    db2 = ItemStore.open(str(tmp / "suite2.db"))
    db2.add_item(source="archiver", platform="x", username="u",
                 identifier="n1", file_path=str(tmp / "n1.mp4"))
    db2.conn.execute("UPDATE items SET status='sent', sent_at=? "
                     "WHERE identifier='n1'",
                     (_iso(datetime.now(timezone.utc) - timedelta(minutes=10)),))
    db2.conn.commit()
    db2.add_item(source="archiver", platform="x", username="u",
                 identifier="n2", file_path=str(tmp / "n2.mp4"))
    eta2 = db2.drain_eta(window_minutes=60)
    check(eta2["eta_seconds"] is not None and eta2["rate_bps"] is None,
          "NULL-bytes rows: files/sec fallback, no phantom byte rate")
    db2.close()

    # ── human formatting ─────────────────────────────────────────────────────
    check(human_eta(None) == "n/a" and human_eta(45) == "~45s"
          and human_eta(150) == "~2m" and human_eta(7920) == "~2h 12m"
          and human_eta(2 * 86400 + 4 * 3600) == "~2d 4h",
          "human_eta buckets")

    # ── stats line renders the eta ───────────────────────────────────────────
    args = types.SimpleNamespace(platform=None, username=None, json=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        handle_stats(db, args)
    out = buf.getvalue()
    check("upload eta: ~" in out and "GB remaining" in out,
          "stats prints the eta line")
    args.json = True
    buf = io.StringIO()
    with redirect_stdout(buf):
        handle_stats(db, args)
    check('"eta"' in buf.getvalue(), "stats --json carries the eta object")

    db.close()
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
