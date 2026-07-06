# Archiver Suite → Windows Conversion Plan

> Planning workspace. The source lives in `../Archiver suite`. This document is a
> self-contained execution plan for porting the macOS/POSIX suite to Windows.
> It supersedes/expands the in-repo `WINDOWS_PORT.md` with a concrete file
> inventory verified against the current tree (2026-07-06).

---

## 0. Verdict

**Difficulty: Medium.** The suite is POSIX-bound in **exactly four mechanisms**,
all concentrated in the `core` + `recorder` packages. The DB spine (SQLite WAL),
the media pipeline (ffmpeg/yt-dlp/gallery-dl subprocesses), and the Telegram path
(Telethon/FastTelethon/hachoir) are **already cross-platform**. Estimated effort:
**2–4 focused days** for a working port; the recorder's daemon/process-group
logic is the only part needing real redesign rather than an adapter swap.

The guiding rule: **do not scatter `if os.name == "nt"` across the codebase.**
Introduce one platform-adapter package and route the four POSIX seams through it,
leaving `store` / `ingest` / `send` / `media_prep` platform-blind.

---

## 1. The four POSIX blockers (verified locations)

| # | Mechanism | Why it breaks on Windows | Files |
|---|---|---|---|
| 1 | **`fcntl.flock`** file locking | `fcntl` module absent on Windows | `core/core/instance_lock.py:24,75,79,88,107`; sibling locks in `dispatcher/`, `recorder/recorder/lock.py` |
| 2 | **`os.killpg` / `os.getpgid` / `start_new_session`** process-group kill | No POSIX process groups; orphans child ffmpeg → data loss | `recorder/recorder/capture.py:171,177,191` |
| 3 | **`os.fork` / `os.setsid`** daemonize | Neither exists on Windows | `recorder/recorder/cli.py:149,151,152` |
| 4 | **`signal.SIGTERM`** shutdown | `SIGTERM` not deliverable; `signal.signal(SIGTERM,…)` raises | `dispatcher/dispatcher/cli.py:99`; `recorder/recorder/cli.py:136,137,216,217,243`; `archiver/archiver/cli.py` uses only SIGINT (OK) |

Plus one **path** concern (not a hard crash, but wrong behavior):
- Hardcoded `~/.config/archiver-suite/…` and `~/.recorder/pid` resolve to the
  wrong place on Windows. Must route through `platformdirs`.

Everything else (`os.replace` atomic writes, `os.kill(pid, 0)` liveness probe —
which *does* work on Windows, `pathlib`) is portable.

---

## 2. Strategy: one seam, four adapters

Add a new package `core/core/platform/` exporting four adapters, each with a
POSIX and a Windows implementation selected at import time by `os.name`:

```
core/core/platform/
  __init__.py        # selects impl by os.name; exports the 4 adapter APIs
  paths.py           # config_root / state_dir / locks_dir  (platformdirs)
  filelock.py        # acquire_exclusive / probe_shared / release
  procgroup.py       # spawn_group / signal_group / kill_group
  service.py         # install / start / stop / status  (launchd | Task Sched)
```

New dependencies: `platformdirs` (all platforms), `pywin32` (Windows-only,
`sys_platform == 'win32'` marker in each `pyproject.toml`).

---

## 3. Phased execution

### Phase 0 — Hygiene (low risk, do first)
- [ ] Delete checked-in `build/` trees (`core/build`, `dispatcher/build`,
      `recorder/build`, `archiver/build`, `ops/build`, `librarian/build`) — stale
      source duplicates that double every grep and hide edits. Add `**/build/` to
      `.gitignore`.
- [ ] Confirm `ffmpeg`, `ffprobe`, `yt-dlp`, `gallery-dl` resolve on Windows
      PATH (all already discovered via PATH — no hardcoded prefixes exist).
- [ ] Establish a Windows test box / VM with Python 3.12+ matching the pins in
      `requirements.txt`.

### Phase 1 — Path centralization (blocks everything) ✅ DONE (2026-07-06)
- [x] Added `core/core/platform/` package (`__init__.py` + `paths.py`) exposing
      `config_dir(app)`, `locks_dir()`, and app constants `SUITE/DISPATCHER/
      ARCHIVER/RECORDER`.
- [x] **Deviation from original plan:** did **NOT** use `platformdirs`. Its macOS
      default resolves to `~/Library/Application Support/<app>`, which would
      silently relocate existing macOS installs off the suite's `~/.config`
      convention. Instead: POSIX branch = `$XDG_CONFIG_HOME`/`~/.config` (byte-for-
      byte unchanged); Windows branch = `%APPDATA%` (or `~/AppData/Roaming`). No
      new dependency needed.
- [x] Routed every code literal through it: `core/paths.py`, `core/schema.py`
      (new `default_db_path()`, exported from `core`), `core/policy_store.py`,
      `core/migrate.py`, `core/instance_lock.py` (dir only — mechanism is Phase 2),
      `dispatcher/config.py` (.env, session, banned-words, db default),
      `dispatcher/instance_lock.py` (dir only), `archiver/config.py`,
      `archiver/reconcile.py`, `archiver/cli.py`, `recorder/config.py`
      (.env, config.toml, tiktok.lock). `sanitize.py` needed no change (expands a
      passed-in path). `~/.recorder` + `~/recorder-output` left as-is (`~`
      expands correctly on Windows).
- [x] `ARCHIVER_DB` env override preserved (wins in `schema.db_path()`).
- **Verified:** POSIX paths unchanged (smoke test); Windows branch selects
      `%APPDATA%`; all packages import; `core` instance-lock + safebrake selftests
      pass (55 checks). Full `test_seams.py` deferred to a box with `pytest`
      installed / the Windows test pass.

### Phase 2 — Instance lock + liveness (load-bearing) ✅ DONE (2026-07-06)
- [x] `platform/filelock.py`: POSIX = `fcntl.flock(LOCK_EX/SH|LOCK_NB)`/`LOCK_UN`;
      Windows = `msvcrt.locking(fd, LK_NBLCK, 1)` on byte 0. Both give **kernel
      auto-release on crash/kill**. API is non-blocking `try_acquire_exclusive`/
      `try_acquire_shared`/`release` over an open file handle. (Windows has no
      shared mode → shared degrades to a non-blocking exclusive attempt, which is
      exactly what the diagnostic holder-pid probe needs.)
- [x] `platform/process.py`: portable `pid_alive(pid)`. **Critical Windows
      finding:** `os.kill(pid, 0)` on Windows routes to TerminateProcess and would
      *kill* the process it means to probe. Windows impl uses `OpenProcess` +
      `GetExitCodeProcess` via ctypes (no pywin32). `core.heartbeat.pid_alive`
      (the suite's one liveness primitive) now delegates here.
- [x] **Wider scope than planned** — the `fcntl` surface was 2 sites, not 1:
      - `core/instance_lock.py` (the InstanceLock; dispatcher's session lock
        subclasses it, so it's covered for free — it never had its own `fcntl`).
      - `core/media_prep.py` `_prep_lock` (the per-file prep flock) — also routed.
      - `recorder/lock.py` needed **no** change: it's a *soft* presence lock
        (heartbeat + liveness), never used `fcntl`.
      - `os.kill(pid,0)` liveness fixed at `heartbeat.py` + `recorder/cli.py`
        (status) + `tests/test_seams.py` (`_dead_pid` helper).
- [x] Holder-pid probe now short-circuits on a missing lock file (never creates
      it just to probe) and opens `a+` so the probe handle is lockable on Windows.
- [x] **No PID-file fallback** — the kernel-auto-release guarantee is preserved
      on both platforms.
- **Verified (POSIX):** instance-lock selftest (acquire / refuse-2nd / holder-pid
      / re-acquire-after-exit) + media_prep selftest pass (80 checks); all packages
      import. **Windows kill-frees-lock test deferred to the Windows box.**

### Phase 3 — Recorder process-group kill (data-loss guard) ✅ DONE (2026-07-06)
- [x] `platform/procgroup.py` with `popen_kwargs()` / `terminate(proc)` /
      `kill(proc)`:
      - POSIX = `start_new_session=True` spawn; `terminate`→SIGTERM, `kill`→SIGKILL
        to the group via `killpg(getpgid(pid), …)`.
      - Windows = `CREATE_NEW_PROCESS_GROUP` spawn; `terminate`→`CTRL_BREAK_EVENT`
        to the group (lets ffmpeg flush and close the file), `kill`→
        `taskkill /PID <pid> /T /F` (`/T` = whole descendant tree ⇒ the child
        ffmpeg cannot survive). Chose taskkill /T over a Job Object: simpler, no
        long-lived job handle to babysit, and it satisfies the same invariant.
- [x] Routed `capture.py`: spawn now uses `**procgroup.popen_kwargs()`; the old
      `_signal_group` method (with its `hasattr(os,"killpg")` bare-pid fallback,
      which would orphan ffmpeg on Windows) is **deleted** — `_terminate` calls
      the adapter, falling back to the bare pid only when the group is already
      gone. `import os`/`import signal` dropped from `capture.py`.
- [x] Confirmed capture.py is the ONLY production process-group site (remux /
      ffmpeg use synchronous `subprocess.run`).
- **Verified (POSIX):** capture selftest incl. "terminate kills the whole group —
      NO orphan, child stops writing" passes (10 checks); all recorder modules
      import; `popen_kwargs()` returns the right flag. The `_selftest_capture.py`
      group test is POSIX-only (`sh -c`); the **Windows kill → no stray ffmpeg.exe
      in Task Manager** test is deferred to the Windows box.

### Phase 4 — Signals & shutdown ✅ DONE (2026-07-06)
- [x] `platform/signals.py` with `install_sync(handler)` (recorder — threaded)
      and `install_async(loop, callback)` (dispatcher — asyncio). Shutdown signal
      set is `(SIGINT, SIGTERM)` on POSIX, `(SIGINT, SIGBREAK)` on Windows.
- [x] **Correction to the plan's premise:** the real Windows blocker was the
      *dispatcher*, not the recorder. `loop.add_signal_handler` (dispatcher)
      raises `NotImplementedError` on Windows loops — `install_async` catches it
      and falls back to `signal.signal` + `call_soon_threadsafe`. Bare
      `signal.signal(SIGTERM,…)` (recorder) is actually *allowed* on Windows; it's
      just never delivered, so we register `SIGBREAK` there instead.
- [x] Routed: `dispatcher/cli.py` (→ `install_async`), `recorder/cli.py` both
      handler sites (→ `install_sync`). Dropped now-unused `import signal` from
      both. `archiver/cli.py` uses only `SIGINT` (Windows-safe) — left as-is.
- [x] `recorder stop` (`os.kill(pid, SIGTERM)`) → `procgroup.terminate_pid(pid)`:
      POSIX SIGTERM to the recorder (its handler stops gracefully + group-kills
      the capture); Windows `taskkill /PID <pid> /T /F` (tree kill — recording
      stays playable via MPEG-TS/--no-part). Stale-pid check now uses the
      Windows-safe `heartbeat.pid_alive`.
- **Verified (POSIX):** all packages import; `shutdown_signals()` = SIGINT/SIGTERM;
      capture + instance-lock selftests pass (41 checks).

### Phase 5 — Daemonize + service management ✅ DONE (2026-07-06)
- [x] **Deleted** `_daemonize()` (the `os.fork`/`os.setsid`/`os.dup2` double-fork
      — the last POSIX-only process primitive). `--daemon` is now an accepted
      no-op that logs a pointer to `ops install`; the arg stays so old scripts
      don't break.
- [x] **Mechanism chosen: Task Scheduler** (per user's call — runs logged in,
      manual `ops`-style control). It's the direct analog of today's per-user
      launchd LaunchAgent (at-logon, runs as the user → `%APPDATA%` paths
      resolve, restart-on-failure), needs no admin / stored password / bundled
      binary. (Windows Service + NSSM were the headless-server alternatives.)
- [x] `platform/service.py` — launchd backend (POSIX, byte-faithful to the old
      plist XML + launchctl load/unload/kickstart/list) + Task Scheduler backend
      (Windows: `schtasks /Create /XML` with LogonTrigger + RestartOnFailure for
      daemons / CalendarTrigger for the daily logrotate job; `/Run` `/End`
      `/Change /ENABLE|/DISABLE` `/Query`). Same verbs: install / uninstall /
      load / unload / restart / definition_exists / running_pid / log_dir.
- [x] `platform/process.py` gained `proc_stats` (POSIX `ps` / Windows `tasklist`)
      and `find_worker_pid` (POSIX `ps` / Windows `wmic` cmdline match).
- [x] Rewired `ops/cli.py` (install/uninstall/load/unload/restart → adapter;
      verbs/health/watch/logrotate unchanged) and `ops/health.py`
      (`launchctl_pid`→`service.running_pid`; `foreground_pid`/`proc_stats`→
      `process`; owner label "launchd"→"service"). Removed the inline plist XML
      + `ops/launchd/` plists + dead `subprocess`/`shlex` imports. Dispatcher's
      "already running" hint is now OS-neutral (`ops unload dispatcher`).
- **`ops/logrotate.py` left as-is:** copy-truncate is OS-agnostic and is in fact
      the *correct* Windows choice too (a rename would strand a service that holds
      the log's handle open — the same reason it was chosen for launchd).
- **Verified (POSIX):** all packages import; launchd plist XML (daemon + calendar)
      generates identically; `ops health` renders `service · pid` via launchd;
      logrotate + all 6 core/capture selftests pass. Windows `schtasks` lifecycle
      + `wmic` pid-match deferred to the Windows box.

---

## 4. Already portable — do NOT touch
- **SQLite WAL + single-file coordination** — identical on Windows. This is
  *why* the port is tractable.
- **Atomic writes** (`os.replace` + tmp + fsync in `heartbeat.py`,
  `policy_store.py`, sorter). `os.replace` is atomic on Windows too.
  ⚠️ **Caveat:** Windows cannot replace/delete a file with an **open handle** —
  audit that nothing holds the target open across the replace.
- ~~**`os.kill(pid, 0)`** liveness probe — works on Windows.~~ **WRONG** (Phase 2
  correction): on Windows `os.kill(pid, 0)` routes to TerminateProcess and would
  *kill* the target. Now handled by `core.platform.process.pid_alive`
  (OpenProcess). Do not reintroduce raw `os.kill(pid, 0)`.
- **Telethon / FastTelethon / hachoir / media_prep / ffmpeg subprocesses** —
  cross-platform.

---

## 5. Test order on Windows
1. `core` selftests (DB, paths, policy_store, media_prep) — needs ffmpeg on PATH.
2. `filelock` two-instance race + kill-frees-lock.
3. Recorder capture + kill → no orphan ffmpeg.
4. Full seam suite `tests/test_seams.py` with
   `PYTHONPATH=core;archiver;recorder;dispatcher;ops` (Windows uses `;`).
5. Service install + reboot survival + restart-on-crash.

## 6. Definition of done
- [x] **Code complete (Phases 1–5).** No `fcntl`, `os.fork`, `os.setsid`,
      `os.killpg`, raw `os.kill(pid,0)`, `add_signal_handler`, or bare `SIGTERM`
      reachable on the Windows path — every OS-specific mechanism sits behind
      `core.platform.{paths,filelock,process,procgroup,signals,service}`, selected
      by `os.name`. POSIX behavior preserved (all selftests green on macOS).
- [x] All config under `%APPDATA%\archiver-suite` on Windows (Phase 1).
- [x] **Full-suite validation on macOS (2026-07-06, post-Phase-5 hardening):**
      `tests/test_seams.py` ran for the first time on the port in a proper venv
      (telethon + hachoir + full requirements) — **ALL 210 checks pass**, plus
      all 9 selftests, `compileall` clean, pyflakes clean on every ported file.
      Hardening applied in the same pass:
      - `find_worker_pid` (Windows) now self-heals across tooling drift:
        `wmic` (removed on Win11 24H2+) → PowerShell `Get-CimInstance` fallback;
        strict token-wise cmdline match shared by both probes (no shell-snippet
        false positives — same discipline as the POSIX shlex branch).
      - Verified the Task Scheduler `cmd.exe` argument quoting invariant
        (`/c ""prog" args >> "out" 2>> "err""` — canonical strip-outer form).
      - Dead code removed: unused `Path` imports (archiver/recorder config),
        dead `running` local (recorder status), f-string cosmetic (service).
      - **Design note:** on Windows, `schtasks /End` is a hard tree-kill (no
        graceful console event), so `ops unload` = crash-equivalent stop there.
        Safe by design: the suite is crash-safe end-to-end (WAL, kernel-released
        locks, startup sweeps, claim recovery — all covered by the seam suite).
- [ ] **Remaining — validate on a real Windows box** (can't be done from macOS):
      - Kill-tests: no orphan `ffmpeg.exe`, no stale locks.
      - Workers auto-start at logon + restart on crash via Task Scheduler.
      - `test_seams.py` green (`PYTHONPATH=core;archiver;recorder;dispatcher;ops`).
      - `tasklist`/CIM health probes return sensible values on the target build.
      - Add `pywin32`? Not currently needed — the port uses only stdlib
        (`ctypes`, `msvcrt`, `subprocess`+`schtasks`/`taskkill`). Keep it out
        unless a Windows-box gap forces it.

---

## 7. Open decisions (need a call before/at Phase 5)
- **Service mechanism**: Task Scheduler vs Windows Service vs NSSM.
- **Port in place vs. fork the tree**: this folder can either hold the adapter
  package as a staging area, or the work lands directly in `../Archiver suite`
  behind `os.name` guards (recommended — keeps one codebase, no drift).
- **Distribution**: ship as pipx installs (as today) or bundle a PyInstaller
  `.exe` per binary for non-technical Windows users.
