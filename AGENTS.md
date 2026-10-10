# AGENTS.md

Single agent doc for this repo. Read first. Traps: `CLAUDE.md`. Architecture: `README.md`. Code map: `DESIGN.md`.

## Findings

- None open.

## Commands

- Tests: `python tools/setup_test_venv.py` once, then `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 .venv-test/bin/python3 tests/test_seams.py` (303 checks).
- Selftests: `PYTHONPATH=core:recorder PYTHONUTF8=1 .venv-test/bin/python3 -m recorder._selftest_<name>`; same pattern per package.
- Deploy: all tools are editable `uv tool` installs; `ops restart <worker>` makes source edits live. `ops health` before and after.
- Reinstall (deps/entry points only): `uv tool install --force --editable ./<pkg> --with-editable ./core`; verify `core.__path__` from `/tmp`.
- Code changes go through TriAPI `rebuild/` dispatch.
- Line endings: LF only, enforced by `.gitattributes` (`* text=auto eol=lf`); renormalized 2026-10-10.
- Gated (2026-10-09): the 8 code dirs are in `~/.claude/dispatch-gate-paths.txt`; `.git/hooks/pre-commit` runs the ledger check. Hand fix: owner runs `dispatch-ledger-hand.sh`.
- Apply seams-verified edits with `--check "env PYTHONPATH=core:archiver:recorder:dispatcher:ops PYTHONUTF8=1 .venv-test/bin/python3 tests/test_seams.py" --cwd <repo>`; `--test` rolls back (no pytest counts).

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

- [ ] `archiver/` coverage: `cli.py` run/loop/config/migrate commands have no selftest (destructive guards covered by `_selftest_cli`); `run_stories` auth backoff untested (deferred by owner).
- [ ] Split candidates on size: `archiver/archiver/cli.py`, `dispatcher/dispatcher/send.py`; only if touched again.

## Decisions

- Recorder falls back to `~/.recorder/<user>/` when StoragEDGE is absent (owner, 2026-10-09).
- Burner TikTok account treated as NOT banned (cookies pass login check); no `tiktok.txt` refresh from Firefox (owner, 2026-10-09).
- StoragEDGE ejected for a multi-day live run of the `~/.recorder` fallback, started 2026-10-09 (owner, 2026-10-09).
- Fallback trial runs ~1 week; StoragEDGE stays unmounted until about 2026-10-16 (owner, 2026-10-09).
- Gate this repo's code dirs (dispatch-gate-paths + ledger pre-commit hook), like SemAI/TriAPI (owner, 2026-10-09).
- "Long outdated" means the whole repo: bring docs, deps and layout in line with the current global rules (owner, 2026-10-09).
- `requirements.txt` deleted; per-package `pyproject.toml` is the only dependency source (owner, 2026-10-09).
- `PROJECT_MAP.md` folded and deleted under the current rules (owner, 2026-10-09).
- Historical plan docs folded in and deleted; git history keeps them; `<repo>/.config` leftover trashed (owner, 2026-10-09).
- Linux (systemd) and macOS (launchd) are the only supported platforms (owner, 2026-10-09).
- `tools/recover_suite_db.py` is a generic recover/verify/swap; no backup-merge step (owner, 2026-10-09).
- Three already-applied one-shot path/config migration scripts deleted (owner, 2026-10-09).
- Anything Windows-only can be removed: CRLF → LF renormalize, trash `miki_status.py` + its `.gitignore` lines (owner, 2026-10-10).
- Orchestrator selftests cover disk-full purge, auth circuit, reconcile users; stories auth backoff deferred (owner, 2026-10-10).

## Ask owner

- None open.

## Index

- `core/core/` shared library: store/schema (`suite.db`), ingest, dedup, quarantine, media_prep, `platform/` OS seam.
- `archiver/` media-archiver: scheduled platform scans (X, TikTok, Instagram) via gallery-dl/yt-dlp, reconcile, orphaned routes.
- `dispatcher/` sole Telegram sender: claim/drain/send, fast upload, delete-after-upload.
- `recorder/` TikTok live recorder: `state.py` loop, `capture.py` yt-dlp, `platforms/` URL resolve + browser fallback, `startup_sweep.py`.
- `ops/` CLI: install/load/unload/restart/health/watch/update/logrotate; `RUNBOOK.md`.
- `tests/` `tests/test_seams.py` is the seams runner over `tests/seams/` (topic modules + `_harness.py`), plus the dispatcher stall-backoff test. `tools/` one-off migration/maintenance scripts.
- Docs: `README.md`, `DESIGN.md`, `USER-GUIDE.md`, `AUTOMATION.md`, `ops/RUNBOOK.md`, per-package `README.md`, `CLAUDE.md`.

## Carryover

Last audit: 2026-10-10 (through a47e7c0).
- 2026-10-09: `recording_roots` shipped via TriAPI (`tasks/archiver_recording_roots`); 296 seams, all recorder/archiver selftests, stall-backoff pass.
- 2026-10-09: deployed (`ops restart recorder archiver`); recorder startup sweep logs both roots.
- OPEN fallback trial: StoragEDGE UNMOUNTED 2026-10-09 (`udisksctl unmount`; power-off needs polkit). Live-config probe chose `~/.recorder/<user>`.
- During trial: route folders on `ROUTES_DIR` unreachable; drive data untouched (832M records, 3.0G routes).
- Trial check: `grep 'using fallback' ~/.local/log/recorder.out.log`; recordings in `~/.recorder/<user>/` must upload and get deleted.
- 2026-10-09 17:11-17:22 recorder service stopped; @daviddieal recorded meanwhile by a non-service run into `~/.recorder`, sent 17:23, deleted.
- End trial: remount (`udisksctl mount -b /dev/sdc2`, polkit, owner), `findmnt`; leftovers in `~/.recorder` are swept, no move-back.
- 2026-10-09: F1 re-verified FIXED (e1fcab4): every saved core had curl_cffi 0.14.0 loaded; zero crashes since 0.16.3 reinstall (2026-10-08 23:55).
- Suspected only: 4 `com.duy.dispatcher` SIGABRTs since 2026-09-19 (2 dumps, first-thread `select_epoll_poll_impl`); not curl_cffi, uninvestigated.
- 2026-10-09: wrap-up audit: `ItemStore.retry` (manual requeue) now clears `retry_after`; test `test_manual_retry_clears_backoff`. Reset-failed paths need nothing (failed rows never carry `retry_after`).
- 2026-10-09: F2 shipped (ce26403, `claim_batch` honors `retry_after`); 303 seams; dispatcher restarted. Live backoff not yet observed (needs a real send failure).
- Proposal: one `_READY` SQL fragment in `store.py` for the four `retry_after` filters plus the gated Python compare.
- 2026-10-09: `_selftest_reconnect.test_terminal_rcs_never_reconnect` covers rc -1/-2/-3 terminal without stop (TriAPI `archiver_terminal_rc`); mutant rc -3 caught.
- 2026-10-09: rules sanitation (6c87351): `.venv-test` rebuilt on curl-cffi 0.16.3 (was CVE-range 0.14.0); 303 seams.
- 2026-10-09: `setup_test_venv.py` passes `uv venv --clear` (TriAPI `archiver_venv_clear`); re-run verified.
- Open: drain backoff throughput cost on one-off `ConnectionError`s, unverified until a live send failure.
- DONE 2026-10-09: single-OS cleanup via TriAPI (`tasks/archiver_windows_rest`, 8dcb9c5..484912a); 303 seams + all selftests; workers restarted, health green.
- `termui.ensure_vt` deleted; `recover_suite_db` verified by read-only `--force` dry run on live DB (156,491 rows); `migrate_split_roots --src` now required.
- Exempt from the end grep: `ban_check.py`/`capture.py` browser UA strings, gallery-dl fingerprint `firefox:windows` (`config.py`, USER-GUIDE).
- Trap: `apply_dispatch` check timeout is 120s; the full suite (~124s) exceeds it. Check with seams + the file's selftests (~75s), full run after.
- Trial 2026-10-10: service fallback EXERCISED (04:39+): @thinh_kobe20 6 segments → `~/.recorder`, all `sent`, mp4s deleted; DEADSTREAM/ytdlp logs remain.
- Trial 2026-10-10: @19970609bun 861MB recorded into `~/.recorder`, sent 08:37, deleted. Fallback trial confirmed end to end.
- 2026-10-10: audit of 8dcb9c5..484912a clean except two `Task Scheduler` docstrings; fixed with F3.
- 2026-10-10: F3 fixed via TriAPI (`tasks/archiver_stale_tooling`, 8 files): `pipx`/Task Scheduler wording → `uv tool`/`ops update`/systemd.
- 2026-10-10: 121 CRLF files renormalized to LF; `miki_status.py` trashed. 303 seams, all selftests, stall-backoff pass.
- Trap: `apply_dispatch --response` takes the stored `logs/responses/<sha>.txt` path (from `call_deepseek` stderr), not the task `.out` copy.
- Expect: first `ops update` after 6fc7a6b sees every package changed (EOL-only fingerprint shift) and does one full drain/reinstall/restart; safe.
- 2026-10-10: `cookie_refresh.py` writes atomically (`_write_atomic`) and keeps `#HttpOnly_` (TriAPI `archiver_cookie_atomic`); selftest 23 checks, both mutants caught; recorder restarted.
- 2026-10-10: seams split (TriAPI `archiver_seams_split`): byte-range splitter moved 44 tests into `tests/seams/`; defs verbatim except two edits; 303 checks, output-diff identical.
- Trap: `.venv-test` installs all packages editable, so subprocess cwd in seams can't fail a test; `locks.py` `parents[2]` unprovable by mutant.
- 2026-10-10: `orchestrator.py:370` stories `on_user` swallow now carries the sibling comment (TriAPI `archiver_hook_comment`); 3 hook-guard sites, all commented.
- 2026-10-10: `archiver/_selftest_orchestrator.py` (TriAPI `archiver_orch_selftest`): 34 checks; 7/7 orchestrator mutants caught.
- 2026-10-10: fixed `_download_with_recovery`: `AccountGoneError` on the auth/ENOSPC retry now bans (was uncaught/`disk-full-unresolved`); B.6/B.7 fail on old code.
- 2026-10-10: all three workers restarted after `c5f215a`; health nominal. `archiver.err.log` malloc lines are pre-F1-fix (2026-10-08), not new.
- 2026-10-10 audit: `b8d7a9e` `#HttpOnly_` prefix broke `tiktok._parse_netscape_cookies` (dormant until next refresh); now delegates to `_netscape_to_playwright` (a47e7c0); recorder restarted.
- 2026-10-10: pending-age verified benign: every group under min batch 10 per media bucket; @ynxio218623 claimed at exactly 168h (20:46:42Z), sent 20:50:59Z.
- 2026-10-10: `ops health` null_hash counts unsent rows only (7c20ed4, TriAPI `archiver_nullhash_unsent`); 84 legacy sent rows with deleted files can't be hashed.
- 2026-10-10: `archiver/_selftest_cli.py` (bdfd7f7, TriAPI `archiver_cli_selftest`): 39 checks; 8/9 mutants caught, survivor equivalent (`DeletionGuard.delete` re-checks safebrake).
- Next session: `cli.py` run/loop coverage (Known tech debt); StoragEDGE remount ~2026-10-16.
