# AGENTS.md

Repo-root reference for coding agents. Sections below tagged `triapi:plan` are execution plans appended by TriAPI's Tier 1 planner -- see the run's own checklist for progress.

## `ops unload` couldn't kill a manually-triggered `recorder record` (2026-09-19)

User reported: a Telegram `/record` command (handled by an external bot, not
in this repo -- `dispatcher/` has no inbound command listener, so whatever
sends `recorder record --user X` is outside this codebase) left an orphaned
recorder process that `ops unload` could not stop.

Root cause: `ops unload`'s `cmd_unload` (`ops/ops/cli.py`) only ever called
`core.platform.service.unload` -- pure service-manager action (`systemctl
--user disable --now recorder.service` on Linux). A manual `recorder record
--user X` run (`recorder/recorder/cli.py:202` `cmd_record`) is never in that
unit's cgroup, so disabling the unit finds nothing to kill. Compounding it:
`ops/ops/health.py`'s `worker_pid()` (used everywhere ops looks for a
foreground/unmanaged worker pid) hardcoded `action="start"` when scanning the
process table, so it couldn't even *see* a `record`-argv process to report,
let alone kill it -- `ops health`/`ops watch` would show the recorder as "not
running" while one was live.

Fixed:
- `core/core/platform/process.py` gained `find_worker_pid_any(command,
  actions)`, trying each action in turn (both the Windows and POSIX
  `find_worker_pid` implementations already existed; this is a thin wrapper
  over either, no branch-specific rewrite needed).
- `ops/ops/health.py`'s `worker_pid()` now checks `("start", "record")` for
  the recorder specifically (other workers unchanged -- they have no
  manual-invocation subcommand).
- `ops/ops/cli.py`'s `cmd_unload` now also calls `core.platform.procgroup
  .terminate_pid` (the same primitive `recorder stop` already uses) on any
  pid `worker_pid` reports as `"foreground"`-owned, after the service-level
  unload -- so a manually-run process is SIGTERM'd directly instead of
  silently surviving.

**`/code-review high` caught two real bugs in the first cut, both fixed
same session before commit:**

1. `cmd_unload`'s SIGTERM relied on `worker_pid()`'s `owner` tag, which reads
   `job_state()` -- memoized 15s (`@_memo(min_ttl=15.0)`). A value cached
   moments before `_service.unload()` runs (e.g. by `ops update`'s earlier
   `_drain_worker` call in the same process) could still read `"running"`
   right after, misclassifying a real orphan as service-owned and silently
   skipping the kill -- defeating the fix in exactly the automated-update
   path it needs to work in. Fixed: added `health.foreground_pid(name)`,
   a cache-free variant that re-checks `_service.running_pid` LIVE instead of
   trusting the memo; `cmd_unload` now uses it instead of `worker_pid`.
2. Killing the orphan wasn't enough -- `recorder record`'s own exit path
   (`recorder/recorder/cli.py`'s `cmd_record`) reloads the service
   (`_reload_recorder_service()`, i.e. `ops load recorder`) by default when
   it finishes, on the assumption that an ordinary manual recording should
   hand the daemon back. That fired on this SIGTERM too, silently re-enabling
   the very service `ops unload` was asked to stop. Fixed: `ops unload`
   now touches a one-shot flag (`core.paths.recorder_suppress_reload_flag`,
   `core/core/paths.py`) right before the SIGTERM; `cmd_record`'s new
   `_should_reload()` helper checks + consumes it in both its normal
   `finally:` exit and the double-Ctrl-C hard-exit path, skipping the reload
   exactly once. Same mechanism/pattern as the dispatcher's existing
   `dispatcher_stop_flag`.

Two more findings from the same review, evaluated and left as-is:
- `core/core/platform/process.py`'s new `find_worker_pid_any` re-runs a full
  `ps` scan per action tried (no memo on the POSIX `find_worker_pid`, unlike
  the Windows backend) -- minor overhead on an already-infrequent path
  (`ops health`/`watch`/`unload`), not fixed.
- `recorder/recorder/state.py`'s pre-existing (not from this session)
  `_MIN_SEGMENT_BYTES` stub-discard guard also deletes a genuine short-but-real
  recording under 1MB, not just reconnect stubs -- real gap, but predates this
  session's work and is out of scope for the orphaned-process fix; left in
  "Known tech debt" below for a future pass.

Verified: 285/285 `tests/test_seams.py` seams + 24/24 `ops/ops/_selftest_health.py`
+ 22/22 recorder selftest checks pass after the review fixes. Manually
verified `find_worker_pid`/`find_worker_pid_any` against a spawned
`recorder record --user X`-shaped process (found by `record` action where the
old `start`-only lookup missed it), and the suppress-reload flag's
set/consume/re-arm cycle in isolation.
Hand-fixed directly (small, well-understood, hasn't gone through TriAPI yet --
flag if the user wants it dispatched properly). Committed as `a49cc06`.

**Deployed + confirmed against the real orphan (2026-09-19, same session).**
Found `pid 1215443` LIVE while checking `ops health`: a `recorder record
--user ingyongcuong` process, foreground-owned, mid-recording, systemd task
disabled -- the exact bug, not a simulation. Took it down with `recorder
stop` (its own existing SIGTERM path, not yet the new `ops unload` fix --
that binary wasn't reinstalled yet) rather than a hard kill, to let the
in-flight capture finalize: exited clean in 2s, `items` table confirms
`ingyongcuong_1789871896.mp4` (390MB) enqueued and already `sending` -- no
data lost. `_should_reload()` auto-reloaded the daemon (`ops load recorder`)
as designed since no suppress-flag was set (only `ops unload` sets one).
Then reinstalled `recorder` and `ops` (`--editable --with-editable ./core`
each, `core` verified still editable from `/tmp` for both), `ops restart
recorder` -- came back up service-owned, idle, healthy.

**That "next real orphan" arrived within the hour and exposed a genuine
self-inflicted regression in the fix above (2026-09-19, same session).**
A real `/record longdesaint88890703` command failed: `recorder record`'s own
existing self-cleanup step (it detects a running recorder via the pidfile and
shells out to `ops unload recorder` on itself before taking over) triggered
the new foreground-kill code, which then SIGTERM'd the `longdesaint`
invocation ITSELF (`exit -15`) -- confirmed via a background health/log
watcher running at the time, not guessed after the fact. Root cause:
`ops/ops/health.py`'s `_argv_pid` scanned the process table for ANY process
matching `recorder ... record`/`... start` argv, with no way to tell a
genuine leftover orphan apart from the very process that just asked `ops
unload` to clean up on its behalf -- at scan time the new process is already
alive in the table with matching argv, before it has installed its own
signal handler, so it can be the (only) match. Fixed: replaced the argv scan
for the recorder specifically with `_recorder_pidfile_pid()`, reading
`core.paths.recorder_pid()` directly -- that file still names the PREVIOUS
owner at the exact moment `cmd_record` calls `ops unload` on itself (it only
overwrites the file with its own pid afterward, once the old owner is
confirmed gone), so the race can't happen. Removed the now-unused
`find_worker_pid_any` from `core/core/platform/process.py` (dead code once
nothing needed the multi-action argv scan). Verified with a targeted
simulation: a fake "old owner" pid in the pidfile + a separate live process
with matching argv but NOT yet in the pidfile -- confirmed the lookup finds
only the pidfile's owner, never the live-but-unclaimed process. 285/285 seams
+ 24/24 ops selftest still pass. Reinstalled `ops` (core verified editable),
`ops load recorder` to restore the daemon (had been left disabled by the
incident) -- confirmed healthy again.

**Lesson for next time:** a "kill anything that looks orphaned" mechanism
triggered from INSIDE the same tool that's about to become the new owner is
inherently racy against argv/process-table matching; prefer a durable,
explicitly-owned marker (the pidfile) that the new owner hasn't claimed yet,
over re-deriving "who's the owner" from a live process snapshot.

**Known gap, not fixed:** `windows/` mirror of `ops`/`core.platform.process`
was not touched (same parity-drift pattern as other entries below;
unexercised on this machine).

## Session carryover (2026-09-19, resume here)

User reported "5am recording is all black" + "newer recording not
uploaded." Both diagnosed and the first fixed; the second is a live
external condition, not code.

1. **Fixed + audited: recorder queued sub-second reconnect stubs as if
   they were real recordings.** `@thinh_kobe20` hit a TikTok CDN HTTP 500
   at 06:15, causing 3 reconnects in 18s; the two reconnect-boundary
   segments (106KB, 3.4MB -- confirmed via DB `file_size_bytes`, files
   already deleted post-send so couldn't visually confirm, but sizes are
   consistent with near-zero content) got enqueued and uploaded as full
   "recordings" -- this is what looked black. `recorder/recorder/state.py`
   had no size guard anywhere between capture and DB insert (`_enqueue_job`
   -> `enqueue.py` -> `core.ingest.register_file`; the only existing
   floor is `core/core/stability.py`'s `MIN_FILE_BYTES = 100`, meant for
   corruption not junk clips). Fixed: `_enqueue_job` (`state.py:724`) now
   checks the file against `_MIN_SEGMENT_BYTES` (env
   `RECORDER_MIN_SEGMENT_BYTES`, default 1MB) **before** remuxing, and
   discards+unlinks (never enqueues, never spawns ffmpeg) anything
   smaller, logging `discarded_stub`. /code-review flagged the first cut
   for checking post-remux (wasting an ffmpeg subprocess per stub during
   the exact reconnect-storm case this targets, since `-c copy` remux
   can't change a stub's verdict) -- moved the check pre-remux, re-tested,
   redeployed. Scoped to the recorder package only -- `core.register_file`
   untouched since archiver/orphaned also call it and shouldn't inherit a
   recorder-only heuristic. 285/285 seams + 22/22 recorder selftest checks
   pass both times. Hand-fixed directly (small, well-understood, user
   signed off in the moment) rather than through TriAPI. Deployed twice:
   `uv tool install --force --editable ./recorder --with-editable ./core`
   (verified `core` stayed editable from a neutral cwd each time) +
   `ops restart recorder` (idle both times, confirmed running clean
   afterward).
2. **Resolved by restart, not a code bug: dispatcher was stuck uploading
   `thinh_kobe20_1789821350.mp4` (315MB) since ~10:02, blocking
   `19970609bun`'s 468MB file (queued 07:39) behind it for ~7h.**
   `claim_batch` anchors oldest-cluster-first, so the stuck item held the
   queue head. Root cause was NOT the backoff logic (that 2026-09-17 fix
   is working) -- it was a real Telegram connectivity problem: general
   internet was fine (0% loss to 1.1.1.1/8.8.8.8), TCP connected fine to
   all 3 Telegram DC IPs on :443, but sustained transfers
   (`SaveBigFilePartRequest`) kept failing (`IncompleteReadError: 0 bytes
   read`, connect `TimeoutError`s) -- traceroute showed the path to
   Telegram's Amsterdam DC with 220ms+ latency and several
   silently-dropping hops, consistent with congestion/packet loss on
   that international route. `ops restart dispatcher` forced fresh
   connections; the stuck file sent cleanly within ~4 min post-restart
   and the queue resumed draining normally (`19970609bun` no longer
   blocked). If this recurs, restart is the known unstick.
3. **Playwright `chromium_headless_shell` was manually installed**
   (`~/.cache/ms-playwright/chromium_headless_shell-1243/`, machine-local,
   NOT in this repo). Fixes the age-restricted-TikTok headless-browser
   fallback that was bench-cooldown-looping on `@weejiwooji`. **Still not
   confirmed against a real live age-restricted stream** -- watch for the
   next one, check `recorder.out.log` for `headless-browser fallback`
   followed by a successful record.
4. Closed out, no action needed: `content_hash` backfill (84 `sent` rows
   with source already deleted, hash permanently unfillable, not a bug)
   and `circuit` table junk rows from the 2026-07-28 migration (harmless,
   never read unfiltered) -- both confirmed 2026-09-17, kept only as a
   record of what was checked.

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

**2026-09-19 codebase-wide gap audit** (not a diff review -- separate
from the per-PR "Repo audit backlog" items above): re-checked
`core/core/hashing.py`/`dedup.py` (dedup funnel is properly indexed, no
N+1 risk at 154K+ rows), `dispatcher/dispatcher/fast_upload.py` (parallel
path has solid cleanup + exercised serial fallback -- this is what saved
the stuck-upload incident earlier today from data loss), `delete.py`'s
`maybe_delete` (re-reads status before unlink, no gap), and `store.py`'s
circuit-breaker methods (matches today's observed 60s-pause-after-8-fails
behavior exactly) -- all confirmed clean, no new findings. Full grep sweep
for silent `except:`/`except Exception: pass` and `shell=True` across all
5 packages found nothing beyond the orchestrator.py swallow already
listed above. Did not re-survey `cli.py`/`send.py` line-by-line (already
flagged for size/coverage above; out of budget for one pass).

## ops/health + recorder/watch.py fixes (2026-09-19)

Both High/Low findings from the codebase-wide gap audit above, fixed same
session, user signed off in the moment (hand-fixed, not TriAPI-dispatched,
same carve-out as the other hand fixes today).

1. **`ops/ops/health.py` had zero test coverage.** Added
   `ops/ops/_selftest_health.py` (24 checks) covering `_humanize_eta`
   (bucket boundaries), `_same_volume` (same-device, identical-path, and
   both-vanished-equal/different-string fallback cases), `_disk_fields`
   (real path vs unstat-able path), and `drain_eta_fields`/`queue_health`
   (built on a real `ItemStore`-created temp DB with `health.SUITE_DB`
   monkeypatched at it, same convention as
   `core/core/_selftest_drain_eta.py` -- no sqlite mocking; `@_memo()`'s
   `_DATA_TTL` defaults to 0.0 so no caching to work around). Caught one
   API surprise while writing it: `_memo`'s wrapper only accepts
   positional args, so `drain_eta_fields(window_minutes=60)` raises
   `TypeError` -- call it positionally (`drain_eta_fields(60)`); not fixed
   (out of scope, `ops health`'s own call site already does this
   correctly), just a landmine for future callers.
2. **`recorder/recorder/watch.py`'s `_VIDEO_SUFFIXES` was a hand-copied
   duplicate of `state.py`'s constant.** Replaced the duplicate literal
   with `from .state import _VIDEO_SUFFIXES`; confirmed no import cycle
   (`state.py` doesn't import `watch.py`).

285/285 seams still pass, both selftests pass. Deployed: `ops` reinstalled
(`uv tool install --force --editable ./ops --with-editable ./core`, `core`
verified still editable from a neutral cwd) -- no restart needed, `ops` is
a CLI invoked fresh each call, not a systemd service. `recorder`
reinstalled the same way + `ops restart recorder` (was idle, confirmed
running clean afterward).

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

