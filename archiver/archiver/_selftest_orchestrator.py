"""
Focused validation for three uncovered Archiver paths: disk-full purge with
safebrake, auth recovery and the circuit breaker, and user-set reconciliation.

Run: python archiver/archiver/_selftest_orchestrator.py

Standalone (no pytest). Real PolicyStore and ItemStore on temp dirs, real
folder tree under a temp output_dir. Orchestrator methods are exercised on an
Archiver built with a SimpleNamespace config — no worker machinery, no CLI.
"""
import sys
import asyncio
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo / "core"))
sys.path.insert(0, str(_repo / "archiver"))

from core import ItemStore, PolicyStore, ProtectionPolicy   # noqa: E402
import core.quarantine as q                                  # noqa: E402
from archiver.orchestrator import Archiver                   # noqa: E402
from archiver.platforms import AuthError, AccountGoneError   # noqa: E402

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


class FakePlatform:
    def __init__(self, name: str, users: tuple[str, ...],
                 script: list, recover_result: bool = True):
        self.name = name
        self.users = users
        self._script = list(script)
        self.recover_result = recover_result
        self.download_calls = 0
        self.recovery_calls = 0

    def download(self, username: str, db: ItemStore):
        self.download_calls += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def attempt_recovery(self) -> bool:
        self.recovery_calls += 1
        return self.recover_result


def _write_media(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = b"m" * 300
    path.write_bytes(data)
    return len(data)


def _add_row(db: ItemStore, platform: str, username: str,
             identifier: str, path: Path) -> None:
    db.add_item(source="archiver", platform=platform, username=username,
                identifier=identifier, file_path=str(path))


def _mark_sent(db: ItemStore, identifier: str) -> None:
    db.conn.execute("UPDATE items SET status='sent' WHERE identifier=?",
                    (identifier,))
    db.conn.commit()


def _tmp_archiver(tmp: Path) -> tuple[types.SimpleNamespace, Archiver,
                                      ItemStore, PolicyStore]:
    out = tmp / "archive"
    (out / "x").mkdir(parents=True)
    store = PolicyStore(tmp / "config.toml")
    config = types.SimpleNamespace(policy_store=store, output_dir=str(out),
                                   auth_failure_threshold=3)
    db = ItemStore.open(str(tmp / "suite.db"))
    archiver = Archiver(config, db)
    return config, archiver, db, store


def _run(coro):
    return asyncio.run(coro)


def main() -> int:
    # ── A. Disk-full purge and the ENOSPC branch ───────────────────────────
    tmp_a = Path(tempfile.mkdtemp())
    config_a, arch_a, db_a, store_a = _tmp_archiver(tmp_a)
    q.recorder_lock.live_recording_user = lambda: (False, None)

    out_a = Path(config_a.output_dir)
    f_alice_sent = out_a / "x" / "alice" / "sent.mp4"
    f_alice_pend = out_a / "x" / "alice" / "pending.mp4"
    f_bob_sent = out_a / "x" / "bob" / "sent.mp4"
    size_alice_sent = _write_media(f_alice_sent)
    _write_media(f_alice_pend)
    size_bob_sent = _write_media(f_bob_sent)
    _add_row(db_a, "x", "alice", "a-sent", f_alice_sent)
    _add_row(db_a, "x", "alice", "a-pending", f_alice_pend)
    _add_row(db_a, "x", "bob", "b-sent", f_bob_sent)
    _mark_sent(db_a, "a-sent")
    _mark_sent(db_a, "b-sent")

    freed = arch_a._purge_sent_files("x", "alice", db_a)
    check(freed == size_alice_sent,
          "purge returns the size of the removed sent file")
    check(not f_alice_sent.exists(),
          "alice's sent file is removed")
    check(f_alice_pend.exists(),
          "alice's pending file is untouched")
    check(f_bob_sent.exists(),
          "bob's sent file is untouched")

    # ── A.2 Safebrake protects a sent file from disk-full purge ────────────
    tmp_a2 = Path(tempfile.mkdtemp())
    config_a2, arch_a2, db_a2, store_a2 = _tmp_archiver(tmp_a2)
    out_a2 = Path(config_a2.output_dir)
    f_carol_sent = out_a2 / "x" / "carol" / "sent.mp4"
    _write_media(f_carol_sent)
    _add_row(db_a2, "x", "carol", "c-sent", f_carol_sent)
    _mark_sent(db_a2, "c-sent")
    store_a2.set(ProtectionPolicy.KEY, True, platform="x", username="carol")

    freed2 = arch_a2._purge_sent_files("x", "carol", db_a2)
    check(freed2 == 0,
          "purge returns 0 when the safebrake blocks deletion")
    check(f_carol_sent.exists(),
          "protected sent file is kept on disk")

    # ── A.3 ENOSPC recovery ────────────────────────────────────────────────
    tmp_a3 = Path(tempfile.mkdtemp())
    config_a3, arch_a3, db_a3, store_a3 = _tmp_archiver(tmp_a3)
    out_a3 = Path(config_a3.output_dir)
    f_a3_sent = out_a3 / "x" / "alice" / "sent.mp4"
    _write_media(f_a3_sent)
    _add_row(db_a3, "x", "alice", "a3-sent", f_a3_sent)
    _mark_sent(db_a3, "a3-sent")
    plat_a3 = FakePlatform("x", ("alice",),
                           [OSError(28, "No space left on device"), 5])
    res3 = _run(arch_a3._download_with_recovery(plat_a3, "alice", db_a3))
    check(res3 == {"count": 5},
          "ENOSPC retry returns the successful count")
    check(not f_a3_sent.exists(),
          "ENOSPC retry purged the sent file first")
    check(plat_a3.download_calls == 2,
          "download was called twice after one ENOSPC")

    # ── A.4 ENOSPC unresolved ──────────────────────────────────────────────
    tmp_a4 = Path(tempfile.mkdtemp())
    _, arch_a4, db_a4, _ = _tmp_archiver(tmp_a4)
    plat_a4 = FakePlatform("x", ("alice",),
                           [OSError(28, "No space"), OSError(28, "No space")])
    res4 = _run(arch_a4._download_with_recovery(plat_a4, "alice", db_a4))
    check(res4 == {"_error": {"status": "error",
                              "reason": "disk-full-unresolved"}},
          "second ENOSPC reports disk-full-unresolved")

    # ── A.5 Non-ENOSPC OSError propagates ──────────────────────────────────
    tmp_a5 = Path(tempfile.mkdtemp())
    _, arch_a5, db_a5, _ = _tmp_archiver(tmp_a5)
    plat_a5 = FakePlatform("x", ("alice",), [OSError(13, "Permission denied")])
    raised = None
    try:
        _run(arch_a5._download_with_recovery(plat_a5, "alice", db_a5))
    except OSError as exc:
        raised = exc
    check(raised is not None and raised.errno == 13,
          "non-ENOSPC OSError propagates with its errno")

    # ── B. Auth recovery and circuit breaker ───────────────────────────────
    tmp_b1 = Path(tempfile.mkdtemp())
    config_b1, arch_b1, db_b1, _ = _tmp_archiver(tmp_b1)
    plat_b1 = FakePlatform("x", ("alice",), [AuthError("x"), 7],
                           recover_result=True)
    res_b1 = _run(arch_b1._download_with_recovery(plat_b1, "alice", db_b1))
    check(res_b1 == {"count": 7},
          "auth recovery retry returns the successful count")
    check(db_b1.get_circuit("x")["consecutive_fails"] == 1,
          "one auth failure recorded in the circuit row")
    check("x" not in arch_b1._tripped,
          "platform not tripped after a successful recovery")
    check(plat_b1.recovery_calls == 1,
          "attempt_recovery called once")

    # ── B.2 Second auth failure trips the run flag, no second recovery ─────
    tmp_b2 = Path(tempfile.mkdtemp())
    _, arch_b2, db_b2, _ = _tmp_archiver(tmp_b2)
    plat_b2 = FakePlatform("x", ("alice",),
                           [AuthError("a"), AuthError("b")],
                           recover_result=True)
    res_b2 = _run(arch_b2._download_with_recovery(plat_b2, "alice", db_b2))
    check(res_b2["_error"]["status"] == "auth-failed",
          "double auth failure reports auth-failed")
    check("x" in arch_b2._tripped,
          "platform marked tripped for this run")
    check(plat_b2.recovery_calls == 1,
          "recovery not retried on the second consecutive failure")

    # ── B.3 Failed recovery leaves platform untripped ──────────────────────
    tmp_b3 = Path(tempfile.mkdtemp())
    _, arch_b3, db_b3, _ = _tmp_archiver(tmp_b3)
    plat_b3 = FakePlatform("x", ("alice",), [AuthError("a")],
                           recover_result=False)
    res_b3 = _run(arch_b3._download_with_recovery(plat_b3, "alice", db_b3))
    check(res_b3["_error"]["status"] == "auth-failed",
          "failed recovery reports auth-failed")
    check("x" not in arch_b3._tripped,
          "platform not tripped when recovery itself failed")
    check(plat_b3.download_calls == 1,
          "no download retry after failed recovery")

    # ── B.4 Threshold trip via _handle_auth_failure ────────────────────────
    tmp_b4 = Path(tempfile.mkdtemp())
    config_b4, arch_b4, db_b4, _ = _tmp_archiver(tmp_b4)
    db_b4.bump_circuit_fail("x", "prior")
    db_b4.bump_circuit_fail("x", "prior")
    plat_b4 = FakePlatform("x", ("alice",), [AuthError("third")],
                           recover_result=True)
    res_b4 = _run(arch_b4._download_with_recovery(plat_b4, "alice", db_b4))
    circ4 = db_b4.get_circuit("x")
    check(res_b4["_error"]["status"] == "auth-failed",
          "threshold hit reports auth-failed")
    check("x" in arch_b4._tripped,
          "platform tripped after reaching the threshold")
    until4 = datetime.strptime(circ4["tripped_until_utc"],
                               "%Y-%m-%dT%H:%M:%SZ").replace(
                                   tzinfo=timezone.utc)
    delta4 = until4 - datetime.now(timezone.utc)
    check(5.9 * 3600 <= delta4.total_seconds() <= 6.1 * 3600,
          "trip timestamp is ~6 hours from now")
    check(plat_b4.recovery_calls == 0,
          "recovery not attempted when the circuit trips")

    # ── B.5 AccountGoneError bans the user ─────────────────────────────────
    tmp_b5 = Path(tempfile.mkdtemp())
    config_b5, arch_b5, db_b5, store_b5 = _tmp_archiver(tmp_b5)
    q.recorder_lock.live_recording_user = lambda: (False, None)
    plat_b5 = FakePlatform("x", ("alice",), [AccountGoneError("suspended")])
    res_b5 = _run(arch_b5._download_with_recovery(plat_b5, "alice", db_b5))
    check(res_b5 == {"_error": {"status": "banned", "reason": "suspended"}},
          "AccountGoneError reports banned with the reason")
    check("alice" in store_b5.list_banned("x"),
          "gone account lands on the ban roster")

    # ── C. Reconcile user set ──────────────────────────────────────────────
    tmp_c1 = Path(tempfile.mkdtemp())
    config_c1, arch_c1, db_c1, store_c1 = _tmp_archiver(tmp_c1)
    out_c1 = Path(config_c1.output_dir)
    (out_c1 / "x" / "bob").mkdir(parents=True)
    (out_c1 / "x" / "dave").mkdir(parents=True)
    (out_c1 / "x" / "erin").mkdir(parents=True)
    (out_c1 / "x" / ".deleted").mkdir(parents=True)
    (out_c1 / "x" / "loose.mp4").write_bytes(b"n" * 256)
    store_c1.mark_deleting("x", "dave")
    plat_c1 = FakePlatform("x", ("alice", "bob"), [])
    users_c1 = arch_c1._reconcile_users_for_platform(plat_c1, None)
    check(users_c1 == ("alice", "bob", "erin"),
          "roster union disk dirs, minus dot-dirs, plain files, mid-deleting")

    # ── C.2 user_filter bypasses disk discovery ────────────────────────────
    tmp_c2 = Path(tempfile.mkdtemp())
    _, arch_c2, _, _ = _tmp_archiver(tmp_c2)
    plat_c2 = FakePlatform("x", ("alice",), [])
    check(arch_c2._reconcile_users_for_platform(plat_c2, "@zed") == ("zed",),
          "user_filter returns only the requested user")

    # ── C.3 Missing platform directory falls back to roster ────────────────
    tmp_c3 = Path(tempfile.mkdtemp())
    _, arch_c3, _, _ = _tmp_archiver(tmp_c3)
    (Path(arch_c3.config.output_dir) / "x").rmdir()
    plat_c3 = FakePlatform("x", ("erin", "alice"), [])
    check(arch_c3._reconcile_users_for_platform(plat_c3, None)
          == ("alice", "erin"),
          "absent platform dir yields sorted roster only")

    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
