"""
Self-test for dispatcher.cli._drain_with_stop_grace — cooperative drain stop.

Covers the five contract paths: wedged drain reverts once, finish-inside-grace
skips revert, normal return, exception propagation, outer cancellation cleans
up the drain task — and that the grace constant stays under systemd's limit.

Run: PYTHONPATH=core:dispatcher python3 -m dispatcher._selftest_stop_grace
"""

from __future__ import annotations

import asyncio
import sys

from dispatcher.cli import _STOP_GRACE_S, _drain_with_stop_grace

_checks = 0


def ok(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"✗ {label}")
    _checks += 1
    print(f"✓ {label}")


class _FakeStore:
    def __init__(self) -> None:
        self.reset_calls: list[int] = []

    def reset_stuck_sending(self, older_than_minutes: int) -> int:
        self.reset_calls.append(older_than_minutes)
        return 1


async def _wedged() -> None:
    await asyncio.Event().wait()


async def _quick_finish() -> None:
    await asyncio.sleep(0.02)


async def _raise_timeout() -> None:
    raise TimeoutError("boom")


async def _record_cancel(record: list[bool]) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        record.append(True)
        raise


async def _scenario1() -> None:
    store = _FakeStore()
    stop = asyncio.Event()
    stop.set()
    await _drain_with_stop_grace(_wedged(), stop, store, grace_s=0.05)
    ok(store.reset_calls == [0], "wedged drain: revert once with "
       "older_than_minutes=0")


async def _scenario2() -> None:
    store = _FakeStore()
    stop = asyncio.Event()
    task = asyncio.create_task(
        _drain_with_stop_grace(_quick_finish(), stop, store, grace_s=1.0))
    await asyncio.sleep(0.005)
    stop.set()
    await task
    ok(store.reset_calls == [], "finish inside grace: no revert")
    ok(not task.cancelled(), "finish inside grace: helper not cancelled")


async def _scenario3() -> None:
    store = _FakeStore()
    stop = asyncio.Event()
    await _drain_with_stop_grace(_quick_finish(), stop, store, grace_s=1.0)
    ok(store.reset_calls == [], "normal return: no revert")


async def _scenario4() -> None:
    store = _FakeStore()
    stop = asyncio.Event()
    raised = False
    try:
        await _drain_with_stop_grace(
            _raise_timeout(), stop, store, grace_s=1.0)
    except TimeoutError:
        raised = True
    ok(raised, "drain TimeoutError propagates")
    ok(store.reset_calls == [], "drain exception: no revert")


async def _scenario5() -> None:
    store = _FakeStore()
    stop = asyncio.Event()
    record: list[bool] = []
    outer = asyncio.get_running_loop().create_task(
        _drain_with_stop_grace(
            _record_cancel(record), stop, store, grace_s=1.0))
    await asyncio.sleep(0.005)
    outer.cancel()
    try:
        await outer
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.005)
    ok(record == [True], "outer cancel cancels the drain task")
    ok(store.reset_calls == [], "outer cancel: no revert")


async def _run_all() -> None:
    await _scenario1()
    await _scenario2()
    await _scenario3()
    await _scenario4()
    await _scenario5()


def main() -> int:
    print("dispatcher.cli._drain_with_stop_grace self-test\n")
    ok(_STOP_GRACE_S < 45, "_STOP_GRACE_S under systemd's 45s default")
    asyncio.run(_run_all())
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
