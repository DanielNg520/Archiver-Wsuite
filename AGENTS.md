# AGENTS.md

Repo-root reference for coding agents. Sections below tagged `triapi:plan` are execution plans appended by TriAPI's Tier 1 planner -- see the run's own checklist for progress.

## Session carryover (2026-09-17, resume here)

Pipeline health-pass after the two hand-fixes below. **All three services
running and nominal** (`ops health`) as of this writing -- dispatcher
draining (258 pending, 0 failed, ~71 sent/24h), recorder mid-recording,
archiver scanning. Nothing left broken; items below are follow-ups, not
blockers.

**Next actions, in order (read the numbered detail below each first):**
1. Run `ops health` first thing -- confirm the three services are still
   nominal and the dispatcher queue is still draining (was ~258 pending,
   ETA ~3h at session end) before touching anything else.
2. Watch for the next age-restricted TikTok live (any user, not just
   `@weejiwooji`) and confirm the recorder actually records it -- this is
   the one unverified fix from today (item 1 below). Check
   `~/.local/log/recorder.out.log` for a `headless-browser fallback`
   line followed by a successful record, not another `BrowserType.launch`
   error.
3. Then work the "Repo audit backlog" section further down this file,
   top-down by priority (High -> Medium -> Low) -- untouched since it was
   filed, still the main queued work. Follow the TriAPI dispatch rule
   (`~/.claude/CLAUDE.md`) for anything that's an actual code change.
4. Two smaller follow-ups from today, either can be picked up any time
   (both already filed under "Known tech debt" below): the missing
   `state.py` reconnect-loop regression test, and verifying the
   dispatcher's now-unconditional `backoff_s` doesn't over-penalize
   ordinary transient failures (TriAPI task `b41afde0`).
5. Items 2 (`content_hash` backfill) and 3 (`circuit` table junk rows)
   below are closed out / confirmed-harmless -- no action needed, kept
   here only as a record of what was checked and why it's not a bug.

1. **Playwright `chromium_headless_shell` was manually installed**
   (`~/.cache/ms-playwright/chromium_headless_shell-1243/`, machine-local,
   NOT in this repo) after Playwright's own installer kept timing out
   (30s Node HTTP timeout) against a CDN `curl` fetched in 15s with no
   issue. Fixes the age-restricted-TikTok headless-browser fallback that
   was bench-cooldown-looping on `@weejiwooji`. **Not yet confirmed
   against a real live age-restricted stream** (none occurred since the
   fix) -- watch for the next one. Per-machine state (gitignored
   `~/.cache`), not Hivemind-synced; if another machine hits the same
   Playwright-CDN-timeout, it needs the same manual pull.
2. **`archiver backfill` was run** (existing tool, not a code change) for
   the 84 `content_hash IS NULL` rows `ops health` flagged. Result: all 84
   are `status='sent'` with their source file already deleted
   post-upload -- hash is permanently unfillable, not a bug. The
   `ops health` warning line is a false-positive nag for this case;
   low-priority follow-up would be excluding already-`sent` rows from
   that warning in `ops/ops/health.py`.
3. **`suite.db`'s `circuit` table has ~44 junk rows** (of 47 total) from
   the 2026-07-28 Windows→Linux DB-unification migration
   (`core/core/migrate.py`'s `circuit`-copy loop, ~line 184: reads the old
   `archiver.db`'s `circuit` table by column name into the new schema,
   but the old table apparently had a different column layout, so
   `platform` came through `NULL`/mismatched on many rows -- e.g. rows
   with `platform=NULL, consecutive_fails='archiver', last_error='Sean.vc'`,
   or a content-hash string sitting in the `platform` column).
   `PRAGMA integrity_check` is clean; **confirmed harmless** -- every
   current read of `circuit` (`core/core/store.py`'s `bump_circuit_fail`/
   `trip_circuit`/`circuit_state`) is a parameterized `WHERE platform=?`
   lookup for a real platform name, never an unfiltered `SELECT *`, so
   the junk rows are never touched. Low-priority cleanup candidate
   (`DELETE FROM circuit WHERE platform NOT IN
   ('instagram','x','tiktok')` after a backup), not urgent.
4. Verified NOT a bug: recorder's frequent (~7-20min) systemd stop/start
   pairs are the existing one-shot `recorder record` reload-on-finish
   behavior (2026-09-15 fix, section below), not a crash loop --
   `journalctl` shows clean Stop/Start pairs, no failure exits.

## recorder: stall-guard rc=-3 never reached terminal check (2026-09-17)

**Process note:** hand-edited directly, not dispatched through TriAPI,
per user sign-off in the moment (same carve-out as the dispatcher
`backoff_s` fix below) -- caught live and broken on `origin/main`.

Code-review audit of the 2026-09-17 stall-guard commit found
`recorder/recorder/state.py`'s `_wait_for_recording_done()` only treated
`rc == -1`/`-2` as terminal; the new `rc == -3` (stall guard tripped) fell
through to `_confirm_still_live()`, which reports `True` for the same
idle-but-live room, so it just reconnected -- and `zero_byte_streak`
resets every cycle since the stalled segment had bytes > 0, so no other
guard caught it either. Net effect: the stall guard chopped the runaway
session into repeating ~5-8min stall-then-reconnect cycles instead of
ending it -- the exact unbounded-session bug it was written to fix.
Fixed: `rc == -3` now breaks the reconnect loop like `-2` does.
285/285 seams + 22/22 recorder selftest checks still pass (neither
exercises this reconnect-loop path either way -- still a coverage gap).

## Repo audit backlog (2026-09-17, not started)

Full repo audit after the recorder stall-guard fix below. Merged in from
`improvements.md` (deleted 2026-09-18 per this repo's doc-hygiene rule --
one `AGENTS.md`, no separate backlog file). Verified during the merge:
`connection_fix.md` already has a `## Resolution (2026-09-05)` section, so
the four stale `triapi:plan` blocks that used to sit at the bottom of this
file (each asking to append it) were pruned. Work top-down by priority,
follow the TriAPI dispatch rule per `~/.claude/CLAUDE.md`, run
`tests/test_seams.py` before calling anything deployed, then replace the
matching bullet below with a dated section (see style above).

High:
- [ ] `archiver/` has thin test coverage for its size: 2 selftests total vs
  `orchestrator.py` (1248 lines, concurrent scan/priority logic) and
  `cli.py` (2258 lines, largest file in repo). Check whether
  `tests/test_seams.py` Seam 34 already covers orchestrator's
  staleness-first ordering before adding a selftest.
- [ ] Windows/Linux parity drift is wider than tracked below (every
  `windows/<pkg>` tree is smaller than its Linux counterpart, not just the
  two gaps already listed). Needs a systematic file-by-file diff if that
  tree is ever revisited; not urgent, unexercised on this machine.
- [ ] `archiver/archiver/orchestrator.py:370` swallows an `on_user`
  callback exception with a bare `except Exception: pass`, no comment --
  two identical swallows nearby (~777, ~823) explain themselves, this one
  doesn't.

Medium:
- [ ] `archiver/archiver/cli.py` (2258 lines) and
  `dispatcher/dispatcher/send.py` (1402 lines) are split candidates on
  size alone; no reported bug traced to size itself, scope only if either
  is touched again.

Low:
- `build/lib/` dirs under all 5 packages (+ windows mirrors) are stale,
  gitignored clutter -- safe to `rm -rf` any time.
- No `shell=True` usage, no hardcoded secrets, no stray
  TODO/FIXME/XXX/HACK markers found repo-wide.

## recorder: stall guard for multi-hour zero-progress sessions (2026-09-17)

Investigated a report of a single TikTok user's recording "dragging on for
5-7 hours with no true recording." `recorder/recorder/capture.py`'s existing
dead-stream guard (`StreamCapture.wait()`) only checks `_recorded_bytes() ==
0` -- literally zero bytes ever written, checked once `start_timeout_s`
(120s) after launch. It has no ongoing stall/throughput check: once a single
byte lands, that condition is permanently false for the rest of the run no
matter how the connection behaves afterward. `max_session_minutes` also
defaults to `0` (uncapped). So a TikTok room that keeps `is_live()` reporting
true (host idle/AFK, a throttled edge, a near-silent keepalive) while
producing almost no data was never caught by anything.

Confirmed with real production log evidence (`/home/dyne/.local/log/recorder.out.log`)
before fixing: `@shuji_muscle` ran 19h39m for 41 MB, `@justinsgainz` ran
16h23m for 7 MB, `@traideptenten` 17h20m for 554 MB, `@arikoufit` 12h08m for
267 MB -- all with 0-1 reconnects (a single long-held connection, not the
reconnect-loop path). Caught a live instance mid-fix: `@justinsgainz` was
recording with its output flat at 860 MB for 3h+ at the moment of the
`recorder` service restart below; the restart finalized/enqueued the 860 MB
cleanly (startup sweep: `already-queued 1`, nothing lost) and cut the dead
tail.

Fix: `StreamCapture` gained a `stall_timeout_s: float = 300.0` constructor
parameter (`recorder/recorder/capture.py`). `wait()` now tracks, alongside
the existing dead-stream guard, the last-seen byte count and when it last
grew; once bytes have started flowing (>0), if they stay flat for
`stall_timeout_s` seconds, `wait()` terminates the capture the same way the
dead-stream guard does and returns a new exit code `-3` (0 disables the
check, matching the existing `start_timeout_s` convention). This is
independent of and does not replace the zero-byte dead-stream guard -- they
coexist in the same poll loop. `RecorderConfig` gained a matching
`stall_timeout_s` field (`recorder/recorder/config.py`, default 300.0, `
[recorder]` config.toml key, no env var), wired through both
`StreamCapture(...)` construction sites in `recorder/recorder/cli.py`.
Regression test: `recorder/recorder/_selftest_capture.py`'s new
`test_stall_guard_terminates_on_no_growth` (all 22 checks pass, `PYTHONPATH=
core:recorder <recorder-tool-venv>/bin/python3 -m recorder._selftest_capture`).

Built via TriAPI's rebuild pipeline (`~/Documents/Coding/TriAPI/rebuild/`,
tasks `32888884`, `2a72b473`, `e23a8f98`, `ddd0bd9f`) -- DeepSeek wrote each
piece from a Claude-written spec, Claude audited and applied every reply by
hand (one real bug caught in each of two replies: a `wait()` log-message
typo mangling "yt-dlp" as "ytd-lp" in the capture.py task, and the test
task's writer subprocess had no keep-alive loop so the parent would exit
before the stall window could ever fire -- fixed before applying). 285/285
`tests/test_seams.py` seams still pass. Deployed live: `recorder` reinstalled
(`uv tool install --force --editable ./recorder --with-editable ./core`,
verified `core` stayed the editable checkout from a neutral cwd) and
restarted (`ops restart recorder`).

Not touched: `windows/recorder/recorder/capture.py`/`config.py`/`cli.py` (the
unexercised Windows mirror) -- same parity-drift pattern as the existing
dispatcher entry under "Known tech debt" below; flag there too if anyone
ever revisits the Windows tree.

## recorder daemon reload fixes (2026-09-15)

Two bugs in `recorder/recorder/cli.py`'s `cmd_record` / `recorder/recorder/cli.py`'s
CLI-arg construction, found while investigating an unrelated recorder outage:

1. `cmd_record`'s stop-timeout path (`log.error("recorder did not stop in time")`)
   returned early without calling `_reload_recorder_service()`, unlike every other
   exit from the function — one failed auto-stop left the recorder service
   permanently disabled with no automatic recovery. Fixed: reload before that
   `return 1`, same as the other exit paths.
2. Callers invoking one-shot `recorder record --user <handle>` with `--no-reload`
   leave the persistent watch-loop service down after the recording finishes —
   that flag is for a caller that intends to manage the reload itself; a caller
   that doesn't should omit it so the default (reload-on-finish) applies.

Recovery if the service is ever found down with a stale `tiktok.lock`:
`ops restart recorder`, then if the lock is still held, check the pid inside it
is dead and `rm` it (see `ops/RUNBOOK.md`'s "Recorder is stuck" section).

## dispatcher connection/stall fixes (2026-09-05, see connection_fix.md)

`dispatcher/dispatcher/config.py`'s `DispatcherConfig` gained five fields:
`use_ipv6` (bool, default `False`, env `USE_IPV6`),
`fast_upload_connect_timeout_s` (float, default `8.0`, env
`FAST_UPLOAD_CONNECT_TIMEOUT_S`), `fast_upload_connect_retries` (int,
default `2`, env `FAST_UPLOAD_CONNECT_RETRIES`),
`fast_upload_connect_stagger_s` (float, default `0.1`, env
`FAST_UPLOAD_CONNECT_STAGGER_S`), and `stall_backoff_s` (float, default
`300.0`, env `STALL_BACKOFF_S`). All five are wired through
`dispatcher/dispatcher/cli.py` into both `TelethonSendStrategy(...)`
construction sites.

`core/core/schema.py` is at `SCHEMA_VERSION = 5`: migration 5 adds
`items.retry_after` (nullable TEXT) -- NULL/past means claimable now, a
future ISO timestamp hides the row from `core/core/store.py`'s
`claim_next()`/`claim_batch()` until it passes.
`ItemStore.mark_failed()` gained an optional `backoff_s` parameter that
stamps `retry_after` only on the non-terminal pending transition.
`core/core/models.py`'s `Item` dataclass has a matching `retry_after: str
| None = None` field (required — `Item.from_row()` does `SELECT *`, so
every row now includes this column).

`dispatcher/dispatcher/send.py`'s `SendResult` gained a typed
`stalled: bool = False` field, set by `_send_with_retries` only when
retries were exhausted via the stall-watchdog
`TimeoutError`/`asyncio.TimeoutError` path (never by matching `error`
text). `dispatcher/dispatcher/drain.py` passes it through to
`mark_failed`'s `backoff_s`.

Regression coverage: `tests/test_dispatcher_stall_backoff.py` (new file;
does not touch `tests/test_seams.py`, which stays oversized — see
`connection_fix.md`'s Resolution section and "Known tech debt" below).

## dispatcher: backoff_s gated too narrowly on `result.stalled` (2026-09-17)

**Process note:** this one-line `drain.py` change was hand-edited directly by
the assistant session rather than dispatched through TriAPI's rebuild pipeline
(the normal rule per `~/.claude/CLAUDE.md`). This is a documented exception,
not a violation: the user was asked explicitly ("How do you want to unstick
the queue right now?") and answered "fix it at root cause by hand if needed"
in the same conversation, which is the CLAUDE.md-carved-out
sign-off-in-the-moment case. A TriAPI task (`b41afde0`, see below) was
separately filed for the regression-test half of this work, which does go
through the normal pipeline.

The 2026-09-05 fix above only applied `backoff_s` when `SendResult.stalled`
was `True` (stall-watchdog `TimeoutError` path). `dispatcher/dispatcher/drain.py`'s
whole-batch-failure `else:` branch (~line 621, `mark_failed(...)` call) had
`backoff_s=config.stall_backoff_s if result.stalled else None` — so an ordinary
`ConnectionError`/`OSError` failure (the "network err attempt N/4" path in
`send.py`'s `_send_with_retries`, which never sets `stalled=True`) went back to
`status='pending'` with `retry_after=None`, immediately reclaimable. Because
`claim_batch` anchors on the earliest-anchored (platform, username) cluster, a
poisoned album with a persistent connection issue won every `claim_batch` call
ahead of the rest of the queue, forever — observed starving 300+ pending items
for ~12h (one tiktok user's 2-item album, 2026-09-16 20:24 → 2026-09-17 08:20),
tripping the circuit breaker every ~2h without ever losing its head-of-queue
spot. Fixed: that branch now always passes `backoff_s=config.stall_backoff_s`
regardless of `result.stalled` — the whole branch is already the SYSTEMIC
bucket (network/stall/unknown; see its own comment), so the backoff should
apply uniformly. Deployed live via `systemctl --user restart
com.duy.dispatcher.service` (both `dispatcher` and `core` are editable-installed
into this repo checkout, so no `uv tool install --force` reinstall was needed).
285/285 `tests/test_seams.py` seams still pass.

Regression coverage: TriAPI task `b41afde0` queued to add a seam test asserting
`retry_after` gets set on a plain (non-stalled) systemic failure — not yet
landed as of this writing; check `tests/test_seams.py` for a seam referencing
this fix before assuming it's covered.

`windows/dispatcher/dispatcher/drain.py` (the unexercised Windows mirror) still
has the OLD signature — its `mark_failed(...)` call doesn't pass `backoff_s` at
all (predates even the 2026-09-05 fix). Not backported here; flagged as
existing parity drift, see "Known tech debt" below.

## Known tech debt

- [ ] **No regression test exercises `state.py`'s reconnect loop with
  `StreamCapture.wait()`'s exit codes.** The rc=-3 terminal-check bug above
  shipped past both `test_seams.py` and the recorder selftest because both
  only check `wait()`'s return value in isolation, never
  `_wait_for_recording_done()`'s handling of it. Needs a test driving the
  loop with a fake capture returning -1/-2/-3 and asserting each is
  terminal.

- [ ] **`dispatcher/dispatcher/drain.py`'s unconditional `backoff_s`
  (2026-09-17 fix) may over-penalize ordinary transient failures** — every
  whole-batch failure now gets the full `stall_backoff_s` (300s default),
  not just the previously-targeted poisoned/stalled case. Intentional per
  the added comment, but TriAPI task `b41afde0` (dedicated regression test)
  hasn't landed, so the queue-throughput cost on one-off `ConnectionError`s
  is unverified.

- [ ] **`windows/recorder/recorder/capture.py`/`config.py`/`cli.py` are missing
  the 2026-09-17 stall guard** (`stall_timeout_s` on `StreamCapture`, the
  matching `RecorderConfig` field, and the two `StreamCapture(...)` call-site
  wirings — see that section above). Same parity-only, not-exercised-here
  reasoning as the dispatcher `retry_after`/`backoff_s` gap below; not
  backported.

- [ ] **`tests/test_seams.py` is oversized** (156,987 chars). Splitting it
  into smaller cohesive modules along its existing "── Seam N" boundaries
  was attempted twice via TriAPI's automated dispatch (external supervisor
  repo, not part of this codebase) during the 2026-09-05 connection fix,
  and both times failed immediately with zero actual tier attempts made —
  looks like a structural limitation of that pipeline's patch mechanism
  for a create-multiple-files-and-delete-one refactor, not a content
  problem with this file. Deferred rather than blocking that fix. Needs
  either a manual split (preserve every test's behavior verbatim, group by
  subsystem) or a smarter automated approach that doesn't require deleting
  the original file in the same patch as creating its replacements.

- [ ] **`core/core/manual_delete.py`'s `_default_trash` docstring says "Recycle
  Bin"** — a Windows-era leftover; `send2trash` actually targets the
  freedesktop trash on Linux. Found during the 2026-09-11 doc pass, not fixed
  (in-code docstring, out of scope for a docs-only pass; low severity — the
  behavior is correct, only the comment is stale).

- [ ] **`windows/dispatcher/dispatcher/drain.py` is missing the whole
  `retry_after`/`backoff_s` stall-backoff mechanism** from the 2026-09-05 fix
  (and thus also missed the 2026-09-17 follow-up above) — its `mark_failed(...)`
  call in the whole-batch-failure branch has no `backoff_s` kwarg at all. The
  Windows tree is parity-only and not exercised on this machine (Linux/systemd
  is the deployment target — see the repo README), so this was left as-is
  rather than backporting the feature; would need the matching `config.py`
  field, `core/core/store.py` changes, and `SendResult.stalled` plumbing too if
  ever picked up.

- [ ] **`CLAUDE.md`'s config-root path is stale for the current logging setup.**
  It says all per-app state including logs lives under `<repo>/.config/<app>`;
  in practice the live dispatcher/archiver/recorder services' actual DB/config
  root is `/home/dyne/.archive/.config/<app>` (per the systemd units' own
  `--config-home`/env), and their `StandardOutput`/`StandardError` log files
  are redirected to `/home/dyne/.local/log/<app>.{out,err}.log` — NOT under
  `.config/archiver-suite/logs/`, which is a stale copy frozen since the
  2026-07-28 Windows→Linux migration (its `dispatcher.out.log` etc. haven't
  been written to since). Found 2026-09-17 while diagnosing a dispatcher
  starvation bug; check `cat /home/dyne/.config/systemd/user/com.duy.<app>.service`
  for the actual paths before trusting either doc.

- [ ] **`recorder/recorder/cookie_refresh.py` (2026-09-11 feature) has two minor
  gaps, found in the same day's audit, not yet fixed:** `_refresh_async`'s
  `Path(cookies_file).write_text(...)` isn't atomic — a crash mid-write could
  truncate the live TikTok cookie file (this repo already has a temp-file +
  `os.replace` convention for exactly this, see `recorder/recorder/cli.py`'s
  config-TOML writer). And `_playwright_to_netscape` never re-adds the
  `#HttpOnly_` line prefix that `tiktok_browser._netscape_to_playwright` reads
  on the way in, so `sid_tt`/`sessionid` silently lose their HttpOnly marking
  on every refresh cycle. Both low-severity, not blocking; queued as follow-up
  TriAPI tasks if wanted, not hand-fixed.

## Doc corrections (2026-09-11)

- `REFACTOR_PLAN_bans_and_paths.md` was still headed "Status: PLAN — ready
  to implement" though every phase of both refactors (quarantine,
  account_gone, recorder ban subsystem, manual-delete lifecycle,
  `routes_dir` split, `migrate_split_roots.py`) is already shipped and live
  (verified by direct file inspection). Added a `HISTORICAL` callout
  matching `WINDOWS_PORT.md`'s convention; body left unchanged.
- `Tiktok_auto_refresh.md` proposed reinventing infra that already exists:
  new DB helpers instead of `ItemStore.meta_get`/`meta_set` (already
  designed for this — `core/core/schema.py`'s `metadata` table docstring
  says so), a Firefox persistent-profile browser instead of
  `platforms/tiktok_browser.py`'s existing ephemeral-Chromium pattern (which
  also sidesteps the profile-lock problem the draft invented a workaround
  for), and a nonexistent `StreamState.OFFLINE`/`get_active_stream_count()`
  hook instead of the real `RecorderState.HANDOFF` transition in
  `state.py`. Corrected via an external LLM dispatch (docs-worker role);
  Claude wrote the task spec + verified the result, did not write the doc
  text itself.

