# AGENTS.md

Repo-root reference for coding agents. Sections below tagged `triapi:plan` are execution plans appended by TriAPI's Tier 1 planner -- see the run's own checklist for progress.

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

## Known tech debt

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

<!-- triapi:plan run_id=20260905-060103-e92639 start -->
## TriAPI Plan (run 20260905-060103-e92639, appended 2026-09-05)

1. Phase A: Fast connect timeout for parallel upload senders
- [ ] `dispatcher/dispatcher/config.py`: Add `fast_upload_connect_timeout_s` (default 8.0, env `FAST_UPLOAD_CONNECT_TIMEOUT_S`), `fast_upload_connect_retries` (default 2, env `FAST_UPLOAD_CONNECT_RETRIES`), and `fast_upload_connect_stagger_s` (default 0.1, env `FAST_UPLOAD_CONNECT_STAGGER_S`) to `DispatcherConfig`, matching the existing `upload_connections` pattern. Verify with: `python -m py_compile dispatcher/dispatcher/config.py`
- [ ] `dispatcher/dispatcher/fast_upload.py`: Add `connect_timeout` and `retries` parameters to `_connect_sender()` and `upload_file()`/`_parallel_upload()`, passing them through to `MTProtoSender(...)`. In `_parallel_upload`'s worker-connect loop, add a stagger (`await asyncio.sleep(fast_upload_connect_stagger_s)`) between opening each successive sender to reduce simultaneous SYN bursts. Verify with: `python -m py_compile dispatcher/dispatcher/fast_upload.py`
- [ ] `dispatcher/dispatcher/send.py`: Add `connect_timeout`, `retries`, and `connect_stagger_s` parameters to `TelethonSendStrategy.__init__` (defaulting to the same values). Store them and pass them into the `fast_upload.upload_file(...)` call alongside `connections=self._upload_connections`. Verify with: `python -m py_compile dispatcher/dispatcher/send.py`
- [ ] `dispatcher/dispatcher/cli.py`: Update the two `TelethonSendStrategy(...)` construction sites (which currently pass `upload_connections`) to also pass the three new `fast_upload_connect_*` fields from `config`. Verify with: `python -m py_compile dispatcher/dispatcher/cli.py`

2. Phase B: Opt-in IPv6
- [ ] `dispatcher/dispatcher/config.py`: Add `use_ipv6: bool = False` to `DispatcherConfig`, sourced from the environment (`USE_IPV6`). The default must remain `False`. Verify with: `python -m py_compile dispatcher/dispatcher/config.py`
- [ ] `dispatcher/dispatcher/send.py`: Add `use_ipv6: bool = False` to `TelethonSendStrategy.__init__`, stored as `self._use_ipv6`. In `_build_client()`, pass `use_ipv6=self._use_ipv6` to the `TelegramClient(...)` constructor alongside `auto_reconnect=False` and `connection=KeepAliveConnectionTcpFull`. Verify if fast_upload's borrowed senders inherit this and leave a comment noting the finding. Verify with: `python -m py_compile dispatcher/dispatcher/send.py`
- [ ] `dispatcher/dispatcher/cli.py`: Update both `TelethonSendStrategy(...)` construction sites to pass `use_ipv6=config.use_ipv6`. Verify with: `python -m py_compile dispatcher/dispatcher/cli.py`
- [ ] `dispatcher/README.md`: Document the new `USE_IPV6` environment variable alongside existing dispatcher `.env` knobs. Verify with: `grep -q USE_IPV6 dispatcher/README.md`

3. Phase C: Per-item stall backoff
- [ ] `core/core/schema.py`: Bump `SCHEMA_VERSION` to 5. Append a new migration dictionary to the `_MIGRATIONS` list with `target_version: 5` and the SQL statement `"ALTER TABLE items ADD COLUMN retry_after TEXT"`. Do not alter `ITEMS_DDL`. Verify with: `python -m py_compile core/core/schema.py`
- [ ] `core/core/store.py`: In `claim_next()` and `claim_batch()`, append `AND (retry_after IS NULL OR retry_after <= ?)` to the pending-row `SELECT`'s `WHERE` clause, binding `now_iso()` as the parameter. In `mark_failed()`, if the new status resolves to `'pending'` and `backoff_s` is provided, compute `retry_after` as an ISO timestamp `backoff_s` seconds in the future (matching how `reset_stuck_sending()` computes its cutoff) and set it in the `UPDATE`. If the new status is `'failed'`, leave `retry_after` as `NULL`. Verify with: `python -m py_compile core/core/store.py`
- [ ] `dispatcher/dispatcher/send.py`: Add a typed flag `stalled: bool = False` to the `SendResult` class. In `_send_with_retries`, track if any attempt exits via the `except (TimeoutError, asyncio.TimeoutError)` stall-watchdog branch; if the final attempt fails this way, set `stalled=True` on the returned `SendResult`. Do not use error substring matching. Verify with: `python -m py_compile dispatcher/dispatcher/send.py`
- [ ] `dispatcher/dispatcher/config.py`: Add `stall_backoff_s: float = 300.0` to `DispatcherConfig`, sourced from `env.opt_float('STALL_BACKOFF_S', min_value=0.0)`. Verify with: `python -m py_compile dispatcher/dispatcher/config.py`
- [ ] `dispatcher/dispatcher/drain.py`: In the batch failure loop's `else:` branch, update the `store.mark_failed(...)` call to pass `backoff_s=config.stall_backoff_s if result.stalled else None`. Verify with: `python -m py_compile dispatcher/dispatcher/drain.py`

4. Phase D: Tests
- [ ] `tests/test_seams.py`: Add regression tests to verify that `claim_next`/`claim_batch` honor `retry_after`, `mark_failed` correctly sets `retry_after` only on pending transitions when `backoff_s` is given, `SendResult.stalled` is set exclusively on stall exhaustion, `DispatcherConfig` loads new fields with correct defaults, and `TelethonSendStrategy` properly passes `use_ipv6` to `TelegramClient` via mock inspection. Verify with: `python tests/test_seams.py`
- [ ] `connection_fix.md`: Append a short "resolution" note at the end of the file pointing to the implemented commit/fixes, without altering the existing incident analysis. Verify with: `tail -n 10 connection_fix.md`
<!-- triapi:plan run_id=20260905-060103-e92639 end -->

<!-- triapi:plan run_id=20260905-070455-c6136e start -->
## TriAPI Plan (run 20260905-070455-c6136e, appended 2026-09-05)

1. Phase A: Finish the config wiring
- [ ] `dispatcher/dispatcher/config.py`: Add `use_ipv6: bool = False`, `fast_upload_connect_timeout_s: float = 8.0`, `fast_upload_connect_retries: int = 2`, and `fast_upload_connect_stagger_s: float = 0.1` to `DispatcherConfig`. Source them from environment variables `USE_IPV6` (using `env.opt_bool`), `FAST_UPLOAD_CONNECT_TIMEOUT_S`, `FAST_UPLOAD_CONNECT_RETRIES`, and `FAST_UPLOAD_CONNECT_STAGGER_S` (using `env.opt_float`/`env.opt_int` as appropriate). Verify with: `python -m py_compile dispatcher/dispatcher/config.py && python3 -c "from dispatcher.dispatcher.config import DispatcherConfig; import dataclasses; names={f.name for f in dataclasses.fields(DispatcherConfig)}; assert {'use_ipv6','fast_upload_connect_timeout_s','fast_upload_connect_retries','fast_upload_connect_stagger_s'} <= names, names"`
- [ ] `dispatcher/dispatcher/cli.py`: Update both `TelethonSendStrategy(...)` construction sites to additionally pass `connect_timeout=config.fast_upload_connect_timeout_s`, `retries=config.fast_upload_connect_retries`, and `connect_stagger_s=config.fast_upload_connect_stagger_s`. Leave `upload_connections` untouched. Verify with: `python -m py_compile dispatcher/dispatcher/cli.py`

2. Phase B: Split the oversized test file
- [ ] `tests/test_seams.py`: Split this oversized file (156,987 chars) into smaller modules grouped logically by the existing "Seam N" boundaries (grep for `── Seam `). Create targeted files (e.g. `tests/test_seams_lock.py`, `tests/test_seams_queue.py`) while preserving all test behavior verbatim without rewriting or truncating. Delete the original oversized file. Verify with: `wc -c tests/test_seams*.py` (confirming all output files are under 73728 chars) and `for f in tests/test_seams*.py; do PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 python "$f" || exit 1; done`
- [ ] `CLAUDE.md`: Update the testing section documentation line that specifies how to run tests (previously `python tests/test_seams.py`) to reflect the new split test file execution pattern. Verify with: `grep "test_seams" CLAUDE.md`

3. Phase C: Add regression tests for stall backoff
- [ ] `tests/test_dispatcher_stall_backoff.py`: Add a new regression test module. Implement coverage verifying: `claim_next()`/`claim_batch()` honor `retry_after` (claims NULL or past timestamps, skips future timestamps); `mark_failed()` sets `retry_after` exclusively on the pending transition when `backoff_s` is given; `SendResult.stalled` is True only upon exhausting `_send_with_retries` via `TimeoutError`/`asyncio.TimeoutError` and False otherwise; `DispatcherConfig` loads new fields with exact correct defaults (False, 8.0, 2, 0.1, 300.0) from the environment; and `TelethonSendStrategy._build_client` passes `use_ipv6` to the `TelegramClient(...)` constructor (using mock inspection, no live connection). Verify with: `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 python tests/test_dispatcher_stall_backoff.py`
 
4. Phase D: Close the incident record
- [ ] `connection_fix.md`: Append a "## Resolution" section at the end of the document without altering any existing incident analysis content above it. Summarize Fix 1 (fast connect timeout + retries, plus a staggered-connect delay replacing reduced connections), Fix 2 (opt-in USE_IPV6, default off), and Fix 3 (per-item retry_after stall backoff, noting the 5.3h circuit breaker root cause). Record that Fix 4 (reducing UPLOAD_CONNECTIONS to 4) was rejected per user instruction and replaced by the stagger. Add a note that the schema migration defect from an earlier attempt was caught by human review and hand-corrected. Verify with: `tail -n 20 connection_fix.md | grep -q "^## Resolution"`
- [ ] `AGENTS.md`: Update the document to detail the four newly present config fields (`use_ipv6`, `fast_upload_connect_timeout_s`, `fast_upload_connect_retries`, `fast_upload_connect_stagger_s`), the `SendResult.stalled` flag, and the test file split performed in Phase B. Verify with: `grep -E "retry_after|use_ipv6|fast_upload_connect" AGENTS.md`
<!-- triapi:plan run_id=20260905-070455-c6136e end -->

<!-- triapi:plan run_id=20260905-072724-880b38 start -->
## TriAPI Plan (run 20260905-072724-880b38, appended 2026-09-05)

1. Phase A: Finish cli.py wiring
- [ ] `dispatcher/dispatcher/cli.py`: At both `TelethonSendStrategy(...)` construction sites that currently pass `use_ipv6=config.use_ipv6`, additionally pass `connect_timeout=config.fast_upload_connect_timeout_s`, `retries=config.fast_upload_connect_retries`, and `connect_stagger_s=config.fast_upload_connect_stagger_s`. Leave `upload_connections` untouched. Verify with: `python3 -m py_compile dispatcher/dispatcher/cli.py`

2. Phase B: Split the oversized test file
- [ ] `tests/test_seams.py`: Split this oversized file (156,987 chars) into smaller cohesive modules logically grouped by subsystem along the existing "Seam N" boundaries (grep for `── Seam `). Create targeted files (e.g., `tests/test_seams_lock.py`, `tests/test_seams_queue.py`) while preserving all test behavior verbatim without rewriting. Delete the original oversized file. Verify with: `wc -c tests/test_seams*.py` (confirming all are under 73728 chars) and `for f in tests/test_seams*.py; do PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 ~/.local/share/uv/tools/dispatcher/bin/python3 "$f" || exit 1; done`
- [ ] `CLAUDE.md`: Update the testing section documentation line that specifies how to run tests (previously `python tests/test_seams.py`) to reflect the new split test file execution pattern. Verify with: `grep "test_seams" CLAUDE.md`

3. Phase C: Regression tests for stall backoff
- [ ] `tests/test_dispatcher_stall_backoff.py`: Add a new regression test module with `unittest`/`assert` coverage verifying: `claim_next()`/`claim_batch()` honor `retry_after` (claims NULL or past timestamps, skips future timestamps); `mark_failed()` sets `retry_after` exclusively on the pending transition when `backoff_s` is given; `SendResult.stalled` is True only upon exhausting `_send_with_retries` via `TimeoutError`/`asyncio.TimeoutError` and False for other failures; `DispatcherConfig` loads the five new fields (`use_ipv6`, `fast_upload_connect_timeout_s`, `fast_upload_connect_retries`, `fast_upload_connect_stagger_s`, `stall_backoff_s`) from env with exact correct defaults (False, 8.0, 2, 0.1, 300.0) when unset; and `TelethonSendStrategy._build_client` passes `use_ipv6` to the `TelegramClient(...)` constructor (using mock inspection, no live connection). Verify with: `PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 ~/.local/share/uv/tools/dispatcher/bin/python3 tests/test_dispatcher_stall_backoff.py`

4. Phase D: Close the incident record and doc index
- [ ] `connection_fix.md`: Append a "## Resolution" section at the very end of the document without altering any existing incident analysis above it. Summarize Fix 1 (fast connect timeout + retries on parallel-upload senders, plus a staggered-connect delay used instead of reducing `UPLOAD_CONNECTIONS`), Fix 2 (opt-in `USE_IPV6`, default off), and Fix 3 (per-item `retry_after` stall backoff, noting the 5.3h circuit breaker root cause). Explicitly record that Fix 4 (reducing `UPLOAD_CONNECTIONS` to 4) was rejected per user instruction. Note that two hand-corrections (a schema-migration defect and a broken verify command that caused two bad import edits to `config.py`) were caught and reverted by human review. Verify with: `grep -n "^## Resolution" connection_fix.md`
- [ ] `AGENTS.md`: Update the document to detail the five new `DispatcherConfig` fields (`use_ipv6`, `fast_upload_connect_timeout_s`, `fast_upload_connect_retries`, `fast_upload_connect_stagger_s`, `stall_backoff_s`), the `retry_after` column / `SCHEMA_VERSION` 5, the `SendResult.stalled` flag, and the test file split performed in Phase B. Verify with: `grep -E "retry_after|use_ipv6|fast_upload_connect|stall_backoff_s" AGENTS.md`
<!-- triapi:plan run_id=20260905-072724-880b38 end -->

<!-- triapi:plan run_id=20260905-075937-cac9b7 start -->
## TriAPI Plan (run 20260905-075937-cac9b7, appended 2026-09-05)

1. Phase A: Tests
- [ ] `tests/test_dispatcher_stall_backoff.py`: Add a new regression test module. Implement an `if __name__ == '__main__': unittest.main()` block that exits non-zero on failure. Add tests verifying: `core.store`'s `claim_next()`/`claim_batch()` honor `retry_after` (claims NULL or past timestamps, skips future timestamps); `store.mark_failed()` sets `retry_after` (to a future timestamp) exclusively on the pending transition when `backoff_s` is given, leaving it unset for 'failed' status or if `backoff_s` is None; `dispatcher.send.SendResult.stalled` is True only upon exhausting `_send_with_retries` via `TimeoutError`/`asyncio.TimeoutError`, and False for other failures; `dispatcher.config.DispatcherConfig.load()` loads the five new fields with exact defaults (`use_ipv6=False`, `fast_upload_connect_timeout_s=8.0`, `fast_upload_connect_retries=2`, `fast_upload_connect_stagger_s=0.1`, `stall_backoff_s=300.0`) when unset; and `dispatcher.send.TelethonSendStrategy._build_client` passes `use_ipv6` to the `TelegramClient(...)` constructor (using mock inspection, no live connection). Verify with: `python3 -m py_compile tests/test_dispatcher_stall_backoff.py && PYTHONPATH="core:archiver:recorder:dispatcher:ops" PYTHONUTF8=1 ~/.local/share/uv/tools/dispatcher/bin/python3 tests/test_dispatcher_stall_backoff.py`

2. Phase B: Documentation and Incident Record
- [ ] `connection_fix.md`: Append a "## Resolution" section at the very end of the document without altering any existing incident analysis above it. Summarize Fix 1 (fast connect timeout + retries on parallel-upload senders, plus a staggered-connect delay used INSTEAD of reducing `UPLOAD_CONNECTIONS`), Fix 2 (opt-in `USE_IPV6`, default off), and Fix 3 (per-item `retry_after` stall backoff, noting the 5.3h circuit breaker root cause). Explicitly record that Fix 4 (reducing `UPLOAD_CONNECTIONS` to 4) was rejected per user instruction. Note that the `tests/test_seams.py` split was attempted and deferred as pipeline tech debt (see `knowledge/TECH_DEBT.md` in the TriAPI repo) rather than blocking this fix. Verify with: `grep -n "^## Resolution" connection_fix.md`
- [ ] `AGENTS.md`: Update the document to detail the five new `DispatcherConfig` fields (`use_ipv6`, `fast_upload_connect_timeout_s`, `fast_upload_connect_retries`, `fast_upload_connect_stagger_s`, `stall_backoff_s`), the `retry_after` column / `SCHEMA_VERSION` 5, the `SendResult.stalled` flag, and the new `tests/test_dispatcher_stall_backoff.py` test module. Verify with: `grep -E "retry_after|use_ipv6|fast_upload_connect|stall_backoff_s" AGENTS.md`
<!-- triapi:plan run_id=20260905-075937-cac9b7 end -->
