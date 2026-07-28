"""
tools/migrate_paths_to_windows.py
─────────────────────────────────
One-time migration helper for the macOS → Windows move.

The suite.db that came over from the Mac stores every item's file_path as a
macOS absolute path under the external StorEDGE volume, e.g.

    /Volumes/StorEDGE/archiver_downloads/x/<user>/<file>

On Windows that media now lives on D: (StorEDGE), so the paths must be rewritten
or the dispatcher can't find the files to upload and `ops` can't locate the
archive volume. The on-disk layout is identical below the volume root, so the
fix is a pure prefix swap:

    /Volumes/StorEDGE   ->   D:

Forward slashes in the result (``D:/archiver_downloads/...``) are valid on
Windows — pathlib, ffmpeg, and Telethon all accept them — so we don't bother
flipping separators.

Dry-run by default (prints what WOULD change, verifies a sample resolves on
disk). Pass --apply to write; a ``suite.db.premigration-bak`` copy is made
first. Run it with the dispatcher/archiver/recorder STOPPED.

    python tools/migrate_paths_to_windows.py            # preview
    python tools/migrate_paths_to_windows.py --apply    # rewrite

Override the mapping if your drive differs:
    python tools/migrate_paths_to_windows.py --from /Volumes/StorEDGE --to E:

Stdlib only; no PYTHONPATH needed. Resolves suite.db from %APPDATA% exactly as
the suite does, so run it in the same shell/user the workers run under.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path


def db_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "archiver-suite" / "suite.db"


def main() -> int:
    ap = argparse.ArgumentParser(description="Rewrite suite.db file paths for Windows.")
    ap.add_argument("--from", dest="src", default="/Volumes/StorEDGE",
                    help="path prefix to replace (default: /Volumes/StorEDGE)")
    ap.add_argument("--to", dest="dst", default="D:",
                    help="replacement prefix (default: D:)")
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default is a dry run)")
    args = ap.parse_args()

    db = db_path()
    if not db.exists():
        print(f"ERROR: suite.db not found at {db}", file=sys.stderr)
        print("Run this in the shell/user the suite runs under.", file=sys.stderr)
        return 1

    src = args.src.rstrip("/")
    like = src + "/%"

    conn = sqlite3.connect(db)
    total = conn.execute(
        "SELECT COUNT(*) FROM items WHERE file_path LIKE ?", (like,)
    ).fetchone()[0]

    print(f"DB      : {db}")
    print(f"mapping : {src}  ->  {args.dst}")
    print(f"rows    : {total} item(s) with a '{src}' path")

    if total == 0:
        print("nothing to do (no matching paths).")
        conn.close()
        return 0

    print("sample (after rewrite, checking the file exists on disk):")
    for (p,) in conn.execute(
        "SELECT file_path FROM items WHERE file_path LIKE ? LIMIT 5", (like,)
    ):
        win = p.replace(src, args.dst, 1)
        print(("  EXISTS   " if Path(win).exists() else "  MISSING  ") + win)

    if not args.apply:
        print("\nDRY RUN -- re-run with --apply to write the change.")
        conn.close()
        return 0

    conn.close()
    bak = db.with_name(db.name + ".premigration-bak")
    shutil.copyfile(db, bak)
    print(f"\nbacked up to {bak}")

    conn = sqlite3.connect(db)
    n = conn.execute(
        "UPDATE items SET file_path = REPLACE(file_path, ?, ?) WHERE file_path LIKE ?",
        (src, args.dst, like),
    ).rowcount
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    print(f"rewrote {n} path(s). Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
