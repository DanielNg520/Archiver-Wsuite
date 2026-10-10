"""
tools.recover_suite_db
──────────────────────
One-shot recovery for a corrupted suite.db.

What it does (dry-run by default; nothing is touched without --apply):

  1. integrity_check the live DB — refuses to run on a healthy one (--force).
  2. `.recover` (sqlite3 CLI) the live DB into a fresh file. Recovery keeps
     every row that survives; only rows on the corrupt pages are lost.
  3. Verify: integrity_check == ok, and the recovered row count must be ≥
     the live DB's index-served count. Abort (leaving the live DB alone) if not.
  4. --apply only: stop the workers (service manager + any manual run),
     swap the recovered file in (the corrupt original is KEPT as
     suite.db.corrupt-<timestamp>), and `ops install` + `ops load` everything
     so the suite comes back fully service-managed.

Run:  python tools/recover_suite_db.py            # inspect, no changes
      python tools/recover_suite_db.py --apply    # do it

Requires the sqlite3 CLI (Fedora: `sudo dnf install sqlite`) for `.recover` —
Python's sqlite3 module does not expose the recovery extension.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for pkg in ("core", "ops"):
    p = str(REPO / pkg)
    if p not in sys.path:
        sys.path.insert(0, p)

from core import db_path                                     # noqa: E402
from core.platform import process as _process                # noqa: E402
from core.platform import procgroup as _procgroup            # noqa: E402
from ops.update import wait_processes_down                   # noqa: E402

# Columns copied when re-homing lost_and_found rows — the 21 payload columns
# in schema order, i.e. every items column except the id, which callers
# supply separately.
_MERGE_COLS = (
    "source, platform, username, identifier, file_path, upload_date, "
    "file_size_bytes, title, discovered_at, status, priority, caption, "
    "attempts, claimed_at, sent_at, last_error, tg_message_id, content_hash, "
    "chat_id, group_key, topic_id"
)


def find_sqlite3() -> str | None:
    return shutil.which("sqlite3")


def integrity(path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        return [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    finally:
        conn.close()


def count_items(path: Path) -> int:
    """Row count of items, or -1 when the corruption defeats every counting
    strategy (2026-07-09 incident: the btree damage broke COUNT(*) AND the
    status-index GROUP BY, so the old two-step fallback crashed the tool
    before it could even start recovering). -1 means 'unknown' — the caller
    must then skip the recovered≥live sanity gate instead of aborting."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        # COUNT(*) via the pk btree can die on the corrupt pages; secondary
        # indexes may survive. Try each cheap strategy in turn.
        for sql in (
            "SELECT COUNT(*) FROM items",
            "SELECT COUNT(username) FROM items INDEXED BY idx_items_user_disc",
            "SELECT MAX(id) FROM items",     # upper bound; better than nothing
        ):
            try:
                n = conn.execute(sql).fetchone()[0]
                if n is not None:
                    return int(n)
            except sqlite3.Error:
                continue
        return -1
    finally:
        conn.close()


def salvage_lost_and_found(recovered: Path) -> int:
    """Re-home items rows that .recover parked in lost_and_found tables.

    When the corruption hits the items tree's ROOT page (2026-07-09 incident:
    'Tree 8 page 8 cell 0: invalid page number'), .recover can no longer tell
    which table the orphaned leaf pages belong to and dumps every record into
    lost_and_found* as (rootpgno, pgno, nfield, id, c0, c1, …). For an items
    row: `id` holds the rowid, c0 is the NULL INTEGER-PRIMARY-KEY alias slot,
    and c1..c21 are the 21 payload columns in schema order. Rows written
    before the last ALTER TABLE ADD COLUMN carry nfield=21, current ones 22 —
    both map identically (missing trailing fields read as NULL).

    Row filter is deliberately strict — nfield >= 21, the id-alias slot NULL,
    and c10 (status) a legal lifecycle value — so orphaned INDEX records
    (2–4 fields) and any foreign table's rows can never be misfiled into
    items. The lost_and_found tables are dropped afterwards so the swapped-in
    DB carries no recovery residue."""
    ncols = len(_MERGE_COLS.split(","))          # 21 payload columns
    sel = ", ".join(f"c{i}" for i in range(1, ncols + 1))
    conn = sqlite3.connect(recovered)
    total = 0
    try:
        tabs = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'lost_and_found%'")]
        for t in tabs:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info([{t}])")}
            if not {"nfield", "id", f"c{ncols}"} <= cols:
                continue
            cur = conn.execute(
                f"INSERT OR IGNORE INTO items (id, {_MERGE_COLS}) "
                f"SELECT id, {sel} FROM [{t}] "
                f"WHERE nfield >= {ncols} AND c0 IS NULL "
                f"AND c10 IN ('pending','sending','sent','failed')")
            total += cur.rowcount
        for t in tabs:
            conn.execute(f"DROP TABLE [{t}]")
        conn.commit()
    finally:
        conn.close()
    return total


def stop_workers() -> None:
    """Stop every writer: managed services first via `ops unload`, then any
    manual worker via SIGTERM (each worker's own handler stops gracefully).
    Workers are crash-safe by design (kernel-released locks, claim watchdog),
    so a hard stop loses no data."""
    ops = shutil.which("ops") or str(Path.home() / ".local" / "bin" / "ops")
    for name in ("archiver", "recorder", "dispatcher"):
        subprocess.run([ops, "unload", name], capture_output=True, text=True)
    pids = []
    for name, action in (("dispatcher", "start"), ("recorder", "start"),
                         ("archiver", "loop")):
        pid = _process.find_worker_pid(name, action)
        if pid is not None:
            print(f"  stopping manual {name} (pid {pid})")
            _procgroup.terminate_pid(pid)
            pids.append(pid)
    if pids:
        wait_processes_down(pids)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--apply", action="store_true",
                    help="stop workers, swap the recovered DB in, restart")
    ap.add_argument("--force", action="store_true",
                    help="recover even if integrity_check says ok")
    args = ap.parse_args()

    db = db_path()
    sqlite = find_sqlite3()
    if sqlite is None:
        print("ERROR: sqlite3 CLI not found — install it (Fedora: `sudo dnf install sqlite`)")
        return 1
    if not db.exists():
        print(f"ERROR: {db} not found")
        return 1

    print(f"DB        : {db}")
    findings = integrity(db)
    healthy = findings == ["ok"]
    print(f"integrity : {'ok' if healthy else f'{len(findings)} finding(s), e.g. {findings[0][:80]}'}")
    if healthy and not args.force:
        print("DB is healthy — nothing to recover (use --force to run anyway).")
        return 0
    live_count = count_items(db)
    print(f"live rows : {'UNKNOWN (all counting strategies hit corrupt pages)' if live_count < 0 else f'{live_count:,}'}")

    # ── recover into a scratch file ──
    workdir = Path(tempfile.mkdtemp(prefix="suite-recover-"))
    recovered = workdir / "suite.recovered.db"
    print(f"\nrecovering → {recovered}")
    # Hard-fail on a non-zero dump rc so a silent empty recovery can never pass.
    db_uri = "file:" + str(db) + "?mode=ro"
    dump = subprocess.Popen([sqlite, db_uri, ".recover"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    load = subprocess.run([sqlite, str(recovered)], stdin=dump.stdout,
                          capture_output=True, text=True)
    dump.stdout.close()
    dump_err = dump.stderr.read().decode(errors="replace")
    dump.wait()
    if dump.returncode != 0:
        print(f"ERROR: .recover dump failed (rc={dump.returncode}): "
              f"{dump_err[:300]}")
        return 1
    if load.returncode != 0:
        print(f"ERROR: recovery load failed: {load.stderr[:300]}")
        return 1
    salvaged = salvage_lost_and_found(recovered)
    if salvaged:
        print(f"salvaged  : {salvaged:,} items rows re-homed from lost_and_found")
    if count_items(recovered) <= 0:
        print("ERROR: recovery produced ZERO rows from a non-empty live DB — "
              "refusing to continue.")
        return 1

    rec_findings = integrity(recovered)
    if rec_findings != ["ok"]:
        print(f"ERROR: recovered DB fails integrity: {rec_findings[:3]}")
        return 1
    rec_count = count_items(recovered)
    print(f"recovered : {rec_count:,} rows, integrity ok")

    final_count = count_items(recovered)
    print(f"final     : {final_count:,} rows (live {live_count:,} → recovered {rec_count:,})")
    if live_count >= 0 and final_count < live_count:
        print("ERROR: recovered has FEWER rows than the live DB reports "
              "— not swapping. Inspect manually.")
        return 1
    if live_count < 0:
        print("NOTE: live row count unknowable (corruption) — the recovered≥live "
              "gate cannot run; relying on integrity_check only.")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to:")
        print("  stop workers → swap recovered DB in → ops install + load all")
        return 0

    # ── the real thing ──
    print("\nstopping workers …")
    stop_workers()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    corrupt_keep = db.with_name(f"{db.name}.corrupt-{stamp}")
    print(f"keeping corrupt original as {corrupt_keep.name}")
    os.replace(db, corrupt_keep)
    for side in (db.parent / (db.name + "-wal"), db.parent / (db.name + "-shm")):
        if side.exists():
            os.replace(side, corrupt_keep.parent / (corrupt_keep.name + side.suffix))
    shutil.copyfile(recovered, db)

    post = integrity(db)
    print(f"swapped in; integrity: {'ok' if post == ['ok'] else post[:2]}")

    ops = shutil.which("ops") or str(Path.home() / ".local" / "bin" / "ops")
    print("\nreinstalling + starting services …")
    subprocess.run([ops, "install"])
    subprocess.run([ops, "load"])
    print("\ndone — check with `ops health`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
