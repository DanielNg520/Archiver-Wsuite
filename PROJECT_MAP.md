# Media Archiver Suite — Project Map

> Brief whole-picture backup note for fast re-onboarding. Read this first; see
> `DESIGN.md` for the dense reference, `README.md`/`USER-GUIDE.md` for what/how.

| | |
|---|---|
| **Purpose** | Auto-download social media (X, IG, TikTok + TikTok-live capture), dedupe, and ship to Telegram channels. |
| **Stack** | Python 3.13 · SQLite (WAL) · Telethon/MTProto · yt-dlp + gallery-dl · ffmpeg · pipx venvs + launchd |
| **Shape** | 4 binaries (`archiver`, `recorder`, `dispatcher`, `ops`) + shared `core` lib; coordinate via ONE SQLite file, no sockets |
| **Root** | `/Users/duynguyen/Documents/Coding/Archiver suite` |
| **Status** | Active |
| **Updated** | 2026-07-05 |
| **Priorities** | integrity > self-healing > seam robustness > efficiency |

## What it does
Producers (archiver downloads, recorder captures live) write media to disk plus a
`pending` DB row. The dispatcher claims rows and uploads them to Telegram. All
coordination is through `~/.config/archiver-suite/suite.db` — no IPC.

## Architecture
| Component | Role |
|---|---|
| `core` | Shared spine every worker imports: schema, store, ingest, dedup, media_prep, policies, routing, paths/heartbeat. |
| `archiver` | Downloads platforms (X/IG via gallery-dl, TikTok via yt-dlp), reconciles disk↔DB, enqueues. |
| `recorder` | Captures TikTok live → segments → enqueues at higher priority than archiver. |
| `dispatcher` | Claims pending rows, uploads via Telethon, deletes per policy. The only Telegram talker. |
| `ops` | install/health/logs via launchd; reads artifacts + DB read-only, never a worker. |

## Pipeline
```
archiver download ─┐
recorder capture  ─┤─► media_prep (compat/split) ─► ingest (stabilize+hash+dedup) ─► suite.db [pending]
manual chat_id drop┘                                                                       │
                                                          dispatcher: claim_batch (homogeneous album)
                                                                                           ▼
                                                       send (FastTelethon 8-conn → Telethon) ─► Telegram
                                                                                           ▼
                                                       mark_sent ─► maybe_delete (policy + safebrake)
```

## Layout (key modules)
- **core/** — `schema.py` (DDL, v4) · `store.py`/`stores.py` (ItemStore, `claim_batch`, role-view Protocols) · `ingest.py` (the enqueue primitive) · `dedup.py`/`hashing.py` · `media_prep.py` (remux/re-encode/split, upload ceiling) · `orphaned.py` (chat_id-folder ingest) · `routing.py` · `policies.py`/`policy_store.py` · `deletion.py` (safebrake) · `paths.py`/`heartbeat.py` (cross-proc).
- **dispatcher/** — `drain.py` (serial loop + 15-min housekeeping) · `send.py` (Telethon strategy; `_client_for` picks primary vs optional burner per destination) · `fast_upload.py` (parallel upload) · `delete.py` (ship-and-delete gate) · `tg_router.py` (`peer_chat_id` inverse for burner routing). Optional **burner account** (2nd login) is CLI-only: `dispatcher burner login|chats|status`, persisted to `.env` (`TELEGRAM_BURNER_*`, `BURNER_CHAT_IDS`); off unless registered.
- **archiver/** — `orchestrator.py` (cycle) · `platforms.py` (Strategy per site) · `reconcile.py`.
- **recorder/** — `state.py` (capture FSM + uploader thread) · `capture.py` (yt-dlp) · `startup_sweep.py`.

## Data model — `suite.db` (SQLite, WAL)
`items` (one row per file): `id, source, platform, username, file_path` (UNIQUE),
`content_hash` (global dedup key), `status` {pending→sending→sent/failed}, `chat_id,
group_key, topic_id, caption, priority`. Plus `checkpoints`, `circuit`, `metadata`.

## Config & run
- **Config:** `~/.config/<worker>/.env` (creds + tunables) + `config.toml` via `PolicyStore` (scoped user > platform > default).
- **Run/test** from a NEUTRAL cwd (repo root lets `./core` shadow installs):
  `PYTHONPATH=core:archiver:recorder:dispatcher:ops ~/.local/pipx/venvs/dispatcher/bin/python tests/test_seams.py`
- Installs are pipx venvs with an **editable** `core` (frozen core crashes on schema bump). No single venv has every package — use PYTHONPATH for cross-worker tests.

## Invariants & gotchas
- One row per file (UNIQUE `file_path`) and per post (UNIQUE `platform,identifier`).
- Never delete unless `status='sent'` AND policy allows AND not safebraked; never upload bytes already sent (`content_hash`), suppression AFTER delivery.
- Orphaned (chat_id-folder) items **ship-and-delete** (file + row) after upload; the row is dropped only once the file is confirmed gone (else it re-ingests).
- Albums are homogeneous by bucket — photo/video/single for producers; chat_id folders bucket by kind: **mixed** photo+inline-video groups vs **document** groups (`.mkv`/`.gif`, shipped after the subfolder's media). **`hachoir` is a hard dispatcher dep** — without it album videos ship as 1×1 images.
- Upload ceiling = part count (`7936 × 512 KiB ≈ 3.9 GiB`); larger files split at ingest.
- **Editable installs import the working tree** → a worker restart runs whatever branch is checked out (archiver/recorder under launchd; dispatcher manual).
- Recorder capture passes ffmpeg reconnect flags (`--downloader-args ffmpeg:-reconnect …`) so one broadcast stays in one file across TikTok blips; residual genuine-reconnect segments share one album `group_key` (via `register_media(group_key=…)`) so a broadcast never ships as scattered short clips.
