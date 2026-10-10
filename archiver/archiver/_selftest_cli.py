"""
Selftest for destructive-command safety paths in cli.py: cmd_purge_sent
(DeletionGuard safebrake + confirmation + dry-run), cmd_reset (all / failed /
user subcommands + non-TTY guard), _validate_chat_id_format, cmd_run,
cmd_stories, cmd_loop, cmd_config, and cmd_migrate.

Run: python archiver/archiver/_selftest_cli.py

Standalone (no pytest). Real ItemStore on a temp suite.db, real PolicyStore on
a temp config.toml; every scenario builds its own temp dir and closes its
store before returning.
"""
import builtins
import io
import logging
import os
import signal
import sys
import tempfile
import time
import types
from pathlib import Path

_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo / "core"))
sys.path.insert(0, str(_repo / "archiver"))

from core import ItemStore, PolicyStore  # noqa: E402
import archiver.cli as cli              # noqa: E402
import archiver.loop_state as loop_state  # noqa: E402

OK = "✓"
_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    _checks += 1
    print(f"{OK} {label}")


def _count(db: ItemStore, **where) -> int:
    sql = "SELECT COUNT(*) FROM items"
    params: list[object] = []
    if where:
        clauses = [f"{k} = ?" for k in where]
        sql += " WHERE " + " AND ".join(clauses)
        params = list(where.values())
    return db.conn.execute(sql, params).fetchone()[0]


def _media(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 256)
    return path


def _make_config(tmp: Path, store: PolicyStore) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        policy_store=store,
        output_dir=str(tmp / "archive"),
    )


def _safebrake(store: PolicyStore) -> None:
    store.set("protect_from_deletion", True, platform="x", username="carol")


# ── A. cmd_purge_sent ────────────────────────────────────────────────────────
def scenario_purge_sent() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    try:
        _safebrake(store)

        alice_file = _media(tmp / "archive" / "x" / "alice" / "sent.bin")
        alice_pending = _media(tmp / "archive" / "x" / "alice" / "pending.bin")
        missing_file = tmp / "archive" / "x" / "alice" / "missing.bin"
        carol_file = _media(tmp / "archive" / "x" / "carol" / "sent.bin")

        db.add_item(source="archiver", platform="x", username="alice",
                    identifier="a-sent", file_path=str(alice_file))
        db.add_item(source="archiver", platform="x", username="alice",
                    identifier="a-pending", file_path=str(alice_pending))
        db.add_item(source="archiver", platform="x", username="alice",
                    identifier="a-missing", file_path=str(missing_file))
        db.add_item(source="archiver", platform="x", username="carol",
                    identifier="c-sent", file_path=str(carol_file))
        db.conn.execute(
            "UPDATE items SET status='sent' WHERE identifier IN ('a-sent','a-missing','c-sent')"
        )
        db.conn.commit()

        def present() -> dict[str, bool]:
            return {
                "alice-sent": alice_file.exists(),
                "alice-pending": alice_pending.exists(),
                "carol-sent": carol_file.exists(),
            }

        base = {
            "platform": None,
            "username": None,
            "source": None,
            "dry_run": False,
            "yes": False,
        }

        args = types.SimpleNamespace(**{**base, "username": "alice"})
        config = _make_config(tmp, store)
        rc = cli.cmd_purge_sent(args, config, db)
        check(rc == 2, "purge-sent: --user without --platform returns 2")
        check(all(present().values()), "purge-sent: validation abort deletes no files")

        args = types.SimpleNamespace(**{**base, "dry_run": True})
        rc = cli.cmd_purge_sent(args, config, db)
        check(rc == 0, "purge-sent: dry-run of everything returns 0")
        check(all(present().values()), "purge-sent: dry-run deletes no files")

        orig_input = builtins.input
        try:
            builtins.input = lambda prompt="": "n"
            args = types.SimpleNamespace(**{**base, "yes": False})
            rc = cli.cmd_purge_sent(args, config, db)
            check(rc == 1, "purge-sent: user answers 'n' returns 1")
            check(all(present().values()), "purge-sent: 'n' answer deletes no files")

            builtins.input = lambda prompt="": (_ for _ in ()).throw(EOFError())
            args = types.SimpleNamespace(**{**base, "yes": False})
            rc = cli.cmd_purge_sent(args, config, db)
            check(rc == 1, "purge-sent: EOF on prompt returns 1")
            check(all(present().values()), "purge-sent: EOF abort deletes no files")
        finally:
            builtins.input = orig_input

        args = types.SimpleNamespace(**{**base, "yes": True})
        rc = cli.cmd_purge_sent(args, config, db)
        check(rc == 0, "purge-sent: --yes run returns 0")
        remaining = present()
        check(not remaining["alice-sent"], "purge-sent: alice sent file deleted")
        check(remaining["alice-pending"], "purge-sent: alice pending file kept")
        check(remaining["carol-sent"], "purge-sent: safebraked carol file kept")
        check(_count(db) == 4, "purge-sent: database rows untouched")

        args = types.SimpleNamespace(**{**base, "yes": True})
        rc = cli.cmd_purge_sent(args, config, db)
        check(rc == 0, "purge-sent: re-run returns 0")
        check(carol_file.exists(), "purge-sent: re-run keeps safebraked file")
    finally:
        db.close()


# ── B. cmd_reset all ─────────────────────────────────────────────────────────
def scenario_reset_all() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    orig_build = cli.build_platforms
    try:
        for user, n in (("alice", 2), ("bob", 1), ("zed", 1)):
            for i in range(n):
                db.add_item(source="archiver", platform="x", username=user,
                            identifier=f"{user}-{i}",
                            file_path=str(tmp / f"{user}-{i}.bin"))

        fake = types.SimpleNamespace(name="x", users=("alice", "bob"))
        cli.build_platforms = lambda config: [fake]

        base = {
            "reset_cmd": "all",
            "platform": None,
            "user": None,
            "yes": False,
        }
        config = _make_config(tmp, store)

        args = types.SimpleNamespace(**base)
        orig_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("")
            rc = cli.cmd_reset(args, config, db)
        finally:
            sys.stdin = orig_stdin
        check(rc == 2, "reset all: non-TTY without --yes returns 2")
        check(_count(db) == 4, "reset all: non-TTY abort leaves all rows")

        args = types.SimpleNamespace(**{**base, "yes": True})
        rc = cli.cmd_reset(args, config, db)
        check(rc == 0, "reset all: --yes returns 0")
        check(_count(db, platform="x", username="alice") == 0, "reset all: alice rows deleted")
        check(_count(db, platform="x", username="bob") == 0, "reset all: bob rows deleted")
        check(_count(db, platform="x", username="zed") == 1, "reset all: unconfigured zed kept")

        cli.build_platforms = lambda config: []
        args = types.SimpleNamespace(**{**base, "yes": True})
        rc = cli.cmd_reset(args, config, db)
        check(rc == 0, "reset all: no configured platforms returns 0")
        check(_count(db, platform="x", username="zed") == 1, "reset all: empty platforms keep zed")
    finally:
        cli.build_platforms = orig_build
        db.close()


# ── C. cmd_reset failed / user ───────────────────────────────────────────────
def scenario_reset_failed_and_user() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    try:
        db.add_item(source="archiver", platform="x", username="alice",
                    identifier="alice-failed", file_path=str(tmp / "a1.bin"))
        db.add_item(source="archiver", platform="x", username="alice",
                    identifier="alice-sent", file_path=str(tmp / "a2.bin"))
        db.add_item(source="archiver", platform="x", username="bob",
                    identifier="bob-failed", file_path=str(tmp / "b1.bin"))
        db.conn.execute(
            "UPDATE items SET status='failed' WHERE identifier IN ('alice-failed','bob-failed')"
        )
        db.conn.execute("UPDATE items SET status='sent' WHERE identifier='alice-sent'")
        db.conn.commit()

        config = _make_config(tmp, store)
        base = {"reset_cmd": None, "platform": None, "user": None}

        args = types.SimpleNamespace(**{**base, "reset_cmd": "failed",
                                        "platform": "x", "user": "@alice"})
        rc = cli.cmd_reset(args, config, db)
        check(rc == 0, "reset failed: @alice returns 0")
        alice_failed = db.conn.execute(
            "SELECT status FROM items WHERE identifier='alice-failed'"
        ).fetchone()[0]
        bob_failed = db.conn.execute(
            "SELECT status FROM items WHERE identifier='bob-failed'"
        ).fetchone()[0]
        alice_sent = db.conn.execute(
            "SELECT status FROM items WHERE identifier='alice-sent'"
        ).fetchone()[0]
        check(alice_failed == "pending", "reset failed: alice failed row requeued")
        check(bob_failed == "failed", "reset failed: bob row untouched")
        check(alice_sent == "sent", "reset failed: alice sent row untouched")

        args = types.SimpleNamespace(**{**base, "reset_cmd": "user",
                                        "platform": "x", "user": "alice"})
        rc = cli.cmd_reset(args, config, db)
        check(rc == 0, "reset user: alice returns 0")
        check(_count(db, platform="x", username="alice") == 0, "reset user: alice rows deleted")
        check(_count(db, platform="x", username="bob") == 1, "reset user: bob row kept")
    finally:
        db.close()


# ── D. _validate_chat_id_format ──────────────────────────────────────────────
def scenario_validate_chat_id() -> None:
    valid = ("-1001234567890", "12345", "@somechannel", "  -100  ")
    for chat in valid:
        check(cli._validate_chat_id_format(chat) is None,
              f"validate chat id: {chat!r} accepted")

    invalid = ("", "   ", "@", "abc", "-100x")
    for chat in invalid:
        err = cli._validate_chat_id_format(chat)
        check(isinstance(err, str) and len(err) > 0,
              f"validate chat id: {chat!r} rejected with message")


# ── E. cmd_run ──────────────────────────────────────────────────────────────
def scenario_cmd_run() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    config = _make_config(tmp, store)
    orig = cli.Archiver

    calls: list = []
    run_kwargs: list = []
    results_preset: list[dict] = []

    class FakeArchiver:
        def __init__(self, config, db):
            calls.append(self)
            self.config = config
            self.db = db
            self.ingest_lock = None

        async def run(self, **kw):
            run_kwargs.append(kw)
            return results_preset.pop(0)

    try:
        cli.Archiver = FakeArchiver

        # 1. full_history=True with no platform and no user
        args = types.SimpleNamespace(platform=None, user=None, full_history=True)
        rc = cli.cmd_run(args, config, db)
        check(rc == 2, "cmd_run: full-history without platform/user returns 2")
        check(len(calls) == 0, "cmd_run: full-history validation abort constructs no Archiver")

        # 2. full_history=True with platform, re-arms before archiving
        orig_build = cli.build_platforms
        cli.build_platforms = lambda config: [types.SimpleNamespace(name="x", users=("alice", "bob"))]
        try:
            db.mark_full_history_done("x", "alice")
            db.mark_full_history_done("x", "bob")
            results_preset.append({"x/alice": {"status": "ok"}})
            args = types.SimpleNamespace(platform="x", user=None, full_history=True)
            rc = cli.cmd_run(args, config, db)
            check(rc == 0, "cmd_run: full-history with platform returns 0")
            check(db.needs_full_history("x", "alice"), "cmd_run: alice full-history re-armed")
            check(db.needs_full_history("x", "bob"), "cmd_run: bob full-history re-armed")
        finally:
            cli.build_platforms = orig_build

        # 3. filtered run with sentinel ingest_lock
        sentinel = object()
        results_preset.append({"a": {"status": "ok"}, "b": {"status": "banned"}})
        args = types.SimpleNamespace(platform="x", user="@alice", full_history=False)
        rc = cli.cmd_run(args, config, db, ingest_lock=sentinel)
        check(rc == 0, "cmd_run: filtered run returns 0")
        check(run_kwargs[-1]["user_filter"] == "alice", "cmd_run: user_filter passed to run")
        check(run_kwargs[-1]["platform_filter"] == "x", "cmd_run: platform_filter passed to run")
        check(calls[-1].ingest_lock is sentinel, "cmd_run: ingest_lock attached to Archiver")

        # 4. partial result → 1
        results_preset.append({"a": {"status": "partial"}})
        args = types.SimpleNamespace(platform=None, user=None, full_history=False)
        rc = cli.cmd_run(args, config, db)
        check(rc == 1, "cmd_run: partial result returns 1")

        # 5. error result → 1
        results_preset.append({"a": {"status": "error", "reason": "boom"}})
        rc = cli.cmd_run(args, config, db)
        check(rc == 1, "cmd_run: error result returns 1")
    finally:
        cli.Archiver = orig
        db.close()


# ── F. cmd_stories ──────────────────────────────────────────────────────────
def scenario_cmd_stories() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    orig = cli.Archiver
    calls: list = []
    run_stories_kwargs: list = []
    results_preset: list[dict] = []

    class FakeArchiver:
        def __init__(self, config, db):
            calls.append(self)

        async def run_stories(self, **kw):
            run_stories_kwargs.append(kw)
            return results_preset.pop(0)

    try:
        cli.Archiver = FakeArchiver

        config = types.SimpleNamespace(policy_store=store,
                                       output_dir=str(tmp / "archive"),
                                       instagram=None)
        args = types.SimpleNamespace(user=None)
        rc = cli.cmd_stories(args, config, db)
        check(rc == 0, "cmd_stories: no instagram config returns 0")
        check(len(calls) == 0, "cmd_stories: no instagram constructs no Archiver")

        config = types.SimpleNamespace(policy_store=store,
                                       output_dir=str(tmp / "archive"),
                                       instagram=types.SimpleNamespace(stories_interval=0))
        results_preset.append({"a": {"status": "ok"}, "b": {"status": "gone"}, "c": {"status": "skipped"}})
        args = types.SimpleNamespace(user="@bob")
        rc = cli.cmd_stories(args, config, db)
        check(rc == 0, "cmd_stories: ok/gone/skipped returns 0")
        check(run_stories_kwargs[-1]["user_filter"] == "bob",
              "cmd_stories: user filter passed to run_stories")

        results_preset.append({"a": {"status": "error"}})
        args = types.SimpleNamespace(user=None)
        rc = cli.cmd_stories(args, config, db)
        check(rc == 1, "cmd_stories: error result returns 1")
    finally:
        cli.Archiver = orig
        db.close()


# ── G. cmd_loop ─────────────────────────────────────────────────────────────
def scenario_cmd_loop() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    recorded_runs: list = []
    fake_sequence: list = []

    def fake_cmd_run(args, config, db, **kw):
        recorded_runs.append((args, kw))
        if not fake_sequence:
            raise KeyboardInterrupt()
        item = fake_sequence.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    orig_cmd_run = cli.cmd_run
    orig_sleep = time.sleep
    orig_write_running = loop_state.write_running
    orig_write_sleeping = loop_state.write_sleeping
    orig_scan_done = loop_state.scan_done
    orig_clear = loop_state.clear
    clear_calls: list = []

    def check_loop_cleanup(label: str) -> None:
        check(signal.getsignal(signal.SIGINT) is before_sigint,
              f"cmd_loop: SIGINT handler restored ({label})")
        check(len(logging.getLogger("archiver.loop").handlers) == before_handlers,
              f"cmd_loop: loop logger handlers restored ({label})")

    before_sigint = signal.getsignal(signal.SIGINT)
    before_handlers = len(logging.getLogger("archiver.loop").handlers)

    try:
        cli.cmd_run = fake_cmd_run
        time.sleep = lambda *a, **k: None
        loop_state.write_running = lambda *a, **k: None
        loop_state.write_sleeping = lambda *a, **k: None
        loop_state.scan_done = lambda *a, **k: None
        loop_state.clear = lambda *a, **k: clear_calls.append(a)

        config = types.SimpleNamespace(log_file=str(tmp / "logs" / "archiver.log"),
                                       db_path=str(tmp / "suite.db"),
                                       instagram=None,
                                       policy_store=store,
                                       output_dir=str(tmp / "archive"))
        base = dict(min_sleep=1, max_sleep=1, max_fails=1, platform=None,
                    user=None, ingest_interval=0)

        # 1. validation failures, no cmd_run called
        for bad in (dict(min_sleep=0), dict(min_sleep=10, max_sleep=5), dict(max_fails=0)):
            args = types.SimpleNamespace(**{**base, **bad})
            recorded_runs.clear()
            fake_sequence[:] = [0]
            rc = cli.cmd_loop(args, config, db)
            check(rc == 2, f"cmd_loop: invalid args {bad} return 2")
            check(len(recorded_runs) == 0, f"cmd_loop: invalid args {bad} never call cmd_run")
            check_loop_cleanup(f"invalid {bad}")

        # 2. bail after consecutive failures
        recorded_runs.clear()
        fake_sequence[:] = [1, 1]
        args = types.SimpleNamespace(**{**base, "max_fails": 2})
        rc = cli.cmd_loop(args, config, db)
        check(rc == 1, "cmd_loop: consecutive failures bail with rc=1")
        check(len(recorded_runs) == 2, "cmd_loop: exactly 2 calls before bail")
        check((tmp / "logs" / "loop.log").exists(), "cmd_loop: loop.log created")
        check_loop_cleanup("consecutive failures")

        # 3. success resets consecutive count
        recorded_runs.clear()
        fake_sequence[:] = [1, 0, 1, 1]
        args = types.SimpleNamespace(**{**base, "max_fails": 2})
        rc = cli.cmd_loop(args, config, db)
        check(rc == 1, "cmd_loop: success resets consecutive failures, bails after 4")
        check(len(recorded_runs) == 4, "cmd_loop: exactly 4 calls after reset")
        check_loop_cleanup("success reset")

        # 4. crash counts as failure
        recorded_runs.clear()
        fake_sequence[:] = [RuntimeError("boom")]
        args = types.SimpleNamespace(**{**base, "max_fails": 1})
        rc = cli.cmd_loop(args, config, db)
        check(rc == 1, "cmd_loop: crash counts as failure, bails")
        check(len(recorded_runs) == 1, "cmd_loop: exactly 1 call after crash")
        check_loop_cleanup("crash")

        # 5. KeyboardInterrupt exits cleanly
        recorded_runs.clear()
        clear_calls.clear()
        fake_sequence[:] = [0, KeyboardInterrupt()]
        args = types.SimpleNamespace(**{**base, "max_fails": 1})
        rc = cli.cmd_loop(args, config, db)
        check(rc == 0, "cmd_loop: KeyboardInterrupt exits cleanly with rc=0")
        check(len(recorded_runs) == 2, "cmd_loop: exactly 2 calls before interrupt")
        check(len(clear_calls) > 0, "cmd_loop: clear recorder called on exit")
        check_loop_cleanup("keyboard interrupt")
    finally:
        cli.cmd_run = orig_cmd_run
        time.sleep = orig_sleep
        loop_state.write_running = orig_write_running
        loop_state.write_sleeping = orig_write_sleeping
        loop_state.scan_done = orig_scan_done
        loop_state.clear = orig_clear
        db.close()


# ── H. cmd_config ───────────────────────────────────────────────────────────
def scenario_cmd_config() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    config = _make_config(tmp, store)
    try:
        args = types.SimpleNamespace(config_cmd="list", platform=None, user=None)
        rc = cli.cmd_config(args, config, db)
        check(rc == 0, "cmd_config: list returns 0")

        args = types.SimpleNamespace(config_cmd="add", platform="x", user="@alice")
        rc = cli.cmd_config(args, config, db)
        check(rc == 0, "cmd_config: add alice returns 0")
        check("alice" in store.list_users("x"), "cmd_config: alice added")

        rc = cli.cmd_config(args, config, db)
        check(rc == 1, "cmd_config: duplicate add returns 1")

        store.ban_user("x", "dave", reason="gone")
        args = types.SimpleNamespace(config_cmd="add", platform="x", user="dave")
        rc = cli.cmd_config(args, config, db)
        check(rc == 0, "cmd_config: add banned user returns 0")
        check("dave" in store.list_users("x"), "cmd_config: dave added")
        check("dave" not in store.list_banned("x"), "cmd_config: dave unbanned")

        args = types.SimpleNamespace(config_cmd="remove", platform="x", user="alice")
        rc = cli.cmd_config(args, config, db)
        check(rc == 0, "cmd_config: remove alice returns 0")
        check("alice" not in store.list_users("x"), "cmd_config: alice removed")

        rc = cli.cmd_config(args, config, db)
        check(rc == 1, "cmd_config: remove missing user returns 1")
    finally:
        db.close()


# ── I. cmd_migrate ──────────────────────────────────────────────────────────
def scenario_cmd_migrate() -> None:
    tmp = Path(tempfile.mkdtemp())
    db = ItemStore.open(str(tmp / "suite.db"))
    store = PolicyStore(tmp / "config.toml")
    config = _make_config(tmp, store)
    managed_keys = set()
    env_snapshot: dict[str, str | None] = {}
    for k in list(os.environ.keys()):
        if (k in ("X_USERS", "TIKTOK_USERS", "INSTAGRAM_USERS",
                  "DELETE_AFTER_UPLOAD") or
                k.startswith("DELETE_AFTER_UPLOAD_")):
            managed_keys.add(k)
            env_snapshot[k] = os.environ.get(k)
    for k in managed_keys:
        os.environ.pop(k, None)

    try:
        os.environ["X_USERS"] = "@alice, bob,"
        os.environ["TIKTOK_USERS"] = "zed"
        os.environ["DELETE_AFTER_UPLOAD"] = "yes"
        os.environ["DELETE_AFTER_UPLOAD_X"] = "0"
        os.environ["DELETE_AFTER_UPLOAD_X_ALICE"] = "true"
        os.environ["DELETE_AFTER_UPLOAD_TIKTOK_ZED"] = "1"
        os.environ["DELETE_AFTER_UPLOAD_X_BOB"] = "maybe"
        os.environ["DELETE_AFTER_UPLOAD_FOO"] = "1"

        store.add_user("x", "alice")

        args = types.SimpleNamespace()
        rc = cli.cmd_migrate(args, config, db)
        check(rc == 0, "cmd_migrate: returns 0")
        x_users = store.list_users("x")
        check(sorted(x_users) == ["alice", "bob"],
              "cmd_migrate: x users are alice and bob")
        check("zed" in store.list_users("tiktok"), "cmd_migrate: tiktok user zed added")

        check(store.get("delete_after_upload") is True,
              "cmd_migrate: global delete_after_upload True")
        check(store.get("delete_after_upload", platform="x") is False,
              "cmd_migrate: platform x delete_after_upload False")
        check(store.get("delete_after_upload", platform="x", username="alice") is True,
              "cmd_migrate: per-user alice delete_after_upload True")
        check(store.get("delete_after_upload", platform="tiktok", username="zed") is True,
              "cmd_migrate: per-user tiktok/zed delete_after_upload True")
        check(store.get("delete_after_upload", platform="x", username="bob") is False,
              "cmd_migrate: per-user bob falls back to platform value False")

        rc = cli.cmd_migrate(args, config, db)
        check(rc == 0, "cmd_migrate: second run returns 0")
        check(len(store.list_users("x")) == 2, "cmd_migrate: idempotent user count")
    finally:
        to_remove = set()
        for k in list(os.environ.keys()):
            if (k in ("X_USERS", "TIKTOK_USERS", "INSTAGRAM_USERS",
                      "DELETE_AFTER_UPLOAD") or
                    k.startswith("DELETE_AFTER_UPLOAD_")):
                to_remove.add(k)
        for k in to_remove:
            os.environ.pop(k, None)
        for k, v in env_snapshot.items():
            if v is not None:
                os.environ[k] = v
        db.close()

    current_managed: dict[str, str | None] = {}
    for k in list(os.environ.keys()):
        if (k in ("X_USERS", "TIKTOK_USERS", "INSTAGRAM_USERS",
                  "DELETE_AFTER_UPLOAD") or
                k.startswith("DELETE_AFTER_UPLOAD_")):
            current_managed[k] = os.environ.get(k)
    check(current_managed == env_snapshot,
          f"cmd_migrate: managed env keys restored to snapshot, got {current_managed!r}")


def _unexpected_prompt(prompt: str = "") -> str:
    raise AssertionError(f"FAILED: unexpected confirmation prompt {prompt!r}")


def main() -> int:
    orig_input = builtins.input
    builtins.input = _unexpected_prompt
    try:
        scenario_purge_sent()
        scenario_reset_all()
        scenario_reset_failed_and_user()
        scenario_validate_chat_id()
        scenario_cmd_run()
        scenario_cmd_stories()
        scenario_cmd_loop()
        scenario_cmd_config()
        scenario_cmd_migrate()
    finally:
        builtins.input = orig_input
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
