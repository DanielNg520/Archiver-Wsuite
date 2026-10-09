# AGENTS.md

Single agent doc for this repo. Read first. Traps: `CLAUDE.md`. Architecture: `README.md`. Code map: `DESIGN.md`.

## Findings

(none open)

## Commands

- Tests: `python tools/setup_test_venv.py` once, then `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 .venv-test/bin/python3 tests/test_seams.py` (285 checks).
- Selftests: `PYTHONPATH=core:recorder PYTHONUTF8=1 .venv-test/bin/python3 -m recorder._selftest_<name>`; same pattern per package.
- Deploy: all tools are editable `uv tool` installs; `ops restart <worker>` makes source edits live. `ops health` before and after.
- Reinstall (deps/entry points only): `uv tool install --force --editable ./<pkg> --with-editable ./core`; verify `core.__path__` from `/tmp`.
- Code changes go through TriAPI `rebuild/` dispatch; `apply_dispatch.py` rewrites CRLF to LF, re-CRLF the file before commit.

## Live deployment (this machine, Fedora)

- Units `com.duy.{archiver,dispatcher,recorder}.service` + `com.duy.logrotate.timer`: enabled, `Restart=always`, linger on. Must stay always-on (owner, 2026-10-09).
- Config root `~/.archive/.config` via `ARCHIVER_CONFIG_HOME` (unit drop-ins `10-env.conf` + shell). Without it, code falls back to `<repo>/.config`.
- Logs: `~/.local/log/<app>.{out,err}.log`. `$CONFIG/archiver-suite/logs` frozen since 2026-07-28.
- `OUTPUT_DIR=/home/dyne/.archive`; `ROUTES_DIR=/run/media/dyne/StoragEDGE/.routes`.
- Recorder `output_dir=/run/media/dyne/StoragEDGE/.records`; fallback root `state_dir` = `~/.recorder` (see below).
- StoragEDGE (120GB USB, `/dev/sdc2`) can re-enumerate on a USB disconnect storm, leaving a dead mount; remount fixes it.
- SanDisk ULTRAFIT stopgap reverted 2026-10-09; an SD card reader is on order as StoragEDGE's permanent connection.
- Playwright Chromium + headless shell in `~/.cache/ms-playwright` (machine-local); age-restricted browser fallback confirmed resolving URLs.

## Recorder storage fallback (2026-10-09)

- `StreamCapture._choose_run_dir` (`recorder/recorder/capture.py`): primary `output_dir/<user>` only if root exists and a probe write succeeds.
- Otherwise warns and uses `fallback_dir/<user>`; `cli.py` passes `fallback_dir=config.state_dir`. Never creates the `output_dir` root.
- Reconnects re-run `start`, so a drive lost mid-session moves the next segment to the fallback; the in-flight segment is lost.
- Startup sweep runs on `output_dir` then `state_dir` (separate try/except, logs `fallback startup sweep`).
- Test: `_selftest_capture.test_start_falls_back_when_output_missing`. Built via TriAPI (DeepSeek), one test reply rejected and redispatched.
- `recorder watch` (`watch._active_recording`) scans both roots; a dead root is skipped per root.
- Gap: `archiver.reconcile` recordings scan and ban `quarantine_user`/`restore_user` cover `output_dir` only, not the fallback root.
- Proposal (systemic): one `recording_roots(config)` helper in `recorder.config`, used by sweep, watch, reconcile, quarantine/restore.
- Gap: `windows/recorder` mirror lacks the fallback (parity-only tree).

## Durable behavior notes

- Recorder `wait()` exit codes: -1 clean stop, -2 dead stream (zero bytes), -3 stall guard (`stall_timeout_s`, default 300); all three end the reconnect loop.
- Recorder discards segments under `RECORDER_MIN_SEGMENT_BYTES` (default 1MB) before remux; logs `discarded_stub`.
- `recorder record --user X` reloads the service on every exit path; `--no-reload` only when the caller reloads itself.
- `ops unload recorder` also SIGTERMs a manual `recorder record` (`health.foreground_pid`), touching `recorder_suppress_reload_flag` so it skips its reload.
- Dispatcher whole-batch failures always set `retry_after` (`stall_backoff_s`, 300s); schema v5 `items.retry_after` gates `claim_next`/`claim_batch`.
- Dispatcher wedged on one upload with Telegram route loss: `ops restart dispatcher` is the known unstick.
- Stale `tiktok.lock` with service down: `ops restart recorder`; if still held and its pid is dead, remove it (`ops/RUNBOOK.md`).
- Windows branch: a file with an open handle cannot be replaced or deleted; keep that in mind for any `windows/` or `core.platform` nt change.
- `ops.health` `@_memo` wrappers accept positional args only: `drain_eta_fields(60)`, not `window_minutes=60`.

## Known tech debt

- [ ] No test drives `state._wait_for_recording_done` with fake rc -1/-2/-3 to assert each is terminal.
- [ ] Drain unconditional `backoff_s` throughput cost on one-off `ConnectionError`s unverified; TriAPI task `b41afde0` (non-stalled seam test) not landed.
- [ ] `tests/test_seams.py` oversized (~157K chars); split along `── Seam N` boundaries, preserving behavior.
- [ ] `core/core/manual_delete.py` docstrings/log say "Recycle Bin"; Linux uses freedesktop trash.
- [ ] `recorder/recorder/cookie_refresh.py`: cookie write not atomic (use temp + `os.replace`); drops `#HttpOnly_` prefix on rewrite.
- [ ] `archiver/archiver/orchestrator.py:370` bare `except Exception: pass` without a comment.
- [ ] `archiver/` thin coverage: 2 selftests for `orchestrator.py` (~1250 lines) and `cli.py` (~2260); check Seam 34 first.
- [ ] Split candidates on size: `archiver/archiver/cli.py`, `dispatcher/dispatcher/send.py`; only if touched again.
- [ ] `windows/` mirrors drift: missing stall guard, `retry_after`/`backoff_s`, storage fallback; needs file-by-file diff if revived.
- [ ] Stale gitignored `*/build/lib/` dirs under packages; safe to remove.

## Decisions

- Recorder falls back to `~/.recorder/<user>/` when StoragEDGE is absent (owner, 2026-10-09).
- Burner TikTok account treated as NOT banned (cookies pass login check); no `tiktok.txt` refresh from Firefox (owner, 2026-10-09).
- Historical plan docs folded in and deleted; git history keeps them; `<repo>/.config` leftover trashed (owner, 2026-10-09).

## Ask owner

(none open)

## Index

- `core/core/` shared library: store/schema (`suite.db`), ingest, dedup, quarantine, media_prep, `platform/` OS seam.
- `archiver/` media-archiver: scheduled platform scans (X, TikTok, Instagram) via gallery-dl/yt-dlp, reconcile, orphaned routes.
- `dispatcher/` sole Telegram sender: claim/drain/send, fast upload, delete-after-upload.
- `recorder/` TikTok live recorder: `state.py` loop, `capture.py` yt-dlp, `platforms/` URL resolve + browser fallback, `startup_sweep.py`.
- `ops/` CLI: install/load/unload/restart/health/watch/update/logrotate; `RUNBOOK.md`.
- `tests/` seams suite + dispatcher stall-backoff test. `tools/` one-off migration/maintenance scripts.
- `windows/` parity mirror of the four packages, unexercised on Linux.
- Docs: `README.md`, `DESIGN.md`, `USER-GUIDE.md`, `AUTOMATION.md`, `PROJECT_MAP.md`, per-package `README.md`, `CLAUDE.md`. `windows/` keeps its own doc copies.

## Carryover

Last audit: 2026-10-09 (recorder fallback, watch dual-root, docs sanitation).
- 2026-10-09: recorder storage fallback shipped and deployed (`ops restart recorder`, both sweeps logged); 285 seams + all recorder selftests pass.
- 2026-10-09: historical docs deleted, `connection_fix.md` code-comment refs reworded via TriAPI; 285 seams + stall-backoff tests pass.
- Next: pick from Known tech debt.
