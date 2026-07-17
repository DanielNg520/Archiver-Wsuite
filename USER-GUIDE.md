# User Guide

Task-oriented reference for daily use. Architecture is in **README.md**;
the dense code map is in **DESIGN.md**; unattended (Task Scheduler) setup is in
**AUTOMATION.md**.

The model in one line: **producers write file-rows into one `suite.db`; the
dispatcher uploads them to Telegram.** You mostly drop files in the right place
and let it run.

---

## The ways content gets uploaded

| You want to… | Put files here | Routed to |
|---|---|---|
| Archive a social account | (downloaded) `output_dir/<platform>/<user>/` | per-platform/user chat |
| Capture a TikTok live | (recorded automatically) | TikTok-live chat |
| Send a hand-managed library like a platform | `output_dir/<localname>/<user>/` after `archiver local add` | per-`localname`/user chat |
| Keep a built-in platform but manage files yourself | `output_dir/instagram/<user>/` + `archiver download set --platform instagram --enabled false` | that platform's chat |
| Send loose files to a specific channel | `routes_dir/[<label>~]<chat_id>[.t<topic>]/…` | that chat_id (+ topic) |

---

## Platforms (downloaded)

```bash
archiver config add --platform instagram --user someone
archiver start                # run continuously (was: loop)
archiver start --once         # single cycle (was: run)
```

Platform uploads **batch**: an album is held until 10 items accumulate (or 7
days), then sent. Tune with `dispatcher config set min_batch_size`.

### Download off, still upload (manual backup of a platform)

```bash
archiver download set --platform instagram --enabled false   # stop fetching
archiver download                       # show resolved on/off per platform
archiver download unset --platform instagram   # back to default (on)
```
With download off, every run still walks `output_dir/instagram/` and uploads
everything — configured users, disk-discovered users, and loose root files —
and needs no cookies. (Keep `instagram` in `ENABLED_PLATFORMS` with ≥1
configured user so the platform still exists.)

## Local platforms (no download, you manage the files)

```bash
archiver local add mylibrary       # then drop files under output_dir/mylibrary/<username>/
archiver local list
archiver local remove mylibrary    # files on disk kept
```
Each subfolder is a username; routed via `TELEGRAM_CHAT_ID_MYLIBRARY[_<USER>]`.

## Sorting `unsorted/` into platform/user folders

Files named `<username>_<unixtimestamp>_…` dumped into `output_dir/unsorted/`
can be auto-filed into `output_dir/<platform>/<username>/` (created if absent):

```
output_dir/unsorted/1stagram_0406_1780186897_3915641126.mp4
    → output_dir/instagram/1stagram_0406/1stagram_0406_1780186897_3915641126.mp4
```

The username is everything before the first 10-digit Unix timestamp segment, so
usernames may contain digits and underscores. A file with no recognizable
timestamp is left in `unsorted/` and logged — never guessed. Sidecars
(`.json` / `.info.json`) travel with their media; existing files are never
overwritten. After sorting, the normal reconcile/upload path takes over.

```bash
archiver sort                                    # default platform: instagram
archiver sort --platform tiktok --dry-run        # preview, change nothing
archiver auto-sort set --enabled true --platform instagram  # run each cycle
```

## Loose files → a specific chat (chat_id folders)

```
output_dir/-1001234567890/holiday clip.mp4      → sent individually, caption "holiday clip"
output_dir/-1001234567890/Beach day/John.jpg    → album, caption "Beach day\nJohn\nJess"
                         /Beach day/Jess.jpg
```
- **Directly in the chat_id folder** → one message each, filename as caption.
- **In a subfolder** → one album per subfolder, subfolder name + filenames.

```bash
archiver ingest                                  # scan chat_id folders under the routes root
archiver ingest --path "/any/folder" --chat -100123   # ingest an arbitrary folder
archiver auto-ingest set --enabled true          # do it automatically every cycle
```
A top-level folder that's neither a known/local platform nor a valid chat_id is
skipped with a warning — never guessed.

**Name a route folder for humans.** Prefix any route folder with a readable
label and a `~`, and/or suffix it with a forum topic `.t<topic_id>`:

```
output_dir/family-chat~-1001234567890/…          → chat -100…, General topic
output_dir/memes~-1001234567890.t42/…            → chat -100…, forum topic 42
output_dir/-1001234567890/…                      → still works (bare id, no label)
```
The label is cosmetic — it's stripped before routing, so files still go to the
bare chat_id. Split is on the **last** `~`, so the label may contain spaces,
underscores, or dots. (Avoid `[name]_chat_id`-style names: `_` is a legal
`@handle` character and `[ ]` are PowerShell/shell wildcards.) Do **not** start
the folder name with a `.` — a leading dot marks a hidden/working dir and the
whole folder is skipped by the scanner (`.name~<chat_id>` never routes;
`name.surname~<chat_id>` is fine).

The chat_id scan root is `ROUTES_DIR` (`.env`), which defaults to `OUTPUT_DIR` —
set it to keep route folders on a different volume than the platform downloads
(the two-root split; see README "On-disk layout").

---

## Dedup & cleanup (automatic)

- **No duplicate is ever uploaded.** Every file is content-hashed; if those
  bytes were already sent, the dispatcher suppresses the copy and deletes it.
- **Move an old, already-uploaded file back in** → recognized by content (even
  renamed) and deleted from disk instead of re-uploaded.
- **One-time:** `archiver backfill` after upgrading so this covers pre-upgrade
  files.

## Delete-after-upload

```bash
archiver policy set --delete true                    # global ON
archiver policy set --platform x --delete false      # …except X
dispatcher config set delete_after_upload true --platform orphaned   # chat_id folders
dispatcher config set delete_after_upload_records true               # live recordings
```
Restart the dispatcher after changing delete/batch policies.

## Recorder split mode (slice big recordings into ≤2 GiB parts)

By default a file is only split when it exceeds the ~3.9 GiB Telegram upload
ceiling. To split **every** recording over 2 GiB (after it's made
Telegram-compatible) into ≤2 GiB parts — each shipped as one ordered album —
turn on split mode in the recorder's config:

```toml
# C:\Users\danie\.archive\.config\recorder\config.toml
[recorder]
split_at_chunk_size = true   # split recordings over the chunk size
split_chunk_gib     = 2.0    # part size / split trigger (default 2 GiB)
```

Applies only to the recorder output folder (reconciled by the archiver). Needs
AutoSplitter installed; without it an oversize recording is left on disk and
retried, never shipped broken.

**When do dropped files get picked up?** `archiver loop` runs a background
**ingest sweeper** every ~3 minutes (`--ingest-interval`) that walks the drop
folders — the record folder, orphaned chat_id dirs, and local platforms —
independent of the slow 2–4h download cycle. So a file you drop into the record
folder is enqueued within minutes, then the dispatcher uploads it. (A *running*
loop won't see this code or a changed `--ingest-interval` until it restarts.)

---

## Banned accounts (automatic quarantine)

When an account is detected gone (banned / suspended / deleted), it's retired
automatically: dropped from the active list, recorded on the app's banned
roster, and its folder moved to `<platform>\.deleted\<user>\` — **moved, not
deleted**, so an unban restores everything.

- **Archiver:** the extractors detect it during a run (explicit "account
  suspended"-style errors only; a private or flaky account never trips it).
- **Recorder:** a TikTok user unstartable for **≥6 cooldowns across ≥24 h**
  AND whose profile page explicitly says the account is gone is auto-banned.
  Network errors, bot-walls and private accounts never ban — they just stay
  in the normal retry cooldown.

```bash
archiver banned                       # roster + reason + when
archiver banned unban --platform tiktok --user someone --re-add
recorder banned                       # recorder's own roster
recorder banned unban --user someone --re-add
```
`unban` also moves the folder back out of `.deleted\`. The two rosters are
independent — banning in one app doesn't ban in the other.

## Deleting a user (manual, goes to the Recycle Bin)

Distinct from a ban: `archiver delete` is intentional and terminal, but staged
so nothing un-uploaded is ever lost:

1. **Request** — dropped from the active list immediately; files and DB rows
   untouched.
2. **Trash** — once **every** row is uploaded, the folder goes to the Windows
   **Recycle Bin** (checked every cycle; never while a live recording holds
   the user).
3. **Purge** — 30 days after the trash, the DB rows are deleted. (This also
   forgets dedup memory — re-adding the user later could re-upload old bytes.)

```bash
archiver delete --platform x --user someone     # start the lifecycle
archiver deleting                               # status + purge countdown
archiver deleting cancel --platform x --user someone
```
`cancel` before the trash restores the user completely; after the trash it
stops the row purge (recover the folder from the Recycle Bin yourself).

## Inspecting & fixing the queue

```bash
<app> stats                          # DB counts (archiver/dispatcher/recorder)
dispatcher queue list --status failed --limit 100
dispatcher queue retry <id>          # failed/sent → pending
dispatcher queue cancel <id>         # pending/sending → failed
archiver reset failed                # re-queue all failed
archiver reset uploads --platform x  # re-send everything (no re-download)
```

## Settings

```bash
dispatcher config set <key> <value> [--platform P] [--user U]
dispatcher config get <key> [--platform P]
dispatcher config list
```
Common keys: `min_batch_size`, `min_batch_max_wait_h`, `delete_after_upload`,
`delete_after_upload_records`, `dedup_after_download`, `auto_ingest_orphaned`,
`download_enabled`, `local_platforms`.

## Second (burner) account — optional

Route a dedicated set of chats through a second Telegram account; the primary
sends everything else and is the fallback if the burner can't log in. Nothing
changes until you register it — set up via the CLI only (no `.env` editing):

```bash
dispatcher burner login --phone +49…        # interactive; creates the session
dispatcher burner chats add -100123 100456  # dash-free numeric ok → -100456
dispatcher burner chats list                 # (remove <id…> also)
dispatcher burner status                     # active? authorized? which chats?
```

Restart the dispatcher after registering. Full details: **dispatcher/README.md**.

---

## "Why isn't my file uploading?"

1. **Platform file, <10 pending?** Batching — waits for 10 or 7 days.
   `dispatcher config set min_batch_size 1` to send now.
2. **Duplicate?** If its bytes were already sent, it's suppressed by design
   (and the copy deleted). Check the dispatcher log for "suppressed as
   duplicate".
3. **Loose folder not a chat_id?** Folders that aren't a chat_id or known
   platform are skipped — rename to `[<label>~]<chat_id>[.t<topic>]` (don't lead
   with a `.`) or use `ingest --path … --chat`.
4. **Dispatcher running?** The queue is durable; rows wait at `pending`.
5. **`failed` with `FilePartsInvalid`?** The file is over Telegram's ~3.9 GiB
   upload ceiling and can never send whole. Since 2026-07-12 this self-heals:
   the archiver's sweep splits it and requeues the parts automatically (one
   split per cycle). Details: [ops/RUNBOOK.md](ops/RUNBOOK.md)
   "FilePartsInvalid".
