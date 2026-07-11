# Media Archiver Suite

A four-process system that archives social media (X, Instagram, TikTok) and
TikTok live streams to Telegram, losslessly and unattended.

```
┌─────────────┐       ┌──────────────┐
│  archiver   │──┐    │   recorder   │
│ (VOD pull)  │  │    │ (live record)│
└─────────────┘  │    └──────┬───────┘
                 │           │
                 ▼           ▼
            ┌────────────────────┐
            │     suite.db       │   ← the ONE database (shared state)
            │    (items table)   │
            └─────────┬──────────┘
                      │
                      ▼
              ┌───────────────┐
              │  dispatcher   │──→ Telegram
              │ (owns session)│
              └───────────────┘

         ops  ──→ health checks + launchd control (reads everything, owns nothing)
```

## Why four processes instead of one

The original archiver did everything in one process: download, then upload
to Telegram inline. That coupling caused three problems this redesign fixes:

1. **One Telegram sender, many producers.** Telegram allows only sane use
   of one user session at a time. With a single archiver that was fine, but
   adding a live recorder meant two things wanting to send at once. The
   **dispatcher** is now the *sole* process that talks to Telegram; everything
   else writes jobs to a queue and the dispatcher drains it serially. No
   session contention, ever. (The dispatcher may hold a second, optional
   *burner* account for a dedicated set of chats — still one serial sender,
   still the only talker; see **dispatcher/README.md**.)

2. **Downloads shouldn't block on uploads.** A slow Telegram upload (or a
   FloodWait) used to stall the whole archive cycle. Now the archiver
   enqueues and moves on; uploading happens asynchronously in the dispatcher.

3. **Live recording is real-time; archiving is batch.** They have opposite
   scheduling needs. Splitting them lets the recorder react in seconds while
   the archiver runs every few hours.

## The one rule that holds it together

**There is exactly one source of truth: the `items` table in `suite.db`.**
A file's entire life — discovered, downloaded, queued, sending, sent or
failed — is one row with one `status` column. Every process reads and
writes that one table through a shared `core` library; none of them keeps
its own private copy of delivery state, so there is nothing to reconcile.

The processes stay separate (separate crashable, separately-scheduled
units — see below), but they share *code*, not duplicate it. `core` holds
the schema, the state-machine transitions, the policy store, and the
filesystem helpers. One definition, imported four times. (The earlier
design copied `policy_store`/`tg_router` into each package "to preserve
independence"; in practice that just meant the copies could drift against
the one schema they all depend on. Process isolation comes from running
separate processes — not from giving them disjoint code.)

Install: the **Install** section below. Day-to-day usage of every feature:
**USER-GUIDE.md**. Dense architecture map for revising the code: **DESIGN.md**.
Running on **this Windows machine right now**: see **Quick start (Windows)**
just below — config, DB, sessions and cookies are already migrated and
validated; nothing needs re-auth or re-setup.

---

## Quick start (Windows — this machine, migrated 2026-07-07)

The suite was migrated from the macOS box and fully validated on this PC
(all selftests + 210-check seam test pass; suite.db integrity `ok`;
124k-item history intact). It is installed **exactly like macOS — pipx apps
with `core` injected editable** — so day-to-day you just type `dispatcher
status`, `ops health`, etc. No `PYTHONPATH`, no `python -m`.

**Already done on this machine** (one-time setup, listed here for a rebuild):

```powershell
# 1. UTF-8 mode — the ONE Windows delta from macOS. Without it the workers'
#    status glyphs (● ✓ →) crash with UnicodeEncodeError whenever stdout is
#    redirected (Task Scheduler capture, pipes). macOS is always UTF-8; this
#    makes Windows match. Persistent, per-user; restart the shell after.
setx PYTHONUTF8 1

# 2. Install each app as its own pipx venv, then inject the shared core
#    library editable (same two-step as macOS — apps don't depend on core
#    directly). pipx puts dispatcher/recorder/archiver/ops on PATH via
#    %USERPROFILE%\.local\bin.
pipx install .\dispatcher --python 3.13
pipx install .\recorder   --python 3.13
pipx install .\archiver   --python 3.13
pipx install .\ops        --python 3.13

pipx inject --editable dispatcher     .\core
pipx inject --editable recorder       .\core
pipx inject --editable media-archiver .\core   # archiver's package name
pipx inject --editable ops            .\core

# 3. Recorder's headless-browser download (age-restricted lives). OPTIONAL:
#    the recorder self-heals a missing/stale Chromium on first use (auto-runs
#    this once), but pre-running it avoids a one-time inline delay mid-stream.
& "$env:USERPROFILE\pipx\venvs\recorder\Scripts\python.exe" -m playwright install chromium
```

**Daily use** — bare commands, from anywhere:

```powershell
ops health          # system status
dispatcher start    # drain the queue → Telegram
recorder start      # watch TikTok lives
archiver start      # VOD pull cycle
```

To run unattended (auto-start at logon, restart on crash) register the
Task Scheduler services once — the Windows analog of `launchctl load`:

```powershell
ops install         # register task definitions (resolves the pipx .exes)
ops load            # start + enable all workers
```

After editing an app's code: `pipx reinstall <package>` (`dispatcher`,
`recorder`, `media-archiver`, `ops`). Editing `core` needs nothing — it's
injected editable, so changes are live in every app immediately.

Where everything lives on this machine:

| What | Where |
|------|-------|
| config / DB / sessions / cookies | `%APPDATA%\archiver-suite`, `%APPDATA%\dispatcher`, `%APPDATA%\recorder` |
| archiver downloads | `D:\archiver_downloads` |
| recorder output | `D:\records` |
| worker logs (service capture) | `%APPDATA%\archiver-suite\logs` |
| AutoSplitter (oversize-video splitter) | `C:\Users\danie\Documents\Coding\autosplitter` — sibling checkout, auto-discovered by `core.media_prep`; no config needed |

Requirements already satisfied on this box: Python 3.13, ffmpeg/ffprobe on
PATH (winget Gyan build), Firefox profile `default-release` present (cookie
auto-refresh works). Telegram sessions migrated as-is — if Telegram flags
the new device on first connect, re-auth interactively with the credentials
already in the `.env` files.

---

## The four modules

### dispatcher — the only thing that talks to Telegram

Owns the Telegram session(s). Polls the shared `items` table for `pending`
rows, claims one atomically, sends it, marks it `sent` (or `failed` after
retries). Optionally routes a dedicated set of chats through a second *burner*
account, with the primary as the fallback (**dispatcher/README.md**).
Handles FloodWait, retries with backoff, and an optional delete-after-upload
policy. A startup watchdog reverts rows stuck in `sending` (from a previous
crash) back to `pending`.

Priority order: lower number drains first. Archiver enqueues at **10**,
recorder at **20** — so VOD backlog sends ahead of (less time-sensitive)
finished recordings.

Detailed docs: `dispatcher/README.md`.

### archiver — pulls VODs from X / Instagram / TikTok

Your existing multi-platform archiver, now writing pending rows directly
into the shared `items` table instead of sending to Telegram itself. There
is no feature flag and no Telegram session in the archiver anymore — it
holds zero Telegram credentials. Writing the row *is* the handoff; the
dispatcher claims it on its next poll.

The download cutoff (`date_floor`) reads `MAX(upload_date WHERE
status='sent')` straight from the one table, so it only advances past
posts the dispatcher has actually confirmed delivered. A crash or a slow
queue never loses ground, even though sending is asynchronous — and there
is no mirror column or reconcile step to keep in sync.

While the recorder is actively recording TikTok, the archiver skips the
TikTok *download* step (it reads the recorder's lockfile). Uploads of existing
TikTok backlog still proceed.

### recorder — captures TikTok live streams

Watches a priority-ordered list of TikTok usernames. When one goes live, it
records the stream with yt-dlp (ffmpeg backend), and when the stream ends it
registers the file in `suite.db` as a pending dispatcher item. Records one
stream at a time; between recordings it re-scans the list so a higher-priority
user who just went live gets picked up.

Holds a lockfile (`~/.config/archiver-suite/locks/tiktok.lock`) only while actively
recording, so the archiver knows to skip TikTok downloads during that window.

TikTok live detection uses the `TikTokLive` library (not fragile manual
scraping). ffmpeg must be installed.

Detailed docs: `recorder/` source headers.

### ops — health checks and service management

Reads the other three (via the OS service manager, the SQLite DBs, and the
lockfile) but imports none of them. The service seam is
`core.platform.service`: launchd LaunchAgents on macOS, **Task Scheduler
tasks on Windows** — same verbs on both. Provides:

```
ops health      one-shot system status
ops watch       auto-refreshing status
ops install     register the service definitions
ops load        start + enable all three services
ops unload      stop all three
ops restart <s> restart one service
```

Also ships `RUNBOOK.md` (failure recovery).

---

## On-disk layout

POSIX paths shown; on Windows every `~/.config/<app>` maps to
`%APPDATA%\<app>` (see `core.platform.paths`), worker logs land in
`%APPDATA%\archiver-suite\logs`, and the output volumes are
`D:\archiver_downloads` / `D:\records` on this machine.

```
~/.config/archiver-suite/
    suite.db                THE ONE DATABASE: items + checkpoints + circuit
                            + metadata (+ -wal, -shm while running)
    config.toml             shared policy store (user lists + per-user
                            delete-after-upload / dedup policies)

~/.config/dispatcher/
    .env                    Telegram credentials + chat routing
    session.session         dispatcher's Telegram session

~/.config/recorder/
    .env                    TIKTOK_COOKIES_FILE, paths
    config.toml             priority-ordered TikTok user list + output_dir;
                            optional split mode (split_at_chunk_size/_chunk_gib)

~/.local/log/
    {dispatcher,recorder,archiver}.log          rotating app logs (if wired)
    {dispatcher,recorder,archiver}.{out,err}.log  launchd capture (unbounded)

~/.recorder/
    pid                     recorder pid file
    room_id_cache.json      TikTok room id cache

/Volumes/StorEDGE/archiver_downloads/           media output (external drive)
~/recorder-output/                              recorder output
```

---

## Install

> **Same pipx flow on macOS and Windows.** The block below is the canonical
> install; on Windows use `.\` path separators and first run `setx PYTHONUTF8 1`
> (see Quick start above for why). This machine is already set up this way.

Install order **no longer matters** — `core` creates the schema
idempotently the first time any process connects. Each app is installed
with pipx, then the shared `core` library is injected (editable) into each
app's venv:

```
pipx install ./dispatcher --python 3.13
pipx install ./archiver   --python 3.13
pipx install ./recorder   --python 3.13
pipx install ./ops        --python 3.13

pipx inject --editable dispatcher     ./core
pipx inject --editable media-archiver ./core
pipx inject --editable recorder       ./core
pipx inject --editable ops            ./core
```

The dispatcher needs `hachoir` (Telethon's video-metadata backend) — it ships
as a declared dependency, so a clean `pipx install ./dispatcher` pulls it in.
Without it, album videos upload as 1×1 static images and the dispatcher
refuses to start.

Editing `core` needs no reinstall (editable). After editing a service:
`pipx reinstall <package> --python 3.13` (packages: `dispatcher`,
`media-archiver`, `recorder`, `ops`).

Schema is created and migrated idempotently on first open (`PRAGMA
user_version`, current `core.schema.SCHEMA_VERSION`) — install order doesn't
matter and there is nothing to run by hand. First run requires interactive
Telegram auth once (launchd can't answer the SMS prompt). See AUTOMATION.md
step 1.

---

## Daily operation, once automated

You don't run anything by hand. The service manager (launchd on macOS,
Task Scheduler on Windows) keeps all three alive. You check:

```
ops health          # (python -m ops health when running from source)
```

and read `RUNBOOK.md` when something's wrong. Full automation setup and the
recommended *staged* rollout (given prior kernel-panic history) are in
**AUTOMATION.md**.

---

## Module doc index

| Doc | Covers |
|-----|--------|
| `README.md` (this file) | architecture, install, layout |
| `DESIGN.md` | dense architecture map — modules, seams, choke points, invariants |
| `USER-GUIDE.md` | task-oriented daily use — every upload path + commands |
| `AUTOMATION.md` | launchd setup, staged rollout, every automated piece |
| `archiver/README.md` | archiver CLI, env vars, platforms |
| `dispatcher/README.md` | dispatcher CLI, env vars, queue smoke test |
| `ops/RUNBOOK.md` | failure recovery procedures |

**Capabilities** (see USER-GUIDE.md): global content-hash dedup (the same bytes
never upload twice; re-introduced uploads are cleaned up), a min-batch policy
(platform albums wait for 10 items / 7 days), chat_id "orphaned" folders for
loose files (with forum-topic routing and split-album grouping for oversize
files), local (download-free) platforms, a per-platform download toggle,
banned-word sanitizing of filenames/captions, auto-retirement of
suspended/banned accounts, and a harmonized CLI (`start`/`--once`, shared
`stats`/`queue`/`config`). Self-healing throughout: stuck-row watchdog, transient
failed-row auto-recovery, FloodWait/stall recovery, and a circuit breaker — see
DESIGN.md's self-healing map.
