# AUTOMATION.md — Running the suite unattended

The complete guide to automating all four processes via Windows Task Scheduler,
plus what each automated piece does and how to verify it. Install and on-disk
layout live in [README.md](README.md); this doc is only about running headless.

---

## What "automated" means here

`ops` registers Task Scheduler tasks through `core.platform.service` — one per
worker, plus a daily log-rotation job. Each task runs at logon and restarts on
failure, the direct analog of a launchd LaunchAgent.

| Task | Label | What Task Scheduler does |
|------|-------|--------------------------|
| dispatcher | `com.duy.dispatcher` | starts at logon, restart-on-failure, drains the queue forever |
| recorder | `com.duy.recorder` | starts at logon, restart-on-failure, watches for lives forever |
| archiver | `com.duy.archiver` | starts at logon, runs `archiver loop` (download cycle → sleep 2–4h → repeat) PLUS a background ingest sweeper that enqueues drop-folder files every ~3 min; restart-on-failure |
| logrotate | `com.duy.logrotate` | a calendar job (not a daemon), daily 04:05, copytruncate-rotates `C:\Users\danie\.archive\.config\archiver-suite\logs\*.log` so captured output can't grow unbounded |

**How the tasks run hidden.** Each task's action is a generated VBScript shim
(`C:\Users\danie\.archive\.config\archiver-suite\launchers\<tag>.vbs`) that runs the worker through
`cmd /c` with **no console window** but *waits* for it — so Task Scheduler keeps
tracking liveness and RestartOnFailure still fires on a crash, while a clean
stop (exit 0) does not trigger a restart. stdout/err are redirected to
`C:\Users\danie\.archive\.config\archiver-suite\logs\<tag>.{out,err}.log`.

RestartOnFailure interval is `PT1M` (Task Scheduler's hard 1-minute minimum);
`ops install` regenerates every definition with this machine's absolute pipx
`.exe` paths, so they always match where the CLIs actually live.

> Both the task definitions and the launcher shims embed absolute config/log
> paths — after moving the config root (see "Config migration" in
> [ops/RUNBOOK.md](ops/RUNBOOK.md)) run `ops uninstall` + `ops install` to
> regenerate them.

---

## Prerequisites (all must be true before `ops load`)

Run from anywhere (the CLIs are on PATH via pipx). Every check must pass.

```powershell
# 1. All four resolve on PATH
Get-Command dispatcher, archiver, recorder, ops | Select-Object Name, Source

# 2. All import cleanly
dispatcher --help > $null; if ($?) { "dispatcher OK" }
archiver   --help > $null; if ($?) { "archiver OK" }
recorder   --help > $null; if ($?) { "recorder OK" }
ops health         > $null; if ($?) { "ops OK" }

# 3. ffmpeg present (recorder + archiver need it)
Get-Command ffmpeg | Select-Object Source

# 4. The shared suite database can be initialized
$env:PYTHONPATH = "core"
python -c "from core import ItemStore; s=ItemStore.open(); s.close(); print('suite.db OK')"

# 5. The output root exists / is writable
Test-Path C:\Users\danie\.archive
```

Not installed yet? See [README.md](README.md) → **Install (Windows)**.

---

## Step 1 — Telegram auth (one time, interactive, BEFORE Task Scheduler)

Task Scheduler cannot type the SMS code. Authenticate the dispatcher's session
by hand once:

```powershell
dispatcher start
```

Enter the code Telegram sends. When you see `telethon: connected` and it idles
on the queue, Ctrl-C. Confirm the session file exists:

```powershell
Test-Path $env:USERPROFILE\.archive\.config\dispatcher\session.session
```

The archiver has no Telegram session — the dispatcher owns Telegram credentials
and routing.

**Optional burner account.** If you route some chats through a second (burner)
account, register it now too — its login is also interactive and Task Scheduler
can't type the code:

```powershell
dispatcher burner login --phone +49…     # interactive, one time
dispatcher burner chats add -100123      # chats the burner should send
dispatcher burner status                 # confirm authorized + chats
```

A burner that isn't logged in never blocks the daemon — those chats just fall
back to the primary. See [dispatcher/README.md](dispatcher/README.md).

---

## Step 2 — Install the task definitions

`ops install` generates all four definitions (with absolute paths + the hidden
launcher shims) and registers them via `schtasks`, and creates the log dir:

```powershell
ops install
```

This does **not** start anything — the tasks activate on `ops load` (next step),
which lets you stage the rollout. The **logrotate** calendar job is harmless on
its own and keeps the captured `logs\*.log` from growing unbounded (gzip
history, copytruncate so the append handle is never orphaned).

---

## Step 3 — Start the workers

You can load everything at once:

```powershell
ops load
ops health
```

…or stage it if you'd rather add I/O load incrementally (bring one up, confirm
`ops health`, then add the next):

```powershell
ops load dispatcher   # lowest load: idle-polls an empty queue
ops health
ops load archiver     # adds the download cycle + ingest sweeper
ops health
ops load recorder     # heaviest I/O (live video capture) — add last
ops health
```

Feed the queue manually to watch a drain end-to-end before walking away:

```powershell
archiver start --once   # downloads, inserts pending rows, exits
ops watch               # watch the dispatcher drain them
```

---

## Step 4 — Verify full automation

```powershell
ops health
```

Expect all three workers `running`, queue `pending` trending toward 0,
`tiktok.lock` `not held` (unless recording), disk healthy. Then `ops watch` and
leave it open through one archiver cycle: archiver enqueues → dispatcher sends →
rows go `sent`.

To confirm restart-on-crash works, kill a worker tree and watch Task Scheduler
respawn it within ~1 minute:

```powershell
ops restart dispatcher   # or kill its tree in Task Manager, then:
ops health
```

---

## What each automated piece does, end to end

1. **At logon**, Task Scheduler starts dispatcher, recorder, archiver.
2. **Dispatcher** connects to Telegram and polls `items` every 2s. On startup it
   runs the watchdog (reverts stuck `sending` rows).
3. **Archiver** runs a cycle: for each configured user on each platform, it
   downloads new media and inserts pending `items` rows (priority 10), then
   sleeps 2–4h and repeats. If the recorder holds the TikTok lock, it skips
   TikTok downloads that cycle.
   - **Ingest sweeper (background, every ~3 min).** The heavy download cycle
     only reconciles the *drop folders* (record folder, orphaned chat_id dirs,
     local platforms) at its tail — hours apart. So `archiver loop` also runs a
     background thread that sweeps just those folders every `--ingest-interval`
     seconds (default 180, min 30; `0` disables) on its own DB connection. A
     hand-dropped file is enqueued within minutes. It shares a lock with the
     heavy run so the two never prep/split the same file at once.
4. **Recorder** polls its TikTok user list every 60s. When someone's live, it
   acquires the lock, records with yt-dlp until the stream ends, releases the
   lock, enqueues the file (priority 5), and re-scans.
5. **Dispatcher** claims queued rows in priority order — recorder (5), chat_id
   folders (6), archive media (10) — sends each to the resolved Telegram chat,
   marks `sent`, and (if the delete policy is on) removes the local file +
   sidecars.
6. **Auto-ban of gone accounts** — when an archiver cycle's extractor reports an
   account is gone (suspended/banned/deleted, distinct from cookie expiry), the
   archiver moves that user into a per-platform banned roster (`config.toml`)
   and prints a summary at the end of the run. Already-queued uploads still
   deliver. Inspect with `archiver banned list`; restore with `archiver banned
   unban --platform <p> --user <u> --re-add`. Detection is conservative (auth
   failures and per-item 404s never ban).
7. **You** run `ops health` whenever you want to check, and consult
   [ops/RUNBOOK.md](ops/RUNBOOK.md) if something breaks.

---

## Managing users and policies (no restart needed)

```powershell
# Archiver VOD users
archiver config add --platform x --user someone
archiver config list

# Recorder live users (order = priority)
recorder config add --user tiktoker
recorder config priority --user tiktoker --rank 1

# Delete-after-upload (dispatcher honors this)
archiver policy set --delete true --platform tiktok

# Local platforms (hand-managed folders, no download)
archiver local add mylibrary

# Per-platform download toggle (off = reconcile/upload only)
archiver download set --platform instagram --enabled false

# Auto-ingest chat_id folders each cycle (default off)
# folder name = destination: [<label>~]<chat_id>[.t<topic>] under ROUTES_DIR
archiver auto-ingest set --enabled true

# Upload batching (dispatcher; restart it to apply)
dispatcher config set min_batch_size 10
```

Archiver-side settings (users, local platforms, auto-ingest, download toggle,
delete *policy*) are read on the next cycle — no reload. The **dispatcher** reads
its policies (delete, min-batch) at startup, so `ops restart dispatcher` after
changing those.

> **Batching note:** with the default `min_batch_size = 10`, platform albums are
> held until 10 files accumulate (or 7 days). Set `min_batch_size 1` to send
> immediately. Recorder and chat_id uploads are never held.

---

## Turning it off

```powershell
ops unload            # stops + disables all workers (kills orphaned trees too)
ops unload recorder   # stop just one while you edit its config
```

There is no direct-send rollback path. If the dispatcher is unhealthy, stop the
services, fix or re-authenticate the dispatcher, and let the durable `pending`
rows drain when it is healthy. See [ops/RUNBOOK.md](ops/RUNBOOK.md).
