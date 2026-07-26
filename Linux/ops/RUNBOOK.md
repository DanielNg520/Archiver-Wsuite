# RUNBOOK — archiver / recorder / dispatcher

Operations reference for the four-process system. Keep this where you'll find it
at 2am. Install and layout are in [../README.md](../README.md); unattended setup
in [../AUTOMATION.md](../AUTOMATION.md).

```
recorder ──┐
           ├──→ suite.db ──→ dispatcher ──→ Telegram
archiver ──┘
```

All workers run under systemd --user (`com.duy.*`). Manage them with the `ops`
tool, not raw `systemctl --user`, unless debugging systemd itself.

---

## Quick reference

```bash
ops health                 # is everything alive? queue depth? disk?
ops watch                  # same, auto-refreshing
ops load                   # start + enable all workers (all, or `ops load <name>`)
ops unload                 # stop + disable all (kills orphaned trees too)
ops restart dispatcher     # restart one (dispatcher|recorder|archiver)
ops update                 # redeploy on a code change (see "Updating the code")
```

Log locations (all under `<repo>/.config/archiver-suite/logs\`):
- Captured worker stdout/err: `<name>.out.log` / `<name>.err.log`
- Rotated daily by the `logrotate` calendar job (copytruncate, gzip history)

When something's wrong, read the `.err` log first for crashes that happened
before app logging started.

---

## Updating the code (redeploy after an edit)

From the repo root, one command does the whole safe-redeploy dance:

```bash
ops update
```

What it does, in order (package-aware — every case does the **minimum**):
1. **Change detection, per package.** Content-hashes every `.py`/`.toml` under
   `core/ ops/ archiver/ recorder/ dispatcher/` (skipping `build/`, `dist/`,
   `__pycache__`) into a per-package fingerprint map. It then reacts to exactly
   what changed:
   - **only `ops`** → editable and imported by no service, so the new code is
     already live. No drain, no reinstall, no restart — it just records the new
     fingerprint and enters `ops watch`. (This is how a dashboard/health change
     like the concurrent-scan + stories rows deploys.)
   - **only `core`** → editable-injected, loaded on restart. No reinstall, but
     **every** worker is restarted (drain → unload → reload) so they pick it up.
   - **a worker package** (`archiver`/`recorder`/`dispatcher`) → reinstall +
     restart **just that worker**. The others keep running untouched — an
     archiver-only update never disturbs a recorder mid-capture or a dispatcher
     mid-upload.
   - nothing changed → no-op. `--force` reinstalls + restarts all workers.
2. **Per-worker graceful drain** — only for the workers being restarted, each
   reaching its own stop point on its own clock (they need not be idle at the
   same instant):
   - **dispatcher** → writes a cooperative stop-flag (`…/locks/dispatcher.stop`);
     the drain loop checks it *between* batches and exits only after the
     file/album currently uploading finishes — never chopped mid-send. Waits up
     to `--stop-timeout` seconds (default 300).
   - **recorder** → if a capture is in flight (TikTok lock held), waits for it
     to **finish naturally** before unloading — a live stream is never chopped.
     Waits up to `--recording-timeout` seconds (default 1800). (Only waited when
     the recorder is actually being restarted *and* recording.)
   - either falls back to a hard stop via the unload if its budget elapses.
3. **Unload only the affected workers**, then **wait for their processes to
   exit** + a short settle, so the reinstall never overwrites a venv a worker is
   still importing from (a half-updated venv mid-import).
4. **Reinstall** only the changed worker packages (each pipx step retried a few
   times to ride out a transient exe lock): `pipx install --force ./archiver`
   (the app is **`media-archiver`**) / `./dispatcher` / `./recorder`, then
   `pipx inject media-archiver --force --editable ./core` **iff** the archiver
   was reinstalled. `ops` and `core` are **never** force-reinstalled here — both
   are editable (see below), so they ride along live. (See the naming traps.)
5. On success it records the new per-package fingerprint, **reloads the
   restarted workers**, and enters `ops watch`. On a reinstall failure it clears
   the flag, reloads with the *previously* installed code, leaves the
   fingerprint unchanged (so the next run retries), and exits non-zero.

**Editable `ops` (required for `ops update` to cover ops-side changes).** A
running process can't force-reinstall its own locked venv, so `ops` — like
`core` — is installed **editable**; its `.py` edits are then live the instant
they're saved, and `ops update` needs only to record them. Do this once (and
after any `ops` **dependency or console-script** change, the lone case editable
can't pick up):

```bash
python -m pipx install --force --editable ./ops
python -m pipx inject ops --force --editable ./core
```

**Two naming traps** (why the hand-typed inject fails):
- The archiver's pipx app/venv is `media-archiver` (its `pyproject` name), not
  `archiver` — `pipx inject archiver …` errors "nonexistent Virtual
  Environment". Target `media-archiver`.
- `pipx inject` without `--force` is a **no-op** when `core` is already injected
  ("already seems to be injected"). `ops update` always passes `--force`.

**Bootstrap.** `ops update` lives in the `ops` package, so to get the command
itself the first time (and after any edit to `ops`/`dispatcher` sources — `core`
edits are editable and live on restart), reinstall those by hand once:

```bash
python -m pipx install --force ./ops
python -m pipx inject ops --force --editable ./core
python -m pipx install --force ./dispatcher
python -m pipx inject media-archiver --force --editable ./core
```

> If a package's absolute path changed, also run `ops uninstall && ops install`
> to regenerate the systemd unit definitions (they embed absolute paths).

---

## Telegram session died (dispatcher can't send)

Symptom: `ops health` shows dispatcher running but queue `pending` only climbs,
never `sent`. Log shows auth/session errors.

Telethon sessions expire or get invalidated (password change, logout-all-devices,
too-long offline). Re-auth is interactive, so systemd must be out of the
way:

```bash
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

```bash
archiver cookies refresh                    # pulls fresh cookies from Firefox
ops restart recorder
```

The recorder reads `TIKTOK_COOKIES_FILE` from `<repo>/.config/recorder/.env`. On this
machine it points at `<repo>/.config/archiver-suite\tiktok.txt`, so the archiver
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

```bash
ops unload
python tools/recover_suite_db.py            # inspect
python tools/recover_suite_db.py --apply    # recover + back up the corrupt copy
ops load
ops health
```

Manual fallback if you need it (`sqlite3` on PATH):

```bash
ops unload
cd <repo>/.config\archiver-suite
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

```bash
ops restart recorder
```

If the lock is STILL held after restart (stale lock — recorder was force-killed
previously and `__exit__` never ran):

```bash
cat <repo>/.config/archiver-suite/locks/tiktok.lock   # check the pid inside
# if that pid is dead:
rm <repo>/.config/archiver-suite/locks/tiktok.lock
```

The archiver only skips TikTok *downloads* while this lock exists; a stale lock
silently blocks TikTok archiving, so clear it promptly. (Health reads the lock
liveness-gated and self-heals a dead one, but clearing it removes any doubt.)

---

## A folder keeps reappearing on a volume you cleared

Symptom: an output folder you deleted (e.g. an old `/mnt/...` mount that is now
unmounted) recreates itself, empty, and its rows error with `ENODEV` / `ENOENT`
(no such device / directory).

Cause: stale `items` rows still hold the old path; a worker retrying them
recreates the parent via `mkdir`. Stop writers, delete the dead rows, remove the
folder:

```bash
ops unload
# Replace /mnt/dead-volume with the stale path prefix these rows point at.
python3 -c "import sqlite3,os; d=os.path.expanduser('<repo>/.config/archiver-suite/suite.db'); c=sqlite3.connect(d); print('deleting', c.execute(\"DELETE FROM items WHERE file_path LIKE '/mnt/dead-volume/%'\").rowcount); c.commit(); c.execute('PRAGMA wal_checkpoint(TRUNCATE)')"
rm -rf /mnt/dead-volume/<stale-folder>
ops load
```

Back up `suite.db` first; verify the rows aren't the only copy of live data
(check for a `content_hash` twin under the current root before deleting).

---

## Drain the queue manually (dispatcher won't start at all)

There is no manual send path by design — the dispatcher is the only process that
talks to Telegram. Fix the dispatcher, don't bypass it. To inspect what's stuck:

```bash
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

```bash
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

```bash
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

## Config location (self-contained checkout)

On Linux the suite is self-contained **inside the checkout**: every per-app
config dir (plus the DB, sessions, cookies, logs, and locks) lives under
`<repo>/.config/<app>`, resolved by `core.platform.paths._config_home` from the
editable-injected `core`'s own `__file__` — so it is correct in every pipx venv
and moves with the checkout. There is nothing to migrate and no home-directory
state to clean up: delete the checkout and the suite is gone.

`ARCHIVER_CONFIG_HOME` overrides the root on any OS (point it elsewhere to keep
config outside the checkout). `XDG_CONFIG_HOME` is deliberately **not** consulted
on Linux — honoring it (usually `~/.config`) would defeat self-containment.

> Legacy note: the Windows build of this suite migrated config out of
> `%APPDATA%` into a self-contained `~/.archive/.config` root; that path and the
> MSIX-virtualization caveat around it are Windows-only history and do not apply
> to this Linux port.

---

## Two-root split (chat_id route folders → ROUTES_DIR)

`ROUTES_DIR` (archiver `.env`) is the scan root for **chat_id route folders**;
unset it equals `OUTPUT_DIR` (single-tree, byte-identical old behavior). Set it
to keep route folders on a separate volume from the platform downloads/records.
Route folders are named `[<label>~]<chat_id>[.t<topic>]` — the `<label>~`
prefix is cosmetic (stripped before routing), `.t<topic>` targets a forum topic.

```bash
ops unload                                                  # workers MUST be down
python3 tools/migrate_split_roots.py --dest /mnt/routes     # dry-run: inspect
python3 tools/migrate_split_roots.py --dest /mnt/routes --apply
# then set ROUTES_DIR=/mnt/routes in <repo>/.config/archiver-suite/.env
ops load
```

> **⚠ Only route-named folders belong under `ROUTES_DIR`.** The auto-ingest
> scan treats every *other* top-level folder there as a pseudo-platform and
> uploads it — never point `ROUTES_DIR` at a directory that already holds
> unrelated content.
>
> The move is **cross-drive** (copy+delete), so the destination volume needs
> free space ≥ the route folders' total size; the source stays intact until
> each folder's copy completes. `ENOSPC` (no space left) mid-run = destination full —
> free space and re-run (already-moved folders are skipped as clashes).

*Status (2026-07-17): `ROUTES_DIR=D:\routes` is set; the physical folder move is
being done manually (D: was full at attempt time).*

**Disk gauge follows the split.** `ops health` / `ops watch` derive each root's
volume from item `file_path`s (no worker config read), keyed on `chat_id`:
route-folder items (non-null `chat_id`) sample `ROUTES_DIR`, everything else
samples `OUTPUT_DIR`. When the two roots sit on the **same** physical volume —
single-tree layout, or a split whose folders haven't been physically moved yet —
the panel shows a single `archive volume` gauge. Once the route folders actually
live on a separate volume it splits into two gauges, `media · OUTPUT_DIR` and
`routes · ROUTES_DIR`, so a tight `D:` is visible independently of `C:`.

---

## Disk filling up

`ops health` shows the free-space figure — one `archive volume` gauge, or a
`media · OUTPUT_DIR` + `routes · ROUTES_DIR` pair once the two-root split lands
on separate volumes (see "Two-root split" above). Check whichever gauge is
tight. If it's getting tight:

- Confirm `delete_after_upload` is ON for users you don't want to keep locally
  (`archiver policy` / dispatcher delete policy).
- The archiver self-purges already-sent files on ENOSPC, but that's a last
  resort, not a strategy.
- Recorder output (`~/.archive/.records`) is NOT auto-deleted unless
  the dispatcher's `delete_after_upload_records` policy is on. Live recordings
  are large — check there first.

---

## Sanity checklist after any intervention

```bash
ops health
```

Expect: all workers `running`, queue `pending` trending toward 0, lock
`not held` (unless recording), disk healthy. Watch one drain cycle with
`ops watch` before walking away.
