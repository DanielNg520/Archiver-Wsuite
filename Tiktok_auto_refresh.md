# TikTok Auto-Refresh Implementation Plan

## 1. Context and Goals
TikTok's session cookies expire or get invalidated if they aren't actively refreshed by normal web browsing activity. To prevent API/extraction failures, we need a background mechanism that keeps the session alive by mimicking human browsing.

**Constraints & Triggers:**
- **Trigger Condition:** Executes immediately after a live recording finishes at the `RecorderState.HANDOFF` transition. Since the recorder is single-threaded and records at most one user at a time, no other streams are active.
- **Frequency:** Randomly triggered using a probability curve, but forced to occur if 48 hours have passed.
- **Behavior:** Headless browser automation mimicking human interaction (scrolling, random delays).

---

## 2. Architecture & Subsystem Integration

This logic strictly belongs inside the **Recorder** subsystem (specifically within the state machine in `recorder/recorder/state.py`). Note that `recorder/recorder/watch.py` is solely the renderer for the CLI dashboard (`recorder watch`) and is not part of the state machine.

### Component A: State Management (`core.schema.metadata`)
To guarantee the "once every 2 days" rule without polling, we will store the last refresh timestamp in the shared SQLite database using `ItemStore`'s existing metadata table.
- **Task:** Directly use the existing `meta_get()` and `meta_set()` methods on `ItemStore` (`core/core/store.py`) with the key `tiktok_last_cookie_refresh`. No new store methods are needed.
- **Why:** The `metadata` table is already designed for generic key/value storage (such as cookie refresh timestamps) and persists across daemon restarts, ensuring the "at least once every 2 days" rule is reliably tracked across sessions.

### Component B: The Refresher Module (`recorder/recorder/cookie_refresh.py`)
A dedicated module for Playwright automation, following the headless browser pattern established in `recorder/recorder/platforms/tiktok_browser.py`.
- **Task:** Implement `simulate_human_browsing()` which launches a headless, non-persistent Chromium instance (`pw.chromium.launch(headless=True)`).
- **Cookie Loading & Missing Guard:**
  - Retrieve the cookie file path from `RecorderConfig.tiktok_cookies_file` (`recorder/recorder/config.py`).
  - If `tiktok_cookies_file` is unset, or if the file is missing or empty, skip the refresh cleanly (log an informational message and return; do not raise).
  - Open a fresh context via `browser.new_context()` per call and load cookies using the existing Netscape-format parsing helper (`_netscape_to_playwright`).
- **Cookie Persistence (Session Keep-Alive):**
  - The previous draft never mentioned persisting updated cookies, meaning simulated browsing would fail to actually refresh anything. After simulated browsing completes, extract rotated session cookies from `context.cookies()` and write them back to `tiktok_cookies_file`.
  - Always close the browser in a `finally` block.
- **No Profile Lock Issues:**
  - Because non-persistent Chromium contexts are used without persistent browser profile directories, there is no browser profile directory and therefore no profile-lock risk (`parent.lock`).
- **Evasion Tactics:**
  - Randomize scroll depths and sleep intervals.
  - Mimic reading times by waiting on certain videos before scrolling.

### Component C: The Trigger Hook (`recorder/recorder/state.py`)
The recorder loop handles transitions via `RecorderState` (`LISTENING -> RECORDING -> HANDOFF -> LISTENING`). Single-threaded execution means exactly one user is recorded at a time. The transition into `HANDOFF` indicates a recording has just ended and the recorder is about to re-scan the priority list.
- **Condition Logic (Probability Curve):** Hook directly into the `RecorderState.HANDOFF` transition in `state.py`. Since there is only ever zero or one active recording, "no other streams active" is automatically true at `HANDOFF`—no active stream counting or concurrency checks are needed.
  ```python
  last_refresh_raw = store.meta_get("tiktok_last_cookie_refresh")
  time_since_refresh = now - parse_timestamp(last_refresh_raw) if last_refresh_raw else timedelta(days=999)

  run_refresh = False

  # Probability Curve
  if time_since_refresh > timedelta(hours=48):
      run_refresh = True  # Forced guarantee
  elif time_since_refresh > timedelta(hours=24):
      run_refresh = random.random() < 0.30  # 30% chance
  elif time_since_refresh > timedelta(hours=12):
      run_refresh = random.random() < 0.10  # 10% chance

  if run_refresh:
      await cookie_refresh.simulate_human_browsing(config, store)
  ```
- **Why:** Tying execution to `HANDOFF` leverages natural breaks between captures when no stream recording is in progress, preventing headless Chromium from contending for CPU, RAM, or network bandwidth during active recordings.

---

## 3. Step-by-step Implementation Tasks

- [ ] **Step 1: DB State Hookup**
  - Use `ItemStore.meta_get()` and `ItemStore.meta_set()` with key `tiktok_last_cookie_refresh` for timestamp persistence.
- [ ] **Step 2: Playwright Module (`cookie_refresh.py`)**
  - Implement headless, non-persistent Chromium navigation mirroring `tiktok_browser.py`.
  - Check `RecorderConfig.tiktok_cookies_file`: if unset, missing, or empty, log and cleanly skip.
  - Load cookies via the Netscape-format helper, execute browsing, read back updated session cookies from `context.cookies()`, and write them back to disk.
  - Guarantee browser closure in a `finally` block.
- [ ] **Step 3: Humanization**
  - Add bezier-curve scrolling, random element hovering, and 3-7 second randomized sleep intervals between views.
- [ ] **Step 4: Integration Hook**
  - Hook the probability trigger into `recorder/recorder/state.py` on transition to `RecorderState.HANDOFF`.
- [ ] **Step 5: Testing**
  - Create automated test module `recorder/recorder/_selftest_cookie_refresh.py` following the codebase's `_selftest_*.py` convention.
  - Verify the probability curve boundaries (<12h, 12h-24h, 24h-48h, >48h).
  - Verify skip behavior when `tiktok_cookies_file` is unset, missing, or empty.
  - Verify that refresh execution only triggers from `RecorderState.HANDOFF`.

