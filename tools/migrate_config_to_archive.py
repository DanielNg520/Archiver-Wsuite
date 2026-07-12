r"""
tools.migrate_config_to_archive
───────────────────────────────
Move ALL per-app config out of %APPDATA% into the archive root, making the
suite fully self-contained under one tree (2026-07-12 refactor):

Before:  %APPDATA%\archiver-suite   (config, .env, suite.db, sessions, cookies,
         %APPDATA%\dispatcher        logs, locks, launchers, tiktok.txt, …)
         %APPDATA%\recorder
         %APPDATA%\archiver

After:   C:\Users\danie\.archive\.config\archiver-suite
         C:\Users\danie\.archive\.config\dispatcher
         C:\Users\danie\.archive\.config\recorder
         C:\Users\danie\.archive\.config\archiver

core.platform.paths resolves the new root automatically once
`.archive\.config\archiver-suite` exists (see _config_home); until then every
worker keeps reading the legacy %APPDATA% layout, so this move is the single
atomic switch. ARCHIVER_CONFIG_HOME overrides both for non-standard roots.

Also rewrites absolute `...\AppData\Roaming\<app>\...` path VALUES inside the
moved `.env` files (TELEGRAM_SESSION, LOG_FILE, STATE_DIR, cookie paths …) so
they point at the new tree.

RUN FROM A NORMAL USER SHELL ONLY — never from inside an MSIX-packaged app
(Claude desktop and similar). Packaged processes have %APPDATA% writes
FILESYSTEM-VIRTUALIZED into AppData\Local\Packages\<app>\LocalCache\Roaming:
the move "succeeds" in that process's merged view while the real Roaming dirs
stay untouched for every other process — a split-brain where restarted
workers find a half-copied target and start a fresh suite.db (happened
2026-07-12; see ops/RUNBOOK.md "Config migration").

WHAT THIS TOOL DOES NOT DO (run order below):
- It does NOT stop workers. Stop them first — suite.db, sessions and logs are
  open files while they run; a move would fail or corrupt.
- It does NOT touch Task Scheduler. The scheduled tasks + launcher .vbs embed
  the OLD log paths, so uninstall them BEFORE and reinstall AFTER.

RUN ORDER (PowerShell):
    ops unload                                   # stop + disable workers
    ops uninstall                                # drop tasks + launchers
    python tools\migrate_config_to_archive.py            # dry-run: inspect
    python tools\migrate_config_to_archive.py --apply    # move + rewrite
    python -m pipx install --force .\dispatcher  # non-editable pkg: pick up fixes
    ops install                                  # regenerate tasks @ new paths
    ops load                                     # start workers

Dry-run by default; --apply performs the move. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

APPS = ("archiver-suite", "dispatcher", "recorder", "archiver")
WORKER_IMAGES = ("archiver.exe", "recorder.exe", "dispatcher.exe")


def appdata() -> Path:
    v = os.environ.get("APPDATA")
    return Path(v) if v else Path.home() / "AppData" / "Roaming"


def new_root() -> Path:
    override = os.environ.get("ARCHIVER_CONFIG_HOME")
    return Path(override) if override else Path.home() / ".archive" / ".config"


def running_workers() -> list[str]:
    """Suite worker processes currently alive (they hold config files open)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=30,
        ).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return []          # can't tell; the move itself will fail loudly if held
    return [img for img in WORKER_IMAGES if f'"{img}"' in out]


def rewrite_env_paths(env_file: Path, old_base: Path, new_base: Path) -> int:
    """Point absolute `<old_base>\\<app>\\...` values in one .env at the new
    tree. Tolerates either slash direction and any casing of the base."""
    try:
        text = env_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    n = 0
    for app in APPS:
        # e.g.  C:\Users\danie\AppData\Roaming\archiver-suite  (either slashes)
        pat = re.compile(
            re.escape(str(old_base)).replace(r"\\", r"[\\/]") + r"[\\/]"
            + re.escape(app),
            re.IGNORECASE,
        )
        repl = str(new_base / app)
        text, k = pat.subn(lambda _m: repl, text)   # lambda: repl is literal,
        n += k                                      # backslashes not re-escapes
    if n:
        env_file.write_text(text, encoding="utf-8")
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--apply", action="store_true",
                    help="perform the move (default is dry-run)")
    ap.add_argument("--force", action="store_true",
                    help="skip the running-workers check")
    args = ap.parse_args()

    src_root, dst_root = appdata(), new_root()
    moves = [(src_root / app, dst_root / app)
             for app in APPS if (src_root / app).is_dir()]
    if not moves:
        print(f"nothing to migrate: no suite dirs under {src_root}")
        return 0

    if args.apply and not args.force:
        alive = running_workers()
        if alive:
            print(f"REFUSING: workers still running: {', '.join(alive)}\n"
                  f"Run `ops unload` first (or --force if you know better).")
            return 1

    clashes = [d for _, d in moves if d.exists()]
    if clashes:
        print("REFUSING: target(s) already exist "
              f"(migrated already?): {', '.join(map(str, clashes))}")
        return 1

    print(f"{'APPLY' if args.apply else 'DRY-RUN'}: "
          f"{src_root}  →  {dst_root}")
    for src, dst in moves:
        print(f"  move {src.name:16s} → {dst}")
    if not args.apply:
        print("\n(no changes made — re-run with --apply)")
        return 0

    dst_root.mkdir(parents=True, exist_ok=True)
    for src, dst in moves:
        try:
            os.rename(src, dst)                    # same volume: atomic
        except OSError:
            shutil.move(str(src), str(dst))        # cross-volume fallback
        print(f"  moved {src.name}")

    total = 0
    for env in dst_root.rglob("*.env"):            # matches `.env` dotfiles too
        total += rewrite_env_paths(env, src_root, dst_root)
    print(f"  rewrote {total} path value(s) in .env files")

    print("\nDone. Now:  pipx install --force .\\dispatcher ; "
          "ops install ; ops load")
    return 0


if __name__ == "__main__":
    sys.exit(main())
