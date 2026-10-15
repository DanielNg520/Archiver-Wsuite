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


def test_parse_command_handles() -> None:
    ok(commands.parse_command("/record -rf") is None,
       "parse_command: rejects handle with -rf")
    ok(commands.parse_command("/record a b") is None,
       "parse_command: rejects handle with space")
    ok(commands.parse_command("/re_add @") is None,
       "parse_command: rejects bare @ handle")
    ok(commands.parse_command("/record " + "a" * 65) is None,
       "parse_command: rejects 65-char handle")
    ok(commands.parse_command("/record @some.user_1") == (
           "record", "some.user_1"),
       "parse_command: accepts valid @some.user_1")


def test_read_env_file_export_and_comments() -> None:
    with tempfile.TemporaryDirectory() as td:
        env_path = Path(td) / "test.env"
        env_path.write_text(
            "export KEY=v\n"
            "A=val # comment\n"
            "B=a#b\n"
            'C="x # y"\n'
            'D="tok" # c\n'
            "E='x' # c\n"
            'F="abc\n'
            'G="a"b\n',
            encoding="utf-8",
        )
        res = read_env_file(env_path)
        ok(res.get("KEY") == "v",
           "read_env_file: export KEY=v stripped")
        ok(res.get("A") == "val",
           "read_env_file: inline comment stripped")
        ok(res.get("B") == "a#b",
           "read_env_file: hash without space kept")
        ok(res.get("C") == "x # y",
           "read_env_file: quoted hash kept")
        ok(res.get("D") == "tok",
           "read_env_file: double-quoted value with comment stripped")
        ok(res.get("E") == "x",
           "read_env_file: single-quoted value with comment stripped")
        ok(res.get("F") == '"abc',
           "read_env_file: unterminated quote kept")
        ok(res.get("G") == '"a"b',
           "read_env_file: quote followed by text kept")


def test_broadcast_retry() -> None:
    class FlakyBotAPI:
        def __init__(self) -> None:
            self.calls = 0
            self.sent_id: int | None = None

        def send_message(self, chat_id: int, text: str) -> int:
            self.calls += 1
            if self.calls == 1:
                raise TelegramError("first call transient failure")
            self.sent_id = 501
            return self.sent_id

    class FailingBotAPI:
        def send_message(self, chat_id: int, text: str) -> int:
            raise TelegramError("always fails")

    class WorkingBotAPI:
        def send_message(self, chat_id: int, text: str) -> int:
            return 601

    sleep_calls: list[float] = []
    orig_sleep = loop.time.sleep
    loop.time.sleep = lambda sec: sleep_calls.append(sec)
    try:
        chat = 12345
        allowed = frozenset([chat])

        with tempfile.TemporaryDirectory() as td:
            state = BotState(Path(td))
            api = FlakyBotAPI()
            failed = loop._broadcast(
                api,
                state,
                allowed,
                current_user="alice",
                prev_user=None,
                clock=lambda: 1000.0,
            )
            ok(
                failed == frozenset([chat]),
                "broadcast retry: flaky returns failed chat on first call",
            )
            ok(api.calls == 1,
               "broadcast retry: exactly one send attempt made")
            ok(state.get_report(chat) is None,
               "broadcast retry: report is None after failure")
            ok(len(sleep_calls) == 0,
               "broadcast retry: loop.time.sleep is never called")

            failed2 = loop._broadcast(
                api,
                state,
                frozenset([chat]),
                current_user="alice",
                prev_user=None,
                clock=lambda: 1000.0,
            )
            ok(failed2 == frozenset(),
               "broadcast retry: second call returns empty frozenset")
            ok(
                api.sent_id is not None
                and state.get_report(chat) == api.sent_id,
                "broadcast retry: successful attempt updates report",
            )

        with tempfile.TemporaryDirectory() as td:
            state = BotState(Path(td))
            api_fail = FailingBotAPI()
            failed_fail = loop._broadcast(
                api_fail,
                state,
                allowed,
                current_user="alice",
                prev_user=None,
                clock=lambda: 1000.0,
            )
            ok(
                failed_fail == frozenset([chat]),
                "broadcast retry: persistent failure returns failed chat",
            )
            ok(
                state.get_report(chat) is None,
                "broadcast retry: persistent failure leaves report None",
            )

        with tempfile.TemporaryDirectory() as td:
            state = BotState(Path(td))
            api_work = WorkingBotAPI()
            failed_end = loop._broadcast(
                api_work,
                state,
                allowed,
                current_user=None,
                prev_user="alice",
                clock=lambda: 1000.0,
            )
            ok(
                failed_end == frozenset(),
                "broadcast retry: end notice returns empty frozenset",
            )
    finally:
        loop.time.sleep = orig_sleep


def test_loop_broadcast_retry_nonblocking() -> None:
    class FakeBotAPI:
        def __init__(self) -> None:
            self.send_calls = 0
            self.updates_calls = 0
            self.sent: list[tuple[int, str]] = []

        def get_updates(
            self, offset: int, timeout: int = 30
        ) -> list[dict[str, Any]]:
            self.updates_calls += 1
            return []

        def send_message(self, chat_id: int, text: str) -> int:
            self.send_calls += 1
            if self.send_calls <= 2:
                raise TelegramError("first two calls fail")
            self.sent.append((chat_id, text))
            return 999

    sleep_calls: list[float] = []
    orig_sleep = loop.time.sleep
    loop.time.sleep = lambda sec: sleep_calls.append(sec)

    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = BotState(root / "state")
            lock_path = root / "tiktok.lock"
            bins = {"recorder": "/bin/echo", "archiver": "/bin/echo"}
            core.heartbeat.write_atomic(
                lock_path, {"pid": os.getpid(), "username": "alice"}
            )
            api = FakeBotAPI()
            passes = 0

            def should_stop() -> bool:
                nonlocal passes
                if passes >= 5:
                    return True
                passes += 1
                return False

            loop.run(
                api,  # type: ignore
                state,
                allowed=frozenset([111]),
                lock_path=lock_path,
                bins=bins,
                poll_interval_s=0,
                clock=lambda: 1000.0,
                should_stop=should_stop,
            )

            ok(
                len(api.sent) == 1,
                "loop nonblocking: start notice sent exactly once",
            )
            ok(
                state.get_report(111) == 999,
                "loop nonblocking: report was set",
            )
            ok(
                api.updates_calls == 5,
                "loop nonblocking: get_updates called on every pass",
            )
            ok(
                not any(sec > 0 for sec in sleep_calls),
                "loop nonblocking: sleep never called with positive value",
            )
    finally:
        loop.time.sleep = orig_sleep


def test_usage_hint() -> None:
    hint_rec = "Usage: /record <handle> (letters, digits, _ or ., max 64)"
    hint_ar = "Usage: /ar_add <handle> (letters, digits, _ or ., max 64)"
    hint_rem = "Usage: /remove <handle> (letters, digits, _ or ., max 64)"

    ok(commands.usage_hint("/record -rf") == hint_rec,
       "usage_hint: /record -rf returns record usage hint")
    ok(commands.usage_hint("/record") == hint_rec,
       "usage_hint: /record returns record usage hint")
    ok(commands.usage_hint("/ar-add @") == hint_ar,
       "usage_hint: /ar-add @ returns ar_add usage hint")
    ok(commands.usage_hint("/remove a b") == hint_rem,
       "usage_hint: /remove a b returns remove usage hint")
    ok(commands.usage_hint("/record alice") is None,
       "usage_hint: /record alice returns None")
    ok(commands.usage_hint("/list") is None,
       "usage_hint: /list returns None")
    ok(commands.usage_hint("/foo") is None,
       "usage_hint: /foo returns None")
    ok(commands.usage_hint("hello") is None,
       "usage_hint: hello returns None")

    class FakeBotAPI:
        def __init__(self, updates: list[dict[str, Any]]) -> None:
            self.updates = list(updates)
            self.sent: list[tuple[int, str]] = []
            self.next_id = 200

        def get_updates(
            self, offset: int, timeout: int = 30
        ) -> list[dict[str, Any]]:
            res = self.updates
            self.updates = []
            return res

        def send_message(self, chat_id: int, text: str) -> int:
            self.next_id += 1
            self.sent.append((chat_id, text))
            return self.next_id

    update_msg = {
        "update_id": 1,
        "message": {
            "message_id": 77,
            "from": {"id": 111},
            "chat": {"id": 111},
            "text": "/record -rf",
            "date": 1000,
        },
    }
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state = BotState(root / "state")
        lock_path = root / "tiktok.lock"
        bins = {"recorder": "/bin/echo", "archiver": "/bin/echo"}
        api = FakeBotAPI([update_msg])
        loop._process_updates(
            api,
            state,
            allowed=frozenset([111]),
            bins=bins,
            lock_path=lock_path,
            clock=lambda: 1000.0,
        )
        ok(
            len(api.sent) == 1
            and api.sent[0][1].startswith("Usage: /record"),
            "usage_hint: sent hint starting with Usage: /record",
        )
        ok(
            any(c == 111 and m == 77 for c, m, _ in state.due(1000.0, 0.0)),
            "usage_hint: update message id tracked in state",
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
    test_parse_command_handles()
    test_read_env_file_export_and_comments()
    test_broadcast_retry()
    test_loop_broadcast_retry_nonblocking()
    test_usage_hint()
    print(f"\nALL PASS ({_checks} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
