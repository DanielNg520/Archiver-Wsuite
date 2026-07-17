# Media Archiver Suite

A four-process system that archives social media (X, Instagram, TikTok) and
TikTok live streams to Telegram, losslessly and unattended. Runs on Windows
under Task Scheduler.

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

    ops  ──→ health checks + Task Scheduler control (reads everything, owns nothing)
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
   still the only talker; see [dispatcher/README.md](dispatcher/README.md).)

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

The processes stay separate (separately crashable, separately-scheduled
units), but they share *code*, not duplicate it. `core` holds the schema,
the state-machine transitions, the policy store, and the filesystem helpers.
One definition, imported four times. Process isolation comes from running
separate processes — not from giving them disjoint code.

## Where to read next

| Doc | Covers |
|-----|--------|
| **README.md** (this file) | architecture, install, on-disk layout — the hub |
| [DESIGN.md](DESIGN.md) | dense code map — modules, seams, choke points, invariants |
| [PROJECT_MAP.md](PROJECT_MAP.md) | 30-second orientation card |
| [USER-GUIDE.md](USER-GUIDE.md) | task-oriented daily use — every upload path + commands |
| [AUTOMATION.md](AUTOMATION.md) | Task Scheduler setup, what each automated piece does |
| [ops/RUNBOOK.md](ops/RUNBOOK.md) | failure recovery procedures |
| [archiver/README.md](archiver/README.md) | archiver CLI, env vars, platforms |
| [dispatcher/README.md](dispatcher/README.md) | dispatcher CLI, env vars, burner account, queue smoke test |
| [recorder/README.md](recorder/README.md) | recorder config, split mode, cookies, quality/fallback behavior |
| [CLAUDE.md](CLAUDE.md) | traps for agent/assistant sessions (MSIX virtualization, `python -m pipx`, test invocation) |

Historical plan docs (kept as period records, paths may be outdated):
[CONVERSION_PLAN.md](CONVERSION_PLAN.md) / [WINDOWS_PORT.md](WINDOWS_PORT.md)
(the completed 2026-07 Windows port) and
[REFACTOR_PLAN_bans_and_paths.md](REFACTOR_PLAN_bans_and_paths.md) (pending:
ban quarantine + two-root storage split).

---

## Install (Windows)

The suite installs as **pipx apps with the shared `core` injected editable** —
so day-to-day you just type `dispatcher status`, `ops health`, etc. No
`PYTHONPATH`, no `python -m`.

```powershell
# 1. UTF-8 mode — the ONE Windows delta. Without it the workers' status glyphs
#    (● ✓ →) crash with UnicodeEncodeError whenever stdout is redirected (Task
#    Scheduler capture, pipes). Persistent, per-user; restart the shell after.
setx PYTHONUTF8 1

# 2. Install each app as its own pipx venv, then inject the shared core
#    library editable (apps don't depend on core directly). pipx puts
#    dispatcher/recorder/archiver/ops on PATH via %USERPROFILE%\.local\bin.
#    Always `python -m pipx` (never bare `pipx`) — stale shims shadow the exe
#    in some shells (see CLAUDE.md).
python -m pipx install .\dispatcher --python 3.13
python -m pipx install .\recorder   --python 3.13
python -m pipx install .\archiver   --python 3.13
python -m pipx install .\ops        --python 3.13

python -m pipx inject --editable dispatcher     .\core
python -m pipx inject --editable recorder       .\core
python -m pipx inject --editable media-archiver .\core   # archiver's package name
python -m pipx inject --editable ops            .\core

# 3. Recorder's headless-browser download (age-restricted lives). OPTIONAL:
#    the recorder self-heals a missing/stale Chromium on first use (auto-runs
#    this once), but pre-running it avoids a one-time inline delay mid-stream.
& "$env:USERPROFILE\pipx\venvs\recorder\Scripts\python.exe" -m playwright install chromium
```

Install order does not matter — `core` creates the schema idempotently
(`PRAGMA user_version` against `core.schema.SCHEMA_VERSION`) the first time any
process connects. There is nothing to run by hand.

`hachoir` (Telethon's video-metadata backend) is a **declared dispatcher
dependency**, so a clean `python -m pipx install .\dispatcher` pulls it in. Without it,
album videos upload as 1×1 static images and the dispatcher refuses to start
(`python -m pipx inject dispatcher hachoir` to repair an old venv).

**First run requires interactive Telegram auth once** (Task Scheduler can't
answer the SMS prompt) — see [AUTOMATION.md](AUTOMATION.md) step 1.

**After editing code:** `python -m pipx reinstall <package>` (`dispatcher`, `recorder`,
`media-archiver`, `ops`). Editing `core` needs nothing — it's injected editable,
so changes are live in every app immediately.

Requirements on this box (already satisfied): Python 3.13; `ffmpeg`/`ffprobe`,
`yt-dlp`, `gallery-dl` on PATH; a Firefox profile for cookie auto-refresh.

### Daily use — bare commands, from anywhere

```powershell
ops health          # system status
dispatcher start    # drain the queue → Telegram
recorder start      # watch TikTok lives
archiver start      # VOD pull cycle
```

To run unattended (auto-start at logon, restart on crash) register the Task
Scheduler services once — see [AUTOMATION.md](AUTOMATION.md):

```powershell
ops install         # register task definitions (resolves the pipx .exes)
ops load            # start + enable all workers
```

---

## On-disk layout

The suite is fully self-contained under the `.archive` root: config, DB,
sessions, cookies, logs, and locks live in `.archive\.config` (see
`core.platform.paths`; legacy pre-2026-07 installs under `%APPDATA%` are
picked up until `tools/migrate_config_to_archive.py --apply` moves them —
`%CONFIG%` below means `C:\Users\danie\.archive\.config`).

| What | Where (this machine) |
|------|----------------------|
| config / DB / sessions / cookies / logs / locks | `%CONFIG%\archiver-suite`, `%CONFIG%\dispatcher`, `%CONFIG%\recorder` |
| media output (`OUTPUT_DIR`) | `C:\Users\danie\.archive` |
| chat_id route folders (`ROUTES_DIR`) | defaults to `OUTPUT_DIR` (single-tree layout); set it to move ONLY the route folders to another volume — everything else stays put |
| recorder output | `C:\Users\danie\.archive\.records` (dot-prefixed so the orphaned scanner skips it) |
| worker logs (service capture) | `%CONFIG%\archiver-suite\logs` |
| AutoSplitter (oversize-video splitter) | `C:\Users\danie\Documents\Coding\autosplitter` — sibling checkout, auto-discovered by `core.media_prep`; no config needed |

```
%CONFIG%\archiver-suite\
    suite.db                THE ONE DATABASE: items + checkpoints + circuit
                            + metadata (+ -wal, -shm while running)
    config.toml             shared policy store (user lists + per-user
                            delete-after-upload / dedup policies)
    .env                    OUTPUT_DIR, ROUTES_DIR + shared tunables
    cookies\ , logs\ , locks\ , launchers\

%CONFIG%\dispatcher\
    .env                    Telegram credentials + chat routing
    session.session         dispatcher's Telegram session

%CONFIG%\recorder\
    .env                    TIKTOK_COOKIES_FILE
    config.toml             priority-ordered TikTok user list + output_dir;
                            optional split mode (split_at_chunk_size/_chunk_gib)

C:\Users\danie\.archive\
    x\ tiktok\ instagram\ …   platform download folders (per-user subfolders)
    <platform>\.deleted\      quarantined banned users (moved, not deleted —
                              restored by `banned unban`; scanners skip dot-dirs)
    [<label>~]<chat_id>[.t<topic>]\   orphaned route folders (loose files → a
                              chat); optional `<label>~` prefix is cosmetic
                              (stripped before routing), optional `.t<topic>`
                              targets a forum topic. Live under ROUTES_DIR once
                              the two-root split is applied; here while unset
    .records\                 recorder output
```

> **Two-root split:** `ROUTES_DIR` (unset ⇒ `= OUTPUT_DIR`, byte-identical
> single-tree behavior) points the chat_id ingest scan at a separate volume;
> platform downloads, `.records` and the `.deleted\` quarantine always stay
> under `OUTPUT_DIR`. Apply the physical move with
> `tools/migrate_split_roots.py` (workers stopped → `--apply` → set
> `ROUTES_DIR` → restart). Design history: `REFACTOR_PLAN_bans_and_paths.md`.

---

## The four modules

### dispatcher — the only thing that talks to Telegram

Owns the Telegram session(s). Polls the shared `items` table for `pending`
rows, claims one atomically, sends it, marks it `sent` (or `failed` after
retries). Optionally routes a dedicated set of chats through a second *burner*
account, with the primary as the fallback. Handles FloodWait, retries with
backoff, and an optional delete-after-upload policy. A startup watchdog reverts
rows stuck in `sending` (from a previous crash) back to `pending`.

Priority order: lower number drains first — recorder (5), chat_id folders (6),
archiver (10) — so live recordings and loose drops send ahead of VOD backlog.

Detailed docs: [dispatcher/README.md](dispatcher/README.md).

### archiver — pulls VODs from X / Instagram / TikTok

Downloads platforms (X/IG via gallery-dl, TikTok via yt-dlp) and writes pending
rows directly into the shared `items` table instead of sending to Telegram
itself. There is no Telegram session in the archiver — it holds zero Telegram
credentials. Writing the row *is* the handoff.

The download cutoff (`date_floor`) reads `MAX(upload_date WHERE status='sent')`
straight from the one table, so it only advances past posts the dispatcher has
actually confirmed delivered. While the recorder is actively recording TikTok,
the archiver skips the TikTok *download* step (it reads the recorder's
lockfile); uploads of existing TikTok backlog still proceed.

Detailed docs: [archiver/README.md](archiver/README.md).

### recorder — captures TikTok live streams

Watches a priority-ordered list of TikTok usernames. When one goes live, it
records the stream with yt-dlp (ffmpeg backend), and when the stream ends it
registers the file in `suite.db` as a pending dispatcher item. Records one
stream at a time; between recordings it re-scans the list so a higher-priority
user who just went live gets picked up.

Holds a lockfile (`%CONFIG%\archiver-suite\locks\tiktok.lock`) only while
actively recording, so the archiver knows to skip TikTok downloads during that
window. Live detection uses the `TikTokLive` library (not fragile scraping).

Detailed docs: recorder source headers; [DESIGN.md](DESIGN.md).

### ops — health checks and service management

Reads the other three (via Task Scheduler, the SQLite DB, and the lockfile) but
imports none of them. The service seam is `core.platform.service` (Task
Scheduler tasks on Windows, launchd on macOS — same verbs on both). Provides:

```
ops health        one-shot system status
ops watch         auto-refreshing status
ops install       register the service definitions
ops load          start + enable all workers
ops unload        stop all workers
ops restart <s>   restart one service (dispatcher|recorder|archiver)
```

Also ships [ops/RUNBOOK.md](ops/RUNBOOK.md) (failure recovery).

---

## Capabilities

Global content-hash dedup (the same bytes never upload twice; re-introduced
uploads are cleaned up), a min-batch policy (platform albums wait for 10 items /
7 days), chat_id "orphaned" folders for loose files (with forum-topic routing
and split-album grouping for oversize files), local (download-free) platforms, a
per-platform download toggle, banned-word sanitizing of filenames/captions,
auto-retirement of suspended/banned accounts, and a harmonized CLI
(`start`/`--once`, shared `stats`/`queue`/`config`). Self-healing throughout:
stuck-row watchdog, transient failed-row auto-recovery, FloodWait/stall
recovery, and a circuit breaker — see [DESIGN.md](DESIGN.md)'s self-healing map.
Day-to-day usage of every feature: [USER-GUIDE.md](USER-GUIDE.md).
