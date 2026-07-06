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
"""

from __future__ import annotations

import os

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
