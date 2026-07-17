# RUNBOOK — archiver / recorder / dispatcher

Operations reference for the four-process system. Keep this where you'll find it
at 2am. Install and layout are in [../README.md](../README.md); unattended setup
in [../AUTOMATION.md](../AUTOMATION.md).

```
recorder ──┐
           ├──→ suite.db ──→ dispatcher ──→ Telegram
archiver ──┘
```

All workers run under Task Scheduler (`com.duy.*`). Manage them with the `ops`
tool, not raw `schtasks`, unless debugging Task Scheduler itself.

---

## Quick reference

```powershell
ops health                 # is everything alive? queue depth? disk?
ops watch                  # same, auto-refreshing
ops load                   # start + enable all workers (all, or `ops load <name>`)
ops unload                 # stop + disable all (kills orphaned trees too)
ops restart dispatcher     # restart one (dispatcher|recorder|archiver)
```

Log locations (all under `C:\Users\danie\.archive\.config\archiver-suite\logs\`):
- Captured worker stdout/err: `<name>.out.log` / `<name>.err.log`
- Rotated daily by the `logrotate` calendar job (copytruncate, gzip history)

When something's wrong, read the `.err` log first for crashes that happened
before app logging started.

---

## Telegram session died (dispatcher can't send)

Symptom: `ops health` shows dispatcher running but queue `pending` only climbs,
never `sent`. Log shows auth/session errors.

Telethon sessions expire or get invalidated (password change, logout-all-devices,
too-long offline). Re-auth is interactive, so Task Scheduler must be out of the
way:

```powershell
ops unload dispatcher
dispatcher start                            # complete the SMS code prompt
# wait for "connected", confirm a test send drains, then Ctrl-C
ops load dispatcher
```

The queue is durable — nothing is lost while the session is down. Jobs wait at
`pending`.

---

## TikTok cookies expired (recorder stops detecting / archiver TikTok fails)

Symptom: recorder never finds anyone live even when they are; or archiver TikTok
health check fails. Cookies from your browser go stale.

```powershell
archiver cookies refresh                    # pulls fresh cookies from Firefox
ops restart recorder
```

The recorder reads `TIKTOK_COOKIES_FILE` from `C:\Users\danie\.archive\.config\recorder\.env`. On this
machine it points at `C:\Users\danie\.archive\.config\archiver-suite\tiktok.txt`, so the archiver
refresh covers both.

---

## suite.db is corrupt

Symptom: a worker crashes on startup with `database disk image is malformed`, or
`ops health` can't read queue counts.

> **This suite has a corruption history** (see the memory note
> `suite-db-corruption-recovery`): it came from swapping/writing `suite.db` under
> live writers. **Always `ops unload` first** — stop every writer before touching
> the DB.

Prefer the repo's recovery tool, which salvages rows and preserves backups:

```powershell
ops unload
python tools\recover_suite_db.py            # inspect
python tools\recover_suite_db.py --apply    # recover + back up the corrupt copy
ops load
ops health
```

Manual fallback if you need it (`sqlite3` on PATH):

```powershell
ops unload
cd $env:USERPROFILE\.archive\.config\archiver-suite
sqlite3 suite.db ".recover" | sqlite3 suite_recovered.db
Rename-Item suite.db suite.db.corrupt
Rename-Item suite_recovered.db suite.db
ops load; ops health
```

If `.recover` fails entirely: keep `suite.db.corrupt`, recreate an empty
`suite.db` (delete `suite.db*` — removes `-wal`/`-shm` too), then run `archiver
bootstrap` / `archiver reconcile` for configured users to re-register files
still on disk. The media files remain the durable source.

---

## Recorder is stuck (recording that never ends, or won't pick up new lives)

Symptom: `tiktok.lock` shows HELD for hours, recorder pid alive, no new files.

A yt-dlp capture can hang if a stream half-dies (socket open, no data). Restart
the recorder — it terminates the capture cleanly (whole process tree) and
releases the lock on shutdown:

```powershell
ops restart recorder
```

If the lock is STILL held after restart (stale lock — recorder was force-killed
previously and `__exit__` never ran):

```powershell
Get-Content $env:USERPROFILE\.archive\.config\archiver-suite\locks\tiktok.lock   # check the pid inside
# if that pid is dead:
Remove-Item $env:USERPROFILE\.archive\.config\archiver-suite\locks\tiktok.lock
```

The archiver only skips TikTok *downloads* while this lock exists; a stale lock
silently blocks TikTok archiving, so clear it promptly. (Health reads the lock
liveness-gated and self-heals a dead one, but clearing it removes any doubt.)

---

## A folder keeps reappearing on a drive you cleared

Symptom: an output folder you deleted/formatted (e.g. an old `D:\...` root)
recreates itself, empty, and its rows error with `WinError 1005`
(volume not recognized).

Cause: stale `items` rows still hold the old path; a worker retrying them
recreates the parent via `mkdir`. Stop writers, delete the dead rows, remove the
folder:

```powershell
ops unload
python -c "import sqlite3,os; d=os.path.expanduser(r'~\.archive\.config\archiver-suite\suite.db'); c=sqlite3.connect(d); print('deleting', c.execute(\"DELETE FROM items WHERE REPLACE(file_path,char(92),'/') LIKE 'D:/%'\").rowcount); c.commit(); c.execute('PRAGMA wal_checkpoint(TRUNCATE)')"
Remove-Item 'D:\<stale-folder>' -Recurse -Force
ops load
```

Back up `suite.db` first; verify the rows aren't the only copy of live data
(check for a `content_hash` twin under the current root before deleting).

---

## Drain the queue manually (dispatcher won't start at all)

There is no manual send path by design — the dispatcher is the only process that
talks to Telegram. Fix the dispatcher, don't bypass it. To inspect what's stuck:

```powershell
dispatcher status                            # counts + top pending
dispatcher queue list --status failed --limit 100
dispatcher queue retry <id>                  # reset a failed row to pending
dispatcher queue cancel <id>                 # give up on a row
```

Keep the durable rows in `pending`, fix or re-authenticate the dispatcher, then
let it drain. (If a burner account is registered, the primary is already the
fallback for its chats — a logged-out burner never wedges the queue.)

---

## Uploads look "stuck" but nothing is failing (min-batch holding)

Symptom: `dispatcher stats` shows `pending` flat, none going to `failed`,
dispatcher healthy. Usually the **min-batch gate**: platform albums are held
until `min_batch_size` (default 10) files accumulate, or `min_batch_max_wait_h`
(default 168h = 7 days) elapses.

```powershell
dispatcher config set min_batch_size 1          # send whatever's pending now
ops restart dispatcher                          # policies read at startup
```

Recorder (live) and chat_id (orphaned) rows are exempt and never held — if those
aren't draining, it's a real problem (session/route), not batching.

---

## A file vanished without uploading (dedup suppression)

If a file you dropped in is gone with no new Telegram message, and its **bytes
were already sent**, this is by design: the dispatcher suppresses the duplicate
and deletes it. The log shows `suppressed as duplicate of id=…`. Only the
redundant copy is removed; the originally-sent file is untouched.

---

## FilePartsInvalid — a file too big to ever upload

`last_error: FilePartsInvalidError … (caused by SaveBigFilePart)` means the
file needs more than Telegram's ~8000 × 512 KiB parts (over
`core.media_prep.max_upload_bytes()`, ≈ 3.87 GiB): **no retry can ever
succeed** — the fix is a split, then a re-queue. The failure classifier
deliberately treats it as PERMANENT (quarantined in `failed`, never
auto-re-armed), so the rest of the queue keeps flowing.

Since 2026-07-12 this should not recur — and if it does, it **self-heals**:

- *Prevention:* the orphaned sweep's keep-original-as-document path splits an
  oversize original into a document album (`media_prep.split_for_upload`),
  `prepare()` enforces the ceiling even when ffprobe fails, and the dispatcher
  preflights the size and quarantines on the first hit instead of re-uploading
  multi-GB per retry.
- *Recovery:* the archiver's ingest sweep runs
  `core.ingest.recover_oversize_failed` (~3 min cadence): any failed row with
  the FilePartsInvalid signature is stream-copy split, its parts requeued as
  an ordered `[original]` album with the row's own routing, and the poison row
  retired (one split per sweep, so a backlog heals incrementally). A row whose
  file was replaced with an under-ceiling copy is simply re-armed. This is the
  ONE exception to "failed rows wait for a human" — safe because the oversize
  signature is deterministic and the repair replaces the row rather than
  re-arming it into a retry storm.

Manual fallback (archiver down, or you want it out NOW):

```powershell
# 1. identify the file (see the failed row's file_path)
# 2. split it losslessly next to itself (~3 parts for a 4-5 GB file)
ffmpeg -v error -i IN.mp4 -c copy -f segment -segment_format mp4 `
  -segment_time 9000 -reset_timestamps 1 -map 0 -avoid_negative_ts make_zero `
  -y "IN_orig_part%03d.mp4"
# 3. drop the parts where the original was (the sweep registers them; give
#    them a shared album by leaving them in the same drop folder), delete the
#    failed row (or `dispatcher reset failed` AFTER removing the oversize
#    original so it can't re-queue), and remove the original once parts send.
```

---

## Config migration (%APPDATA% → .archive\.config)

Since 2026-07 the suite is self-contained: all per-app config lives under
`C:\Users\danie\.archive\.config\<app>`. `core.platform.paths` auto-detects the
new root (presence of `.archive\.config\archiver-suite`); until then a legacy
`%APPDATA%` install keeps working untouched. To migrate a legacy box:

```powershell
ops unload                                    # stop + disable workers
ops uninstall                                 # tasks + launchers embed old log paths
python tools\migrate_config_to_archive.py     # dry-run: inspect the plan
python tools\migrate_config_to_archive.py --apply
python -m pipx install --force .\dispatcher   # non-editable pkgs re-copy their code
ops install                                   # regenerate tasks at the new paths
ops load
```

The tool refuses to run while workers are alive and rewrites the absolute
paths inside the moved `.env` files (session, log, cookie paths).
`ARCHIVER_CONFIG_HOME` overrides the root on any OS.

> **Run this from a NORMAL user shell — never from inside an MSIX-packaged
> app** (Claude desktop, anything under `AppData\Local\Packages\...`).
> Packaged processes get `%APPDATA%` filesystem-virtualized: their "moves" of
> Roaming dirs only touch a per-app overlay
> (`...\Packages\<app>\LocalCache\Roaming`), the real dirs stay put, and the
> restarted workers then find a half-copied target and start a FRESH suite.db
> (this exact split-brain happened on 2026-07-12; recovered from the intact
> real DB, ~30 duplicate sends). If the counts look wrong right after a
> migration (`sent` near zero), `ops unload` immediately and check whether
> `%APPDATA%\archiver-suite\suite.db` still exists — that one is the truth.

*Status: this box was migrated 2026-07-12; `%APPDATA%` no longer holds suite
config here.*

---

## Two-root split (chat_id route folders → ROUTES_DIR)

`ROUTES_DIR` (archiver `.env`) is the scan root for **chat_id route folders**;
unset it equals `OUTPUT_DIR` (single-tree, byte-identical old behavior). Set it
to keep route folders on a separate volume from the platform downloads/records.
Route folders are named `[<label>~]<chat_id>[.t<topic>]` — the `<label>~`
prefix is cosmetic (stripped before routing), `.t<topic>` targets a forum topic.

```powershell
ops unload                                            # workers MUST be down
python tools\migrate_split_roots.py --dest D:\routes  # dry-run: inspect
python tools\migrate_split_roots.py --dest D:\routes --apply
# then set ROUTES_DIR=D:\routes in .archive\.config\archiver-suite\.env
ops load
```

> **⚠ Only route-named folders belong under `ROUTES_DIR`.** The auto-ingest
> scan treats every *other* top-level folder there as a pseudo-platform and
> uploads it — never point `ROUTES_DIR` at a directory that already holds
> unrelated content.
>
> The move is **cross-drive** (copy+delete), so the destination volume needs
> free space ≥ the route folders' total size; the source stays intact until
> each folder's copy completes. `WinError 112` mid-run = destination full —
> free space and re-run (already-moved folders are skipped as clashes).

*Status (2026-07-17): `ROUTES_DIR=D:\routes` is set; the physical folder move is
being done manually (D: was full at attempt time).*

---

## Disk filling up

`ops health` shows the free-space figure. If it's getting tight:

- Confirm `delete_after_upload` is ON for users you don't want to keep locally
  (`archiver policy` / dispatcher delete policy).
- The archiver self-purges already-sent files on ENOSPC, but that's a last
  resort, not a strategy.
- Recorder output (`C:\Users\danie\.archive\.records`) is NOT auto-deleted unless
  the dispatcher's `delete_after_upload_records` policy is on. Live recordings
  are large — check there first.

---

## Sanity checklist after any intervention

```powershell
ops health
```

Expect: all workers `running`, queue `pending` trending toward 0, lock
`not held` (unless recording), disk healthy. Watch one drain cycle with
`ops watch` before walking away.
