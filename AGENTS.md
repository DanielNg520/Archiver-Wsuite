# AGENTS.md

Single agent doc for this repo. Read first. Traps: `CLAUDE.md`. Architecture: `README.md`. Code map: `DESIGN.md`.

## Findings

- None open.

## Commands

- Tests: `python tools/setup_test_venv.py` once, then `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 .venv-test/bin/python3 tests/test_seams.py` (303 checks).
- Ops selftests: `PYTHONPATH=core:ops PYTHONUTF8=1 .venv-test/bin/python3 -m ops._selftest_bot` (also `_selftest_health`); not in `test_seams.py`.
- Selftests: `PYTHONPATH=core:recorder PYTHONUTF8=1 .venv-test/bin/python3 -m recorder._selftest_<name>`; same pattern per package.
- Deploy: all tools are editable `uv tool` installs; `ops restart <worker>` makes source edits live. `ops health` before and after.
- Reinstall (deps/entry points only): `uv tool install --force --editable ./<pkg> --with-editable ./core`; verify `core.__path__` from `/tmp`.
- Code changes go through TriAPI `rebuild/` dispatch.
- Line endings: LF only, enforced by `.gitattributes` (`* text=auto eol=lf`); renormalized 2026-10-10.
- Gated (2026-10-09): the 8 code dirs are in `~/.claude/dispatch-gate-paths.txt`; `.git/hooks/pre-commit` runs the ledger check. Hand fix: owner runs `dispatch-ledger-hand.sh`.
- Apply seams-verified edits with `--check "env PYTHONPATH=core:archiver:recorder:dispatcher:ops PYTHONUTF8=1 .venv-test/bin/python3 tests/test_seams.py" --cwd <repo>`; `--test` rolls back (no pytest counts).

## Live deployment (this machine, Fedora)

- Units `com.duy.{archiver,dispatcher,recorder,bot}.service` + `com.duy.logrotate.timer`: enabled, `Restart=always`, linger on. Must stay always-on (owner, 2026-10-09).
- Config root `~/.archive/.config` via `ARCHIVER_CONFIG_HOME` (unit drop-ins `10-env.conf` + shell). Without it, code falls back to `<repo>/.config`.
- Logs: `~/.local/log/<app>.{out,err}.log`. `$CONFIG/archiver-suite/logs` frozen since 2026-07-28.
- `OUTPUT_DIR=/home/dyne/.archive`; `ROUTES_DIR=/run/media/dyne/StoragEDGE/.routes`.
- Recorder `output_dir=/run/media/dyne/StoragEDGE/.records`; fallback root `state_dir` = `~/.recorder` (see below).
- StoragEDGE (120GB USB, NTFS; device letter varies, `sda2` on 2026-10-11, find via `lsblk`) can re-enumerate on a USB disconnect storm, leaving a dead mount; remount fixes it.
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

## Durable behavior notes

- Priority order for design trade-offs: integrity > self-healing > seam robustness > efficiency.
- Recorder `wait()` exit codes: -1 clean stop, -2 dead stream (zero bytes), -3 stall guard (`stall_timeout_s`, default 300); all three end the reconnect loop.
- Recorder discards segments under `RECORDER_MIN_SEGMENT_BYTES` (default 1MB) before remux; logs `discarded_stub`.
- `recorder record --user X` reloads the service on every exit path; `--no-reload` only when the caller reloads itself.
- `ops unload recorder` also SIGTERMs a manual `recorder record` (`health.foreground_pid`), touching `recorder_suppress_reload_flag` so it skips its reload.
- Dispatcher whole-batch failures always set `retry_after` (`stall_backoff_s`, 300s); schema v5 `items.retry_after` gates `claim_next`/`claim_batch`.
- Dispatcher wedged on one upload with Telegram route loss: `ops restart dispatcher` is the known unstick.
- Stale `tiktok.lock` with service down: `ops restart recorder`; if still held and its pid is dead, remove it (`ops/RUNBOOK.md`).
- `ops.health` `@_memo` wrappers accept positional args only: `drain_eta_fields(60)`, not `window_minutes=60`.
- Archiver needs `curl-cffi>=0.16.0` (libcurl 8.21.0): 0.14.0's libcurl 8.15.0 UAF (CVE-2026-10536) SIGABRTed `archiver loop` ~100 times in `curl_easy_reset`.
- Check a crash's curl_cffi: `eu-unstrip -n --core=<core>`; 0.14.0 build-id `88b47b15…`, 0.16.3 `f92375e0…`. Floor applies only after a reinstall.

- Drain tests needing an immediate reclaim after a failed send must set `stall_backoff_s=0` (default 300s backoff); see Seams 11b, 15.
- Same-user recorder rows go up as one album per claim even with `BatchPolicy.SIZE_KEY` 1.
- Archiver groups under min batch (10) wait up to 168h (`BatchPolicy.DEFAULT_WAIT_H`); hours without sends with pending rows is normal.

## Known tech debt

- [ ] Split candidates on size: `archiver/archiver/cli.py`, `dispatcher/dispatcher/send.py`; only if touched again.

## Decisions

- Recorder falls back to `~/.recorder/<user>/` when StoragEDGE is absent (owner, 2026-10-09).
- Delete `core.notify`/`notify_outbox` and the recorder `queue_event`; `ops bot` is the only start/end notifier (owner, 2026-10-11; moved from Ops Center 2026-10-14).
- Burner TikTok account treated as NOT banned (cookies pass login check); no `tiktok.txt` refresh from Firefox (owner, 2026-10-09).
- StoragEDGE ejected for a multi-day live run of the `~/.recorder` fallback, started 2026-10-09 (owner, 2026-10-09).
- Bot token for @Nothing_is_available_ARC_bot lives in suite `.env` as `RECORDER_BOT_API`; its only consumer is `ops bot` (reads it via `core.env.read_env_file`) (owner, 2026-10-12).
- Bot commands: `/record`, `/recstatus`, `/ar_add` (archiver), `/re_add` (recorder), `/remove`, `/list` (recorder); old `/add`, `/addrecord` dropped (owner, 2026-10-13).
- Fallback trial runs ~1 week; StoragEDGE stays unmounted until about 2026-10-16 (owner, 2026-10-09).
- Trial ended early: StoragEDGE replugged and auto-mounted 2026-10-11 15:28 (owner, 2026-10-11).
- Gate this repo's code dirs (dispatch-gate-paths + ledger pre-commit hook), like SemAI/TriAPI (owner, 2026-10-09).
- "Long outdated" means the whole repo: bring docs, deps and layout in line with the current global rules (owner, 2026-10-09).
- `requirements.txt` deleted; per-package `pyproject.toml` is the only dependency source (owner, 2026-10-09).
- `PROJECT_MAP.md` folded and deleted under the current rules (owner, 2026-10-09).
- Historical plan docs folded in and deleted; git history keeps them; `<repo>/.config` leftover trashed (owner, 2026-10-09).
- Linux (systemd) and macOS (launchd) are the only supported platforms (owner, 2026-10-09).
- Absorb `vlog_bridge` (ARC bot) into the suite; purge Ops Center completely after a tarball to `~/.archive` (owner, 2026-10-14).
- ARC bot scope: every suite CLI command via shorthand, graceful per-command status reply, record start/stop messages (owner, 2026-10-14).
- ARC bot deletes its own messages and the owner's commands 24h after sending (owner, 2026-10-14; Bot API caps deletes at 48h).
- ARC bot destructive commands need an inline Yes/No confirm, 60s expiry; read-only and additive run directly (owner, 2026-10-14).
- Fix dispatcher reconnect wedge (stale Telethon `_ping` kills each reconnect at 60s) and add an `ops health` stall warning (owner, 2026-10-14).
- ARC bot allowed ids live in suite `.env` as `ARC_BOT_ALLOWED_IDS` (comma list), next to `RECORDER_BOT_API` (owner, 2026-10-14).
- Fix all F11-F18 via dispatch (owner, 2026-10-14); done in `05d1362` (2026-10-15).
- During DeepSeek peak (01:00-04:00, 06:00-10:00 UTC weekdays), dispatch falls back to OpenRouter instead of pausing (owner, 2026-10-15).
- ARC bot lives in `ops` as `ops bot`, unit `com.duy.bot`, stdlib HTTP only, ops keeps zero deps (owner, 2026-10-14).
- `tools/recover_suite_db.py` is a generic recover/verify/swap; no backup-merge step (owner, 2026-10-09).
- Three already-applied one-shot path/config migration scripts deleted (owner, 2026-10-09).
- Anything Windows-only can be removed: CRLF → LF renormalize, trash `miki_status.py` + its `.gitignore` lines (owner, 2026-10-10).
- Bound `TelethonSendStrategy.__aexit__` disconnects to 5s each so shutdown fits 45s (owner, 2026-10-10).
- Shutdown disconnect problems are log-only, no Telegram notify: the dispatcher is the sole sender and already disconnected (owner delegated, 2026-10-10).
- Disconnect-timeout investigation deferred to a new session (owner, 2026-10-10).
- Orchestrator selftests cover disk-full purge, auth circuit, reconcile users, stories auth backoff (owner, 2026-10-11).
- Fix every known open problem in-session; no deferred "still open" lists (owner, 2026-10-11).
- Ops Center references removed from Hivemind (memory + inventory) and TriAPI AGENTS.md, pushed (owner, 2026-10-14).
- Archiver stack is Fedora-only: Mac repo copies, config dirs and Gemini project state deleted; Fedora is the sole deployment (owner, 2026-10-14).

## Ask owner

- None open.

## Index

- `core/core/` shared library: store/schema (`suite.db`), ingest, dedup, quarantine, media_prep, `platform/` OS seam.
- `archiver/` media-archiver: scheduled platform scans (X, TikTok, Instagram) via gallery-dl/yt-dlp, reconcile, orphaned routes.
- `dispatcher/` sole Telethon media sender (`ops bot` sends only control messages via Bot API): claim/drain/send, fast upload, delete-after-upload.
- `recorder/` TikTok live recorder: `state.py` loop, `capture.py` yt-dlp, `platforms/` URL resolve + browser fallback, `startup_sweep.py`.
- `ops/` CLI: install/load/unload/restart/health/watch/update/logrotate/bot; `bot/` ARC Telegram bot; `RUNBOOK.md`.
- `tests/` `tests/test_seams.py` is the seams runner over `tests/seams/` (topic modules + `_harness.py`), plus the dispatcher stall-backoff test. `tools/` one-off migration/maintenance scripts.
- Docs: `README.md`, `DESIGN.md`, `USER-GUIDE.md`, `AUTOMATION.md`, `ops/RUNBOOK.md`, per-package `README.md`, `CLAUDE.md`.

## Carryover

Last audit: 2026-10-14 wrap-up (audited through `c953383`; 0 new findings, light path).

- Audit 2026-10-14 (65ba6fb..HEAD): reconnect wedge and stall-header fixes verified; F11-F18 logged above; fast_upload "new file_id per retry" refuted as stated.
- Telethon-internal auto-reconnect re-sends the pending ping, so only the manual `_force_reconnect` path needed the `_ping` reset (source read, Telethon 1.45).
- Reconnect wedge fixed (TriAPI `archiver_reconnect_ping`); `ops health` amber stall header via `recorder_stall_warning` (`archiver_health_stall`); 303 seams.
- Watch: after a network drop, `reconnected after stall` must NOT be followed by `Automatic reconnection failed` 60s later; not yet seen live.
- Watch: first video upload over 60s after a reconnect must complete; if `Automatic reconnection failed` returns 60s after `reconnected`, F9 is not closed.
- Incident 2026-10-14: queue stalled 6h (19970609bun, rhmn1999 videos; uploads over 60s failed `Cannot send requests while disconnected`) until `ops restart dispatcher` 13:49; both sent.
- Watch: `_drain_with_stop_grace` and live `retry_after` backoff not yet seen live; shutdown disconnect logs: grep `disconnect timed out after` in `dispatcher.out.log`.
- F11-F18 fixed in `05d1362`; dispatcher and bot restarted 2026-10-14 23:08 PDT, healthy.

### ARC bot (P1 live 2026-10-14; next: P2 in a new session)
- P1 (ab9f425): `ops bot` = `com.duy.bot`, `KillMode=process`; state in `$CONFIG/archiver-suite/bot/` (offset, reports, sent ledger).
- Ops Center purged (tar `~/.archive/opscenter-2026-10-14.tar.gz`); vlog-bridge disabled. Errors only in `~/.local/log/bot.err.log`; silence = polling fine.
- Watch: first 24h sweep ~2026-10-15 19:15; tracked messages must vanish from chat and `sent.json`.
- Trap: OpenRouter fallback (DeepSeek peak) mangled fences/docstrings and invented APIs; agy wrote all bot modules clean. Prefer agy for new files.
- P2 reuse: `commands.run_cli` + `bins`, loop `_handle_command`; add `callback_query` handling for confirm buttons.
- P2 (full CLI): shorthand alias table (data, not code per command) plus `/a /r /d /o <raw args>` passthrough for every other command.
- P2: daemon/interactive verbs (`start`, `loop`, `watch`, `tui`) blocked; `record` stays async; per-entry timeout (ops update needs minutes).
- P2: destructive = alias flag or passthrough verb not in a read-only allowlist; inline Yes/No, 60s expiry.
- P2: graceful status: post "running <argv>", then edit to ok/fail, exit code, trimmed output tail; update `setMyCommands`.
- P2 trap: `ops restart bot`/`ops unload bot`/`ops update` from the bot kills or restarts itself mid-reply; reply first, then exec.

### Standing traps
- Test-writing: DeepSeek failed fake-heavy selftests three rounds; agy wrote all four first try (2026-10-15). Prefer agy for new tests.
- `apply_dispatch` check timeout is 120s; the full suite (~124s) exceeds it. Check with seams + the file's selftests (~75s), full run after.
- `apply_dispatch --response` takes the stored `logs/responses/<sha>.txt` path, not the task `.out`; `--new` needs one ```python fence.
- agy may write the target file itself and prefix prose; diff the tree after every agy call, before apply.
- `ops restart` takes exactly one worker per call.
- `.venv-test` installs all packages editable, so subprocess cwd in seams can't fail a test; `locks.py` `parents[2]` unprovable by mutant.
- Exempt from the end grep: `ban_check.py`/`capture.py` browser UA strings, gallery-dl fingerprint `firefox:windows` (`config.py`, USER-GUIDE).
- Fallback trial 2026-10-09→10-11 PASSED; StoragEDGE back 2026-10-11 15:28 (`/dev/sda2`), primary path verified live 20:25.
- `recover_suite_db` verified by read-only `--force` dry run on live DB; `migrate_split_roots --src` is required.
