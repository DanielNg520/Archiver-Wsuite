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
    "logrotate": cmd_logrotate,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _DISPATCH[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
