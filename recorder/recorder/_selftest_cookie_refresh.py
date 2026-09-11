"""
Self-test for cookie refresh scheduling in StateMachine._scan_priority_list_once.

Asserts:
  - Never refreshed before -> simulate_human_browsing called (forced), timestamp updated.
  - Last refreshed >48h ago -> simulate_human_browsing called (forced), timestamp updated.
  - Last refreshed <12h ago -> simulate_human_browsing NOT called, timestamp unchanged.
  - Refresh reports failure/skip (returns False) -> timestamp NOT updated.

No Playwright, no network: simulate_human_browsing is stubbed in-process.
Uses a real core.ItemStore-backed SQLite DB to verify metadata persistence.

Run: PYTHONPATH=core:recorder python3 -m recorder._selftest_cookie_refresh
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo / "core"))
sys.path.insert(0, str(_repo / "recorder"))

from core import ItemStore                                     # noqa: E402
from recorder import cookie_refresh                            # noqa: E402
from recorder.config import RecorderConfig                     # noqa: E402
from recorder.state import StateMachine                        # noqa: E402

_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        print(f"✗ FAIL: {label}")
        raise SystemExit(1)
    print(f"✓ {label}")


class ScriptedPlatform:
    name = "tiktok"

    def __init__(self):
        self.is_live_calls = 0

    def is_live(self, username: str) -> bool:
        self.is_live_calls += 1
        return False

    def stream_url(self, username: str) -> str:
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


def _sm(tmp: Path, db_path: Path) -> StateMachine:
    cfg = RecorderConfig(
        poll_interval_s=0.0, db_path=str(db_path), output_dir=str(tmp),
        state_dir=str(tmp), lock_path=str(tmp / "l"),
        tiktok_users=("alice",), tiktok_cookies_file=str(tmp / "cookies.txt"),
        live_confirm_samples=1, live_confirm_interval_s=0.0,
        reconnect_backoff_base_s=0.0, max_zero_byte_reconnects=3)
    return StateMachine(cfg, ScriptedPlatform(), FakeCapture(), lambda *a: None,
                        FakeLock())


class RefreshStub:
    def __init__(self, return_value: bool = True):
        self.calls = 0
        self.last_config = None
        self.return_value = return_value

    def __call__(self, config):
        self.calls += 1
        self.last_config = config
        return self.return_value


@contextmanager
def stub_refresh(return_value: bool = True):
    orig = cookie_refresh.simulate_human_browsing
    stub = RefreshStub(return_value)
    cookie_refresh.simulate_human_browsing = stub
    try:
        yield stub
    finally:
        cookie_refresh.simulate_human_browsing = orig


def test_never_refreshed(tmp: Path) -> None:
    print("\n── never refreshed before: forced call + updates timestamp ──")
    db_path = tmp / "test.db"
    store = ItemStore.open(str(db_path))
    check(store.meta_get("tiktok_last_cookie_refresh") is None,
          "initially no refresh timestamp in db")
    store.close()

    sm = _sm(tmp, db_path)
    with stub_refresh(return_value=True) as stub:
        sm._scan_priority_list_once()

    check(stub.calls == 1, "refresh function called when never refreshed before")

    store = ItemStore.open(str(db_path))
    val = store.meta_get("tiktok_last_cookie_refresh")
    check(bool(val), "metadata timestamp updated to non-empty string")
    parsed = datetime.fromisoformat(val.replace("Z", "+00:00"))
    age_s = (datetime.now(timezone.utc) - parsed).total_seconds()
    check(0 <= age_s < 60, "updated timestamp is current")
    store.close()


def test_last_refreshed_over_48h(tmp: Path) -> None:
    print("\n── last refreshed >48h ago: forced call + updates timestamp ──")
    db_path = tmp / "test.db"
    old_time = datetime.now(timezone.utc) - timedelta(hours=50)
    old_iso = old_time.isoformat()

    store = ItemStore.open(str(db_path))
    store.meta_set("tiktok_last_cookie_refresh", old_iso)
    store.close()

    sm = _sm(tmp, db_path)
    with stub_refresh(return_value=True) as stub:
        sm._scan_priority_list_once()

    check(stub.calls == 1, "refresh function called when >48h old")

    store = ItemStore.open(str(db_path))
    new_val = store.meta_get("tiktok_last_cookie_refresh")
    check(bool(new_val) and new_val != old_iso, "timestamp updated to new value")
    parsed = datetime.fromisoformat(new_val.replace("Z", "+00:00"))
    age_s = (datetime.now(timezone.utc) - parsed).total_seconds()
    check(0 <= age_s < 60, "updated timestamp is current")
    store.close()


def test_last_refreshed_under_12h(tmp: Path) -> None:
    print("\n── last refreshed <12h ago: refresh NOT called, timestamp unchanged ──")
    db_path = tmp / "test.db"
    recent_time = datetime.now(timezone.utc) - timedelta(hours=2)
    recent_iso = recent_time.isoformat()

    store = ItemStore.open(str(db_path))
    store.meta_set("tiktok_last_cookie_refresh", recent_iso)
    store.close()

    sm = _sm(tmp, db_path)
    with stub_refresh(return_value=True) as stub:
        sm._scan_priority_list_once()

    check(stub.calls == 0, "refresh function NOT called when <12h old")

    store = ItemStore.open(str(db_path))
    val = store.meta_get("tiktok_last_cookie_refresh")
    check(val == recent_iso, "metadata timestamp left unchanged")
    store.close()


def test_refresh_reports_not_run(tmp: Path) -> None:
    print("\n── refresh reports False: timestamp NOT updated ──")
    # Subtest 1: Never refreshed before, refresh called but returns False
    db_path_1 = tmp / "test_never_skipped.db"
    sm1 = _sm(tmp, db_path_1)
    with stub_refresh(return_value=False) as stub:
        sm1._scan_priority_list_once()

    check(stub.calls == 1, "refresh function was called")
    store1 = ItemStore.open(str(db_path_1))
    check(store1.meta_get("tiktok_last_cookie_refresh") is None,
          "timestamp not written when refresh returns False (fresh db)")
    store1.close()

    # Subtest 2: Refreshed >48h ago, refresh called but returns False
    db_path_2 = tmp / "test_old_skipped.db"
    old_time = datetime.now(timezone.utc) - timedelta(hours=60)
    old_iso = old_time.isoformat()

    store2 = ItemStore.open(str(db_path_2))
    store2.meta_set("tiktok_last_cookie_refresh", old_iso)
    store2.close()

    sm2 = _sm(tmp, db_path_2)
    with stub_refresh(return_value=False) as stub:
        sm2._scan_priority_list_once()

    check(stub.calls == 1, "refresh function was called for >48h old entry")
    store2 = ItemStore.open(str(db_path_2))
    check(store2.meta_get("tiktok_last_cookie_refresh") == old_iso,
          "prior timestamp preserved unchanged when refresh returns False")
    store2.close()


def main() -> int:
    print("recorder.state cookie-refresh self-test")
    with tempfile.TemporaryDirectory() as d:
        test_never_refreshed(Path(d) / "a")
        test_last_refreshed_over_48h(Path(d) / "b")
        test_last_refreshed_under_12h(Path(d) / "c")
        test_refresh_reports_not_run(Path(d) / "d")
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
