"""
core.platform.signals
──────────────────────
Portable "shut down cleanly" signal wiring.

Two OS differences this hides:

  1. Which signals mean "stop". POSIX delivers SIGTERM (from `kill` / launchd /
     the recorder's own stop command) and SIGINT (Ctrl-C). Windows never
     delivers SIGTERM to a running process — the graceful console signals are
     SIGINT (Ctrl-C) and SIGBREAK (Ctrl-Break, i.e. the CTRL_BREAK_EVENT the
     procgroup adapter and the service manager send).

  2. How an asyncio loop registers them. ``loop.add_signal_handler`` works on
     POSIX but raises NotImplementedError on Windows' event loops, so there the
     handler must go through plain ``signal.signal`` + ``call_soon_threadsafe``.

API:

  install_sync(handler)         # for threaded/sync workers (recorder):
                                #   handler(signum, frame)
  install_async(loop, callback) # for asyncio workers (dispatcher):
                                #   callback(signum), scheduled on the loop
"""

from __future__ import annotations

import os
import signal

if os.name == "nt":
    # SIGBREAK is Windows-only; SIGTERM is registerable but never delivered to a
    # live process, so it's useless as a graceful-stop trigger here.
    _SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGBREAK)
else:
    _SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def shutdown_signals() -> tuple[int, ...]:
    """The signals that mean 'shut down' on this OS."""
    return _SHUTDOWN_SIGNALS


def install_sync(handler) -> None:
    """Register a classic ``handler(signum, frame)`` for every shutdown signal.
    For sync/threaded workers (the recorder)."""
    for sig in _SHUTDOWN_SIGNALS:
        signal.signal(sig, handler)


def install_async(loop, callback) -> None:
    """Register ``callback(signum)`` on an asyncio ``loop`` for every shutdown
    signal, portably. POSIX uses ``loop.add_signal_handler``; on Windows (where
    that raises NotImplementedError) it falls back to ``signal.signal`` +
    ``loop.call_soon_threadsafe`` so the callback still runs on the loop thread."""
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, callback, sig)
        except (NotImplementedError, RuntimeError):
            def _handler(signum, _frame, _cb=callback, _loop=loop):
                _loop.call_soon_threadsafe(_cb, signum)
            signal.signal(sig, _handler)
