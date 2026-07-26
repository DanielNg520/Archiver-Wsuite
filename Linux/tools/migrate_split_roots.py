"""
tools.migrate_split_roots
─────────────────────────
Physical half of the two-root storage split (Refactor 2, Phase 6): move the
top-level chat_id route folders OUT of the unified `.archive` root onto the
dedicated routes volume, and rewrite their suite.db file_path rows to match.

Before (interim single root, 2026-07-11):
    C:/Users/danie/.archive/<platforms>/...     stays
    C:/Users/danie/.archive/.records/...        stays
    C:/Users/danie/.archive/<chat_id>[.tN]/...  MOVES → <routes_dir>/<chat_id>/

After the move, set ROUTES_DIR=<routes_dir> in the archiver .env (the code
side landed in Phase 5 — ROUTES_DIR unset keeps scanning output_dir, so run
order is: stop workers → --apply → set ROUTES_DIR → restart workers).

Safety (same pattern as migrate_paths_to_archive.py):
- ⚠️ STOP THE WORKERS FIRST (`ops unload`). The 2026-07 corruption came from
  touching suite.db under live writers; this script also physically moves
  folders the archiver scans every cycle.
- Dry-run by default; --apply writes, after a timestamped DB backup (the clean
  `suite.db.premigration-bak` is preserved, never overwritten).
- Refuses to clobber: an already-existing destination folder skips that route
  (nothing merged, nothing overwritten) — resolve by hand and re-run.
- Only folders whose name parses as a chat_id route (core.parse_route, incl.
  the `.t<topic>` suffix) are candidates; platforms, `.records`, `unsorted`,
  dot-dirs and anything else are never touched.
- Per-folder ordering: move the folder, then rewrite its rows — an interrupted
  run leaves every processed folder consistent and every unprocessed folder
  untouched; re-running resumes cleanly (already-moved folders just have no
  source dir anymore).
- The move IS cross-drive (that's the point): shutil.move copy+deletes. With
  the workers stopped there is no concurrent writer to torn-copy against.

Run:  python tools/migrate_split_roots.py --dest D:/routes           # dry run
      python tools/migrate_split_roots.py --dest D:/routes --apply   # do it
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "core"))
from core import db_path, parse_route  # noqa: E402

ARCHIVE = Path("C:/Users/danie/.archive")


def _route_dirs(root: Path) -> list[Path]:
    """Top-level chat_id route folders under `root` — exactly the set the
    orphaned ingest scan would adopt (parse_route match), nothing else."""
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        try:
            if not d.is_dir() or d.name.startswith("."):
                continue
        except OSError:
            continue
        if parse_route(d.name) is not None:
            out.append(d)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Move chat_id route folders out of .archive onto the "
                    "routes volume and rewrite their DB rows.")
    ap.add_argument("--src", default=str(ARCHIVE),
                    help=f"current unified root (default {ARCHIVE})")
    ap.add_argument("--dest", required=True,
                    help="routes root to move chat_id folders into (e.g. D:/routes)")
    ap.add_argument("--apply", action="store_true",
                    help="write the change (default: dry run)")
    args = ap.parse_args()

    src_root = Path(args.src)
    dest_root = Path(args.dest)
    db = db_path()
    if not db.exists():
        print(f"ERROR: suite.db not found at {db}", file=sys.stderr)
        return 1
    if not src_root.is_dir():
        print(f"ERROR: source root {src_root} does not exist", file=sys.stderr)
        return 1

    routes = _route_dirs(src_root)
    conn = sqlite3.connect(db)

    def rows_under(prefix: str) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM items WHERE REPLACE(file_path, char(92), '/') "
            "LIKE ?", (prefix.replace("\\", "/") + "/%",),
        ).fetchone()[0]

    print(f"DB         : {db}")
    print(f"source     : {src_root}")
    print(f"destination: {dest_root}")
    print(f"chat_id route folders found: {len(routes)}")
    total_rows = 0
    conflicts = 0
    for d in routes:
        n = rows_under(str(src_root / d.name))
        total_rows += n
        dest = dest_root / d.name
        clash = "  ⚠ DEST EXISTS — will be skipped" if dest.exists() else ""
        files = sum(1 for _ in d.rglob("*") if _.is_file())
        print(f"  {d.name:>20}  {files:>5} file(s)  {n:>4} DB row(s){clash}")
        if dest.exists():
            conflicts += 1
    print(f"total DB rows to rewrite: {total_rows} "
          f"(chat_id ingests are leave-no-trace, so small is expected)")
    if conflicts:
        print(f"⚠ {conflicts} destination clash(es) — those folders will be "
              f"SKIPPED; resolve by hand and re-run.")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply to write the change.")
        print("⚠ Make sure the workers are STOPPED first: `ops unload`, then "
              "verify with `ops health`.")
        conn.close()
        return 0

    # ── apply ────────────────────────────────────────────────────────────────
    conn.close()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = db.with_name(f"{db.name}.pre-split-{stamp}")
    shutil.copyfile(db, bak)
    print(f"\nbacked up live DB -> {bak}")
    pmb = db.with_name(db.name + ".premigration-bak")
    if pmb.exists():
        keep = pmb.with_name(pmb.name + ".keep")
        if not keep.exists():
            shutil.copyfile(pmb, keep)
            print(f"preserved clean backup -> {keep}")

    dest_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    moved = skipped = rewritten = 0
    for d in routes:
        dest = dest_root / d.name
        if dest.exists():
            print(f"  SKIP {d.name} — destination already exists")
            skipped += 1
            continue
        shutil.move(str(d), str(dest))
        moved += 1
        # Rewrite this folder's rows immediately after its move so an
        # interruption leaves every processed folder consistent.
        old_pfx = str(src_root / d.name).replace("\\", "/")
        new_pfx = str(dest).replace("\\", "/")
        conn.execute(   # normalize backslashes first so the prefix match is uniform
            "UPDATE items SET file_path = REPLACE(file_path, char(92), '/') "
            "WHERE file_path LIKE '%' || char(92) || '%' AND "
            "REPLACE(file_path, char(92), '/') LIKE ?", (old_pfx + "/%",))
        n = conn.execute(
            "UPDATE items SET file_path = ? || SUBSTR(file_path, ?) "
            "WHERE file_path LIKE ?",
            (new_pfx, len(old_pfx) + 1, old_pfx + "/%"),
        ).rowcount
        conn.commit()
        rewritten += n
        print(f"  moved {d.name} -> {dest}  ({n} row(s) rewritten)")

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    leftover = conn.execute(
        "SELECT COUNT(*) FROM items WHERE REPLACE(file_path, char(92), '/') "
        "LIKE ?", (str(src_root).replace("\\", "/") + "/-%",),
    ).fetchone()[0]
    conn.close()
    print(f"\ndone. moved={moved} skipped={skipped} rows rewritten={rewritten}")
    print(f"rows still on chat_id paths under {src_root}: {leftover} "
          f"(should be 0 unless folders were skipped)")
    print(f"\nNEXT: set ROUTES_DIR={dest_root} in the archiver .env, then "
          f"restart the workers (`ops load`).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
