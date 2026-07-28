# AUTOMATION.md — Running the suite unattended

The complete guide to automating all four processes via systemd user services,
plus what each automated piece does and how to verify it. Install and on-disk
layout live in [README.md](README.md); this doc is only about running headless.

---

## What "automated" means here

`ops` registers systemd user services through `core.platform.service`. Each
task runs as a user daemon and restarts on failure.

| Task | Label | What systemd does |
|------|-------|--------------------------|
| dispatcher | `com.duy.dispatcher` | starts at boot (via linger), restart-on-failure, drains the queue forever |
| recorder | `com.duy.recorder` | starts at boot (via linger), restart-on-failure, watches for lives forever |
| archiver | `com.duy.archiver` | starts at boot, runs `archiver loop` PLUS a background ingest sweeper; restart-on-failure |
| logrotate | `com.duy.logrotate` | a systemd timer (not a daemon), daily 04:05, copytruncate-rotates `~/.archive/.config/archiver-suite/logs/*.log` |

**How the tasks run hidden.** Each task's action is a systemd service unit
(`~/.config/systemd/user/<label>.service`) that runs the worker. Task liveness
and RestartOnFailure is handled natively by systemd. stdout/err are redirected to
`~/.local/log/<tag>.{out,err}.log`.

RestartOnFailure interval is 30 seconds;
`ops install` regenerates every definition with this machine's absolute pipx
paths, so they always match where the CLIs actually live.

---

## Prerequisites (all must be true before `ops load`)

Run from anywhere (the CLIs are on PATH via pipx). Every check must pass.

```bash
# 1. All four resolve on PATH
which dispatcher archiver recorder ops

# 2. All import cleanly
dispatcher --help > /dev/null && echo "dispatcher OK"
archiver   --help > /dev/null && echo "archiver OK"
recorder   --help > /dev/null && echo "recorder OK"
ops health         > /dev/null && echo "ops OK"

# 3. ffmpeg present (recorder + archiver need it)
which ffmpeg

# 4. The shared suite database can be initialized
export PYTHONPATH="core"
python -c "from core import ItemStore; s=ItemStore.open(); s.close(); print('suite.db OK')"

# 5. The output root exists / is writable
test -d ~/.archive || echo "missing"
```

Not installed yet? See [README.md](README.md) → **Install (Linux)**.

---

## Step 1 — Telegram auth (one time, interactive, BEFORE systemd)

systemd cannot type the SMS code. Authenticate the dispatcher's session
by hand once:

```bash
dispatcher start
```

Enter the code Telegram sends. When you see `telethon: connected` and it idles
on the queue, Ctrl-C. Confirm the session file exists:

```bash
test -f ~/.archive/.config/dispatcher/session.session && echo "OK"
```

The archiver has no Telegram session — the dispatcher owns Telegram credentials
and routing.

**Optional burner account.** If you route some chats through a second (burner)
account, register it now too:

```bash
dispatcher burner login --phone +49…     # interactive, one time
dispatcher burner chats add -100123      # chats the burner should send
dispatcher burner status                 # confirm authorized + chats
```

---

## Step 2 — Install the task definitions

`ops install` generates all four definitions (with absolute paths) and registers them via `systemctl --user`, and creates the log dir:

```bash
ops install
```

This does **not** start anything — the tasks activate on `ops load` (next step),
which lets you stage the rollout. The **logrotate** calendar job is harmless on
its own and keeps the captured `logs/*.log` from growing unbounded.

> **IMPORTANT**: To allow systemd user services to run when you are not logged in, you must enable linger:
> `loginctl enable-linger $USER`

---

## Step 3 — Start the workers

You can load everything at once:

```bash
ops load
ops health
```

…or stage it if you'd rather add I/O load incrementally:

```bash
ops load dispatcher   # lowest load: idle-polls an empty queue
ops health
ops load archiver     # adds the download cycle + ingest sweeper
ops health
ops load recorder     # heaviest I/O (live video capture) — add last
ops health
```

Feed the queue manually to watch a drain end-to-end before walking away:

```bash
archiver start --once   # downloads, inserts pending rows, exits
ops watch               # watch the dispatcher drain them
```

---

## Step 4 — Verify full automation

```bash
ops health
```

Expect all three workers `running`, queue `pending` trending toward 0,
`tiktok.lock` `not held` (unless recording), disk healthy. Then `ops watch` and
leave it open through one archiver cycle: archiver enqueues → dispatcher sends →
rows go `sent`.

To confirm restart-on-crash works, kill a worker tree and watch systemd
respawn it within ~30 seconds:

```bash
ops restart dispatcher   # or kill it, then:
ops health
```

---

## What each automated piece does, end to end

1. **At boot**, systemd starts dispatcher, recorder, archiver.
2. **Dispatcher** connects to Telegram and polls `items` every 2s. On startup it
   runs the watchdog (reverts stuck `sending` rows).
3. **Archiver** runs a cycle: for each configured user on each platform, it
   downloads new media and inserts pending `items` rows (priority 10), then
   sleeps 2–4h and repeats. If the recorder holds the TikTok lock, it skips
   TikTok downloads that cycle.
   - **Walk order is staleness-first**, not alphabetical: within a platform the
     users scanned longest ago (or never) go first, so a cycle — or a restart
     mid-cycle — favors whoever a previous pass didn't reach instead of starving
     the tail. Bounded jitter shuffles equal-staleness users (anti-detection).
     **Priority users** (hand-marked via `archiver priority`, or opt-in
     auto-detected regular posters) lead every cycle and get re-scanned partway
     through a long pass so frequent posters stay fresh. See USER-GUIDE.md
     "Scan order & priority users" for the knobs.
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
   account is gone, the archiver moves that user into a per-platform banned roster.
7. **You** run `ops health` whenever you want to check, and consult
   [ops/RUNBOOK.md](ops/RUNBOOK.md) if something breaks.

---

## Managing users and policies (no restart needed)

```bash
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
archiver auto-ingest set --enabled true

# Upload batching (dispatcher; restart it to apply)
dispatcher config set min_batch_size 10
```

Archiver-side settings are read on the next cycle — no reload. The **dispatcher** reads
its policies at startup, so `ops restart dispatcher` after changing those.

---

## Redeploying after a code change

```bash
ops update            # from the repo root
```

One command: fingerprints the source, drains the dispatcher **cleanly**, `pipx install --force`s the three worker
apps, re-injects editable `core`, reloads every worker, and enters
`ops watch`. Full detail is under "Updating the code" in [ops/RUNBOOK.md](ops/RUNBOOK.md).

---

## Turning it off

```bash
ops unload            # stops + disables all workers
ops unload recorder   # stop just one while you edit its config
```

There is no direct-send rollback path. If the dispatcher is unhealthy, stop the
services, fix or re-authenticate the dispatcher, and let the durable `pending`
rows drain when it is healthy. See [ops/RUNBOOK.md](ops/RUNBOOK.md).
