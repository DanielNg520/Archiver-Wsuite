"""
core.platform
─────────────
The single seam between the suite and the host operating system. Everything
POSIX-specific lives behind an adapter here, so the rest of the codebase
(store / ingest / send / media_prep) stays platform-blind.

Adapters:
  • paths      — config/state/lock directories
  • filelock   — fcntl.flock
  • process    — os.kill(pid,0)
  • procgroup  — os.killpg
  • signals    — SIGTERM, sync/async wiring
  • service    — launchd / systemd user units

Design rule: each adapter exposes ONE API, branching between Linux and macOS
only where they differ; behavior is kept parallel.
"""

from __future__ import annotations

from . import paths
from . import filelock
from . import process
from . import procgroup
from . import signals
from . import service

__all__ = ["paths", "filelock", "process", "procgroup", "signals", "service"]
