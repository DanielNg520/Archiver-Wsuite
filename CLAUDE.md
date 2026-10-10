# CLAUDE.md — working on this repo from an agent/assistant session

Read [README.md](README.md) for architecture; [DESIGN.md](DESIGN.md) is the
dense code map. This file is only the traps that bite automated sessions.

## Environment traps (Linux)

- **Config root: live state is `~/.archive/.config`, not `<repo>/.config`.**
  Code default on Linux is `<repo>/.config/<app>` (`core.platform.paths`,
  `XDG_CONFIG_HOME` ignored), but this machine sets
  `ARCHIVER_CONFIG_HOME=/home/dyne/.archive/.config` in the login shell and the
  `com.duy.*.service.d/10-env.conf` drop-ins. A process started without that
  env silently uses a fresh `<repo>/.config` instead.
- **Worker logs** are `~/.local/log/<app>.{out,err}.log` (unit `StandardOutput`),
  not `$CONFIG/archiver-suite/logs` (frozen since 2026-07-28).
- **Unit names are `com.duy.{archiver,dispatcher,recorder,logrotate}`** — a bare
  `systemctl --user is-active recorder` reports `inactive` for a running service.
- **`yt-dlp` shims:** always `python -m yt_dlp` — bare exe names can resolve to
  broken/stale shims on a stale-PATH shell.
- Set `PYTHONUTF8=1` for any suite process whose stdout is redirected
  (status glyphs crash a non-UTF8 locale otherwise).

## Build / test

- Packages: **`uv tool` venvs** (`dispatcher`, `media-archiver`, `recorder`, `ops`;
  check with `uv tool list`), each installed **editable** with `core` injected
  editable — source edits to any package are live on worker restart
  (`ops restart <worker>`). Reinstall only for dependency/entry-point changes:
  `uv tool install --force --editable ./<pkg> --with-editable ./core`.
  **`--with-editable ./core` is not optional** — a bare `uv tool install
  --force --editable ./<pkg>` recreates the venv from scratch and DROPS the
  separately-injected `core` dependency entirely (none of the four packages
  declare `core` in their own `pyproject.toml`, so nothing else re-adds it).
  The break is sneaky: `import core` doesn't fail outright afterward if your
  shell's cwd happens to be the repo root (Python's `-c`/cwd-relative import
  then picks up the outer `core/` folder as a *namespace* package — the same
  trap noted below — instead of raising ModuleNotFoundError), so it can look
  like it's working right up until a submodule import fails. Verify any
  reinstall from a neutral cwd: `cd /tmp && <tool>/bin/python -c "import
  core; print(core.__path__)"` should print
  `.../Archiver-Suite/core/core`, not `.../Archiver-Suite/core`. (Hit for
  real during the recorder-notify feature, 2026-08-05 — recovered by
  reinstalling with the flag.)
- Tests (`:` is the PYTHONPATH separator on Linux; no pytest installed, plain
  asserts). No single package's own `uv tool` venv (nor the bare system
  `python`) has every dependency `tests/test_seams.py` needs — run
  `python tools/setup_test_venv.py` once to build `.venv-test` (gitignored)
  with the union of all five packages' dependencies, then:
  `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1
  .venv-test/bin/python3 tests/test_seams.py`. Per-module `_selftest_*.py`
  files run the same way (or against a single package's own venv when the
  module only needs that package's deps).
- `import core` from the repo root picks up the outer `core/` folder as a
  namespace package and shadows the real one — run import checks from a
  neutral cwd.

## Operational rules

- Workers run as **systemd --user services** (`ops install/load/unload/uninstall`);
  the unit files under `~/.config/systemd/user/` embed absolute tool paths —
  regenerate with `ops uninstall && ops install` after any path change. Enable
  `loginctl enable-linger $USER` for the services to run while logged out.
- Don't run destructive DB/config operations while workers are up; check with
  `ops health` first. The dispatcher is the ONLY Telegram sender.
- `FilePartsInvalid` failures are permanent by design (oversize file needs a
  split, not a retry) — see [ops/RUNBOOK.md](ops/RUNBOOK.md).

## Porting notes

- The OS seam is `core/core/platform/` (`service` = systemd/launchd;
  `filelock`; `procgroup`; `signals`; `process`; `paths`). Keep the Linux and
  macOS branches behavior-parallel — every verb exists on both.
- Windows support is dropped (owner, 2026-10-09); never add an `nt`/`win32`
  branch. Remaining Windows code is being removed (AGENTS.md Carryover).
- The macOS (`launchd`) branch is kept but not exercised here; Linux/systemd is
  the deployment target.
