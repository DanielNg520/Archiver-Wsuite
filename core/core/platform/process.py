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
    import time as _time

    # ── stats via ctypes (no subprocess) ──
    # The old implementation shelled out to `tasklist` per call — at ops
    # watch's refresh cadence that is a process spawn per worker per frame,
    # forever, on a box meant to run 24/7. kernel32 answers the same questions
    # in-process AND better: uptime + cumulative CPU from GetProcessTimes and
    # working-set from K32GetProcessMemoryInfo (both fine under the
    # QUERY_LIMITED_INFORMATION right we already use for pid_alive).

    class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    _EPOCH_AS_FILETIME = 116444736000000000     # 1970-01-01 in 100ns-since-1601

    def _ft_to_100ns(ft: wintypes.FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    def _fmt_span(seconds: float) -> str:
        s = int(max(0, seconds))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{sec:02d}s"
        return f"{sec}s"

    def proc_stats(pid: int) -> "str | None":
        try:
            pid = int(pid)
        except (ValueError, TypeError):
            return None
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            creation, exit_, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not _kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(exit_),
                    ctypes.byref(kernel), ctypes.byref(user)):
                return None
            started = (_ft_to_100ns(creation) - _EPOCH_AS_FILETIME) / 1e7
            up = _fmt_span(_time.time() - started)
            cpu = _fmt_span((_ft_to_100ns(kernel) + _ft_to_100ns(user)) / 1e7)
            pmc = _PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            mem = ""
            if _kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                mem = f", mem {pmc.WorkingSetSize // (1024 * 1024)}MB"
            return f"up {up}, cpu {cpu}{mem}"
        finally:
            _kernel32.CloseHandle(handle)

    def _working_set(pid: int) -> int:
        """Working-set bytes for a pid (0 when unreadable). Discovery uses it
        to tell a real worker interpreter from its venv-redirector twin."""
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return 0
        try:
            pmc = _PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            if _kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize)
            return 0
        finally:
            _kernel32.CloseHandle(handle)

    def _image_basename(pid: int) -> "str | None":
        """Executable basename (lowercase) for a pid, or None. Used to verify a
        cached worker pid hasn't been recycled for an unrelated process."""
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if not _kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)):
                return None
            return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            _kernel32.CloseHandle(handle)

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

    # ── one process-table snapshot, shared and short-lived ──
    # Enumerating command lines needs wmic (removed on Win11 24H2+) or a
    # PowerShell CIM call (~1s of startup EACH). ops health probes THREE
    # workers per render and ops watch renders continuously, so per-worker
    # spawns are the dominant cost of monitoring. Three layers keep it cheap:
    #   1. a per-(command,action) pid memo, revalidated with two ctypes calls
    #      (alive + image name) — the steady-state path, no subprocess at all;
    #   2. a whole-table snapshot with a short TTL, so one spawn serves all
    #      workers of a render burst (discovery / a worker died);
    #   3. wmic tried once, then remembered as missing — on 24H2+ it would
    #      otherwise add a failed spawn to every discovery.
    _SNAP_TTL_S = 3.0
    _snap: "tuple[float, list[tuple[int, str]]] | None" = None
    _wmic_missing = False
    _pid_memo: "dict[tuple[str, str], int]" = {}

    def _snapshot_wmic() -> "list[tuple[int, str]] | None":
        global _wmic_missing
        if _wmic_missing:
            return None
        try:
            # errors="replace": a stray non-UTF8 byte in ANY process's command
            # line must mangle that line only, not kill the whole snapshot
            # (text=True alone raises UnicodeDecodeError in the reader thread).
            out = subprocess.run(
                ["wmic", "process", "get", "ProcessId,CommandLine",
                 "/FORMAT:CSV"],
                capture_output=True, text=True, errors="replace", timeout=6,
            )
        except FileNotFoundError:
            _wmic_missing = True                 # removed on Win11 24H2+
            return None
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0:
            _wmic_missing = True                 # present but broken: same cure
            return None
        rows: "list[tuple[int, str]]" = []
        for row in out.stdout.splitlines():
            parts = row.rsplit(",", 1)           # Node,CommandLine,ProcessId
            if len(parts) == 2 and parts[1].strip().isdigit():
                rows.append((int(parts[1].strip()), parts[0]))
        return rows

    def _snapshot_powershell() -> "list[tuple[int, str]] | None":
        # [Console]::OutputEncoding pins the pipe to UTF-8 — PowerShell 5.1
        # otherwise emits the OEM codepage, and one process with a non-ASCII
        # command line (any localized app) corrupts the decode. errors=
        # "replace" backstops even that: a mangled foreign cmdline is fine,
        # a crashed monitor is not.
        script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  "Get-CimInstance Win32_Process | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 script],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if out.returncode != 0 or not out.stdout.strip():
            return None
        try:
            data = json.loads(out.stdout)
        except ValueError:
            return None
        if isinstance(data, dict):
            data = [data]
        rows: "list[tuple[int, str]]" = []
        for row in data:
            try:
                rows.append((int(row["ProcessId"]), row.get("CommandLine") or ""))
            except (KeyError, TypeError, ValueError):
                continue
        return rows

    def _process_table() -> "list[tuple[int, str]]":
        global _snap
        now = _time.time()
        if _snap is not None and now - _snap[0] < _SNAP_TTL_S:
            return _snap[1]
        rows = _snapshot_wmic()
        if rows is None:
            rows = _snapshot_powershell() or []
        _snap = (now, rows)
        return rows

    def find_worker_pid(command: str, action: str) -> "int | None":
        # Steady state: revalidate the memoized pid with two ctypes calls. The
        # image-name check guards against Windows recycling the pid for an
        # unrelated process; a worker is either its venv interpreter or a
        # launcher shim named after the command.
        key = (command, action)
        cached = _pid_memo.get(key)
        if cached is not None:
            image = _image_basename(cached)
            if image is not None and pid_alive(cached) and (
                    image.startswith("python")
                    or image in (command.lower(), f"{command.lower()}.exe")):
                return cached
            _pid_memo.pop(key, None)

        # Discovery: one shared snapshot. Several processes can match
        # `<command> <action>`: the console-script launcher shim
        # (`dispatcher.exe start`), the venv python.exe REDIRECTOR (on Windows
        # it relays to the base interpreter as a child with an IDENTICAL
        # command line), and the real interpreter doing the work. Stats read
        # off a relay describe a ~1 MB stub, so prefer python-image candidates
        # and, among those indistinguishable twins, the largest working set —
        # a loaded worker dwarfs its redirector.
        python_best: "tuple[int, int] | None" = None      # (working_set, pid)
        shim = None
        for pid, cmdline in _process_table():
            if command not in cmdline or action not in cmdline:
                continue
            if not _cmdline_matches(cmdline, command, action):
                continue
            head = cmdline.split(None, 1)[0].strip('"').lower() if cmdline else ""
            if "python" in head.rsplit("\\", 1)[-1]:
                ws = _working_set(pid)
                if python_best is None or ws > python_best[0]:
                    python_best = (ws, pid)
            elif shim is None:
                shim = pid
        winner = python_best[1] if python_best is not None else shim
        if winner is not None:
            _pid_memo[key] = winner
        return winner

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
