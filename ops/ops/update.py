"""
ops.update
──────────
The `ops update` machinery: detect that the repo's package source changed since
the last install, drain the dispatcher CLEANLY (finish the in-flight upload,
never chop it), reinstall the four pipx packages, then hand back to the caller
to reload + watch.

Kept out of cli.py so the change-detection + reinstall steps are unit-testable
in isolation (source_fingerprint / reinstall_commands take plain paths).

BOUNDARY: like ops.health, this imports NOTHING from the worker packages
(dispatcher/recorder/archiver) — the cooperative stop is done purely through
on-disk artifacts whose locations live in the shared `core` library
(core.paths.dispatcher_stop_flag), so writer (dispatcher) and trigger (ops)
can't drift apart.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core import paths as _paths
from core.platform import paths as _osp
from core.platform import process as _process

# The pipx-managed packages, in the exact order they must be (re)installed:
# the three worker apps, then core re-injected editable into the archiver (a
# `pipx install --force` wipes an app's injected packages, so the inject MUST
# come last). `ops` itself is deliberately absent — it is the process running
# this command, so reinstalling it would fight its own file locks; and `core`
# is not a standalone app, it rides along as each app's dependency + the
# editable inject. This mirrors the commands a human would run by hand.
#
# Two naming traps the hand-typed commands hit (see the user's failed inject):
#   • the archiver's pipx APP/venv is `media-archiver` (its distribution name in
#     pyproject), NOT `archiver` — so the inject must target `media-archiver`.
#     The install step still points at the `./archiver` DIR; pipx derives the
#     app name from the package, so installing the dir reinstalls media-archiver.
#   • `pipx inject` without `--force` is a NO-OP when core is already injected
#     (it warns "already seems to be injected" and changes nothing). `--force`
#     makes the re-inject unconditional — required whenever the venv survived
#     rather than being wiped, and harmless on a fresh one.
ARCHIVER_APP = "media-archiver"
REINSTALL_STEPS: list[list[str]] = [
    ["install", "--force", "archiver"],
    ["install", "--force", "dispatcher"],
    ["install", "--force", "recorder"],
    ["inject", ARCHIVER_APP, "--force", "--editable", "core"],
]

# Source trees whose contents decide "did the code change?". core is included
# even though it is not force-installed: an editable-injected core edit still
# ships new behavior on the next worker restart, so a core-only change is a real
# update worth reloading for.
_FINGERPRINT_DIRS = ["core", "archiver", "recorder", "dispatcher"]

# Files that define what gets installed / how the code behaves. mtime is NOT
# used — a `git checkout` can rewrite mtimes without changing bytes, and a
# content hash is the honest question ("are the installed bytes different?").
_FINGERPRINT_SUFFIXES = (".py", ".toml", ".cfg")

# Directories never worth hashing: build artifacts, caches, venvs, VCS. Skipping
# `build`/`dist` matters — a stale `dispatcher/build/lib/...` tree would
# otherwise fold old bytes into the fingerprint.
_SKIP_DIRS = {"build", "dist", "__pycache__", ".git", ".venv", "venv",
              ".mypy_cache", ".pytest_cache"}


def fingerprint_path() -> Path:
    """Where the last successfully-installed source fingerprint is remembered."""
    return _osp.config_dir(_osp.SUITE) / "update.fingerprint"


def _iter_source_files(repo_root: Path):
    """Yield every install-relevant source file under the package dirs, in a
    stable order. Uses os.walk with in-place `dirnames` pruning — the idiomatic
    way to skip whole subtrees (build/, __pycache__, .egg-info) WITHOUT first
    descending into them, so a stale `dispatcher/build/lib/...` tree costs
    nothing to ignore."""
    def _pruned(base: Path):
        for root, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in _SKIP_DIRS
                                 and not d.endswith(".egg-info"))
            for name in sorted(filenames):
                if Path(name).suffix.lower() in _FINGERPRINT_SUFFIXES:
                    yield Path(root) / name

    for pkg in _FINGERPRINT_DIRS:
        base = repo_root / pkg
        if base.is_dir():
            yield from _pruned(base)


def source_fingerprint(repo_root: Path) -> str:
    """A stable sha256 over every install-relevant source file under the four
    package dirs. Path-relative + content, sorted, so it is deterministic and
    independent of where the repo is checked out."""
    h = hashlib.sha256()
    for path in _iter_source_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def read_stored_fingerprint() -> str | None:
    try:
        return fingerprint_path().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_fingerprint(value: str) -> None:
    p = fingerprint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n", encoding="utf-8")


def looks_like_repo_root(path: Path) -> bool:
    """A directory is the suite repo root iff every package we install lives
    directly under it (each with a pyproject.toml)."""
    return all((path / pkg / "pyproject.toml").is_file()
               for pkg in ("core", "archiver", "recorder", "dispatcher"))


def _pipx_argv(step: list[str], repo_root: Path) -> list[str]:
    """Turn a REINSTALL_STEPS entry into a full `python -m pipx …` argv with the
    package path resolved against the repo root. `python` is taken from PATH:
    `ops update` runs in the user's own shell (never a packaged session), so its
    PATH `python` is the real interpreter that owns pipx — and CLAUDE.md's rule
    is to spell it `python -m pipx`, never the bare pipx shim."""
    python = shutil.which("python") or shutil.which("python3") or "python"
    argv = [python, "-m", "pipx", *step]
    # The trailing token of an install/inject step is a package DIR — make it an
    # absolute path so the command is CWD-independent.
    pkg_dir = repo_root / argv[-1]
    if pkg_dir.is_dir():
        argv[-1] = str(pkg_dir)
    return argv


def run_reinstall(repo_root: Path) -> int:
    """Run the four pipx steps in order, streaming their output. Stops at the
    first failure (a later step assuming an earlier install is pointless) and
    returns that step's exit code; 0 iff every step succeeded."""
    for step in REINSTALL_STEPS:
        argv = _pipx_argv(step, repo_root)
        print(f"\n$ {' '.join(argv)}", flush=True)
        rc = subprocess.run(argv).returncode
        if rc != 0:
            print(f"pipx step failed (exit {rc}): {' '.join(step)}",
                  file=sys.stderr)
            return rc
    return 0


def graceful_stop_dispatcher(dispatcher_pid: int | None,
                             timeout_s: float) -> bool:
    """Ask a running dispatcher to finish its current batch and exit, then wait
    for it to actually go. Writes the cooperative stop-flag and polls the pid's
    liveness (a fresh OS probe, never a cached snapshot).

    Returns True if the dispatcher exited (or was never running), False if it
    was still alive when the timeout elapsed — the caller then falls back to a
    hard unload. The flag file is left in place for the caller to clear right
    before it reloads the workers."""
    flag = _paths.dispatcher_stop_flag()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("ops update: drain then exit\n", encoding="utf-8")

    if dispatcher_pid is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not _process.pid_alive(dispatcher_pid):
            return True
        time.sleep(2.0)
    return not _process.pid_alive(dispatcher_pid)


def clear_stop_flag() -> None:
    """Remove the cooperative stop-flag so a reloaded dispatcher runs normally."""
    try:
        _paths.dispatcher_stop_flag().unlink(missing_ok=True)
    except OSError:
        pass
