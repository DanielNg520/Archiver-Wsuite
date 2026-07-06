"""
core.platform
─────────────
The single seam between the suite and the host operating system. Everything
POSIX-specific that the port must replace lives behind an adapter here, so the
rest of the codebase (store / ingest / send / media_prep) stays platform-blind
— no scattered ``if os.name == "nt"`` checks.

Adapters (added phase by phase):
  • paths      — config/state/lock directories        (Phase 1, this commit)
  • filelock   — fcntl.flock ↔ msvcrt.locking          (Phase 2)
  • procgroup  — os.killpg ↔ Job Object / taskkill /T   (Phase 3)
  • service    — launchd ↔ Task Scheduler / Service     (Phase 5)

Design rule: each adapter exposes ONE platform-blind API; the POSIX and Windows
implementations sit side by side and are selected at call time by ``os.name``.
POSIX behavior must stay byte-for-byte what it was before the port so existing
macOS/Linux installs are untouched.
"""

from __future__ import annotations

from . import paths

__all__ = ["paths"]
