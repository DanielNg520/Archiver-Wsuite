"""
ops.cli
───────
  ops install          register the three service definitions + logrotate job
  ops uninstall        unload + remove the definitions
  ops health           one-shot system health report
  ops watch            health report refreshed every few seconds
  ops load [name]      start + enable managed services (all, or just one)
  ops unload [name]    stop + disable managed services (all, or just one —
                       stop a single worker while you edit its config,
                       then `ops load <name>` to bring it back)
  ops restart <name>   restart one service (dispatcher|recorder|archiver)
  ops update           on a codebase change: drain the dispatcher cleanly,
                       pipx-reinstall the four packages, reload + watch
  ops logrotate        copytruncate-rotate oversized worker logs (gzip history)

install/load/unload/restart are thin wrappers over the OS service manager
(launchd on macOS, Task Scheduler on Windows) via core.platform.service, so you
don't have to remember its verbs. Definitions are GENERATED for THIS machine's
home + pipx bin dir, not shipped as static files, so the absolute paths always
match where the CLIs actually live.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from core import termui as _termui
from core.platform import service as _service
from core.platform.service import JobSpec

from . import health as _health
from . import update as _update
from .health import LABELS, render
from .logrotate import DEFAULT_KEEP, DEFAULT_MAX_BYTES, rotate_logs

# Where the OS service manager captures each worker's stdout/err. Owned by the
# platform adapter (launchd → ~/.local/log, Task Scheduler → %APPDATA%/logs).
LOG_DIR = _service.log_dir()

# Calendar job (not a daemon): rotates the workers' captured logs daily so
# history survives reboots/truncation. Installed/removed alongside the three
# service definitions but never health-checked — it has no liveness.
LOGROTATE_LABEL = "com.duy.logrotate"

# service name → (CLI command on PATH, subcommand args). Mirrors what each
# launchd job should run: the dispatcher/recorder drain/listen continuously,
# the archiver loops `run` on a random interval.
_SERVICE_CMD: dict[str, tuple[str, list[str]]] = {
    "dispatcher": ("dispatcher", ["start"]),
    "recorder":   ("recorder",   ["start"]),
    "archiver":   ("archiver",   ["loop"]),
}


def cmd_health(_args: argparse.Namespace) -> int:
    print(render())
    return 0


def _watch_frame(anim: int) -> str:
    """One repaint of the watch screen, as a SINGLE string written atomically.

    Flicker-free redraw: never blank the screen (the old `\\033[2J` left a blank
    frame between clear and repaint — the visible blink). Instead home the cursor
    and overwrite in place. Each line ends with `\\033[K` (erase-to-end-of-line)
    so a shorter new line leaves no stale tail; a trailing `\\033[J`
    (erase-to-end-of-screen) drops orphaned rows when a frame is shorter than the
    last. Written in one shot so the terminal repaints in a single pass."""
    body = render(anim)   # render() draws its own header + live clock
    return "\033[H" + "\033[K\n".join(body.split("\n")) + "\033[K\033[J"


# Animation cadence, decoupled from data refresh: frames redraw the spinner /
# pulse / clock at 4 fps, while every data probe behind them is memoized inside
# ops.health for `--interval` seconds (and the expensive OS probes even longer).
# Smoothness costs string formatting only — no extra DB reads or subprocesses.
_FRAME_S = 0.25


def cmd_watch(args: argparse.Namespace) -> int:
    _health.set_data_ttl(args.interval)
    # The alt-screen / cursor / home escapes below need VT processing on a
    # Windows console even when colour is disabled (NO_COLOR) — without it a
    # legacy conhost prints the raw escapes instead of switching screens.
    _termui.ensure_vt()
    out = sys.stdout
    # Alternate screen buffer (like htop/less): watch gets its own screen, so an
    # oversized report can't smear and the user's scrollback is restored on exit.
    # Cursor hidden during the loop to kill its blink too.
    out.write("\033[?1049h\033[?25l")
    out.flush()
    try:
        anim = 0
        while True:
            out.write(_watch_frame(anim))
            out.flush()
            time.sleep(_FRAME_S)
            anim += 1
    except KeyboardInterrupt:
        return 0
    finally:
        out.write("\033[?25h\033[?1049l")  # restore cursor + leave alt screen
        out.flush()


def _resolve_bin(cmd: str) -> str | None:
    """Absolute path to a service CLI. Prefer PATH (pipx puts it there), fall
    back to ~/.local/bin/<cmd>. The service manager needs an absolute path — it
    does not source your shell, so a bare name would never resolve."""
    found = shutil.which(cmd)
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / cmd
    return str(fallback) if fallback.exists() else None


def cmd_logrotate(args: argparse.Namespace) -> int:
    actions = rotate_logs(
        LOG_DIR,
        max_bytes=int(args.max_mb * 1024 * 1024),
        keep=args.keep,
    )
    for a in actions:
        print(a)
    if not actions:
        print("logrotate: nothing over threshold")
    return 1 if any(a.startswith("ERROR") for a in actions) else 0


def cmd_install(_args: argparse.Namespace) -> int:
    """Register the OS service definitions for this machine: the three services
    + the daily logrotate calendar job. Idempotent — re-running overwrites with
    fresh paths. Run `ops load` afterward to start them (and at every login)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rc = 0
    for name, label in LABELS.items():
        cmd, sub_args = _SERVICE_CMD[name]
        program = _resolve_bin(cmd)
        if program is None:
            print(f"{name}: '{cmd}' not found on PATH or in ~/.local/bin — "
                  f"install it first (pipx install ./{cmd}), then re-run. skipped")
            rc = 1
            continue
        try:
            _service.install(JobSpec(label=label, program=program,
                                     args=sub_args, kind="daemon"))
        except (OSError, RuntimeError) as e:
            print(f"{name}: install FAILED — {e}")
            rc = 1
            continue
        print(f"{name}: installed  →  {program} {' '.join(sub_args)}")
    ops_bin = _resolve_bin("ops")
    if ops_bin is None:
        print("logrotate: 'ops' not found on PATH — job skipped")
        rc = 1
    else:
        try:
            _service.install(JobSpec(label=LOGROTATE_LABEL, program=ops_bin,
                                     args=["logrotate"], kind="calendar",
                                     calendar=(4, 5)))
            print(f"logrotate: installed  →  {ops_bin} logrotate (daily 04:05)")
        except (OSError, RuntimeError) as e:
            print(f"logrotate: install FAILED — {e}")
            rc = 1
    if rc == 0:
        print("installed. Now run:  ops load")
    return rc


def _all_jobs() -> list[tuple[str, str]]:
    """(display name, launchd label) for every plist ops manages: the three
    services plus the logrotate calendar job."""
    return [*LABELS.items(), ("logrotate", LOGROTATE_LABEL)]


def _selected_jobs(args: argparse.Namespace) -> list[tuple[str, str]]:
    """Jobs a load/unload acts on: the single named one, or all of them.
    The name is validated by argparse choices, so no not-found case here."""
    name = getattr(args, "service", None)
    if name:
        return [(n, l) for n, l in _all_jobs() if n == name]
    return _all_jobs()


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Unload (if loaded) and remove every ops-managed service definition."""
    cmd_unload(args)
    for name, label in _all_jobs():
        if _service.definition_exists(label):
            _service.uninstall(label)
            print(f"{name}: removed")
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    rc = 0
    for name, label in _selected_jobs(args):
        if not _service.definition_exists(label):
            print(f"{name}: not installed — run `ops install` first. skipped")
            rc = 1
            continue
        ok, msg = _service.load(label)
        print(f"{name}: {msg}" if ok else f"{name}: load failed — {msg}")
        rc = rc or (0 if ok else 1)
    return rc


def cmd_unload(args: argparse.Namespace) -> int:
    rc = 0
    for name, label in _selected_jobs(args):
        if not _service.definition_exists(label):
            continue
        ok, msg = _service.unload(label)
        print(f"{name}: {msg}" if ok else f"{name}: unload failed — {msg}")
        rc = rc or (0 if ok else 1)
    return rc


def cmd_restart(args: argparse.Namespace) -> int:
    if args.service not in LABELS:
        print(f"unknown service: {args.service} (choose from {list(LABELS)})")
        return 2
    label = LABELS[args.service]
    ok, msg = _service.restart(label)
    if ok:
        print(f"{args.service}: restarted")
        return 0
    print(f"{args.service}: restart failed — {msg}")
    return 1


def _drain_worker(name: str, args: argparse.Namespace) -> int | None:
    """Bring ONE worker to a graceful stop point before it is unloaded, and
    return its pid (so the caller can confirm the process is gone before the
    reinstall). Each worker drains on its own clock — the dispatcher finishes
    its current batch, the recorder its current capture — so they need not be
    idle at the same instant. The archiver has no in-flight artifact worth
    protecting, so it just reports its pid."""
    pid, _owner = _health.worker_pid(name)
    if name == "dispatcher":
        if pid is not None:
            _termui.field("dispatcher", f"draining current batch then stopping "
                          f"(pid {pid}, up to {args.stop_timeout:.0f}s)…",
                          accent="yellow")
        stopped = _update.graceful_stop_dispatcher(pid, args.stop_timeout)
        if pid is not None:
            _termui.field("dispatcher", "stopped cleanly" if stopped
                          else "still busy after timeout — will hard-stop with "
                               "the unload", accent="green" if stopped else "yellow")
    elif name == "recorder":
        if pid is not None and _update.recording_in_flight():
            _termui.field("recorder", f"recording in progress — waiting for it to "
                          f"finish (pid {pid}, up to {args.recording_timeout:.0f}s)…",
                          accent="yellow")
            done = _update.graceful_stop_recorder(pid, args.recording_timeout)
            _termui.field("recorder", "capture finished — safe to unload" if done
                          else "still recording after timeout — will hard-stop "
                               "with the unload", accent="green" if done else "yellow")
    return pid


def cmd_update(args: argparse.Namespace) -> int:
    """Detect a codebase change and deploy it with the MINIMUM disruption: only
    the workers actually affected are restarted, and each is brought to a
    graceful stop first (dispatcher finishes its batch, recorder its capture).
    An unaffected worker — e.g. a recorder mid-capture during an archiver-only
    update — is never touched. Then drop into `watch`.

    Package logic (see update.update_plan):
      • only `ops` changed  → editable, already live: nothing to restart.
      • only `core` changed  → restart all workers (no reinstall — editable core).
      • a worker changed     → reinstall + restart just that worker.
      • `--force`            → reinstall + restart everything.

    Run it from the repo root (or pass --repo). A no-op when nothing changed
    since the last successful update, unless --force."""
    repo = Path(args.repo).resolve() if args.repo else Path.cwd().resolve()
    if not _update.looks_like_repo_root(repo):
        print(f"update: {repo} is not the suite repo root (need core/ archiver/ "
              f"recorder/ dispatcher/, each with a pyproject.toml). "
              f"cd there or pass --repo.", file=sys.stderr)
        return 2

    current = _update.package_fingerprints(repo)
    stored = _update.read_stored_fingerprints()
    changed = _update.changed_packages(current, stored)
    if not changed and not args.force:
        _termui.field("update", "no codebase changes since the last update — "
                      "nothing to do (use --force to reinstall anyway)",
                      accent="green")
        return 0

    restart, reinstall = _update.update_plan(changed, force=args.force)

    if not restart and not reinstall:
        # The universal-but-trivial case: only `ops` (editable, imported by no
        # service) changed — the new code is live the instant it was saved.
        _termui.field("update", "only ops changed — editable, already live; "
                      "no reinstall or worker restart needed", accent="green")
        _update.write_fingerprints(current)
        _termui.field("update", "done — entering watch (Ctrl-C to exit)",
                      accent="green")
        time.sleep(1.0)
        return cmd_watch(argparse.Namespace(interval=args.interval))

    what = ("forced reinstall" if args.force else "codebase changed")
    detail = f"changed: {', '.join(sorted(changed))}  ·  restarting: " \
             f"{', '.join(sorted(restart))}" \
             + (f"  ·  reinstalling: {', '.join(sorted(reinstall))}"
                if reinstall else "  ·  no reinstall (editable)")
    _termui.field("update", f"{what}  ·  {detail}", accent="cyan")

    # Restart in a deterministic order so a dependent worker isn't left running
    # against a half-updated peer. Drain each worker to its own graceful stop
    # point first, remembering the pids so we can confirm they're gone before
    # the reinstall (avoids the Windows exe-lock on the pipx overwrite).
    order = [n for n in ("dispatcher", "recorder", "archiver") if n in restart]
    pids: list[int] = []

    rc = 0
    try:
        for name in order:
            pid = _drain_worker(name, args)
            if pid is not None:
                pids.append(pid)

        # Unload ONLY the affected workers (an unaffected recorder keeps
        # recording), then wait for their processes to actually exit + a settle
        # so the venv/exe locks are released before the reinstall overwrites them.
        for name in order:
            cmd_unload(argparse.Namespace(service=name))
        _update.wait_processes_down(pids)

        # Reinstall only the changed worker packages (a core-only change ships
        # via the editable inject on restart alone — reinstall set is empty).
        if reinstall:
            rc = _update.run_reinstall(repo, reinstall)
    finally:
        # Whatever happens, never leave the stop-flag behind for the reload.
        _update.clear_stop_flag()

    if rc != 0:
        print("update: reinstall failed — reloading workers with the "
              "PREVIOUSLY installed code and leaving the fingerprint unchanged "
              "so the next `ops update` retries.", file=sys.stderr)
        for name in order:
            cmd_load(argparse.Namespace(service=name))
        return 1

    # Success: remember what we installed so the next run can no-op, then bring
    # the restarted workers back up.
    _update.write_fingerprints(current)
    for name in order:
        cmd_load(argparse.Namespace(service=name))

    # Hand off to watch.
    _termui.field("update", "done — entering watch (Ctrl-C to exit)",
                  accent="green")
    time.sleep(1.0)
    return cmd_watch(argparse.Namespace(interval=args.interval))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ops",
                                description="Ops tooling for the archiver/recorder/dispatcher system.")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="register the service definitions")
    sub.add_parser("uninstall", help="unload + remove the definitions")
    sub.add_parser("health", help="one-shot health report")
    w = sub.add_parser("watch", help="auto-refreshing health report")
    w.add_argument("--interval", type=float, default=3.0,
                   help="data refresh seconds (animation redraws faster)")
    job_names = [*LABELS, "logrotate"]
    ld = sub.add_parser("load", help="start + enable managed services "
                                     "(all, or just one)")
    ld.add_argument("service", nargs="?", choices=job_names,
                    help="load only this job (default: all)")
    ul = sub.add_parser("unload", help="stop + disable managed services "
                                       "(all, or just one — e.g. to edit "
                                       "its config while it's stopped)")
    ul.add_argument("service", nargs="?", choices=job_names,
                    help="unload only this job (default: all)")
    r = sub.add_parser("restart", help="restart one service")
    r.add_argument("service", choices=list(LABELS))
    up = sub.add_parser(
        "update",
        help="on a codebase change: drain the dispatcher cleanly, pipx-reinstall "
             "the packages, reload + watch")
    up.add_argument("--repo", default=None,
                    help="suite repo root (default: current directory)")
    up.add_argument("--force", action="store_true",
                    help="reinstall even if no source change is detected")
    up.add_argument("--stop-timeout", dest="stop_timeout", type=float,
                    default=300.0,
                    help="seconds to let the dispatcher finish its in-flight "
                         "batch before hard-stopping it (default 300)")
    up.add_argument("--recording-timeout", dest="recording_timeout", type=float,
                    default=1800.0,
                    help="seconds to wait for the recorder's in-flight capture "
                         "to finish before hard-stopping it (default 1800; only "
                         "waited when the recorder is being restarted AND a "
                         "recording is actually in flight)")
    up.add_argument("--interval", type=float, default=3.0,
                    help="watch data-refresh seconds after the update completes")
    lr = sub.add_parser(
        "logrotate",
        help="copytruncate-rotate oversized ~/.local/log/*.log (gzip history)")
    lr.add_argument("--max-mb", type=float,
                    default=DEFAULT_MAX_BYTES / (1024 * 1024),
                    help="rotate logs larger than this many MiB")
    lr.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                    help="compressed generations to keep per log")
    return p


_DISPATCH = {
    "install":   cmd_install,
    "uninstall": cmd_uninstall,
    "health":    cmd_health,
    "watch":     cmd_watch,
    "load":      cmd_load,
    "unload":    cmd_unload,
    "restart":   cmd_restart,
    "update":    cmd_update,
    "logrotate": cmd_logrotate,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
