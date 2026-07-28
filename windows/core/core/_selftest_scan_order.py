"""
Selftest for core.scan_order.order_users — staleness-first walk ordering.
Run: PYTHONPATH=core python core/core/_selftest_scan_order.py
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.scan_order import (   # noqa: E402
    order_users, due_for_reinjection, auto_priority,
)

OK = "✓"


def det_rng():
    return random.Random(1234)


def test_never_scanned_first():
    users = ["a", "b", "c"]
    # b scanned recently, a long ago, c never (absent).
    last = {"a": 1000.0, "b": 9_000_000.0}
    order = order_users(users, last, jitter_seconds=0, rng=det_rng())
    assert order[0] == "c", f"never-scanned must lead: {order}"
    assert order.index("a") < order.index("b"), f"older before newer: {order}"
    print(f"  {OK} never-scanned + stale lead, fresh trails")


def test_pure_staleness_no_jitter():
    users = ["x", "y", "z"]
    last = {"x": 300.0, "y": 100.0, "z": 200.0}
    order = order_users(users, last, jitter_seconds=0, rng=det_rng())
    assert order == ("y", "z", "x"), order
    print(f"  {OK} jitter=0 → strict oldest-first")


def test_jitter_shuffles_similar():
    # 40 users all scanned at the SAME time → jitter should produce a
    # non-alphabetical, varying order across calls.
    users = [f"u{i:02d}" for i in range(40)]
    last = {u: 1000.0 for u in users}
    o1 = order_users(users, last, jitter_seconds=3600, rng=random.Random(1))
    o2 = order_users(users, last, jitter_seconds=3600, rng=random.Random(2))
    assert o1 != tuple(sorted(users)), "jitter should not equal alphabetical"
    assert o1 != o2, "different rng seeds should give different orders"
    assert set(o1) == set(users) and len(o1) == len(users), "no user lost"
    print(f"  {OK} jitter shuffles equal-staleness users, no dropouts")


def test_stale_beats_jitter_window():
    # A user 2 days stale must beat fresh users even with a 1h jitter window.
    users = ["fresh1", "fresh2", "stale"]
    last = {"fresh1": 1_000_000.0, "fresh2": 1_000_000.0,
            "stale": 1_000_000.0 - 172_800.0}
    for seed in range(20):
        order = order_users(users, last, jitter_seconds=3600,
                            rng=random.Random(seed))
        assert order[0] == "stale", f"seed {seed}: {order}"
    print(f"  {OK} staleness beyond jitter window always leads")


def test_priority_forced_first():
    users = ["a", "b", "c", "d"]
    last = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}  # a is stalest
    order = order_users(users, last, jitter_seconds=0,
                        priority=["d", "c"], rng=det_rng())
    assert set(order[:2]) == {"c", "d"}, f"priority must lead: {order}"
    assert set(order[2:]) == {"a", "b"}, f"rest follows: {order}"
    print(f"  {OK} priority users forced to front")


def test_dedup_and_unknown_priority():
    users = ["a", "b"]
    # "zzz" not in pool → dropped; "a" in both priority and pool → once.
    order = order_users(users, {"a": 1.0, "b": 2.0}, jitter_seconds=0,
                        priority=["a", "zzz"], rng=det_rng())
    assert order == ("a", "b"), order
    print(f"  {OK} unknown priority dropped, no duplicates")


def test_empty_and_single():
    assert order_users([], {}) == ()
    assert order_users(["solo"], {}) == ("solo",)
    print(f"  {OK} empty / single-user degenerate cases")


def test_reinjection_min_of():
    # count arm fires
    assert due_for_reinjection(8, 10.0, every_n_users=8, every_seconds=1800)
    assert not due_for_reinjection(7, 10.0, every_n_users=8, every_seconds=1800)
    # time arm fires even when count hasn't
    assert due_for_reinjection(1, 1801.0, every_n_users=8, every_seconds=1800)
    # min-of: whichever first
    assert due_for_reinjection(8, 1.0, every_n_users=8, every_seconds=1800)
    # disabling arms
    assert not due_for_reinjection(100, 100.0, every_n_users=0, every_seconds=0)
    assert due_for_reinjection(100, 1.0, every_n_users=5, every_seconds=0)  # count only
    assert due_for_reinjection(1, 100.0, every_n_users=0, every_seconds=50)  # time only
    print(f"  {OK} due_for_reinjection min-of trigger + arm disabling")


def test_auto_priority():
    days = {"regular": 12, "sometimes": 8, "rare": 2, "never": 0}
    picked = auto_priority(days, min_active_days=8)
    # ordered most-active-first
    assert picked == ("regular", "sometimes"), picked
    # threshold boundary is inclusive
    assert "sometimes" in auto_priority(days, min_active_days=8)
    assert "sometimes" not in auto_priority(days, min_active_days=9)
    # disabled
    assert auto_priority(days, min_active_days=0) == ()
    # cap keeps the most active, in rank order
    many = {f"u{i:02d}": i for i in range(1, 21)}   # activity 1..20
    capped = auto_priority(many, min_active_days=1, limit=3)
    assert capped == ("u20", "u19", "u18"), capped
    print(f"  {OK} auto_priority ranking, boundary, disable, cap")


def main():
    print("core.scan_order selftest")
    test_never_scanned_first()
    test_pure_staleness_no_jitter()
    test_jitter_shuffles_similar()
    test_stale_beats_jitter_window()
    test_priority_forced_first()
    test_dedup_and_unknown_priority()
    test_empty_and_single()
    test_reinjection_min_of()
    test_auto_priority()
    print(f"{OK} all scan_order tests passed")


if __name__ == "__main__":
    main()
