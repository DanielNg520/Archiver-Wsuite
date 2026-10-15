"""
ops._selftest_bot
─────────────────
Regression coverage for the `ops.bot` package and its two new seams:
commands, state, telegram API client, loop runner, env file parsing,
and platform service unit generation.

Run: python3 -m ops._selftest_bot
Style matches the other _selftest scripts: plain asserts, checkmark per
assertion, nonzero exit on first failure.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import core.heartbeat
from core.env import read_env_file
from core.platform import service
from ops.bot import commands, loop
from ops.bot.state import BotState
from ops.bot.telegram import BotAPI, TelegramError

_checks = 0


def ok(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"✗ {label}")
    _checks += 1
    print(f"✓ {label}")


def test_parse_command() -> None:
    ok(commands.parse_command("/record alice") == ("record", "alice"),
       "parse_command: record with handle")
    ok(commands.parse_command("/record @alice") == ("record", "alice"),
       "parse_command: @ stripped once")
    ok(commands.parse_command("/record@MyBot alice") == ("record", "alice"),
       "parse_command: /record@MyBot alice")
    ok(commands.parse_command("/re-add bob") == ("re_add", "bob"),
       "parse_command: /re-add alias")
    ok(commands.parse_command("/ar-add bob") == ("ar_add", "bob"),
       "parse_command: /ar-add alias")
    ok(commands.parse_command("/record") is None,
       "parse_command: record with no handle -> None")
    ok(commands.parse_command("/record   ") is None,
       "parse_command: record with whitespace handle -> None")
    ok(commands.parse_command("/list extra") == ("list", None),
       "parse_command: /list extra -> ('list', None)")
    ok(commands.parse_command("/recstatus") == ("status", None),
       "parse_command: /recstatus -> ('status', None)")
    ok(commands.parse_command("/foo") is None,
       "parse_command: unknown /foo -> None")
    ok(commands.parse_command("plain text") is None,
       "parse_command: plain text -> None")
    ok(commands.parse_command("/RECORD x") == ("record", "x"),
       "parse_command: case-insensitive /RECORD x")


def test_run_cli() -> None:
    rc, out = commands.run_cli(["/bin/echo", "hi"])
    ok(rc == 0 and "hi" in out, "run_cli: /bin/echo hi -> (0, contains 'hi')")

    rc, _ = commands.run_cli(["/bin/false"])
    ok(rc != 0, "run_cli: /bin/false -> nonzero")

    rc, out = commands.run_cli(["/no/such/binary_ops_selftest"])
    ok(rc == -1 and isinstance(out, str),
       "run_cli: missing binary -> (-1, str) without raising")

    rc, out = commands.run_cli(["/bin/sleep", "5"], timeout_s=0.2)
    ok(rc == -1 and isinstance(out, str),
       "run_cli: timeout -> (-1, str) without raising")


def test_bot_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        s = BotState(root)
        ok(s.get_offset() == 0, "BotState: offset defaults to 0")
        s.set_offset(42)
        ok(s.get_offset() == 42, "BotState: offset round-trips")

        ok(s.get_report(12345) is None, "BotState: get_report defaults None")
        s.set_report(12345, 999)
        ok(s.get_report(12345) == 999, "BotState: get_report returns set id")

        s2 = BotState(root)
        ok(s2.get_report(12345) == 999,
           "BotState: report survives new BotState instance")
        s2.clear_report(12345)
        ok(
            s2.get_report(12345) is None,
            "BotState: clear_report clears report",
        )

        s3 = BotState(root)
        ok(s3.get_report(12345) is None,
           "BotState: clear survives new BotState instance")

        s.track(100, 1, 1000.0)
        s.track(100, 1, 1000.0)
        ok(len(s.due(2000.0, 500.0)) == 1, "BotState: track is idempotent")

        s.track(100, 2, 1500.0)
        ok(s.due(1600.0, 500.0) == [(100, 1, 1000.0)],
           "BotState: due honours age")
        ok(len(s.due(2100.0, 500.0)) == 2,
           "BotState: due returns both when both are old enough")

        s.forget(100, 1)
        ok((100, 1, 1000.0) not in s.due(3000.0, 0.0),
           "BotState: forget removes message entry")

        (root / "sent.json").write_text("{corrupt json content")
        ok(s.due(5000.0, 0.0) == [],
           "BotState: corrupt sent.json reads as empty without raising")


def test_telegram_api() -> None:
    recorded_req: list[urllib.request.Request] = []

    class FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def close(self) -> None:
            pass

    def fake_urlopen(
        req: urllib.request.Request, timeout: float = 30.0
    ) -> FakeResp:
        recorded_req.append(req)
        return FakeResp(b'{"ok": true, "result": {"message_id": 42}}')

    api = BotAPI("TEST_TOKEN_123", urlopen=fake_urlopen)
    mid = api.send_message(100, "hello")
    ok(mid == 42, "telegram: successful sendMessage returns message_id")
    ok(len(recorded_req) == 1, "telegram: fake urlopen called once")
    req = recorded_req[0]
    ok(req.full_url.endswith("/sendMessage"),
       "telegram: request URL ends with /sendMessage")
    body = json.loads(req.data.decode("utf-8"))
    ok(body == {"chat_id": 100, "text": "hello"},
       "telegram: sendMessage body is JSON")

    token = "SECRET_TOKEN_999"
    err_url = f"https://api.telegram.org/bot{token}/deleteMessage"
    desc = "Bad Request: message to delete not found"
    err_body = io.BytesIO(
        json.dumps({"ok": False, "description": desc}).encode("utf-8")
    )
    http_err = urllib.error.HTTPError(
        err_url, 400, "Bad Request", {}, err_body  # type: ignore
    )

    def failing_urlopen(
        req: urllib.request.Request, timeout: float = 30.0
    ) -> Any:
        raise http_err

    api_err = BotAPI(token, urlopen=failing_urlopen)
    caught = False
    try:
        api_err.delete_message(100, 200)
    except TelegramError as exc:
        caught = True
        err_msg = str(exc)
        ok(desc in err_msg,
           "telegram: TelegramError text contains error description")
        ok(token not in err_msg,
           "telegram: TelegramError text never contains token")

    ok(caught, "telegram: HTTPError raised as TelegramError")
    ok(
        token not in repr(api_err),
        "telegram: repr(api) does not contain token",
    )

    def url_error_urlopen(
        req: urllib.request.Request, timeout: float = 30.0
    ) -> Any:
        raise urllib.error.URLError(req.full_url)

    api_url = BotAPI(token, urlopen=url_error_urlopen)
    caught_url = False
    try:
        api_url.delete_message(100, 200)
    except TelegramError as exc:
        caught_url = True
        err_msg = str(exc)
        ok(
            token not in err_msg,
            "telegram: URLError text never contains token",
        )
        ok(
            "<token>" in err_msg,
            "telegram: URLError text contains <token>",
        )

    ok(caught_url, "telegram: URLError raised as TelegramError")


def test_loop_run() -> None:
    class FakeBotAPI:
        def __init__(
            self, batches: list[list[dict[str, Any]]] | None = None
        ) -> None:
            self.batches = list(batches or [])
            self.sent: list[tuple[int, str]] = []
            self.deleted: list[tuple[int, int]] = []
            self.next_id = 100
            self.fail_delete = False

        def get_updates(
            self, offset: int, timeout: int = 30
        ) -> list[dict[str, Any]]:
            if self.batches:
                return self.batches.pop(0)
            return []

        def send_message(self, chat_id: int, text: str) -> int:
            self.next_id += 1
            self.sent.append((chat_id, text))
            return self.next_id

        def delete_message(self, chat_id: int, message_id: int) -> None:
            if self.fail_delete:
                raise TelegramError("delete failed")
            self.deleted.append((chat_id, message_id))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state = BotState(root / "state")
        lock_path = root / "tiktok.lock"
        bins = {"recorder": "/bin/echo", "archiver": "/bin/echo"}
        curr_time = 1000.0

        def fake_clock() -> float:
            return curr_time

        up_allowed = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 111},
                "chat": {"id": 111},
                "text": "/recstatus",
                "date": 1000,
            },
        }
        up_unallowed = {
            "update_id": 2,
            "message": {
                "message_id": 20,
                "from": {"id": 999},
                "chat": {"id": 999},
                "text": "/recstatus",
                "date": 1000,
            },
        }
        up_list = {
            "update_id": 3,
            "message": {
                "message_id": 30,
                "from": {"id": 111},
                "chat": {"id": 111},
                "text": "/list",
                "date": 1010,
            },
        }
        api = FakeBotAPI([[up_allowed, up_unallowed], [up_list]])

        iteration = 0
        t0 = 2000.0
        t_fail = 0.0
        saved_rep_id: int | None = None
        cmd_tracked = False
        kept_at_30h = False

        def should_stop() -> bool:
            nonlocal iteration, curr_time, t_fail, saved_rep_id
            nonlocal cmd_tracked, kept_at_30h
            if iteration == 1:
                cmd_tracked = any(
                    c == 111 and m == 10
                    for c, m, _ in state.due(curr_time, 0.0)
                )
            elif iteration == 2:
                core.heartbeat.write_atomic(
                    lock_path, {"pid": os.getpid(), "username": "alice"}
                )
            elif iteration == 3:
                saved_rep_id = state.get_report(111)
                lock_path.unlink()
            elif iteration == 4:
                core.heartbeat.write_atomic(
                    lock_path, {"pid": os.getpid(), "username": "bob"}
                )
            elif iteration == 5:
                state.track(111, 555, t0)
                curr_time = t0 + 24 * 3600 + 10
            elif iteration == 6:
                t_fail = curr_time
                state.track(111, 777, t_fail)
                api.fail_delete = True
                curr_time = t_fail + 30 * 3600
            elif iteration == 7:
                kept_at_30h = any(
                    c == 111 and m == 777
                    for c, m, _ in state.due(curr_time, 0.0)
                )
                curr_time = t_fail + 48 * 3600
            elif iteration >= 8:
                return True
            iteration += 1
            return False

        loop.run(
            api,  # type: ignore
            state,
            allowed=frozenset([111]),
            lock_path=lock_path,
            bins=bins,
            poll_interval_s=0,
            clock=fake_clock,
            should_stop=should_stop,
        )

        ok(
            (111, "Not recording anyone.") in api.sent,
            "loop: /recstatus replied 'Not recording anyone.'",
        )
        ok(
            not any(chat == 999 for chat, _ in api.sent),
            "loop: unallowed sender got no reply",
        )
        ok(state.get_offset() == 4, "loop: offset advanced past updates")
        ok(cmd_tracked, "loop: allowed command message was tracked")

        ok((111, "config list") in api.sent, "loop: /list replied with echo")

        ok(
            any(
                chat == 111 and "recording started: @alice" in txt
                for chat, txt in api.sent
            ),
            "loop: lock write sent recording started broadcast",
        )
        ok(
            any(
                chat == 111 and "recording ended: @alice" in txt
                for chat, txt in api.sent
            ),
            "loop: lock removal sent recording ended broadcast",
        )

        ok(saved_rep_id is not None, "loop: previous report id existed")
        ok(
            (111, saved_rep_id) in api.deleted,
            "loop: second recording start deleted previous report",
        )

        ok(
            (111, 555) in api.deleted,
            "loop: deletion sweep deleted message after 24h",
        )
        ok(
            not any(
                c == 111 and m == 555 for c, m, _ in state.due(curr_time, 0.0)
            ),
            "loop: 24h deleted message forgotten from state",
        )
        ok(kept_at_30h, "loop: failed delete kept in state at 30h")
        ok(
            not any(
                c == 111 and m == 777 for c, m, _ in state.due(curr_time, 0.0)
            ),
            "loop: failed delete forgotten from state at 48h",
        )


def test_read_env_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        env_path = Path(td) / "test.env"
        env_path.write_text(
            "# comment\n"
            "   # leading whitespace comment\n"
            "\n"
            "KEY1=value1\n"
            'KEY2="quoted value"\n'
            "KEY3='single quoted'\n"
            "KEY4=val=with=equals\n"
            "NO_EQUALS_HERE\n",
            encoding="utf-8",
        )
        res = read_env_file(env_path)
        ok(res.get("KEY1") == "value1", "read_env_file: simple key-value")
        ok(res.get("KEY2") == "quoted value",
           "read_env_file: double quotes stripped")
        ok(res.get("KEY3") == "single quoted",
           "read_env_file: single quotes stripped")
        ok(res.get("KEY4") == "val=with=equals",
           "read_env_file: value containing '=' kept whole")
        ok("NO_EQUALS_HERE" not in res,
           "read_env_file: line without '=' skipped")

        missing = read_env_file(Path(td) / "missing.env")
        ok(missing == {}, "read_env_file: missing file returns {}")


def test_service_unit() -> None:
    if sys.platform == "darwin":
        print("skip: core.platform.service checks on macOS")
        return

    spec_kc = service.JobSpec(
        label="com.duy.bot",
        program="/bin/true",
        keep_children=True,
    )
    unit_kc = service._service_unit(spec_kc)
    ok(
        "KillMode=process" in unit_kc,
        "service: keep_children daemon unit contains KillMode=process",
    )

    spec_def = service.JobSpec(
        label="com.duy.bot",
        program="/bin/true",
    )
    unit_def = service._service_unit(spec_def)
    ok(
        "KillMode=process" not in unit_def,
        "service: default daemon unit does not contain KillMode=process",
    )

    spec_cal = service.JobSpec(
        label="com.duy.bot",
        program="/bin/true",
        kind="calendar",
        keep_children=True,
    )
    unit_cal = service._service_unit(spec_cal)
    ok(
        "KillMode=process" not in unit_cal,
        "service: calendar unit with keep_children has no KillMode=process",
    )


def main() -> int:
    print("ops.bot selftest")
    test_parse_command()
    test_run_cli()
    test_bot_state()
    test_telegram_api()
    test_loop_run()
    test_read_env_file()
    test_service_unit()
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
