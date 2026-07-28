"""
tools.recover_suite_db
──────────────────────
One-shot recovery for a corrupted suite.db (found 2026-07-08: btree page
corruption in the items tree after the macOS → Windows migration; the clean
`suite.db.premigration-bak` proves the damage happened during/after the move).

What it does (dry-run by default; nothing is touched without --apply):

  1. integrity_check the live DB — refuses to run on a healthy one (--force).
  2. `.recover` (sqlite3 CLI) the live DB into a fresh file. Recovery keeps
     everything post-migration activity wrote; only rows on the corrupt pages
     are lost (~3.9k old 'sent' rows in the observed incident).
  3. MERGE those lost rows back from `suite.db.premigration-bak`: any (platform,
     identifier) present in the backup but absent from the recovery is inserted
     with its path prefix rewritten (/Volumes/StorEDGE → D:) — matching what
     tools/migrate_paths_to_windows.py did to the live rows. This restores the
     content_hash dedup memory ("never re-upload bytes already sent") that the
     corruption ate.
  4. Verify: integrity_check == ok, and recovered+merged row count must be ≥
     the live DB's index-served count. Abort (leaving the live DB alone) if not.
  5. --apply only: stop the workers (Task Scheduler jobs + any manual run),
     swap the recovered file in (the corrupt original is KEPT as
     suite.db.corrupt-<timestamp>), and `ops install` + `ops load` everything
     so the suite comes back fully service-managed.

Run:  python tools/recover_suite_db.py            # inspect, no changes
      python tools/recover_suite_db.py --apply    # do it

Requires the sqlite3 CLI (winget install SQLite.SQLite) for `.recover` —
Python's sqlite3 module does not expose the recovery extension.
"""

from __future__ import annotations

import argparse
import glob
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

OLD_PREFIX = "/Volumes/StorEDGE"
NEW_PREFIX = "D:"

# Columns copied on merge — everything except the autoincrement id (backup ids
# may collide with ids the recovery already assigned).
_MERGE_COLS = (
    "source, platform, username, identifier, file_path, upload_date, "
    "file_size_bytes, title, discovered_at, status, priority, caption, "
    "attempts, claimed_at, sent_at, last_error, tg_message_id, content_hash, "
    "chat_id, group_key, topic_id"
)


def find_sqlite3() -> str | None:
    exe = shutil.which("sqlite3")
    if exe:
        return exe
    hits = glob.glob(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\SQLite.SQLite_*\sqlite3.exe"))
    return hits[0] if hits else None


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
    """Stop every writer: managed tasks first, then any manual worker left in
    the process table. Workers are crash-safe by design (kernel-released locks,
    claim watchdog), so a hard stop loses no data."""
    ops = shutil.which("ops") or str(Path.home() / ".local" / "bin" / "ops")
    for name in ("archiver", "recorder", "dispatcher"):
        subprocess.run([ops, "unload", name], capture_output=True, text=True)
    for name, action in (("dispatcher", "start"), ("recorder", "start"),
                         ("archiver", "loop")):
        pid = _process.find_worker_pid(name, action)
        if pid is not None:
            print(f"  stopping manual {name} (pid {pid})")
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True)
    # Give handles a moment to close — Windows can't swap an open file.
    time.sleep(2.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[3])
    ap.add_argument("--apply", action="store_true",
                    help="stop workers, swap the recovered DB in, restart")
    ap.add_argument("--force", action="store_true",
                    help="recover even if integrity_check says ok")
    args = ap.parse_args()

    db = db_path()
    bak = db.with_name(db.name + ".premigration-bak")
    sqlite = find_sqlite3()
    if sqlite is None:
        print("ERROR: sqlite3 CLI not found — winget install SQLite.SQLite")
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
    # URI filename: backslashes are NOT valid URI path separators — with a
    # raw str(db) the CLI opened a nonexistent path, emitted nothing, and the
    # "recovery" was an empty DB that only the backup merge then populated
    # (silently discarding every post-migration row). Forward-slash the path
    # and hard-fail on a non-zero dump rc so that can never pass again.
    db_uri = "file:" + str(db).replace("\\", "/") + "?mode=ro"
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
              "refusing to continue (the result would be backup-merge only).")
        return 1

    rec_findings = integrity(recovered)
    if rec_findings != ["ok"]:
        print(f"ERROR: recovered DB fails integrity: {rec_findings[:3]}")
        return 1
    rec_count = count_items(recovered)
    print(f"recovered : {rec_count:,} rows, integrity ok")

    # ── merge rows the corruption ate, from the clean pre-migration backup ──
    merged = 0
    if bak.exists() and integrity(bak) == ["ok"]:
        conn = sqlite3.connect(recovered)
        try:
            # Plain-path ATTACH (parameterized): URI filenames are rejected by
            # ATTACH unless the connection itself was opened with uri=True.
            conn.execute("ATTACH DATABASE ? AS bak", (str(bak),))
            cur = conn.execute(
                f"INSERT OR IGNORE INTO main.items ({_MERGE_COLS}) "
                f"SELECT source, platform, username, identifier, "
                f"       REPLACE(file_path, ?, ?), upload_date, "
                f"       file_size_bytes, title, discovered_at, status, "
                f"       priority, caption, attempts, claimed_at, sent_at, "
                f"       last_error, tg_message_id, content_hash, chat_id, "
                f"       group_key, topic_id "
                f"FROM bak.items b WHERE NOT EXISTS "
                f"  (SELECT 1 FROM main.items m WHERE m.platform=b.platform "
                f"   AND m.identifier=b.identifier)",
                (OLD_PREFIX, NEW_PREFIX))
            merged = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        print(f"merged    : {merged:,} rows restored from {bak.name}")
    else:
        print(f"NOTE: no clean backup at {bak} — merge step skipped")

    final_count = count_items(recovered)
    print(f"final     : {final_count:,} rows "
          f"(live {live_count:,} → recovered {rec_count:,} + merged {merged:,})")
    if live_count >= 0 and final_count < live_count:
        print("ERROR: recovered+merged has FEWER rows than the live DB reports "
              "— not swapping. Inspect manually.")
        return 1
    if live_count < 0:
        print("NOTE: live row count unknowable (corruption) — the recovered≥live "
              "gate cannot run; relying on integrity_check + the backup merge.")

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
