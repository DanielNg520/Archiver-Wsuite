"""
Selftest for destructive-command safety paths in cli.py: cmd_purge_sent
(DeletionGuard safebrake + confirmation + dry-run), cmd_reset (all / failed /
user subcommands + non-TTY guard), and _validate_chat_id_format.

Run: python archiver/archiver/_selftest_cli.py

Standalone (no pytest). Real ItemStore on a temp suite.db, real PolicyStore on
a temp config.toml; every scenario builds its own temp dir and closes its
store before returning.
"""
import builtins
import io
import sys
import tempfile
import types
from pathlib import Path

_repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_repo / "core"))
sys.path.insert(0, str(_repo / "archiver"))

from core import ItemStore, PolicyStore  # noqa: E402
import archiver.cli as cli              # noqa: E402

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
    finally:
        builtins.input = orig_input
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
