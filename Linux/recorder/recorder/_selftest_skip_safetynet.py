"""
Self-test for recorder.state's "bench an unstartable user" safety net.

Asserts:
  - N-1 consecutive failed starts do NOT bench; the N-th does (N = 3).
  - a benched user is skipped by the poll loop (is_live never called).
  - the bench is a COOLDOWN: once it elapses the user is retried automatically.
  - a successful start resets the consecutive-failure count.
  - CookiesRequiredError benches immediately (fail-fast, first failure).

No network, no yt-dlp: a scripted platform whose stream_url raises stands in.

Run: PYTHONPATH=core:recorder python3 -m recorder._selftest_skip_safetynet
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time                                                    # noqa: E402

from recorder.config import RecorderConfig                     # noqa: E402
from recorder.state import (                                   # noqa: E402
    StateMachine, _SKIP_AFTER_FAILS, _SKIP_COOLDOWN_S)
from recorder.platforms.tiktok_browser import CookiesRequiredError  # noqa: E402

_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        print(f"✗ FAIL: {label}")
        raise SystemExit(1)
    print(f"✓ {label}")


class ScriptedPlatform:
    """stream_url raises `exc` for the first `fail_first` calls, then succeeds.
    is_live is scripted separately so we can prove the poll loop skips."""

    name = "tiktok"

    def __init__(self, exc: Exception, fail_first: int):
        self._exc = exc
        self._fail_first = fail_first
        self.stream_url_calls = 0
        self.is_live_calls = 0

    def is_live(self, username: str) -> bool:
        self.is_live_calls += 1
        return True

    def stream_url(self, username: str) -> str:
        self.stream_url_calls += 1
        if self.stream_url_calls <= self._fail_first:
            raise self._exc
        return f"https://fake/{username}.m3u8"


class FakeCapture:
    def __init__(self):
        self.starts = 0

    def start(self, url: str, username: str) -> None:
        self.starts += 1

    def output_files(self):
        return []


class FakeLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sm(tmp: Path, platform) -> StateMachine:
    cfg = RecorderConfig(
        poll_interval_s=0.0, db_path="/x.db", output_dir=str(tmp),
        state_dir=str(tmp), lock_path=str(tmp / "l"),
        tiktok_users=("alice",), tiktok_cookies_file=None,
        live_confirm_samples=1, live_confirm_interval_s=0.0,
        reconnect_backoff_base_s=0.0, max_zero_byte_reconnects=3)
    return StateMachine(cfg, platform, FakeCapture(), lambda *a: None,
                        FakeLock())


def test_threshold(tmp: Path) -> None:
    print("\n── generic failures bench only at the threshold ──")
    plat = ScriptedPlatform(RuntimeError("boom"), fail_first=99)
    sm = _sm(tmp, plat)
    for i in range(1, _SKIP_AFTER_FAILS):
        check(sm._open_capture("alice") is False, f"fail #{i} returns False")
        check("alice" not in sm._skipped,
              f"still active after {i} failure(s) (< {_SKIP_AFTER_FAILS})")
    check(sm._open_capture("alice") is False,
          f"fail #{_SKIP_AFTER_FAILS} returns False")
    check("alice" in sm._skipped,
          f"benched on the {_SKIP_AFTER_FAILS}th consecutive failure")


def test_poll_skips_benched(tmp: Path) -> None:
    print("\n── the poll loop skips a benched user (no is_live call) ──")
    plat = ScriptedPlatform(RuntimeError("boom"), fail_first=99)
    sm = _sm(tmp, plat)
    sm._skipped["alice"] = time.monotonic() + _SKIP_COOLDOWN_S
    sm._poll_for_live()
    check(plat.is_live_calls == 0,
          "is_live never called for a benched user")


def test_cooldown_elapses_and_retries(tmp: Path) -> None:
    print("\n── an elapsed cooldown re-enables the user (auto-retry) ──")
    plat = ScriptedPlatform(RuntimeError("boom"), fail_first=99)
    sm = _sm(tmp, plat)
    # Bench with an already-past deadline: _is_skipped must evict + retry.
    sm._skipped["alice"] = time.monotonic() - 1.0
    sm._consec_fail["alice"] = _SKIP_AFTER_FAILS
    check(sm._is_skipped("alice") is False, "past-deadline bench reads as active")
    check("alice" not in sm._skipped, "elapsed bench evicted from _skipped")
    check("alice" not in sm._consec_fail, "failure count cleared on re-enable")
    sm._poll_for_live()
    check(plat.is_live_calls == 1, "is_live IS called again after cooldown")


def test_success_resets(tmp: Path) -> None:
    print("\n── a successful start resets the consecutive-failure count ──")
    # Fail twice (below threshold=3), succeed once, then fail twice more.
    plat = ScriptedPlatform(RuntimeError("boom"), fail_first=2)
    sm = _sm(tmp, plat)
    check(sm._open_capture("alice") is False, "fail #1")
    check(sm._open_capture("alice") is False, "fail #2")
    check(sm._open_capture("alice") is True, "3rd call succeeds")
    check("alice" not in sm._consec_fail, "counter cleared after success")
    # Now two fresh failures must NOT trip (proves the count reset, not merely
    # accumulated 2+2 across the success).
    plat._fail_first = 99          # make all further calls fail
    check(sm._open_capture("alice") is False, "post-success fail #1")
    check(sm._open_capture("alice") is False, "post-success fail #2")
    check("alice" not in sm._skipped,
          "still active — reset means 2 post-success fails is below threshold")


def test_cookies_required_fail_fast(tmp: Path) -> None:
    print("\n── CookiesRequiredError benches on the FIRST failure ──")
    plat = ScriptedPlatform(CookiesRequiredError("no cookies"), fail_first=99)
    sm = _sm(tmp, plat)
    check(sm._open_capture("alice") is False, "first call returns False")
    check("alice" in sm._skipped,
          "benched immediately — no retries for a deterministic failure")


def main() -> int:
    print("recorder.state skip-safety-net self-test")
    with tempfile.TemporaryDirectory() as d:
        test_threshold(Path(d) / "a")
        test_poll_skips_benched(Path(d) / "b")
        test_cooldown_elapses_and_retries(Path(d) / "c")
        test_success_resets(Path(d) / "d")
        test_cookies_required_fail_fast(Path(d) / "e")
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
