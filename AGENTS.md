# AGENTS.md

Single agent doc for this repo. Read first. Traps: `CLAUDE.md`. Architecture: `README.md`. Code map: `DESIGN.md`.

## Findings

(none open)

## Commands

- Tests: `python tools/setup_test_venv.py` once, then `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 .venv-test/bin/python3 tests/test_seams.py` (296 checks).
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
- One root list: `core.paths.recording_roots(output_dir, state_dir)`; used by startup sweep, watch, ban quarantine, unban restore, `archiver.reconcile_recordings`.
- Archiver learns the recorder `state_dir` from the recorder's `.env` `STATE_DIR` (`dotenv_values`, never `os.environ`), else `core.paths.recorder_state_dir()`.
- Residual: a `STATE_DIR` set only in the recorder unit environment (not `.env`) is invisible to the archiver; ops reads the pid default only.
- Tests: Seam 36 (`test_recording_roots_seam`), `_selftest_ban_escalation` fallback-root quarantine check.
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
- Archiver needs `curl-cffi>=0.16.0` (libcurl 8.21.0): 0.14.0's libcurl 8.15.0 UAF (CVE-2026-10536) SIGABRTed `archiver loop` ~100 times in `curl_easy_reset`.
- Check a crash's curl_cffi: `eu-unstrip -n --core=<core>`; 0.14.0 build-id `88b47b15…`, 0.16.3 `f92375e0…`. Floor applies only after a reinstall.

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
- StoragEDGE ejected for a multi-day live run of the `~/.recorder` fallback, started 2026-10-09 (owner, 2026-10-09).
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

Last audit: 2026-10-09 (F1 re-verification, through a0c31f1; docs only, no code changes).
- 2026-10-09: `recording_roots` shipped via TriAPI (`tasks/archiver_recording_roots`); 296 seams, all recorder/archiver selftests, stall-backoff pass.
- 2026-10-09: deployed (`ops restart recorder archiver`); recorder startup sweep logs both roots.
- OPEN fallback trial: StoragEDGE UNMOUNTED 2026-10-09 (`udisksctl unmount`; power-off needs polkit). Live-config probe chose `~/.recorder/<user>`.
- During trial: route folders on `ROUTES_DIR` unreachable; drive data untouched (832M records, 3.0G routes).
- Trial check: `grep 'using fallback' ~/.local/log/recorder.out.log`; recordings in `~/.recorder/<user>/` must upload and get deleted.
- End trial: remount (`udisksctl mount -b /dev/sdc2`, polkit, owner), `findmnt`; leftovers in `~/.recorder` are swept, no move-back.
- 2026-10-09: F1 re-verified FIXED (e1fcab4): every saved core had curl_cffi 0.14.0 loaded; zero crashes since 0.16.3 reinstall (2026-10-08 23:55).
- Suspected only: 4 `com.duy.dispatcher` SIGABRTs since 2026-09-19 (2 dumps, first-thread `select_epoll_poll_impl`); not curl_cffi, uninvestigated.
- Next: Known tech debt, top down.
