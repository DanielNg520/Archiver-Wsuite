"""
core.platform
─────────────
The single seam between the suite and the host operating system. Everything
POSIX-specific that the port must replace lives behind an adapter here, so the
rest of the codebase (store / ingest / send / media_prep) stays platform-blind
— no scattered ``if os.name == "nt"`` checks.

Adapters (added phase by phase):
  • paths      — config/state/lock directories        (Phase 1)
  • filelock   — fcntl.flock ↔ msvcrt.locking          (Phase 2)
  • process    — os.kill(pid,0) ↔ OpenProcess liveness (Phase 2)
  • procgroup  — os.killpg ↔ CTRL_BREAK / taskkill /T   (Phase 3)
  • signals    — SIGTERM ↔ SIGBREAK, sync/async wiring   (Phase 4)
  • service    — launchd ↔ Task Scheduler               (Phase 5)

Design rule: each adapter exposes ONE platform-blind API; the POSIX and Windows
implementations sit side by side and are selected at call time by ``os.name``.
POSIX behavior must stay byte-for-byte what it was before the port so existing
macOS/Linux installs are untouched.
"""

from __future__ import annotations

from . import paths
from . import filelock
from . import process
from . import procgroup
from . import signals
from . import service

__all__ = ["paths", "filelock", "process", "procgroup", "signals", "service"]
