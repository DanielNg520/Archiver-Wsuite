"""
core.platform.process
──────────────────────
Portable process-liveness probe.

``os.kill(pid, 0)`` is the POSIX idiom for "does this process exist?" — but on
Windows ``os.kill`` does NOT support signal 0: any signal other than
CTRL_C_EVENT / CTRL_BREAK_EVENT is routed to TerminateProcess, so
``os.kill(pid, 0)`` would *kill* the process it was meant to merely check. This
adapter gives one safe answer on both platforms.

  pid_alive(pid) -> bool

POSIX keeps the exact signal-0 semantics the suite relied on (ProcessLookupError
⇒ dead, PermissionError ⇒ alive-but-another-user). Windows uses
``OpenProcess`` via ctypes (no pywin32 dependency): a handle back ⇒ alive;
ERROR_INVALID_PARAMETER ⇒ no such pid ⇒ dead; access-denied ⇒ the process
exists ⇒ alive.

Also here (used by ops health, both OS-specific):

  proc_stats(pid) -> str | None            # "up 1:10:15, cpu 10.6%, mem 110MB"
  find_worker_pid(command, action) -> int  # locate a worker by its argv
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

if os.name == "nt":                                   # ── Windows backend ──
    import ctypes
    from ctypes import wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _ERROR_INVALID_PARAMETER = 87
    _ERROR_ACCESS_DENIED = 5
    _STILL_ACTIVE = 259

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def pid_alive(pid: int) -> bool:
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            return False
        if pid <= 0:
            return False
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            err = ctypes.get_last_error()
            if err == _ERROR_ACCESS_DENIED:
                return True          # exists, we just can't open it
            return False             # ERROR_INVALID_PARAMETER etc. ⇒ gone
        try:
            # Distinguish a live process from a not-yet-reaped exited one.
            code = wintypes.DWORD()
            if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == _STILL_ACTIVE
            return True
        finally:
            _kernel32.CloseHandle(handle)

else:                                                 # ── POSIX backend ──

    def pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True              # exists, owned by another user
        except (OSError, ValueError, TypeError):
            return False
        return True


# ── process inspection (ops health) ────────────────────────────────────────

if os.name == "nt":                                   # ── Windows backend ──

    import csv
    import io
    import json

    def proc_stats(pid: int) -> "str | None":
        # tasklist gives image + mem; CPU% and uptime aren't cheaply available
        # from a single call, so report memory (the field ops most cares about).
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        line = out.stdout.strip().splitlines()
        if out.returncode != 0 or not line or "No tasks" in out.stdout:
            return None
        try:
            fields = next(csv.reader([line[0]]))
            mem = fields[4].replace("\xa0", " ")     # "110,240 K"
        except (StopIteration, IndexError):
            return None
        return f"mem {mem}"

    def _cmdline_matches(cmdline: str, command: str, action: str) -> bool:
        """Does this process command line run `<command> <action>`? Token-wise
        match on the executable basename (with or without .exe) followed by the
        action — the same discipline the POSIX branch applies via shlex, so a
        shell snippet merely *mentioning* the worker can't false-positive."""
        try:
            argv = next(csv.reader(io.StringIO(cmdline), delimiter=" ",
                                   skipinitialspace=True))
        except (StopIteration, csv.Error):
            argv = cmdline.split()
        for i, tok in enumerate(argv[:-1]):
            base = tok.strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base in (command.lower(), f"{command.lower()}.exe") \
                    and argv[i + 1].strip('"') == action:
                return True
        return False

    def _pids_via_wmic(command: str, action: str) -> "int | None":
        out = subprocess.run(
            ["wmic", "process", "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
            capture_output=True, text=True, timeout=6,
        )
        if out.returncode != 0:
            return None
        for row in out.stdout.splitlines():
            if command not in row or action not in row:
                continue
            # CSV rows are Node,CommandLine,ProcessId
            parts = row.rsplit(",", 1)
            if len(parts) != 2 or not parts[1].strip().isdigit():
                continue
            if _cmdline_matches(parts[0], command, action):
                return int(parts[1].strip())
        return None

    def _pids_via_powershell(command: str, action: str) -> "int | None":
        # wmic was removed from Windows 11 24H2+; CIM via PowerShell is the
        # supported replacement and ships on every Windows this suite targets.
        script = ("Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        try:
            rows = json.loads(out.stdout)
        except ValueError:
            return None
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            cmdline = row.get("CommandLine") or ""
            if command in cmdline and action in cmdline \
                    and _cmdline_matches(cmdline, command, action):
                try:
                    return int(row["ProcessId"])
                except (KeyError, TypeError, ValueError):
                    continue
        return None

    def find_worker_pid(command: str, action: str) -> "int | None":
        # Self-healing probe order: wmic (fast, but removed on Win11 24H2+) →
        # PowerShell CIM (always present). Each layer degrades to the next on
        # any failure so health reporting never hard-fails on tooling drift.
        try:
            pid = _pids_via_wmic(command, action)
            if pid is not None:
                return pid
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            return _pids_via_powershell(command, action)
        except (OSError, subprocess.TimeoutExpired):
            return None

else:                                                 # ── POSIX backend ──

    import shlex

    def proc_stats(pid: int) -> "str | None":
        try:
            out = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "etime=,%cpu=,rss="],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        fields = out.stdout.split()
        if out.returncode != 0 or len(fields) != 3:
            return None
        etime, pcpu, rss_kb = fields
        try:
            mem_mb = int(rss_kb) // 1024
        except ValueError:
            return None
        return f"up {etime}, cpu {pcpu}%, mem {mem_mb}MB"

    def find_worker_pid(command: str, action: str) -> "int | None":
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            fields = line.strip().split(maxsplit=1)
            if len(fields) != 2:
                continue
            try:
                pid = int(fields[0])
                argv = shlex.split(fields[1])
            except (ValueError, IndexError):
                continue
            for index, token in enumerate(argv[:-1]):
                if Path(token).name == command and argv[index + 1] == action:
                    return pid
        return None
