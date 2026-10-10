"""
core.platform.service
──────────────────────
The OS service-manager seam: install / start / stop / restart / status for the
suite's long-running workers, plus the daily logrotate calendar job.

  macOS    → launchd LaunchAgents (~/Library/LaunchAgents/<label>.plist,
             launchctl load/unload/kickstart/list). Per-user, RunAtLoad +
             KeepAlive, capture stdout/err to ~/.local/log.
  Linux    → systemd user units (~/.config/systemd/user, systemctl --user,
             timer for calendar jobs, logs to ~/.local/log).

Both backends expose the SAME verbs so ops/cli.py and ops/health.py stay
platform-blind:

  log_dir() -> Path                     # where worker stdout/err is captured
  install(spec: JobSpec) -> None        # register/write the definition
  uninstall(label) -> None
  load(label)   -> (ok: bool, msg: str) # start + enable
  unload(label) -> (ok: bool, msg: str) # stop + disable
  restart(label)-> (ok: bool, msg: str)
  definition_exists(label) -> bool
  running_pid(label) -> int | None      # managed pid if the OS exposes one

A JobSpec is the platform-neutral description of one job. `kind="daemon"` is a
keep-alive worker (RunAtLoad/KeepAlive ↔ Restart=always in a systemd unit);
`kind="calendar"` is a once-daily job (StartCalendarInterval ↔ OnCalendar timer).
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class JobSpec:
    label: str                       # e.g. "com.duy.dispatcher"
    program: str                     # absolute path to the worker CLI
    args: list[str] = field(default_factory=list)
    kind: str = "daemon"             # "daemon" | "calendar"
    calendar: "tuple[int, int] | None" = None   # (hour, minute) for calendar

    @property
    def tag(self) -> str:
        return self.label.rsplit(".", 1)[-1]     # com.duy.dispatcher → dispatcher


def _log_paths(tag: str) -> "tuple[Path, Path]":
    d = log_dir()
    return d / f"{tag}.out.log", d / f"{tag}.err.log"


if sys.platform == "darwin":                          # ── macOS / launchd ──

    _LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()

    def log_dir() -> Path:
        return Path("~/.local/log").expanduser()

    def _plist_path(label: str) -> Path:
        return _LAUNCH_AGENTS / f"{label}.plist"

    def _plist_xml(spec: JobSpec) -> str:
        out, err = _log_paths(spec.tag)
        prog_lines = "\n".join(f"        <string>{a}</string>"
                               for a in (spec.program, *spec.args))
        if spec.kind == "calendar":
            hour, minute = spec.calendar or (4, 5)
            schedule = (
                "    <key>StartCalendarInterval</key>\n"
                "    <dict>\n"
                f"        <key>Hour</key>\n        <integer>{hour}</integer>\n"
                f"        <key>Minute</key>\n        <integer>{minute}</integer>\n"
                "    </dict>"
            )
            env_block = ""
            workdir_block = ""
        else:
            schedule = (
                "    <key>RunAtLoad</key>\n    <true/>\n"
                "    <key>KeepAlive</key>\n    <true/>\n"
                "    <key>ThrottleInterval</key>\n    <integer>30</integer>"
            )
            bindir = str(Path(spec.program).parent)
            path_env = ":".join([bindir, "/opt/homebrew/bin", "/usr/local/bin",
                                 "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
            env_block = (
                "    <key>EnvironmentVariables</key>\n"
                "    <dict>\n"
                "        <key>PATH</key>\n"
                f"        <string>{path_env}</string>\n"
                "    </dict>\n"
            )
            workdir_block = (
                "    <key>WorkingDirectory</key>\n"
                f"    <string>{Path.home()}</string>\n"
            )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "    <key>Label</key>\n"
            f"    <string>{spec.label}</string>\n"
            "    <key>ProgramArguments</key>\n"
            "    <array>\n"
            f"{prog_lines}\n"
            "    </array>\n"
            f"{schedule}\n"
            f"{env_block}"
            "    <key>StandardOutPath</key>\n"
            f"    <string>{out}</string>\n"
            "    <key>StandardErrorPath</key>\n"
            f"    <string>{err}</string>\n"
            f"{workdir_block}"
            "    <key>ProcessType</key>\n"
            "    <string>Background</string>\n"
            "</dict>\n"
            "</plist>\n"
        )

    def install(spec: JobSpec) -> None:
        _LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        log_dir().mkdir(parents=True, exist_ok=True)
        _plist_path(spec.label).write_text(_plist_xml(spec))

    def uninstall(label: str) -> None:
        p = _plist_path(label)
        if p.exists():
            p.unlink()

    def load(label: str) -> "tuple[bool, str]":
        p = _plist_path(label)
        if not p.exists():
            return False, f"plist missing ({p})"
        r = subprocess.run(["launchctl", "load", str(p)],
                           capture_output=True, text=True)
        return (r.returncode == 0, "loaded" if r.returncode == 0 else r.stderr.strip())

    def unload(label: str) -> "tuple[bool, str]":
        p = _plist_path(label)
        if not p.exists():
            return True, "not present"
        r = subprocess.run(["launchctl", "unload", str(p)],
                           capture_output=True, text=True)
        return (r.returncode == 0, "unloaded" if r.returncode == 0 else r.stderr.strip())

    def restart(label: str) -> "tuple[bool, str]":
        uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"],
            capture_output=True, text=True,
        )
        return (r.returncode == 0, "restarted" if r.returncode == 0 else r.stderr.strip())

    def definition_exists(label: str) -> bool:
        return _plist_path(label).exists()

    def running_pid(label: str) -> "int | None":
        try:
            out = subprocess.run(["launchctl", "list", label],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            s = line.strip()
            if s.startswith('"PID"'):
                digits = "".join(c for c in s if c.isdigit())
                return int(digits) if digits else None
        return None

    def job_state(label: str) -> "str | None":
        """'running' | 'enabled' | 'disabled' | None — the Linux branch's
        contract, mapped onto launchd: a listed job with a PID is running,
        listed without one is loaded/enabled, a plist on disk that launchctl
        doesn't know is unloaded (≈ disabled), no plist means not installed."""
        if running_pid(label) is not None:
            return "running"
        try:
            out = subprocess.run(["launchctl", "list", label],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode == 0:
            return "enabled"
        return "disabled" if definition_exists(label) else None

else:                                                 # ── Linux / systemd ──

    _SYSTEMD_USER = Path("~/.config/systemd/user").expanduser()

    def log_dir() -> Path:
        return Path("~/.local/log").expanduser()

    def _service_path(label: str) -> Path:
        return _SYSTEMD_USER / f"{label}.service"

    def _timer_path(label: str) -> Path:
        return _SYSTEMD_USER / f"{label}.timer"

    def _service_unit(spec: JobSpec) -> str:
        out, err = _log_paths(spec.tag)
        args_str = " ".join([spec.program] + spec.args)

        # A calendar job is a one-shot triggered by its .timer: it runs once and
        # exits. It must NOT carry Restart=always — systemd would relaunch it
        # every RestartSec seconds in an endless loop the moment it succeeds.
        if spec.kind == "calendar":
            restart_block = "Type=oneshot\n"
        else:
            restart_block = "Restart=always\nRestartSec=30\n"

        return (
            "[Unit]\n"
            f"Description=archiver-suite {spec.tag}\n"
            "After=network.target\n"
            "\n"
            "[Service]\n"
            f"ExecStart={args_str}\n"
            f"{restart_block}"
            f"StandardOutput=append:{out}\n"
            f"StandardError=append:{err}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _timer_unit(spec: JobSpec) -> str:
        hour, minute = spec.calendar or (4, 5)
        return (
            "[Unit]\n"
            f"Description=archiver-suite calendar {spec.tag}\n"
            "\n"
            "[Timer]\n"
            f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\n"
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )

    def install(spec: JobSpec) -> None:
        _SYSTEMD_USER.mkdir(parents=True, exist_ok=True)
        log_dir().mkdir(parents=True, exist_ok=True)
        if spec.kind == "calendar":
            _service_path(spec.label).write_text(_service_unit(spec))
            _timer_path(spec.label).write_text(_timer_unit(spec))
        else:
            _service_path(spec.label).write_text(_service_unit(spec))
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    def uninstall(label: str) -> None:
        s = _service_path(label)
        t = _timer_path(label)
        if s.exists():
            s.unlink()
        if t.exists():
            t.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)

    def load(label: str) -> "tuple[bool, str]":
        unit = f"{label}.timer" if _timer_path(label).exists() else f"{label}.service"
        r = subprocess.run(["systemctl", "--user", "enable", "--now", unit],
                           capture_output=True, text=True)
        return (r.returncode == 0, "loaded" if r.returncode == 0 else r.stderr.strip())

    def unload(label: str) -> "tuple[bool, str]":
        unit = f"{label}.timer" if _timer_path(label).exists() else f"{label}.service"
        r = subprocess.run(["systemctl", "--user", "disable", "--now", unit],
                           capture_output=True, text=True)
        return (r.returncode == 0, "unloaded" if r.returncode == 0 else r.stderr.strip())

    def restart(label: str) -> "tuple[bool, str]":
        r = subprocess.run(["systemctl", "--user", "restart", f"{label}.service"],
                           capture_output=True, text=True)
        return (r.returncode == 0, "restarted" if r.returncode == 0 else r.stderr.strip())

    def definition_exists(label: str) -> bool:
        return _service_path(label).exists()

    def running_pid(label: str) -> "int | None":
        r = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", f"{label}.service"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        line = r.stdout.strip()
        if line.startswith("MainPID="):
            pid_str = line.split("=", 1)[1]
            if pid_str != "0" and pid_str.isdigit():
                return int(pid_str)
        return None

    def job_state(label: str) -> "str | None":
        if not definition_exists(label):
            return None
        unit = f"{label}.timer" if _timer_path(label).exists() else f"{label}.service"
        r = subprocess.run(["systemctl", "--user", "is-active", unit],
                           capture_output=True, text=True)
        if r.stdout.strip() == "active":
            return "running"
        r2 = subprocess.run(["systemctl", "--user", "is-enabled", unit],
                            capture_output=True, text=True)
        if r2.stdout.strip() == "enabled":
            return "enabled"
        return "disabled"
