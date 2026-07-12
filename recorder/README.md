# recorder

Watches a priority-ordered list of TikTok users and records their live streams
the moment they start, at the **highest quality the stream offers**
(origin → uhd → hd → sd → ld; FLV preferred over HLS). Recordings land in the
suite's record folder, where the archiver's ingest sweeper picks them up and
queues them for the dispatcher. The recorder never talks to Telegram.

This README covers only what is recorder-specific. The shared story lives in
the hub docs: [../README.md](../README.md) (architecture, install, layout),
[../USER-GUIDE.md](../USER-GUIDE.md) (split mode, drop-folder pickup),
[../AUTOMATION.md](../AUTOMATION.md) (running unattended),
[../ops/RUNBOOK.md](../ops/RUNBOOK.md) (cookies expired, stuck recordings).

## Run

```powershell
recorder start        # watch + record (the Task Scheduler service runs this)
recorder status       # who's live / being recorded right now
recorder stats        # DB counts
recorder record <user>            # one-shot: record a single user now
recorder config add|remove|list|priority   # manage the watched-users list
```

## Config — `C:\Users\danie\.archive\.config\recorder\`

```toml
# config.toml
[recorder]
output_dir = "C:\\Users\\danie\\.archive\\.records"   # dot-prefixed: orphaned scanner skips it
split_at_chunk_size = true    # optional split mode: every recording over…
split_chunk_gib     = 2.0     # …this size is cut into <=2 GiB album parts

[recorder.tiktok]
users = [ "someuser", "another" ]   # order = priority when several go live
```

```ini
# .env
TIKTOK_COOKIES_FILE=C:\Users\danie\.archive\.config\archiver-suite\tiktok.txt
```

Cookies are shared with the archiver's TikTok extractor and auto-refreshed
from Firefox (see [../ops/RUNBOOK.md](../ops/RUNBOOK.md) "TikTok cookies
expired"). Without cookies, live detection still works but age-restricted
lives fail and unstartable users go on a 600 s retry cooldown.

## Recorder-specific behaviors worth knowing

- **Reconnect stitching** — a stream that drops and comes back is recorded as
  segments stamped with one shared `group_key`, so the whole broadcast ships
  as a single ordered album.
- **Age-restricted (18+) lives** — fall back to a headless Chromium
  (Playwright) that lets TikTok's own JS sign the pull URL. The browser
  install self-heals (`playwright install chromium` runs automatically once
  per process if the build is missing/stale).
- **Raw fallback** — a recording that can't be probed/converted at enqueue
  time is queued raw; the dispatcher's send-time streamable net converts it at
  upload, so a recording is never lost. Files over the upload ceiling are
  split (or refused), never enqueued whole.
- **Live-recording protection** — ingest sweepers skip the file actively being
  recorded (`core.recorder_lock`), so a half-written stream is never enqueued.
- **yt-dlp invocation** — capture runs `sys.executable -m yt_dlp` (never the
  bare `yt-dlp` shim; see [../CLAUDE.md](../CLAUDE.md) for why).
