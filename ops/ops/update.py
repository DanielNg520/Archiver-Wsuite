"""
ops.update
──────────
The `ops update` machinery: detect that the repo's package source changed since
the last install, drain the dispatcher CLEANLY (finish the in-flight upload,
never chop it), reinstall the four pipx packages, then hand back to the caller
to reload + watch.

Kept out of cli.py so the change-detection + reinstall steps are unit-testable
in isolation (package_fingerprints / update_plan / reinstall_steps take plain
paths + sets).

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

from core import heartbeat as _heartbeat
from core import paths as _paths
from core.platform import paths as _osp
from core.platform import process as _process

# The pipx-managed packages, in the exact order they must be (re)installed:
# the three worker apps, then core re-injected editable into the archiver (a
# `pipx install --force` wipes an app's injected packages, so the inject MUST
# come last). `core` is not a standalone app, it rides along as each app's
# dependency + the editable inject. This mirrors the commands a human would run
# by hand.
#
# `ops` itself is deliberately absent, and that is now CORRECT rather than a
# gap: ops (and the core it imports) are installed EDITABLE, so an ops-side
# change — a new dashboard field, a health probe — is live the moment the file
# is saved, with nothing to reinstall. A running process cannot force-reinstall
# its own venv anyway (Windows locks the loaded .pyd/.dll), so editable is the
# only way ops changes could deploy without a fragile detached self-reinstall.
# The one-time cost is a single editable install (see ops/RUNBOOK.md "Editable
# ops"); after that `ops update` covers every package for every CODE change.
# The lone residual case — an ops dependency or console-script change, which
# editable can't pick up — still needs a manual `pipx install --force .\ops`
# from the user's shell (a process can't do that to itself live).
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

# The worker packages, in the order they must be (re)installed. The dir name is
# also the pipx-install target (pipx derives the app name from the package), and
# it matches the fingerprint/package key — the one exception is that the
# archiver's *app* is `media-archiver`, which only matters for the core inject.
_INSTALL_ORDER = ("archiver", "dispatcher", "recorder")


def reinstall_steps(packages: "set[str] | frozenset[str]") -> list[list[str]]:
    """The pipx steps to (re)install exactly `packages` (a subset of the worker
    packages), in install order, with the core editable re-inject appended IFF
    the archiver is among them (a `pipx install --force media-archiver` wipes its
    injected packages, so the inject MUST come last — and is pointless when the
    archiver wasn't reinstalled). An empty/None set yields no steps: a core-only
    or ops-only change reinstalls nothing (both ride along editable)."""
    steps = [["install", "--force", pkg]
             for pkg in _INSTALL_ORDER if pkg in (packages or set())]
    if "archiver" in (packages or set()):
        steps.append(["inject", ARCHIVER_APP, "--force", "--editable", "core"])
    return steps

# Source trees whose contents decide "did the code change?". core AND ops are
# included even though neither is force-installed: an editable-injected core
# edit ships new behavior on the next worker restart, and an editable ops edit
# is live immediately — both are real updates worth advancing the fingerprint
# for (and worth the reload, so a core change actually takes effect). Omitting
# ops was a real blind spot: an ops-only change (e.g. a new `ops health` field)
# then read as "nothing changed" and `ops update` no-op'd it away.
_FINGERPRINT_DIRS = ["core", "ops", "archiver", "recorder", "dispatcher"]

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


# Worker packages: the ones whose code the OS-managed services actually run, so
# a change to any of them needs a pipx reinstall + a worker restart. `core`
# rides along editable, but a core edit still needs the workers RESTARTED to
# load it; `ops` is editable and imported by nothing the services run, so an
# ops-only change needs neither reinstall nor restart — it is live at once.
_WORKER_PKGS = ("archiver", "recorder", "dispatcher")
_RESTART_PKGS = frozenset(_WORKER_PKGS) | {"core"}


def _iter_pkg_files(base: Path):
    """Yield every install-relevant source file under ONE package dir, in a
    stable order. Uses os.walk with in-place `dirnames` pruning — the idiomatic
    way to skip whole subtrees (build/, __pycache__, .egg-info) WITHOUT first
    descending into them, so a stale `dispatcher/build/lib/...` tree costs
    nothing to ignore."""
    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP_DIRS
                             and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            if Path(name).suffix.lower() in _FINGERPRINT_SUFFIXES:
                yield Path(root) / name


def _iter_source_files(repo_root: Path):
    """Every install-relevant source file under all package dirs, stable order."""
    for pkg in _FINGERPRINT_DIRS:
        base = repo_root / pkg
        if base.is_dir():
            yield from _iter_pkg_files(base)


def _hash_files(repo_root: Path, files) -> str:
    """Stable sha256 over (repo-relative path + content) for `files`, sorted so
    it is deterministic and independent of where the repo is checked out."""
    h = hashlib.sha256()
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def package_fingerprints(repo_root: Path) -> dict[str, str]:
    """A PER-PACKAGE sha256, so `ops update` can tell WHICH packages changed and
    do the minimum: reinstall only when a worker package changed, restart only
    when a worker or core changed, and treat an ops-only change as already-live
    (editable) — no drain, no reinstall, no restart. Absent package dirs are
    simply omitted."""
    out: dict[str, str] = {}
    for pkg in _FINGERPRINT_DIRS:
        base = repo_root / pkg
        if base.is_dir():
            out[pkg] = _hash_files(repo_root, _iter_pkg_files(base))
    return out


def source_fingerprint(repo_root: Path) -> str:
    """A single stable sha256 over every install-relevant source file (all
    packages). Retained as the combined view; the per-package map is
    package_fingerprints()."""
    return _hash_files(repo_root, _iter_source_files(repo_root))


def changed_packages(current: dict[str, str],
                     stored: dict[str, str] | None) -> set[str]:
    """Which packages differ between the current tree and the last-installed
    fingerprint map. A missing/legacy stored map (None) means an unknown
    baseline → treat every package as changed, so the first run after this
    upgrade does a full, safe update."""
    if not stored:
        return set(current)
    return ({p for p, h in current.items() if stored.get(p) != h}
            | {p for p in stored if p not in current})


def needs_worker_reinstall(changed: set[str]) -> bool:
    """True iff a package the services actually run changed — the only case that
    warrants the pipx reinstall + dispatcher drain."""
    return any(p in _WORKER_PKGS for p in changed)


def needs_worker_restart(changed: set[str]) -> bool:
    """True iff the workers must be restarted to pick the change up: a worker
    package (reinstalled) or core (editable, loaded on restart). An ops-only
    change needs no restart — ops is editable and the services never import it."""
    return any(p in _RESTART_PKGS for p in changed)


def update_plan(changed: "set[str] | frozenset[str]",
                force: bool = False) -> tuple[set[str], set[str]]:
    """Reduce a change set to the MINIMUM work: return
    `(workers_to_restart, workers_to_reinstall)`, each a subset of the worker
    packages.

      • `--force`               → restart + reinstall all workers.
      • a worker package changed → reinstall AND restart just that worker
                                    (the others keep running — a live recording
                                    or in-flight upload on an unaffected worker
                                    is never disturbed).
      • `core` changed           → restart ALL workers (they load the editable
                                    core on restart) but reinstall NONE.
      • only `ops`/nothing       → both sets empty (ops is editable, live).

    A worker to reinstall is always also restarted (the reinstall replaces its
    code, then load brings the new code up)."""
    workers = set(_WORKER_PKGS)
    if force:
        return set(workers), set(workers)
    reinstall = {p for p in changed if p in workers}
    restart = set(reinstall)
    if "core" in changed:
        restart = set(workers)          # every service imports the editable core
    return restart, reinstall


def read_stored_fingerprints() -> dict[str, str] | None:
    """The last-installed per-package fingerprint map, or None when absent /
    unreadable / a legacy single-hash file (which won't parse as JSON — handled
    as an unknown baseline, forcing one full update)."""
    import json
    try:
        text = fingerprint_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None   # legacy bare-hex file → unknown baseline
    return data if isinstance(data, dict) else None


def write_fingerprints(fps: dict[str, str]) -> None:
    import json
    p = fingerprint_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(fps, indent=2, sort_keys=True) + "\n",
                 encoding="utf-8")


def looks_like_repo_root(path: Path) -> bool:
    """A directory is the suite repo root iff every package we install lives
    directly under it (each with a pyproject.toml)."""
    return all((path / pkg / "pyproject.toml").is_file()
               for pkg in ("core", "archiver", "recorder", "dispatcher"))


def _pipx_argv(step: list[str], repo_root: Path) -> list[str]:
    """Turn a reinstall_steps() entry into a full `python -m pipx …` argv with the
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


def wait_processes_down(pids, timeout_s: float = 20.0,
                        settle_s: float = 3.0) -> bool:
    """After an unload, block until none of `pids` is alive (or `timeout_s`
    elapses), then wait a short `settle_s`. This guards the classic Windows
    `WinError 32` ("file is being used by another process") that pipx hits on
    `.local\\bin\\<app>.exe`: `taskkill`/`schtasks /End` return BEFORE the OS
    drops the running image's lock on the exe, so a reinstall fired immediately
    after unload can find the shim still held. Returns True iff every pid was
    confirmed gone (the settle still runs either way — the image-handle release
    lags the pid's disappearance)."""
    alive = [p for p in pids if p]
    deadline = time.monotonic() + max(0.0, timeout_s)
    while alive and time.monotonic() < deadline:
        alive = [p for p in alive if _process.pid_alive(p)]
        if alive:
            time.sleep(0.5)
    time.sleep(max(0.0, settle_s))
    return not alive


def run_reinstall(repo_root: Path, packages: "set[str] | frozenset[str]", *,
                  attempts: int = 3, retry_delay_s: float = 4.0) -> int:
    """Run the pipx steps for `packages` in order, streaming their output. Stops
    at the first hard failure (a later step assuming an earlier install is
    pointless) and returns that step's exit code; 0 iff every step succeeded
    (and 0 for an empty set — nothing to reinstall).

    Each step is retried a few times on failure: the dominant failure mode on
    this box is a TRANSIENT Windows exe lock (the worker's shim still held for a
    beat after unload), which clears on its own within seconds — so a short
    wait-and-retry turns a spurious abort into a clean install. A genuinely
    broken step just exhausts the attempts and returns its code as before."""
    for step in reinstall_steps(packages):
        argv = _pipx_argv(step, repo_root)
        print(f"\n$ {' '.join(argv)}", flush=True)
        rc = subprocess.run(argv).returncode
        tries = 1
        while rc != 0 and tries < attempts:
            print(f"  step failed (exit {rc}) — likely a transient exe lock; "
                  f"retrying in {retry_delay_s:.0f}s "
                  f"(attempt {tries + 1}/{attempts})…", flush=True)
            time.sleep(retry_delay_s)
            rc = subprocess.run(argv).returncode
            tries += 1
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


def recording_in_flight() -> bool:
    """True iff a live capture is in progress right now. Reads the recorder's
    TikTok lock through the SAME liveness gate ops.health uses: a crashed
    recorder's stale lock reads as not-held, so we never wait on a phantom
    recording."""
    return _heartbeat.read_live(_paths.tiktok_lock()) is not None


def graceful_stop_recorder(recorder_pid: int | None, timeout_s: float) -> bool:
    """Wait for the recorder to reach a clean stop point before it is unloaded:
    let the CURRENT capture finish rather than chop a live stream mid-recording.

    The recorder has no cooperative stop-flag the way the dispatcher does, and
    its `_stop` signal *interrupts* a capture — so the graceful lever here is to
    WAIT for the in-flight recording to end naturally (the TikTok lock clears),
    up to `timeout_s`, then let the caller unload. Returns True if the recorder
    is idle (no recording, or it finished within the window), False if a capture
    was still running at the timeout — the caller then unloads anyway (a stream
    that runs longer than the budget can't hold a redeploy forever).

    Independent of the dispatcher drain: each worker reaches its own stop point
    on its own clock; they need not be idle at the same instant."""
    if recorder_pid is None or not recording_in_flight():
        return True
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not _process.pid_alive(recorder_pid):
            return True                      # recorder exited on its own
        if not recording_in_flight():
            return True                      # capture finished — safe to unload
        time.sleep(3.0)
    return not recording_in_flight()
