# CLAUDE.md — working on this repo from an agent/assistant session

Read [README.md](README.md) for architecture; [DESIGN.md](DESIGN.md) is the
dense code map. This file is only the traps that bite automated sessions.

## Environment traps (Linux)

- **Self-contained config root (`<repo>/.config`).** On Linux the suite keeps
  ALL per-app state — config, `suite.db`, sessions, cookies, logs, locks — under
  `<repo>/.config/<app>` (git-ignored), resolved by `core.platform.paths`
  from the editable-injected `core`'s own `__file__`. So the checkout carries
  its own state and there is nothing under `~/.config` to touch. `XDG_CONFIG_HOME`
  is deliberately **not** consulted (it would defeat self-containment);
  `ARCHIVER_CONFIG_HOME` is the one override.
- **`pipx` / `yt-dlp` shims:** always `python -m pipx ...` / `python -m yt_dlp`
  — bare exe names can resolve to broken/stale shims on a stale-PATH shell.
- Set `PYTHONUTF8=1` for any suite process whose stdout is redirected
  (status glyphs crash a non-UTF8 locale otherwise).

## Build / test

- Packages: pipx venvs (`dispatcher`, `media-archiver`, `recorder`, `ops`) with
  `core` injected **editable** — `core` edits are live on worker restart; the
  other four need `python -m pipx install --force ./<pkg>` after edits.
- Tests (`:` is the PYTHONPATH separator on Linux):
  `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1
  python tests/test_seams.py` (no pytest installed). Per-module `_selftest_*.py`
  files run the same way.
- `import core` from the repo root picks up the outer `core/` folder as a
  namespace package and shadows the real one — run import checks from a
  neutral cwd.

## Operational rules

- Workers run as **systemd --user services** (`ops install/load/unload/uninstall`);
  the unit files under `~/.config/systemd/user/` embed absolute pipx paths —
  regenerate with `ops uninstall && ops install` after any path change. Enable
  `loginctl enable-linger $USER` for the services to run while logged out.
- Don't run destructive DB/config operations while workers are up; check with
  `ops health` first. The dispatcher is the ONLY Telegram sender.
- `FilePartsInvalid` failures are permanent by design (oversize file needs a
  split, not a retry) — see [ops/RUNBOOK.md](ops/RUNBOOK.md).

## Porting notes (this is the Linux port)

- The OS seam is `core/core/platform/` (`service` = systemd/launchd/Task
  Scheduler; `filelock` = fcntl/msvcrt; `procgroup` = killpg/taskkill; `signals`;
  `process`; `paths`). Keep the three OS branches behavior-parallel — every verb
  exists on all three.
- The macOS (`launchd`) and Windows (`Task Scheduler`) branches are kept for
  parity but are not exercised here; Linux/systemd is the deployment target.
