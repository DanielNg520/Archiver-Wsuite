"""
tools.x_prune_foreign_gifs
──────────────────────────
One-time (re-runnable) maintenance: remove X/Twitter media that the archiver
should never have kept —

  1. FOREIGN media  — files whose real author is NOT the folder's user. The old
     Pass-2 `/with_replies` walk rendered full conversation threads, so
     gallery-dl yielded the PARENT tweet the user replied to (media posted by
     OTHER accounts). Those parents aren't replies/retweets/quotes, so the
     download-time switches never caught them. Now the extractor carries an
     `image-filter` (author==user AND type!=animated_gif); this tool cleans up
     everything downloaded BEFORE that fix. EXACT: decided from the per-tweet
     sidecar `{date}_{tweet_id}_None.json` `author.name` field.

  2. Reaction GIFs — the X GIF picker (Tenor/Giphy) uploads land as media type
     'animated_gif' and sit in the user's OWN replies (author == user), so the
     author check above misses them (same split as the download filter). The
     post-event sidecar has NO 'type', so these are HEURISTIC: an X animated_gif
     is a looping mp4 with NO audio stream → detected via ffprobe. Opt-in with
     `--gifs` (skipped by default because it ffprobes every mp4 and is a
     heuristic — a genuinely silent short clip is a possible false positive).

DB-driven (per the operator's request): iterates `items` rows with platform='x'
(the foreign parents were all registered under the target username by
_register_new_files before the fix). For each hit it deletes the file through
the DeletionGuard (so a safebraked scope is shielded) AND hard-deletes the DB
row, then sweeps any now-orphaned `_None.json` sidecar (guard.delete's
cleanup_sidecars keys off `<stem>.json` and MISSES the shared `_None` sidecar).

SAFETY / concurrency: rows with status='sending' (claimed by the dispatcher
mid-run) are SKIPPED, never touched. Still, prefer running with the archiver +
dispatcher down (`ops health`, then `ops unload`) for a fully clean pass — a
foreign file the dispatcher is about to send could otherwise race the unlink.

Usage (from repo root):
    PYTHONPATH="core" python3 -m tools.x_prune_foreign_gifs            # dry-run, foreign only
    PYTHONPATH="core" python3 -m tools.x_prune_foreign_gifs --gifs     # dry-run, + GIF heuristic
    PYTHONPATH="core" python3 -m tools.x_prune_foreign_gifs --apply
    PYTHONPATH="core" python3 -m tools.x_prune_foreign_gifs --apply --gifs
    PYTHONPATH="core" python3 -m tools.x_prune_foreign_gifs --apply --user someguy
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path

from core import ItemStore, PolicyStore, DeletionGuard
from core.files import cleanup_sidecars

VIDEO_MP4 = ".mp4"


def _sidecar_for(file_path: str) -> Path | None:
    """The per-tweet post-event sidecar `{date}_{tweet_id}_None.json` that sits
    beside a media file `{date}_{tweet_id}_{num}.{ext}`. Returns None if the
    filename doesn't parse into date_tweetid_num."""
    p = Path(file_path)
    parts = p.stem.split("_")
    if len(parts) < 3:
        return None
    date, tweet_id = parts[0], parts[1]
    if not tweet_id.isdigit():
        return None
    return p.parent / f"{date}_{tweet_id}_None.json"


def _sidecar_author(sidecar: Path, _cache: dict) -> str | None:
    """Lower-cased author screen-name from the sidecar, or None if unreadable /
    missing. Cached per sidecar path (multi-media tweets share one sidecar)."""
    key = str(sidecar)
    if key in _cache:
        return _cache[key]
    author = None
    try:
        with open(sidecar, encoding="utf-8") as fh:
            data = json.load(fh)
        a = data.get("author")
        name = a.get("name") if isinstance(a, dict) else None
        author = name.lower() if isinstance(name, str) else None
    except (OSError, ValueError):
        author = None
    _cache[key] = author
    return author


def _is_audioless_mp4(file_path: str) -> bool:
    """True iff ffprobe finds a video but NO audio stream — the on-disk
    signature of an X animated_gif. Any ffprobe failure → False (never delete on
    inconclusive evidence)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "default=nw=1", file_path],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    kinds = {line.split("=", 1)[1] for line in out.splitlines()
             if line.startswith("codec_type=")}
    return "video" in kinds and "audio" not in kinds


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Remove foreign-author X media (and, with --gifs, reaction GIFs).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete (default: dry-run).")
    ap.add_argument("--gifs", action="store_true",
                    help="Also remove reaction GIFs (audio-less mp4, ffprobe heuristic).")
    ap.add_argument("--user", default=None,
                    help="Restrict to a single X username.")
    ap.add_argument("--ignore-safebrake", action="store_true",
                    help="Bypass the DeletionGuard safebrake for THIS one-time "
                         "maintenance run (persistent config is left untouched). "
                         "Run only with workers down.")
    ap.add_argument("--db", default=None, help="Override suite DB path.")
    args = ap.parse_args()

    db = ItemStore.open(args.db)
    guard = DeletionGuard(PolicyStore())
    author_cache: dict = {}

    sql = ("SELECT id, username, file_path, status FROM items "
           "WHERE platform='x'")
    params: tuple = ()
    if args.user:
        sql += " AND username=?"
        params = (args.user,)
    rows = db.conn.execute(sql, params).fetchall()

    # (row, reason)  where reason in {"foreign", "gif"}
    plan: list[tuple] = []
    no_sidecar = 0
    for r in rows:
        fp = r["file_path"]
        sidecar = _sidecar_for(fp)
        author = _sidecar_author(sidecar, author_cache) if sidecar else None
        if author is None:
            # Can't prove foreign without the sidecar — leave it alone. Still
            # eligible for the GIF heuristic below (which needs no author).
            no_sidecar += 1
        elif author != r["username"].lower():
            plan.append((r, "foreign"))
            continue
        if args.gifs and os.path.splitext(fp)[1].lower() == VIDEO_MP4:
            if _is_audioless_mp4(fp):
                plan.append((r, "gif"))

    # ── Report ──────────────────────────────────────────────────────────────
    by_reason = defaultdict(lambda: [0, 0])          # reason -> [count, bytes]
    per_user = defaultdict(int)
    for r, reason in plan:
        try:
            sz = os.path.getsize(r["file_path"])
        except OSError:
            sz = 0
        by_reason[reason][0] += 1
        by_reason[reason][1] += sz
        per_user[r["username"]] += 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: scanned {len(rows)} X row(s); "
          f"{len(plan)} to delete "
          f"({'foreign + gif' if args.gifs else 'foreign only'}).")
    for reason in ("foreign", "gif"):
        c, b = by_reason[reason]
        if c or reason == "foreign":
            print(f"  {reason:>7}: {c:>6} file(s), {b/1_048_576:8.1f} MB")
    if not args.gifs:
        print("  (GIF pass OFF — add --gifs to also remove reaction GIFs.)")
    if no_sidecar:
        print(f"  note: {no_sidecar} file(s) had no readable sidecar "
              f"(author unknown → kept as foreign-safe).")
    if per_user:
        top = sorted(per_user.items(), key=lambda kv: -kv[1])[:10]
        print("  top users:", ", ".join(f"@{u}:{n}" for u, n in top))

    if not args.apply:
        print("\nSample (first 15):")
        for r, reason in plan[:15]:
            print(f"  id={r['id']:>7} {reason:>7} @{r['username']}  "
                  f"{os.path.basename(r['file_path'])}")
        print("\nRe-run with --apply to delete (files via DeletionGuard + DB rows).")
        db.close()
        return 0

    # ── Apply ───────────────────────────────────────────────────────────────
    deleted = rows_dropped = safebraked = sending = 0
    touched_dirs: set[Path] = set()
    for r, _reason in plan:
        if r["status"] == "sending":
            sending += 1          # dispatcher owns it right now — never race it
            continue
        if args.ignore_safebrake:
            # One-time maintenance bypass: delete directly (cleanup_sidecars
            # never raises). The persistent safebrake config is NOT touched, so
            # every live deletion path stays gated — only THIS run ignores it.
            cleanup_sidecars(r["file_path"])
            ok = True
        else:
            ok = guard.delete("x", r["username"], r["file_path"],
                              reason=f"x-prune ({_reason})")
        if not ok:
            safebraked += 1        # scope protected — leave file AND row intact
            continue
        deleted += 1
        touched_dirs.add(Path(r["file_path"]).parent)
        if db.delete(r["id"]):
            rows_dropped += 1
    db.conn.commit()

    # Sweep now-orphaned per-tweet sidecars: a `{date}_{id}_None.json` whose
    # sibling media (`{date}_{id}_*` non-json) are all gone.
    sidecars_removed = 0
    for d in touched_dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.endswith("_None.json"):
                continue
            date_id = name[:-len("_None.json")]     # "{date}_{tweet_id}_"
            siblings = [n for n in names
                        if n.startswith(date_id) and not n.endswith(".json")]
            if not siblings:
                try:
                    (d / name).unlink()
                    sidecars_removed += 1
                except OSError:
                    pass

    print(f"\nDone: deleted {deleted} file(s), dropped {rows_dropped} DB row(s), "
          f"removed {sidecars_removed} orphan sidecar(s).")
    if safebraked:
        print(f"  safebrake KEPT {safebraked} file(s) (protected scope).")
    if sending:
        print(f"  skipped {sending} file(s) claimed by the dispatcher (status=sending).")
    db.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
