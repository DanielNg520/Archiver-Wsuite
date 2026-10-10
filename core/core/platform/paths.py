"""
core.platform.paths
────────────────────
The OS-correct home for the suite's per-app config directories.

Before the port these were spelled ``~/.config/<app>`` inline in a dozen places.
That is correct on this suite's macOS convention, where per-user config lives
in a dotfolder in the home directory.

This module centralizes the rule:

    any OS                 →  $ARCHIVER_CONFIG_HOME/<app>   (if set)
    Linux                  →  <repo>/.config/<app>
    macOS                  →  $XDG_CONFIG_HOME/<app>        (default ~/.config/<app>)

We deliberately do NOT use ``platformdirs`` here: its macOS default resolves to
``~/Library/Application Support/<app>``, which would silently relocate every
existing macOS install away from the ``~/.config`` layout the suite has always
used. Keeping the macOS branch as literal ``~/.config`` preserves those installs
byte-for-byte.

Everything is a function (not a constant) so the value reflects the environment
at call time — matching core.schema.db_path()'s style and keeping tests that
patch $HOME / $XDG_CONFIG_HOME honest.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The suite's three config "apps". These app names ARE the on-disk directory
# names under the per-user config root; do not rename without a migration.
SUITE = "archiver-suite"
DISPATCHER = "dispatcher"
ARCHIVER = "archiver"
RECORDER = "recorder"


def _codebase_config_home() -> Path:
    """The self-contained config root that lives INSIDE the codebase checkout:
    ``<repo>/.config``. `core` is injected editable, so this file physically
    lives at ``<repo>/core/core/platform/paths.py`` in every venv — parents[3]
    is the repo root regardless of which app imported us. Keeping config + DB
    here (git-ignored) makes the whole suite portable: the checkout carries its
    own state, nothing leaks into ``~/.config``."""
    return Path(__file__).resolve().parents[3] / ".config"


def _config_home() -> Path:
    """The per-user config root for the current OS (no app segment).

    ``ARCHIVER_CONFIG_HOME`` overrides everything (any OS) for non-standard
    archive roots and tests."""
    override = os.environ.get("ARCHIVER_CONFIG_HOME")
    if override:
        return Path(override)
    # Linux: SELF-CONTAINED — config + DB live in <codebase>/.config so the
    # checkout carries its own state and nothing leaks into the home dir. We
    # deliberately do NOT honor XDG_CONFIG_HOME here (it is set to ~/.config on
    # most desktops and would defeat self-containment); ARCHIVER_CONFIG_HOME
    # above remains the explicit escape hatch for a non-default location.
    if sys.platform.startswith("linux"):
        return _codebase_config_home()
    # Other POSIX (macOS): honor XDG, else the long-standing ~/.config layout.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".config"


def config_dir(app: str = SUITE) -> Path:
    """Config directory for ``app`` (e.g. 'archiver-suite', 'dispatcher').

    $ARCHIVER_CONFIG_HOME/<app> if set (any OS); Linux → <repo>/.config/<app>;
    macOS → $XDG_CONFIG_HOME/<app> (default ~/.config/<app>). Not created
    here; callers create on write (matching prior behavior)."""
    return _config_home() / app


def locks_dir() -> Path:
    """Directory holding the suite's cross-process lock files."""
    return config_dir(SUITE) / "locks"
