# Media Archiver Suite — Project Map

> 30-second orientation card for fast re-onboarding. Read this first, then
> [README.md](README.md) for what/how/install and [DESIGN.md](DESIGN.md) for the
> dense code reference. This card only orients — it deliberately does **not**
> restate the architecture, pipeline, or invariants (those live once, in
> DESIGN.md).

| | |
|---|---|
| **Purpose** | Auto-download social media (X, IG, TikTok + TikTok-live capture), dedupe, and ship to Telegram channels. |
| **Stack** | Python 3.13 · SQLite (WAL) · Telethon/MTProto · yt-dlp + gallery-dl · ffmpeg · pipx venvs + Task Scheduler |
| **Shape** | 4 binaries (`archiver`, `recorder`, `dispatcher`, `ops`) + shared `core` lib; coordinate via ONE SQLite file (`C:\Users\danie\.archive\.config\archiver-suite\suite.db`), no sockets |
| **Platform** | Windows (Task Scheduler); self-contained under `C:\Users\danie\.archive` (config in `.archive\.config`); `core.platform.*` keeps a POSIX/launchd path too |
| **Root** | `C:\Users\danie\Documents\Coding\Archiver-Wsuite` |
| **Output** | `C:\Users\danie\.archive` (interim unified root; see `REFACTOR_PLAN_bans_and_paths.md`) |
| **Status** | Active |
| **Priorities** | integrity > self-healing > seam robustness > efficiency |

## The one-liner

Producers (archiver downloads, recorder captures live, or you drop files in a
chat_id folder) write media to disk plus a `pending` DB row. The dispatcher
claims rows and uploads them to Telegram. All coordination is through
`suite.db` — no IPC.

## Components (one line each)

| Component | Role |
|---|---|
| `core` | Shared spine every worker imports: schema, store, ingest, dedup, media_prep, policies, routing, paths/heartbeat, platform adapters. |
| `archiver` | Downloads platforms (X/IG via gallery-dl, TikTok via yt-dlp), reconciles disk↔DB, enqueues. |
| `recorder` | Captures TikTok live → segments → enqueues at higher priority than archiver. |
| `dispatcher` | Claims pending rows, uploads via Telethon, deletes per policy. The only Telegram talker. |
| `ops` | install/health/logs via Task Scheduler; reads artifacts + DB read-only, never a worker. |

## Where the detail lives

- **Architecture, install, on-disk layout** → [README.md](README.md)
- **Dense code map (modules, seams, choke points, invariants, run/test)** → [DESIGN.md](DESIGN.md)
- **Daily use (every upload path + command)** → [USER-GUIDE.md](USER-GUIDE.md)
- **Unattended setup** → [AUTOMATION.md](AUTOMATION.md) · **Recovery** → [ops/RUNBOOK.md](ops/RUNBOOK.md)
