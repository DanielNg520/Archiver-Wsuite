# Refactor plan — banned-user quarantine + two-root storage split

**Status: PLAN — ready to implement.** Two independent refactors on top of the
current interim storage layout. Decisions locked with the user are marked
**[DECIDED]**. Anchors (`file:line`) were re-verified 2026-07-11 against `main`.

> **2026-07-12 update:** per-app config has since moved out of `%APPDATA%` into
> `C:\Users\danie\.archive\.config\<app>` (see ops/RUNBOOK.md "Config
> migration"). Where this plan cites `%APPDATA%\recorder\config.toml` or
> `%APPDATA%\archiver-suite\.env`, read `.archive\.config\recorder\config.toml`
> / `.archive\.config\archiver-suite\.env`. `file:line` anchors into
> `core/core/media_prep.py`, `core/core/orphaned.py`, and
> `dispatcher/dispatcher/send.py` may have shifted slightly (2026-07-12
> oversize/FilePartsInvalid fixes touched those files) — re-verify before
> implementing.

---

## 0. Current state (starting point for this plan)

The suite runs on a **temporary interim layout** established 2026-07-11
(see memory `storage-refactor-unified-archive`):

- **One unified `output_dir`:** `C:\Users\danie\.archive` — holds platform
  download folders (`x/ tiktok/ instagram/ xiaohongshu/ unsorted`), the
  **chat_id route folders**, AND the recorder output, all in one tree.
- **Recorder output is dot-prefixed:** `C:\Users\danie\.archive\.records`
  (`%APPDATA%\recorder\config.toml` `output_dir`). The dot keeps the orphaned
  scanner from adopting it as a pseudo-platform.
- `%APPDATA%\archiver-suite\.env` → `OUTPUT_DIR=C:\Users\danie\.archive`.
- `suite.db` recovered + all ~124k `file_path` rows migrated onto `.archive`
  via `tools/migrate_paths_to_archive.py` (row count grows as workers run).
- **Workers are LIVE** (`com.duy.archiver`/`recorder` Running) — the migration is
  validated in production. New rows use Windows backslash `Path` style (cosmetic;
  both slash directions valid, all rows under `.archive`). ⚠️ Any future DB
  migration (Phase 6) must **stop the workers first** — the corruption history
  came from writing/swapping `suite.db` under live writers
  (memory: `suite-db-corruption-recovery`).

This interim layout is *not* the destination. This plan takes it the rest of
the way: **Refactor 1** adds ban quarantine; **Refactor 2** splits the storage
back into two roots (downloads/records on the internal drive, chat_id route
folders on `D:`).

### Shared extractions (do these first — the bans/quarantine track depends on them)

- **`_ACCOUNT_GONE_SIGNALS` + `_match_account_gone`** → new `core/core/account_gone.py`.
  Currently in `archiver/archiver/platforms.py:84` / `:102`; used by archiver
  extractors and (new) the recorder profile check. Re-point archiver imports.
- **`quarantine_user` / `restore_user`** → new `core/core/quarantine.py` (Phase 1).

(Refactor 2 / the storage split is independent of these extractions.)

---

## Refactor 1 — banned/suspended/deleted user quarantine

### Goal
When a user (any platform) is detected banned/suspended/deleted:
1. record them in the **per-app `config.toml` banned roster** and drop them from
   the active user list, and
2. **move their on-disk folder into `.deleted/`** inside the platform folder
   (move, not delete — reversible).

### Decisions [DECIDED]
- **Ban store:** per-app `config.toml` rosters (archiver + recorder each own
  theirs). No shared DB table. Reuse `core.PolicyStore` ban API for both.
- **Recorder detection:** two-stage gate — long cooldown escalation **AND** a
  profile-page confirmation. Auto-ban only when *both* fire. Manual CLI override
  stays.
- **Quarantine folder:** dot-prefixed `.deleted/` so folder-scan discovery skips
  it automatically.

### What already exists
- `AccountGoneError` from real extractor signals — `archiver/archiver/platforms.py`
  (X `:485`, TikTok `:616`, IG `:899`), raised into orchestrator at `:575`.
- `Orchestrator._ban_account` — `archiver/archiver/orchestrator.py:293` — calls
  `policy_store.ban_user(...)`, drops from active users, stages end-of-run report
  (`_report_banned:314`).
- `core.PolicyStore` ban API — `core/core/policy_store.py`:
  `list_banned:276`, `banned_details:283`, `ban_user:297`, `unban_user:334`
  (stores under `[platform.<name>.banned]`).
- CLI: `archiver banned list|unban` — parser `archiver/archiver/cli.py:346`,
  handler `cmd_banned:1364` (unban branch `:1373`).
- **Gaps:** nothing moves the files; the recorder has no ban concept.

---

### Phase 1 — shared quarantine helper (`core/core/quarantine.py`)

```
def quarantine_user(output_dir, platform, username) -> Path | None:
    """Move {output_dir}/{platform}/{username}/  →
             {output_dir}/{platform}/.deleted/{username}/.
    Returns dest, or None if the source folder didn't exist.
    - Same-drive os.rename (atomic). If dest exists (re-ban after restore),
      suffix a UTC stamp: .deleted/{username}__{YYYYMMDDTHHMMSS}.
    - Idempotent-ish: a second call with no source is a no-op."""

def restore_user(output_dir, platform, username) -> Path | None:
    """Inverse: move .deleted/{username}/ back. Used by `unban`.
    No-op if not quarantined. Refuses to overwrite a live folder."""
```

Rules / cautions:
- **Respect the live-recording lock.** Before moving a `tiktok` folder, check
  `core.recorder_lock`; never move the actively-recorded user out from under a
  live capture. If locked for that user: skip the move + log; the roster entry
  still lands and the folder gets swept next cycle
  (memory: `live-recording-sweep-protection`).
- **Same-drive only.** Quarantine lives under `output_dir`, so `os.rename` is
  always atomic — never a cross-drive copy. (Stays true after Refactor 2:
  quarantine is under `output_dir`, which stays on the internal drive.)
- The `.deleted/` prefix is required so folder-scan discovery skips it.

**`.deleted/` scanner audit (verified 2026-07-11):**
- `LocalPlatform.users` (`platforms.py:226`) — skips dot-dirs ✓
- `core.orphaned` (`orphaned.py:168, :254`) — skips dot ✓
- `core.sorter` (`sorter.py:177`) — skips dot files ✓
- ⚠️ **`archiver/reconcile.py:219`** — the user-dir loop
  (`for user_dir in sorted(p for p in root.iterdir() if p.is_dir())`) does **not**
  skip dot-dirs. **Add `and not p.name.startswith(".")` here** or `.deleted/`
  is reconciled as a phantom username. (This is the only missing guard.)

**Tests — `core/core/_selftest_quarantine.py`:** (core helper only — roster
behavior is tested by its callers in Phases 2/4)
- move creates `.deleted/<user>/`, source gone, files intact
- second ban after restore → timestamp-suffixed dest, no clobber
- no source folder → returns None, no error
- restore round-trips
- (mock) recorder-lock held for the user → move skipped, returns a distinct
  "locked/skipped" signal (not the moved path), no exception

**Gate:** selftest passes; the reconcile dot-guard is in place.

---

### Phase 2 — archiver wire-in

- **`orchestrator.py:293` `_ban_account`:** after `policy_store.ban_user(...)`,
  call `core.quarantine.quarantine_user(self.config.output_dir, platform, username)`.
  Log the moved path into the end-of-run report (`_report_banned:314`).
- **`cli.py:1373` `cmd_banned` unban branch:** after `unban_user`, call
  `core.quarantine.restore_user(...)` and print the restored path.

No detection changes — archiver detection already works.

**Gate:** existing archiver ban-path selftest still green; manual
`archiver banned unban` restores a quarantined folder.

---

### Phase 3 — recorder ban subsystem

The recorder today has only an **in-memory** cooldown bench
(`recorder/recorder/state.py`: `_SKIP_AFTER_FAILS=3` `:60`,
`_SKIP_COOLDOWN_S=600` `:72`, `_note_start_failure:369`, `_is_skipped:379`,
`_deactivate_user:396`, `_consec_fail:208`). It resets on restart, has no
roster, no persistence, no profile check.

**New pieces:**

1. **Roster via PolicyStore.** Load a `core.PolicyStore` against the recorder's
   `config.toml` (`_osp.config_dir(_osp.RECORDER)/config.toml`, already the
   recorder config path — `recorder/recorder/config.py:26`). Reuse
   `ban_user/list_banned/unban_user` (platform `"tiktok"`). At startup and when
   building the poll list, filter out `list_banned("tiktok")` from `tiktok_users`
   (`config.py:104`).

2. **Persistent escalation counter.** In-memory cooldown can't survive restarts
   and ban escalation needs a long window. Add a small JSON in `state_dir`
   (`config.py:100`), e.g. `~/.recorder/unstartable.json`:
   `{username: {"cooldown_cycles": int, "first_seen": iso, "last_seen": iso}}`.
   - Increment `cooldown_cycles` each time `_deactivate_user` fires; persist.
   - Reset/evict on any successful start (mirror `_consec_fail.pop`, `state.py:366`).

3. **Two-stage gate → ban.** New `_maybe_ban_unstartable(username)`, called from
   `_deactivate_user`:
   - **Stage 1 (cheap):** proceed only if `cooldown_cycles >= _BAN_AFTER_COOLDOWNS`
     (new const, ~6, tune) AND `first_seen` age ≥ 24h (so a burst can't fast-track).
   - **Stage 2 (confirm):** call `profile_check(username)`. Ban ONLY on `GONE`.
     `ALIVE`/`PRIVATE`/`UNKNOWN` → stay in cooldown, keep retrying. This makes
     network calls rare (gated by Stage 1) and false positives unlikely.
   - On confirmed ban: `ban_user("tiktok", username, reason, detected_at)`,
     `quarantine_user(output_dir, "tiktok", username)`, evict the unstartable
     entry, drop from the poll set, log WARNING.

4. **`profile_check(username)`** — new `recorder/recorder/ban_check.py`:
   - Lightweight fetch of `https://www.tiktok.com/@<username>` (reuse the
     recorder's Chromium path in `recorder/recorder/tiktok_browser.py`, or a
     plain HTTP GET with the recorder's cookies).
   - Classify via the shared `core.account_gone` signals: "couldn't find this
     account" / "account was banned" → `GONE`; private-but-exists → `PRIVATE`;
     normal → `ALIVE`; network error/ambiguous → `UNKNOWN` (never bans).
   - ⚠️ **Live-data caveat:** the exact TikTok page markers must be confirmed
     against a real known-banned handle before trusting Stage 2. Treat as a
     verify-with-live-data step, not asserted blind.

5. **CLI:** add `recorder banned list|unban` mirroring the archiver command
   (`recorder/recorder/cli.py` — no `banned` subcommand exists yet). `unban` →
   `unban_user` + `restore_user`.

**Tests — `recorder/recorder/_selftest_ban_escalation.py`:**
- `cooldown_cycles` increments + persists across a simulated restart
- Stage-1 gate blocks ban below threshold / below age floor
- Stage-2: GONE→ban+quarantine; ALIVE/PRIVATE/UNKNOWN→no ban
- successful start clears the unstartable entry
- banned users filtered out of the poll list

**Gate:** selftest passes; one live `profile_check` against a real banned handle
returns `GONE`.

**Cross-app note (accepted drift):** archiver and recorder rosters are
independent — archiver banning `tiktok/@x` does not auto-skip it in the recorder
and vice-versa. Acceptable per the per-app-store decision; promote to a shared
DB table only if the drift becomes annoying.

---

### Phase 4 — manual user-deletion lifecycle (defer → Recycle Bin → 30-day row GC)

**Distinct from the auto-ban quarantine above.** Auto-ban moves the folder to
`.deleted/` (in-archive, rows kept, reversible via `unban`). A **manual delete**
is intentional and terminal: the folder goes to the **Windows Recycle Bin** and
the DB rows are purged after a retention window.

**Decisions [DECIDED]:**
- **Destination:** Windows Recycle Bin (via `send2trash` / `SHFileOperation`),
  not `.deleted/`. New dependency — add `Send2Trash` to core.
- **Un-uploaded users are DEFERRED,** never dropped: mark for deletion + drop
  from the active list, but only move to trash once **every** row is `sent`.
- **Row retention:** delete all the user's DB rows **30 days after** the folder
  was trashed (clock starts at `trashed_at`, not at the delete request).

**Lifecycle:**

1. **Request** — `archiver delete --platform <p> --user <u>` (new command, or a
   `--purge` flag on `config remove`, `cli.py:341`, which today only drops from
   the active list / keeps files). It:
   - records the user in a persistent **deletion roster** with `requested_at`
     (UTC) — a new `[platform.<name>.deleting]` table in the app `config.toml`,
     managed via `PolicyStore` (mirror `remove_user:252` / `ban_user:297`);
   - calls `remove_user` (`policy_store.py:252`) so no new downloads;
   - touches NO files and NO rows yet.
   - ⚠️ **Suppress re-adoption during the deferral window.** The folder still
     exists until it's trashed, so a folder-scan platform (`LocalPlatform.users`,
     `platforms.py:219`) would re-discover the user. Filter deletion-roster users
     out of the active user/poll list each run — mirror how banned users are
     filtered — so `remove_user` isn't silently undone before the trash step.

2. **Deferred trash** — new sweeper `_process_pending_deletions`, run each
   archiver cycle (alongside `_maybe_ingest_orphaned`, `orchestrator.py:347`).
   For each roster user without a `trashed_at`:
   - Query per-user status via the `items.username` column
     (`WHERE platform=? AND username=?`; add `ItemStore.user_status_counts`
     next to `counts_by_status`, `store.py:1042`).
   - If ANY row is `pending`/`sending`/`failed` → skip (still uploading), retry
     next cycle.
   - If ALL rows are `sent` → `send2trash({output_dir}/{platform}/{username})`,
     record `trashed_at` (UTC). **Respect `core.recorder_lock`** — skip a tiktok
     user being actively recorded, defer to next cycle. Missing folder → still
     stamp `trashed_at` (idempotent).

3. **Row GC** — same sweeper: for each roster user whose `trashed_at` is > 30
   days old, `DELETE FROM items WHERE platform=? AND username=?` and evict the
   roster entry.
   - ⚠️ **Tradeoff:** purging `sent` rows drops their `content_hash` dedup
     memory — re-adding the user later could re-upload old bytes. Acceptable for
     an intentional manual delete; document it.

4. **CLI visibility:** `archiver deleting list` (requested_at / trashed_at /
   countdown) and `archiver deleting cancel <user>` (before GC: evict roster,
   restore to active list; after trash, the folder stays in the Recycle Bin for
   the user to restore manually).

**Cautions:**
- Recycle Bin is per-drive and only exists on a local volume with one. `.archive`
  is on `C:` → fine. If `output_dir` ever moves to a volume without a Recycle
  Bin, `send2trash` falls back to permanent delete — note in docs.
- The retention clock is wall-clock; store ISO timestamps and compare
  `now - trashed_at >= 30d` (no reliance on process uptime).

**Tests — extend `_selftest_quarantine.py` or new `_selftest_manual_delete.py`:**
- request → roster entry + dropped from active; files + rows untouched
- not-all-sent → no trash (deferred); once all `sent` → `send2trash` called
  (mocked), `trashed_at` set
- recorder-lock held for the user → trash deferred
- `trashed_at`+31d → rows deleted + roster evicted; +29d → rows kept
- `cancel` before trash → restored to active list, no trash

**Gate:** selftest passes; a manual end-to-end on a fully-uploaded test user
lands the folder in the Recycle Bin and (with a stubbed clock) purges its rows.

---

## Refactor 2 — two-root storage split (downloads/records internal, routes on D:)

### Goal
From the interim single `.archive` root, split so that **platform downloads +
records stay on the internal drive** but the top-level **chat_id route folders
move to `D:`**.

### Decision [DECIDED]
Add a separate **`routes_dir`** config key, defaulting to `output_dir` so
behavior is unchanged until it's set.

### Current coupling
chat_id route folders currently live *inside* `output_dir` (`.archive`).
`ingest_chat_id_dirs(output_dir)` scans `output_dir`'s top-level folders for
chat_id-named ones — `core/core/orphaned.py:128`, called from
`orchestrator.py:363` (`_maybe_ingest_orphaned:347`). Platform downloads
(`{output_dir}/{platform}/{username}/`) share the same tree.

### Phase 5 — config + scanner repoint

1. **Add `routes_dir`:**
   - `archiver/archiver/config.py` (near `output_dir` `:256`):
     `routes_dir: str = ""` → in `from_env`,
     `routes_dir = _opt("ROUTES_DIR", "") or output_dir`.
   - Default `= output_dir` keeps every existing install byte-identical.
   - The recorder does **not** ingest route folders (that's archiver-side, via
     `_maybe_ingest_orphaned`), so it does **not** need `routes_dir`.

2. **Point the chat_id scanner at `routes_dir`:**
   - `_maybe_ingest_orphaned` (`orchestrator.py:363`) → pass
     `self.config.routes_dir` to `ingest_chat_id_dirs` instead of `output_dir`.
   - Grep `ingest_chat_id_dirs` + `parse_route` callers; repoint each to
     `routes_dir`. Leave ALL platform-download, archive, and quarantine paths on
     `output_dir`.

3. **Guard against cross-root moves.** Confirm no flow moves a file *between* a
   platform folder and a chat_id folder (would become a cross-drive copy+delete,
   not an atomic rename, once roots are on different drives). Grep `shutil.move`
   / `os.rename` / `.rename(` / `.replace(` in `core/`, `dispatcher/`,
   `archiver/`; verify each src/dst pair stays within one root. If any straddle,
   switch to copy+fsync+unlink or keep both ends on one root.

**Gate:** config self-test — `ROUTES_DIR` unset ⇒ `routes_dir == output_dir`;
set ⇒ independent. Orphaned-ingest test with `routes_dir` distinct from
`output_dir` still finds + enqueues chat_id folders; platform folders under
`output_dir` untouched by the route scan.

### Phase 6 — data migration (`tools/migrate_split_roots.py`)

**STOP the workers first** (the corruption lesson — no live writers on `suite.db`
during the swap; memory `suite-db-corruption-recovery`). Mirror
`tools/migrate_paths_to_archive.py` (same safety pattern: dry-run default, DB
backup, `premigration-bak` preserved):
- **Move the chat_id route folders** out of `C:\Users\danie\.archive\<chat_id>`
  to the `D:` `routes_dir`. Platform folders + `.records` stay in `.archive`.
- Rewrite affected `file_path` rows (chat_id drops are "leave-no-trace" ingests,
  so the surface is small — verify count first).
- Dry-run summary; refuse to clobber existing dests.
- Physical move is the script's job; then set `ROUTES_DIR` in the archiver `.env`.

**Gate:** dry-run shows the chat_id set only; after `--apply`, chat_id folders
resolve under `routes_dir`, platform + `.records` paths unchanged, `integrity ok`.

### Phase 7 — docs + memory
- Update `WINDOWS_PORT.md` / `USER-GUIDE.md` with `OUTPUT_DIR` + `ROUTES_DIR`
  and the "downloads/records internal, routes on D:" layout.
- Update memory `windows-machine-layout` + `storage-refactor-unified-archive`
  after rollout.

---

## Suggested sequencing

| Phase | Track | Scope | Gate |
|------|------|-------|------|
| **0** | bans | Shared extractions: `core/account_gone.py` | archiver extractor selftests green |
| **1** | bans | `core/quarantine.py` + reconcile dot-guard + selftest | `_selftest_quarantine.py` |
| **2** | bans | Archiver ban wire-in (`_ban_account`, unban) | archiver ban selftest |
| **3** | bans | Recorder ban subsystem (roster, counter, 2-stage, profile check, CLI) | `_selftest_ban_escalation.py` + one live `GONE` |
| **4** | bans | Manual-delete lifecycle (roster, defer→Recycle Bin, 30-day GC, CLI) | `_selftest_manual_delete.py` + Recycle-Bin e2e |
| **5** | storage | `routes_dir` config + scanner repoint + cross-root audit | config selftest + orphaned-ingest test |
| **6** | storage | `tools/migrate_split_roots.py` + physical move (stop workers first) | dry-run + `--apply` verify |
| **7** | storage | Docs + memory | — |

The **bans track (0–4)** and the **storage track (5–7)** are independent; do
either first. Within the bans track, Phases 0→1→2 are ordered (each builds on the
last); Phase 3 (recorder) is the largest new surface (~60% of Refactor 1); Phase
4 (manual delete) is archiver-side and depends only on the deletion-roster +
sweeper, so it can land right after Phase 2, independent of Phase 3. Every phase
is independently mergeable with an executable gate.
