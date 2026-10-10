"""
ops._selftest_health
─────────────────────
Regression coverage for `ops.health`'s pure/lightly-coupled logic:
drain_eta_fields, _humanize_eta, queue_health, _same_volume, _disk_fields.
Found untested in the 2026-09-19 codebase-wide gap audit (AGENTS.md's
"Repo audit backlog") -- this is the tool every restart/deploy go/no-go
call in the suite runs through, and had zero coverage anywhere.

drain_eta_fields/queue_health hit SQLite, so they get a real ItemStore-built
temp DB (same convention as core/core/_selftest_drain_eta.py) with
`health.SUITE_DB` monkeypatched to point at it -- no mocking of sqlite
itself, `@_memo()` has _DATA_TTL=0.0 by default so no caching to work
around.

Run: python3 -m ops._selftest_health
Style matches the other _selftest scripts: plain asserts, checkmark per
assertion, nonzero exit on first failure.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import ItemStore

from . import health as h

_checks = 0


def ok(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"✗ {label}")
    _checks += 1
    print(f"✓ {label}")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    print("ops.health selftest")

    # ── _humanize_eta: bucket boundaries ────────────────────────────────
    ok(h._humanize_eta(None) == "n/a", "humanize_eta: None -> n/a")
    ok(h._humanize_eta(45) == "~45s", "humanize_eta: seconds bucket")
    ok(h._humanize_eta(150) == "~2m", "humanize_eta: minutes bucket")
    ok(h._humanize_eta(7920) == "~2h 12m", "humanize_eta: hours bucket")
    ok(h._humanize_eta(2 * 86400 + 4 * 3600) == "~2d 4h",
       "humanize_eta: days bucket")

    # ── _same_volume ─────────────────────────────────────────────────────
    ok(h._same_volume(None, "/tmp") is False, "same_volume: None a -> False")
    ok(h._same_volume("/tmp", None) is False, "same_volume: None b -> False")
    with tempfile.TemporaryDirectory() as d:
        a = str(Path(d) / "a"); b = str(Path(d) / "b")
        Path(a).write_text("x"); Path(b).write_text("y")
        ok(h._same_volume(a, b) is True,
           "same_volume: two real files under one tmp dir -> same st_dev")
        ok(h._same_volume(a, a) is True, "same_volume: identical path -> True")
    ok(h._same_volume("/no/such/path/a", "/no/such/path/a") is True,
       "same_volume: both vanished, equal strings -> True via a==b fallback")
    ok(h._same_volume("/no/such/path/a", "/no/such/path/b") is False,
       "same_volume: both vanished, different strings -> False")

    # ── _disk_fields ─────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as d:
        fields = h._disk_fields(d)
        ok(fields is not None, "disk_fields: real path returns a result")
        label, frac = fields
        ok(label.endswith("GB free"), "disk_fields: label is '<N>GB free'")
        ok(0.0 <= frac <= 1.0, "disk_fields: fraction-used is in [0,1]")
    ok(h._disk_fields("/no/such/mount/point/at/all") is None,
       "disk_fields: unstat-able path -> None")

    # ── drain_eta_fields / queue_health: real ItemStore-built temp DB ────
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    GB = 1_000_000_000
    real_suite_db = h.SUITE_DB
    h.SUITE_DB = tmp / "suite.db"
    try:
        eta = h.drain_eta_fields(60)
        ok(eta is not None and eta["remaining_files"] == 0
           and eta["eta_seconds"] is None,
           "drain_eta_fields: empty queue -> nothing remaining, eta None")

        db.add_item(source="archiver", platform="x", username="alice",
                    identifier="p1", file_path=str(tmp / "p1.mp4"),
                    file_size_bytes=2 * GB)
        eta = h.drain_eta_fields(60)
        ok(eta["remaining_files"] == 1 and eta["eta_seconds"] is None,
           "drain_eta_fields: pending but nothing sent in window -> n/a, "
           "never a guess")

        # 3 GB sent over the last 30 minutes -> ~1.67 MB/s; 2 GB left -> ~20m
        for ident, mins_ago in (("s1", 30), ("s2", 20), ("s3", 10)):
            db.add_item(source="archiver", platform="x", username="alice",
                        identifier=ident, file_path=str(tmp / f"{ident}.mp4"),
                        file_size_bytes=1 * GB)
            db.conn.execute(
                "UPDATE items SET status='sent', sent_at=? WHERE identifier=?",
                (_iso(datetime.now(timezone.utc) - timedelta(minutes=mins_ago)),
                 ident))
        db.conn.commit()
        eta = h.drain_eta_fields(60)
        ok(eta["remaining_bytes"] == 2 * GB,
           "drain_eta_fields: remaining_bytes counts only pending/sending")
        expect_rate = 3 * GB / 1800.0  # 3 GB / 30 min span
        ok(eta["rate_bps"] is not None
           and abs(eta["rate_bps"] - expect_rate) / expect_rate < 0.05,
           "drain_eta_fields: rate_bps ~= bytes-sent / window-span")
        expect_eta = (2 * GB) / eta["rate_bps"]
        ok(abs(eta["eta_seconds"] - expect_eta) < 1,
           "drain_eta_fields: eta_seconds = remaining_bytes / rate_bps")

        qh = h.queue_health()
        ok(qh is not None and qh["null_hash"] == 1,
           "queue_health: null_hash counts only unsent rows with no content_hash")
        ok(qh["oldest_pending"] is not None,
           "queue_health: oldest_pending set while p1 is still pending")
        ok(qh["last_sent"] is not None,
           "queue_health: last_sent reflects the most recent send")

        db.conn.execute(
            "UPDATE items SET status='sending', claimed_at=? "
            "WHERE identifier='p1'", (_iso(datetime.now(timezone.utc)),))
        db.conn.commit()
        qh = h.queue_health()
        ok(qh["oldest_sending"] is not None,
           "queue_health: oldest_sending set once a row is claimed")
    finally:
        db.close()
        h.SUITE_DB = real_suite_db

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
