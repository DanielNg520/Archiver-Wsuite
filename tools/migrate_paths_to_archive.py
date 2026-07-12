"""
tools.migrate_paths_to_archive
──────────────────────────────
Rewrite suite.db file_path values onto the unified `.archive` root
(2026-07-11 storage refactor).

Before: media lived under two roots on two possible volume prefixes —
    /Volumes/StorEDGE/archiver_downloads/...   (macOS origin, never rewritten)
    /Volumes/StorEDGE/records/...
    D:/archiver_downloads/...                  (Windows StorEDGE)
    D:/records/...

After: a single internal root, platforms + chat_id folders directly under it,
recorder output dot-prefixed so folder scanners skip it:
    C:/Users/danie/.archive/...            (was .../archiver_downloads/...)
    C:/Users/danie/.archive/.records/...   (was .../records/...)

Notes
- Forward slashes in the result are valid on Windows (same choice the earlier
  migrate_paths_to_windows.py made). Back-slashed rows are normalized first.
- Dry-run by default. --apply writes, after backing up the live DB. The clean
  `suite.db.premigration-bak` (relied on by recover_suite_db.py) is preserved,
  NOT overwritten.
- Run with the dispatcher/archiver/recorder STOPPED.

Run:  python tools/migrate_paths_to_archive.py           # inspect, no changes
      python tools/migrate_paths_to_archive.py --apply   # do it
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "core"))
from core import db_path  # noqa: E402

# Longest-first so the 3-segment prefixes are unambiguous; records maps to the
# dot-prefixed subfolder, archiver_downloads collapses to the root itself.
ARCHIVE = "C:/Users/danie/.archive"
MAPPINGS = [
    ("/Volumes/StorEDGE/records",            ARCHIVE + "/.records"),
    ("/Volumes/StorEDGE/archiver_downloads", ARCHIVE),
    ("D:/records",                           ARCHIVE + "/.records"),
    ("D:/archiver_downloads",                ARCHIVE),
]
NEEDS_FILE = ("pending", "sending", "failed")  # statuses whose file must exist


def main() -> int:
    ap = argparse.ArgumentParser(description="Rewrite suite.db paths onto the .archive root.")
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    args = ap.parse_args()

    db = db_path()
    if not db.exists():
        print(f"ERROR: suite.db not found at {db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db)

    def count(where, *a):
        return conn.execute(f"SELECT COUNT(*) FROM items WHERE {where}", a).fetchone()[0]

    backslash = count("file_path LIKE '%' || char(92) || '%'")
    print(f"DB              : {db}")
    print(f"back-slashed    : {backslash} row(s) will be normalized to '/'")
    print("mappings        :")
    total = 0
    for src, dst in MAPPINGS:
        n = count("REPLACE(file_path, char(92), '/') LIKE ?", src + "/%")
        total += n
        print(f"  {n:>7}  {src}  ->  {dst}")
    unmapped = count(
        "REPLACE(file_path, char(92), '/') NOT LIKE '/Volumes/StorEDGE/%' "
        "AND REPLACE(file_path, char(92), '/') NOT LIKE 'D:/%'"
    )
    print(f"unmapped        : {unmapped} row(s) (should be 0)")
    print(f"to rewrite      : {total} / {count('1=1')} rows")

    # Existence preview for the rows that actually need a live file.
    print("\nfile-existence check for pending/sending/failed rows (post-rewrite target):")
    def rewrite(p: str) -> str:
        q = p.replace("\\", "/")
        for src, dst in MAPPINGS:
            if q.startswith(src + "/"):
                return dst + q[len(src):]
        return q
    q = "SELECT status, file_path FROM items WHERE status IN (%s)" % ",".join("?" * len(NEEDS_FILE))
    miss = present = 0
    samples = []
    for status, p in conn.execute(q, NEEDS_FILE):
        tgt = rewrite(p)
        if Path(tgt).exists():
            present += 1
        else:
            miss += 1
            if len(samples) < 8:
                samples.append(f"  MISSING [{status}] {tgt}")
    print(f"  present={present}  missing={miss}")
    for s in samples:
        print(s)

    if not args.apply:
        print("\nDRY RUN -- re-run with --apply to write the change.")
        conn.close()
        return 0

    conn.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = db.with_name(f"{db.name}.pre-archive-{stamp}")
    shutil.copyfile(db, bak)
    print(f"\nbacked up live DB -> {bak}")
    pmb = db.with_name(db.name + ".premigration-bak")
    if pmb.exists():
        keep = pmb.with_name(pmb.name + ".keep")
        if not keep.exists():
            shutil.copyfile(pmb, keep)
            print(f"preserved clean backup -> {keep}")

    conn = sqlite3.connect(db)
    # 0) resolve collisions: several rows can map to the SAME .archive target
    #    (same file recorded under both a /Volumes mac path and a D: path, or
    #    slash-only duplicates). UNIQUE(file_path) forbids the merge, so keep the
    #    most-advanced row and delete the redundant duplicates.
    _PRI = {"sent": 3, "sending": 2, "pending": 1, "failed": 0}
    groups: dict[str, list[tuple]] = {}
    for rid, status, p in conn.execute("SELECT id, status, file_path FROM items"):
        q = p.replace("\\", "/")
        t = q
        for src, dst in MAPPINGS:
            if q.startswith(src + "/"):
                t = dst + q[len(src):]
                break
        groups.setdefault(t, []).append((rid, status, p))
    drop_ids = []
    for t, rs in groups.items():
        if len(rs) < 2:
            continue
        # keep highest priority; tie-break: prefer no-backslash original
        rs.sort(key=lambda r: (_PRI.get(r[1], -1), "\\" not in r[2]), reverse=True)
        for rid, status, p in rs[1:]:
            drop_ids.append(rid)
            print(f"  drop dup [{status}] id={rid}  {p}")
    if drop_ids:
        conn.executemany("DELETE FROM items WHERE id = ?", [(i,) for i in drop_ids])
        print(f"  deleted {len(drop_ids)} redundant duplicate row(s)")
    # 1) normalize backslashes so LIKE/prefix logic is uniform
    conn.execute("UPDATE items SET file_path = REPLACE(file_path, char(92), '/') "
                 "WHERE file_path LIKE '%' || char(92) || '%'")
    # 2) apply each prefix mapping (longest-first already ordered)
    for src, dst in MAPPINGS:
        n = conn.execute(
            "UPDATE items SET file_path = ? || SUBSTR(file_path, ?) WHERE file_path LIKE ?",
            (dst, len(src) + 1, src + "/%"),
        ).rowcount
        print(f"  rewrote {n:>7}  {src}")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    remaining = conn.execute(
        "SELECT COUNT(*) FROM items WHERE file_path NOT LIKE ?", (ARCHIVE + "/%",)
    ).fetchone()[0]
    conn.close()
    print(f"done. rows NOT under {ARCHIVE}/: {remaining} (should be 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
