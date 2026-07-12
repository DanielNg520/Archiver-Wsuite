# CLAUDE.md — working on this repo from an agent/assistant session

Read [README.md](README.md) for architecture; [DESIGN.md](DESIGN.md) is the
dense code map. This file is only the traps that bite automated sessions.

## Environment traps (this machine)

- **MSIX filesystem virtualization (CRITICAL).** Sessions launched from the
  Claude desktop app (or any MSIX-packaged host) get `%APPDATA%` /
  `%LOCALAPPDATA%` writes redirected into
  `AppData\Local\Packages\<app>\LocalCache\Roaming`. Reads are a merged view,
  so everything *looks* fine from inside the session while the real disk is
  untouched for every other process. Therefore: **never create, move, rename,
  or delete files under `AppData` from a session** — hand the user a script to
  run in their own shell instead. (In-place writes to *existing* files, e.g.
  sqlite rows, do go through.) This caused the 2026-07-12 config-migration
  split-brain — see ops/RUNBOOK.md "Config migration".
- **`pipx` / `yt-dlp` shims:** always `python -m pipx ...` / `python -m yt_dlp`
  — bare exe names can resolve to broken virtualized shims in packaged
  sessions and stale-PATH shells.
- **Suite config root:** `C:\Users\danie\.archive\.config\<app>` (self-contained
  since 2026-07-12, NOT virtualized). The DB is
  `.archive\.config\archiver-suite\suite.db`. Legacy `%APPDATA%` layout is
  auto-detected only if the new root is absent (`core.platform.paths`).
- Set `PYTHONUTF8=1` for any suite process whose stdout is redirected
  (status glyphs crash cp1252 otherwise).

## Build / test

- Packages: pipx venvs (`dispatcher`, `media-archiver`, `recorder`, `ops`) with
  `core` injected **editable** — `core` edits are live on worker restart; the
  other four need `python -m pipx install --force .\<pkg>` after edits.
- Tests: `PYTHONPATH="core;archiver;recorder;dispatcher;ops" PYTHONUTF8=1
  python tests/test_seams.py` (no pytest installed). Per-module `_selftest_*.py`
  files run the same way.
- `import core` from the repo root picks up the outer `core/` folder as a
  namespace package and shadows the real one — run import checks from a
  neutral cwd.

## Operational rules

- Workers run via Task Scheduler (`ops install/load/unload/uninstall`); task
  XML + launcher `.vbs` embed absolute paths — regenerate with
  `ops uninstall && ops install` after any path change.
- Don't run destructive DB/config operations while workers are up; check with
  `ops health` first. The dispatcher is the ONLY Telegram sender.
- `FilePartsInvalid` failures are permanent by design (oversize file needs a
  split, not a retry) — see ops/RUNBOOK.md.
