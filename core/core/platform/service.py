"""
core.platform.service
──────────────────────
The OS service-manager seam: install / start / stop / restart / status for the
suite's long-running workers, plus the daily logrotate calendar job.

  POSIX    → launchd LaunchAgents (~/Library/LaunchAgents/<label>.plist,
             launchctl load/unload/kickstart/list). Per-user, RunAtLoad +
             KeepAlive, capture stdout/err to ~/.local/log.
  Windows  → Task Scheduler tasks (schtasks /Create /XML, /Run, /End, /Change,
             /Query). Per-user "at log on" trigger + restart-on-failure, the
             direct analog of a launchd LaunchAgent. stdout/err captured by
             wrapping the command in `cmd /c "... >> out 2>> err"`.

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
keep-alive worker (RunAtLoad/KeepAlive ↔ LogonTrigger + RestartOnFailure);
`kind="calendar"` is a once-daily job (StartCalendarInterval ↔ CalendarTrigger).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths as _paths


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


if os.name == "nt":                                   # ── Windows / Task Scheduler ──

    import csv as _csv
    import getpass
    import tempfile
    from xml.sax.saxutils import escape as _xesc

    def log_dir() -> Path:
        return _paths.config_dir(_paths.SUITE) / "logs"

    def _run(cmd: list[str]) -> "subprocess.CompletedProcess":
        # errors="replace": schtasks output is localized text in the OEM
        # codepage; a non-UTF8 byte must not raise mid-read under PYTHONUTF8=1.
        return subprocess.run(cmd, capture_output=True, text=True,
                              errors="replace")

    def _task_xml(spec: JobSpec) -> str:
        out, err = _log_paths(spec.tag)
        # Task Scheduler can't redirect stdout itself — wrap in cmd.exe.
        inner = " ".join([f'"{spec.program}"', *spec.args])
        command = "cmd.exe"
        arguments = f'/c "{inner} >> "{out}" 2>> "{err}""'
        user = getpass.getuser()

        if spec.kind == "calendar":
            hour, minute = spec.calendar or (4, 5)
            trigger = (
                "    <CalendarTrigger>\n"
                f"      <StartBoundary>2020-01-01T{hour:02d}:{minute:02d}:00</StartBoundary>\n"
                "      <Enabled>true</Enabled>\n"
                "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
                "    </CalendarTrigger>"
            )
            restart = ""
            exec_limit = "PT1H"
        else:
            trigger = (
                "    <LogonTrigger>\n"
                f"      <UserId>{_xesc(user)}</UserId>\n"
                "      <Enabled>true</Enabled>\n"
                "    </LogonTrigger>"
            )
            # KeepAlive analog: restart on failure, effectively forever.
            # Interval is PT1M, not the launchd ThrottleInterval's 30s: Task
            # Scheduler's RestartOnFailure/Interval has a hard 1-minute minimum
            # (PT30S is rejected as "out of range" and the whole task fails to
            # register). One minute is the closest legal restart cadence.
            restart = (
                "    <RestartOnFailure>\n"
                "      <Interval>PT1M</Interval>\n"
                "      <Count>999</Count>\n"
                "    </RestartOnFailure>"
            )
            exec_limit = "PT0S"   # unlimited — a daemon runs indefinitely

        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            "  <RegistrationInfo><Description>archiver-suite "
            f"{_xesc(spec.tag)}</Description></RegistrationInfo>\n"
            "  <Triggers>\n"
            f"{trigger}\n"
            "  </Triggers>\n"
            "  <Principals><Principal id=\"Author\">\n"
            f"    <UserId>{_xesc(user)}</UserId>\n"
            "    <LogonType>InteractiveToken</LogonType>\n"
            "    <RunLevel>LeastPrivilege</RunLevel>\n"
            "  </Principal></Principals>\n"
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <AllowHardTerminate>true</AllowHardTerminate>\n"
            "    <Enabled>true</Enabled>\n"
            f"{restart}\n"
            f"    <ExecutionTimeLimit>{exec_limit}</ExecutionTimeLimit>\n"
            "  </Settings>\n"
            "  <Actions Context=\"Author\">\n"
            "    <Exec>\n"
            f"      <Command>{command}</Command>\n"
            f"      <Arguments>{_xesc(arguments)}</Arguments>\n"
            "    </Exec>\n"
            "  </Actions>\n"
            "</Task>\n"
        )

    def install(spec: JobSpec) -> None:
        log_dir().mkdir(parents=True, exist_ok=True)
        # schtasks /Create wants a UTF-16 XML file.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".xml", delete=False, encoding="utf-16"
        ) as fh:
            fh.write(_task_xml(spec))
            xml_path = fh.name
        try:
            r = _run(["schtasks", "/Create", "/TN", spec.label,
                      "/XML", xml_path, "/F"])
        finally:
            try:
                os.unlink(xml_path)
            except OSError:
                pass
        # schtasks reports XML/schema problems on a non-zero exit; without this
        # a rejected task (e.g. an out-of-range setting) would masquerade as a
        # successful install and only surface later as "not installed" at load.
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip() or "unknown schtasks error"
            raise RuntimeError(f"schtasks /Create failed for {spec.label}: {msg}")

    def uninstall(label: str) -> None:
        _run(["schtasks", "/Delete", "/TN", label, "/F"])

    def load(label: str) -> "tuple[bool, str]":
        _run(["schtasks", "/Change", "/TN", label, "/ENABLE"])
        r = _run(["schtasks", "/Run", "/TN", label])
        return (r.returncode == 0,
                "loaded" if r.returncode == 0 else r.stderr.strip() or r.stdout.strip())

    def unload(label: str) -> "tuple[bool, str]":
        _run(["schtasks", "/End", "/TN", label])
        r = _run(["schtasks", "/Change", "/TN", label, "/DISABLE"])
        return (r.returncode == 0,
                "unloaded" if r.returncode == 0 else r.stderr.strip() or r.stdout.strip())

    def restart(label: str) -> "tuple[bool, str]":
        _run(["schtasks", "/End", "/TN", label])
        r = _run(["schtasks", "/Run", "/TN", label])
        return (r.returncode == 0,
                "restarted" if r.returncode == 0 else r.stderr.strip() or r.stdout.strip())

    def definition_exists(label: str) -> bool:
        return _run(["schtasks", "/Query", "/TN", label]).returncode == 0

    def running_pid(label: str) -> "int | None":
        # Task Scheduler doesn't expose the action's PID via schtasks; health
        # falls back to the process-table probe (core.platform.process). Return
        # None to signal "no managed pid available on this OS".
        return None

    def job_state(label: str) -> "str | None":
        """'running' | 'enabled' (registered, not currently running) |
        'disabled' | None (not installed). Lets the monitor distinguish a
        service-managed worker (crash-restart protection active) from one
        started by hand, and surface a task someone disabled and forgot.

        Status text comes from `schtasks /FO CSV` column 3 and is LOCALIZED;
        this parse targets the en-US strings (the deployment target). An
        unrecognized status degrades to 'enabled' — the definition exists —
        rather than to a false alarm."""
        r = _run(["schtasks", "/Query", "/TN", label, "/FO", "CSV", "/NH"])
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            try:
                fields = next(_csv.reader([line]))
            except (StopIteration, _csv.Error):
                continue
            if len(fields) < 3:
                continue
            status = fields[2].strip().lower()
            if status == "running":
                return "running"
            if status == "disabled":
                return "disabled"
            return "enabled"                      # "Ready", or a localized string
        return None

else:                                                 # ── POSIX / launchd ──

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
        """'running' | 'enabled' | 'disabled' | None — the Windows twin's
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
