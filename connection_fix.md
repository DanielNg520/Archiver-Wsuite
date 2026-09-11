# Archiver-Suite: Dispatcher Post-Outage Connection Stalls & Permanent Fixes

## 1. Overview & Problem Statement

Following an internet outage or WAN drop, the `dispatcher` service can become wedged attempting to upload recordings. Even after internet connectivity is restored, items remain in a `sending` or stalled `pending` state, preventing subsequent recordings from uploading.

During the incident on **2026-09-05**, the queue was completely blocked by a single TikTok live recording (`nguyendo_191_1788583685.mp4`), leaving newer and pending recordings (such as the 3 `@vandaihoang4` recordings) unprocessed for over 5 hours.

---

## 2. Incident Symptoms & Observed Logs

### A. Dispatcher Log Pattern (`~/.local/log/dispatcher.out.log`)
The dispatcher repeatedly entered a 40-minute freeze cycle:
```log
2026-09-04 23:57:40 INFO    ▲ @nguyendo_191 uploading nguyendo_191_1788583685.mp4 [tiktok]
2026-09-04 23:59:57 WARNING ⚠ Attempt 1 at connecting failed: TimeoutError: [Errno 110] Connect call failed ('149.154.175.56', 443)
2026-09-05 00:01:28 WARNING ⚠ Server closed the connection: 0 bytes read on a total of 8 expected bytes
2026-09-05 00:04:27 WARNING ⚠ Attempt 1 at connecting failed: TimeoutError: [Errno 110] Connect call failed ('149.154.175.56', 443)
2026-09-05 00:07:41 WARNING ⚠ stall attempt 1/4 (nguyendo_191_1788583685.mp4): no progress for 600s — reconnecting
...
2026-09-05 00:41:13 WARNING ⚠ stall attempt 4/4 (nguyendo_191_1788583685.mp4): no progress for 600s — reconnecting
2026-09-05 00:41:24 WARNING ⚠ @nguyendo_191 upload failed (1 item, pending): stalled: no upload progress for 600s
2026-09-05 00:41:24 INFO    ▲ @nguyendo_191 uploading nguyendo_191_1788583685.mp4 [tiktok]  <-- Immediately re-claimed!
```

### B. Database & Process Diagnostics
- **Queue State**: `suite.db` had 210 pending items, 1 locked in `sending` (`nguyendo_191_1788583685.mp4`), and 0 failed.
- **Socket State**: `lsof -iTCP` showed the primary Telethon connection established to `149.154.175.60:443`, but parallel worker connections to `149.154.175.56:443` stuck indefinitely in `SYN_SENT`.
- **Systemd Watchdog Lease**: Restarting the service did not immediately unblock the item because `drain.py`'s watchdog only resets claims older than `stuck_claim_min=10` minutes.

---

## 3. Deep-Dive Root Cause Analysis

The freeze is caused by the compounding interaction of **three architectural layers**:

```
+-----------------------------------------------------------------------------------+
| 1. Network Layer: Flaky IPv4 WAN route (SYN packet drop) vs. pristine IPv6       |
+-----------------------------------------+-----------------------------------------+
                                          |
+-----------------------------------------v-----------------------------------------+
| 2. Connection Layer: fast_upload MTProtoSender has connect_timeout=None           |
|    -> OS TCP SYN retry takes ~130s per attempt x 5 retries = 650s hang           |
+-----------------------------------------+-----------------------------------------+
                                          |
+-----------------------------------------v-----------------------------------------+
| 3. Loop Layer: Stall watchdog trips at 600s, resets socket, fails after 4 retries  |
|    -> drain_forever polls 2s later, re-claims same file at Priority 5            |
|    -> Infinite 40-minute freeze loop; newer items starved indefinitely           |
+-----------------------------------------------------------------------------------+
```

### Layer 1: Network Route Flapping (IPv4 vs. IPv6)
- **IPv4 Transit Degradation**: After the internet outage, the transit route to Telegram DC1/DC4 (`149.154.175.56`) experienced ~40–60% SYN packet loss. Consecutive TCP handshakes to port 443 intermittently timed out.
- **IPv6 Stability**: The host system has native IPv6 (`2600:1700:...`). Direct testing to Telegram DC1's IPv6 address (`2001:b28:f23d:f001::a:443`) demonstrated **0% packet loss and a 97ms round trip**.
- **Telethon Limitation**: `dispatcher` instantiates `TelegramClient` without `use_ipv6=True`, forcing all traffic through the degraded IPv4 path.

### Layer 2: Fast Upload Worker Hanging (`dispatcher/fast_upload.py`)
In `fast_upload.py`, `_connect_sender()` creates borrowed senders for parallel chunk uploads:
```python
async def _connect_sender(client: Any) -> MTProtoSender:
    dc = await client._get_dc(client.session.dc_id)
    sender = MTProtoSender(client.session.auth_key, loggers=client._log, auto_reconnect=False)
    await sender.connect(client._connection(
        dc.ip_address, dc.port, dc.id,
        loggers=client._log, proxy=client._proxy,
        local_addr=getattr(client, "_local_addr", None),
    ))
    return sender
```
- `MTProtoSender` defaults to `retries=5` and `connect_timeout=None`.
- Under Linux, when a SYN packet is dropped, `tcp_syn_retries=6` causes `asyncio.open_connection()` to wait **~130 seconds** before raising `ETIMEDOUT`.
- With 5 retries, a single worker connection attempt can block for up to `5 × 130s = 650s` (~11 minutes).
- Because `_parallel_upload()` connects workers serially in a loop (`for _ in range(workers): sender = await _connect_sender(client)`), a single hung socket halts the entire upload pipeline before a single byte of data is transferred.

### Layer 3: The Stall Watchdog & Immediate Re-claim Loop
- `send.py` runs a stall watchdog configured with `stall_base_timeout_s = 600.0` (10 minutes).
- Because workers are blocked waiting on the kernel TCP connect timeout, 0 bytes are transferred.
- At 600 seconds, the stall watchdog triggers a forced reconnect, retrying up to 4 times (40 minutes).
- Once all 4 attempts fail, the item is marked as failed back to `pending`.
- However, `drain.py` immediately polls the database 2 seconds later (`poll_interval_s = 2.0`). Because the failed recording has **priority 5** (highest priority in the database), `store.claim_batch()` immediately claims the exact same file again.
- This creates an **infinite loop**: 40 minutes of stalls $\rightarrow$ reset $\rightarrow$ immediate re-claim $\rightarrow$ 40 minutes of stalls.

---

## 4. Recommended Permanent Fixes

### Fix 1: Add Explicit Fast Connect Timeout to `fast_upload.py` (High Priority)
**File**: `dispatcher/dispatcher/fast_upload.py`

Configure borrowed upload senders to fail fast (e.g., 8-second timeout, 2 retries). If a parallel connection cannot be established quickly, `_parallel_upload` catches the exception and immediately drops back to the reliable serial uploader (`return await serial()`) instead of wedging the pipeline for 10+ minutes.

```python
async def _connect_sender(client: Any) -> MTProtoSender:
    dc = await client._get_dc(client.session.dc_id)
    sender = MTProtoSender(
        client.session.auth_key,
        loggers=client._log,
        auto_reconnect=False,
        connect_timeout=8.0,   # Fast connect timeout (seconds)
        retries=2,             # Do not retry 5 times at 130s each
    )
    await sender.connect(client._connection(
        dc.ip_address, dc.port, dc.id,
        loggers=client._log, proxy=client._proxy,
        local_addr=getattr(client, "_local_addr", None),
    ))
    return sender
```

---

### Fix 2: Enable Dual-Stack / IPv6 Support in Dispatcher (High Priority)
Since IPv6 to Telegram is unaffected by local IPv4 transit route flapping, enable IPv6 support via configuration.

1. **Update `dispatcher/dispatcher/config.py`**:
   ```python
   # In DispatcherConfig dataclass:
   use_ipv6: bool = False

   # In DispatcherConfig.load():
   use_ipv6 = env.opt_bool("USE_IPV6", False)
   ```

2. **Update `dispatcher/dispatcher/send.py`**:
   Pass `use_ipv6=self._config.use_ipv6` to `TelegramClient` in `_build_client()`:
   ```python
   def _build_client(self, session_name: str, api_id: int, api_hash: str) -> TelegramClient:
       return TelegramClient(
           session_name, api_id, api_hash,
           auto_reconnect=False,
           use_ipv6=self._config.use_ipv6,
           connection=KeepAliveConnectionTcpFull,
       )
   ```

3. **Enable in `~/.archive/.config/dispatcher/.env`**:
   ```bash
   USE_IPV6=1
   ```

---

### Fix 3: Add Backoff / Cooldown for Stalled Upload Items (Medium Priority)
**File**: `dispatcher/dispatcher/drain.py`

When an upload fails due to a stall watchdog timeout or connection failure, do not allow it to be re-claimed 2 seconds later. Apply a backoff delay (e.g. 5 minutes) or increment its attempt count properly so other items in the queue can make progress:

```python
# In drain.py error handling:
if "stalled: no upload progress" in str(result.error):
    # Set discovered_at or next_retry timestamp 5 minutes into the future
    store.requeue(it.id, reason=f"stalled upload backoff (5m cooldown)")
```

---

### Fix 4: Tune Upload Connections in `.env` (Operational / Config)
**File**: `~/.archive/.config/dispatcher/.env`

Firing 8 concurrent TCP handshakes simultaneously to Telegram port 443 can trip router NAT state limits or stateful firewall flood protection during WAN recovery. Reducing concurrent connections to 4 prevents NAT table spikes while maintaining high upload speeds:
```bash
UPLOAD_CONNECTIONS=4
```

---

## 5. Verification Checklist

1. **Verify IPv6 Connectivity**:
   ```bash
   ncat -6 -zvw3 2001:b28:f23d:f001::a 443
   ```
   *Expected result*: `Connected to 2001:b28:f23d:f001::a:443` in $<100$ms.

2. **Check Current Dispatcher Status**:
   ```bash
   dispatcher status
   ```
   *Verify that `sending` is 0 or progressing and `last sent` updates.*

3. **Check Service Logs**:
   ```bash
   tail -f ~/.local/log/dispatcher.out.log
   ```
   *Ensure uploads show progressing byte counts rather than repeating `Attempt 1 at connecting failed`.*

---

## Resolution (2026-09-05)

Implemented, hand-verified end to end against both a fresh database and a
copy of the live `suite.db`. **Fix 4 (reducing `UPLOAD_CONNECTIONS` to 4)
was rejected per explicit user instruction** — connection count stays at
its default of 8 everywhere.

- **Fix 1 — fast connect timeout + retries, plus a stagger (replaces Fix
  4)**: `dispatcher/dispatcher/fast_upload.py`'s `_connect_sender()` /
  `_parallel_upload()` now accept `connect_timeout`/`retries` (threaded
  from `DispatcherConfig.fast_upload_connect_timeout_s` (8.0s) /
  `fast_upload_connect_retries` (2)), so a dropped SYN during a flaky
  post-outage route fails in ~16s instead of ~650s. `_parallel_upload`'s
  worker-connect loop also staggers each successive sender's connect by
  `fast_upload_connect_stagger_s` (0.1s default) — this is the
  throughput-preserving substitute for cutting connection count: it
  reduces simultaneous SYN bursts against NAT/firewall state limits during
  WAN recovery without touching `UPLOAD_CONNECTIONS`.
- **Fix 2 — opt-in IPv6**: `DispatcherConfig.use_ipv6` (env `USE_IPV6`,
  default `False`) is threaded through `TelethonSendStrategy.__init__` to
  `_build_client()`'s `TelegramClient(..., use_ipv6=...)` call. Off by
  default — some networks have broken IPv6 that would make things worse;
  operators on a route where IPv4 to Telegram is the flaky leg (as in this
  incident) can opt in.
- **Fix 3 — per-item stall backoff (the dominant fix)**: this is the real
  root cause of the ~5.3h queue block, not the network layer. The circuit
  breaker only trips after 8 consecutive 40-minute stall cycles
  (`_CIRCUIT_TRIP_AT=8` × `stall_base_timeout_s=600` × `max_retries=4`) —
  8 × 40min ≈ the incident's observed duration almost exactly. Neither
  `store.requeue()` nor `store.mark_failed()` had any time-based backoff,
  so a stalled high-priority item (recorder items are priority 5 vs the
  default 100) was re-claimed within `poll_interval_s` (2s) of failing.
  Fixed via a new `items.retry_after` column (`core/core/schema.py`
  migration 5, `SCHEMA_VERSION` bumped to 5): `claim_next()`/
  `claim_batch()` (`core/core/store.py`) now skip a row whose
  `retry_after` is in the future; `mark_failed()` takes an optional
  `backoff_s` and stamps `retry_after` only on the pending (non-terminal)
  transition. `dispatcher/dispatcher/send.py`'s `SendResult` gained a
  typed `stalled: bool` field (not string-matching on `error` text) set by
  `_send_with_retries` only when retries were exhausted via the
  stall-watchdog `TimeoutError`/`asyncio.TimeoutError` path.
  `dispatcher/dispatcher/drain.py` passes
  `backoff_s=config.stall_backoff_s if result.stalled else None`
  (`stall_backoff_s` defaults to 300s, env `STALL_BACKOFF_S`).

**Rollout required four hand-corrections after automated-pipeline
attempts, all caught by human review before landing:**
1. A schema-migration defect (an early automated attempt added the wrong
   column, `tg_message_id`, instead of `retry_after`).
2. Two `dispatcher/dispatcher/config.py` import corruptions (`from core
   import X` rewritten to the nonexistent `from core.core import X`, and
   later a stray `sys.path.insert` hack) — both caused by a flawed
   supervisor-authored verify command, not a real code problem.
3. A dropped `use_ipv6=config.use_ipv6` keyword argument at both
   `TelethonSendStrategy(...)` call sites in `cli.py`, silently removed by
   an automated edit that was only adding the new connect-timeout params.
4. Two deeper, previously-undetected bugs surfaced only by direct
   end-to-end testing (not caught by any tier's own verification): (a)
   `core/core/store.py`'s `mark_failed()` was never actually given the
   `backoff_s` parameter despite `drain.py` already calling it with one —
   would have raised `TypeError` on the very first failed send in
   production; (b) `core/core/models.py`'s `Item` dataclass had no
   `retry_after` field, so `Item.from_row()` would have raised `TypeError`
   on *every single claim* the moment the migration landed. Both are fixed
   and verified against a fresh DB and a copy of the live `suite.db`.

**Deferred, not blocking this fix:** splitting the oversized
`tests/test_seams.py` (156,987 chars, over this repo's 73,728-char
ceiling) was attempted twice via automated dispatch and both times failed
structurally (immediate handoff, zero actual attempts made) — queued as
tech debt in this repo's own `AGENTS.md` ("Known tech debt" section)
rather than blocking this fix. New regression coverage for everything above lives in
`tests/test_dispatcher_stall_backoff.py` (a new file, does not touch
`test_seams.py`) and passes (14/14) against both a fresh database and this
repo's real dependencies.
