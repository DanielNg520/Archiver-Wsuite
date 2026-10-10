"""
core.platform.signals
──────────────────────
Portable "shut down cleanly" signal wiring.

Two things this hides:

  1. Which signals mean "stop". POSIX delivers SIGTERM (from `kill` / launchd /
     the recorder's own stop command) and SIGINT (Ctrl-C).

  2. How an asyncio loop registers them. ``loop.add_signal_handler`` works on
     POSIX, but off the main thread it raises RuntimeError, so the handler must
     go through plain ``signal.signal`` + ``call_soon_threadsafe``.

API:

  install_sync(handler)         # for threaded/sync workers (recorder):
                                #   handler(signum, frame)
  install_async(loop, callback) # for asyncio workers (dispatcher):
                                #   callback(signum), scheduled on the loop
"""

from __future__ import annotations

import signal

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
    signal, portably. POSIX uses ``loop.add_signal_handler``; when that raises
    RuntimeError (e.g. off the main thread) it falls back to
    ``signal.signal`` + ``loop.call_soon_threadsafe`` so the callback still
    runs on the loop thread."""
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, callback, sig)
        except (NotImplementedError, RuntimeError):
            def _handler(signum, _frame, _cb=callback, _loop=loop):
                _loop.call_soon_threadsafe(_cb, signum)
            signal.signal(sig, _handler)
