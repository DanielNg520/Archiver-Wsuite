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

### Phase 1 — Path centralization (blocks everything)
- [ ] Add `platform/paths.py`:
      `config_root() = platformdirs.user_config_dir("archiver-suite")`
      (`%APPDATA%\archiver-suite` on Win, `~/.config/archiver-suite` on POSIX),
      plus `state_dir()`, `locks_dir()`.
- [ ] Route every literal through it:
      `core/core/paths.py` (locks, progress.json, loop.json, recorder pid),
      `core/core/policy_store.py` (config.toml), `core/core/schema.py`
      (`DEFAULT_DB_PATH`), `core/core/migrate.py`, `core/core/sanitize.py`
      (banned-words file).
- [ ] Preserve the `ARCHIVER_DB` env override (already wins in `schema.py`).
- **Verify:** paths resolve under `%APPDATA%`; core selftest DB opens; macOS
  paths unchanged.

### Phase 2 — Instance lock (load-bearing)
- [ ] `platform/filelock.py`:
      POSIX = `fcntl.flock(LOCK_EX|LOCK_NB)` + `LOCK_SH` probe + `LOCK_UN`;
      Windows = `msvcrt.locking(fd, LK_NBLCK, 1)`. Both give **kernel
      auto-release on crash/kill** — the whole reason this module exists.
- [ ] Rewrite the three `InstanceLock`/`lock.py` holders to call the adapter,
      preserving the shared-lock live-holder probe.
- [ ] **Do NOT fall back to PID files** — reintroduces the stale-lock race the
      module was built to kill.
- **Verify:** 2nd instance fails while 1st holds; killing holder via Task
  Manager frees the lock instantly, no manual cleanup.

### Phase 3 — Recorder process-group kill (data-loss guard)
- [ ] `platform/procgroup.py`:
      POSIX = `start_new_session=True` spawn + `killpg(getpgid(pid), sig)`;
      Windows = spawn with `CREATE_NEW_PROCESS_GROUP`; stop via
      `CTRL_BREAK_EVENT` then escalate to a **Job Object** kill or
      `taskkill /PID <pid> /T /F` (`/T` kills the child tree — the invariant:
      parent death guarantees child ffmpeg death).
- [ ] Route `capture.py` spawn + `_signal_group` through the adapter. The
      existing `hasattr(os, "killpg")` bare-pid fallback is **insufficient** on
      Windows — it orphans ffmpeg.
- **Verify:** kill recorder mid-capture → **no** stray `ffmpeg.exe` in Task
  Manager, segment not left half-written.

### Phase 4 — Signals & shutdown
- [ ] Guard `SIGTERM` registration to POSIX. On Windows register `SIGINT` +
      `CTRL_CLOSE_EVENT` (or the service stop handler) → same `stop_event` →
      clean drain exit.
- [ ] `recorder/cli.py` stop command (`os.kill(pid, SIGTERM)`) → send
      `CTRL_BREAK_EVENT`/`taskkill` via the procgroup adapter on Windows.

### Phase 5 — Daemonize + service management
- [ ] **Delete** `_daemonize()` (recorder double-fork). Make `--daemon` a no-op
      that points at the service manager (its own comment already says the
      service manager is the real backgrounding mechanism).
- [ ] `platform/service.py` replacing launchd (`ops/launchd/*.plist`,
      `launchctl` wiring). Recommended: **Task Scheduler** (run-at-startup +
      restart-on-failure, simplest) or **Windows Service** via `pywin32`
      `win32serviceutil` (survives logoff), or **NSSM** (wrap CLIs, zero code).
- [ ] Update `ops/ops/health.py` + `ops/ops/cli.py` to query the chosen
      mechanism instead of `launchctl list`. Also review `ops/ops/logrotate.py`
      — its copy-truncate exists because launchd can't be signalled to reopen;
      revisit under the Windows service model.

---

## 4. Already portable — do NOT touch
- **SQLite WAL + single-file coordination** — identical on Windows. This is
  *why* the port is tractable.
- **Atomic writes** (`os.replace` + tmp + fsync in `heartbeat.py`,
  `policy_store.py`, sorter). `os.replace` is atomic on Windows too.
  ⚠️ **Caveat:** Windows cannot replace/delete a file with an **open handle** —
  audit that nothing holds the target open across the replace.
- **`os.kill(pid, 0)`** liveness probe (`heartbeat.py:43`, test_seams) — works
  on Windows.
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
- No `fcntl`, `os.fork`, `os.setsid`, `os.killpg`, or bare `SIGTERM` reachable on
  the Windows code path (`grep` clean under `if os.name == 'nt'`).
- All config under `%APPDATA%\archiver-suite`.
- Kill-tests leave no orphan ffmpeg and no stale locks.
- Workers auto-start on boot and restart on crash via the service manager.
- `test_seams.py` green on Windows.

---

## 7. Open decisions (need a call before/at Phase 5)
- **Service mechanism**: Task Scheduler vs Windows Service vs NSSM.
- **Port in place vs. fork the tree**: this folder can either hold the adapter
  package as a staging area, or the work lands directly in `../Archiver suite`
  behind `os.name` guards (recommended — keeps one codebase, no drift).
- **Distribution**: ship as pipx installs (as today) or bundle a PyInstaller
  `.exe` per binary for non-technical Windows users.
