"""
ops._selftest_bot_p2
────────────────────
Regression coverage for the `ops.bot` package phase 2 additions:
command catalog parsing and permissions, bot commands catalog,
BotAPI additions with fake urlopen, commands.start_detached,
actions confirmation and execution flow, and loop runner integration.

Run: python3 -m ops._selftest_bot_p2
Style matches the other _selftest scripts: plain asserts, checkmark per
assertion, nonzero exit on first failure.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import core.heartbeat
from ops.bot import actions, catalog, commands, loop
from ops.bot.state import BotState
from ops.bot.telegram import BotAPI

_checks = 0


def ok(cond: bool, label: str) -> None:
    global _checks
    if not cond:
        raise AssertionError(f"✗ {label}")
    _checks += 1
    print(f"✓ {label}")


def test_catalog_parse() -> None:
    # alias vs passthrough equivalence (/restart dispatcher == /o restart dispatcher)
    p_alias = catalog.parse("/restart dispatcher")
    p_direct = catalog.parse("/o restart dispatcher")
    ok(p_alias is not None and p_direct is not None, "parse: alias and direct parsed")
    ok(p_alias == p_direct, "parse: /restart dispatcher == /o restart dispatcher")

    # phase 1 names and unknown text return None
    phase1_cmds = [
        "/record alice",
        "/recstatus",
        "/ar_add bob",
        "/ar-add bob",
        "/re_add bob",
        "/re-add bob",
        "/remove bob",
        "/list",
    ]
    for p1 in phase1_cmds:
        ok(catalog.parse(p1) is None, f"parse: phase 1 command '{p1}' -> None")
    ok(catalog.parse("plain text") is None, "parse: plain text -> None")
    ok(catalog.parse("/unknown_command") is None, "parse: unknown command -> None")

    # @botname suffix
    ok(catalog.parse("/health@TestBot") == catalog.parse("/health"),
       "parse: @botname suffix stripped on alias")
    ok(catalog.parse("/a@TestBot stats") == catalog.parse("/a stats"),
       "parse: @botname suffix stripped on tool command")

    # bad quoting returns a str
    err = catalog.parse('/a "unclosed quote')
    ok(isinstance(err, str) and "Command parsing error:" in err,
       "parse: unclosed quote returns parsing error str")

    # each blocked verb returns a str
    blocked = [
        ("/r record --user x", "recorder record"),
        ("/a start", "archiver start"),
        ("/a loop", "archiver loop"),
        ("/r start", "recorder start"),
        ("/r watch", "recorder watch"),
        ("/d start", "dispatcher start"),
        ("/o watch", "ops watch"),
        ("/o bot", "ops bot"),
        ("/d burner login", "dispatcher burner login"),
    ]
    for cmd_str, label in blocked:
        res = catalog.parse(cmd_str)
        ok(isinstance(res, str) and "Refused:" in res, f"parse: blocked '{label}' returns refusal")

    # /r record --user x hints /record
    r_hint = catalog.parse("/r record --user x")
    ok(isinstance(r_hint, str) and "Use /record <handle> instead." in r_hint,
       "parse: /r record hints /record <handle>")

    # safe-prefix commands have confirm False
    safe_cmds = [
        "/a stats",
        "/a health",
        "/a queue list",
        "/r status",
        "/d status",
        "/d check-routes",
        "/o health",
    ]
    for sc in safe_cmds:
        plan = catalog.parse(sc)
        ok(isinstance(plan, catalog.Plan) and plan.confirm is False,
           f"parse: safe-prefix command '{sc}' confirm is False")

    # show-only verbs are safe without args and confirm with args
    show_safe = catalog.parse("/a download")
    ok(isinstance(show_safe, catalog.Plan) and show_safe.confirm is False,
       "parse: show-only verb '/a download' without args is safe")
    show_confirm = catalog.parse("/a download extra")
    ok(isinstance(show_confirm, catalog.Plan) and show_confirm.confirm is True,
       "parse: show-only verb '/a download extra' with args confirms")
    rec_banned_safe = catalog.parse("/r banned")
    ok(isinstance(rec_banned_safe, catalog.Plan) and rec_banned_safe.confirm is False,
       "parse: show-only verb '/r banned' without args is safe")
    rec_banned_confirm = catalog.parse("/r banned add user")
    ok(isinstance(rec_banned_confirm, catalog.Plan) and rec_banned_confirm.confirm is True,
       "parse: show-only verb '/r banned add user' with args confirms")

    # /a reset all confirms
    p_reset = catalog.parse("/a reset all")
    ok(isinstance(p_reset, catalog.Plan) and p_reset.confirm is True,
       "parse: /a reset all confirms")

    # --help anywhere is safe
    p_help_end = catalog.parse("/a reset all --help")
    ok(isinstance(p_help_end, catalog.Plan) and p_help_end.confirm is False,
       "parse: --help at end makes command safe")
    p_help_mid = catalog.parse("/a -h reset all")
    ok(isinstance(p_help_mid, catalog.Plan) and p_help_mid.confirm is False,
       "parse: -h anywhere makes command safe")

    # /a alone is archiver --help
    p_a = catalog.parse("/a")
    ok(isinstance(p_a, catalog.Plan) and p_a.tool == "archiver" and p_a.args == ("--help",) and p_a.confirm is False,
       "parse: /a alone resolves to archiver --help with confirm False")

    # ops update gets --no-watch once (not duplicated)
    p_up1 = catalog.parse("/o update")
    ok(isinstance(p_up1, catalog.Plan) and p_up1.args == ("update", "--no-watch"),
       "parse: /o update gets --no-watch added")
    p_up2 = catalog.parse("/o update --no-watch")
    ok(isinstance(p_up2, catalog.Plan) and p_up2.args == ("update", "--no-watch"),
       "parse: /o update --no-watch does not duplicate flag")
    p_up3 = catalog.parse("/update")
    ok(isinstance(p_up3, catalog.Plan) and p_up3.args == ("update", "--no-watch"),
       "parse: /update alias gets --no-watch added")

    # detach True for /update, /restart bot, /o unload, False for /restart dispatcher
    ok(catalog.parse("/update").detach is True, "parse: /update detach is True")
    ok(catalog.parse("/restart bot").detach is True, "parse: /restart bot detach is True")
    ok(catalog.parse("/o unload").detach is True, "parse: /o unload detach is True")
    ok(catalog.parse("/o unload bot").detach is True, "parse: /o unload bot detach is True")
    ok(catalog.parse("/restart dispatcher").detach is False, "parse: /restart dispatcher detach is False")
    ok(catalog.parse("/o restart dispatcher").detach is False, "parse: /o restart dispatcher detach is False")

    # timeouts 3600/600/120/0 per the contract
    ok(catalog.parse("/update").timeout_s == 0.0, "timeout: detach command has timeout_s 0.0")
    ok(catalog.parse("/a run").timeout_s == 3600.0, "timeout: archiver run has timeout_s 3600.0")
    ok(catalog.parse("/a stories").timeout_s == 3600.0, "timeout: archiver stories has timeout_s 3600.0")
    ok(catalog.parse("/a health").timeout_s == 600.0, "timeout: archiver health has timeout_s 600.0")
    ok(catalog.parse("/d check-routes").timeout_s == 600.0, "timeout: dispatcher check-routes has timeout_s 600.0")
    ok(catalog.parse("/a stats").timeout_s == 120.0, "timeout: archiver stats has default timeout_s 120.0")
    ok(catalog.parse("/a").timeout_s == 120.0, "timeout: /a alone has default timeout_s 120.0")

    # alias with forbidden extra args refused
    for alias_name in ("health", "qstatus", "stats", "rstatus", "update"):
        refused = catalog.parse(f"/{alias_name} extra")
        ok(isinstance(refused, str) and f"Usage: /{alias_name} takes no additional arguments." in refused,
           f"parse: alias /{alias_name} with extra args refused")
    ok(isinstance(catalog.parse("/queue extra"), catalog.Plan),
       "parse: /queue allows extra arguments")


def test_catalog_bot_commands() -> None:
    cmds = catalog.bot_commands()
    ok(isinstance(cmds, list) and len(cmds) > 0, "bot_commands: returns non-empty list")

    names = [c[0] for c in cmds]
    ok(len(names) == len(set(names)), "bot_commands: command names are unique")

    cmd_re = re.compile(r"^[a-z0-9_]{1,32}$")
    for name, desc in cmds:
        ok(bool(cmd_re.match(name)), f"bot_commands: valid telegram name '{name}'")
        ok(0 < len(desc) <= 60, f"bot_commands: description for '{name}' within 60 chars ({len(desc)})")

    ok("record" in names, "bot_commands: includes 'record'")
    for tool_name in ("a", "r", "d", "o"):
        ok(tool_name in names, f"bot_commands: includes '{tool_name}'")


class _FakeHTTPResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        pass


def test_bot_api_additions() -> None:
    recorded_requests: list[tuple[str, dict[str, Any], float]] = []

    def fake_urlopen(req: urllib.request.Request, timeout: float = 30.0) -> _FakeHTTPResponse:
        url = req.full_url
        payload = json.loads(req.data.decode("utf-8")) if req.data else {}
        recorded_requests.append((url, payload, timeout))
        res: dict[str, Any] = {"ok": True, "result": {"message_id": 42}}
        return _FakeHTTPResponse(json.dumps(res).encode("utf-8"))

    api = BotAPI("TEST_TOKEN", urlopen=fake_urlopen)

    # 1. send_message without reply_markup
    mid = api.send_message(12345, "hello world")
    ok(mid == 42, "BotAPI.send_message: returns message_id")
    url, payload, _ = recorded_requests[-1]
    ok(url.endswith("/sendMessage"), "send_message: method name in URL")
    ok(payload["chat_id"] == 12345 and payload["text"] == "hello world", "send_message: payload fields")
    ok("reply_markup" not in payload, "send_message: reply_markup omitted when None")

    # 2. send_message with reply_markup
    markup = {"inline_keyboard": [[{"text": "Yes", "callback_data": "y"}]]}
    api.send_message(12345, "prompt", reply_markup=markup)
    url, payload, _ = recorded_requests[-1]
    ok(payload.get("reply_markup") == markup, "send_message: reply_markup included when provided")

    # 3. edit_message_text without reply_markup
    api.edit_message_text(12345, 42, "new text")
    url, payload, _ = recorded_requests[-1]
    ok(url.endswith("/editMessageText"), "edit_message_text: method name in URL")
    ok(payload["chat_id"] == 12345 and payload["message_id"] == 42 and payload["text"] == "new text",
       "edit_message_text: payload fields")
    ok("reply_markup" not in payload, "edit_message_text: reply_markup omitted when None")

    # 4. edit_message_text with reply_markup
    api.edit_message_text(12345, 42, "new text 2", reply_markup=markup)
    url, payload, _ = recorded_requests[-1]
    ok(payload.get("reply_markup") == markup, "edit_message_text: reply_markup included when provided")

    # 5. answer_callback_query without text
    api.answer_callback_query("cb_123")
    url, payload, _ = recorded_requests[-1]
    ok(url.endswith("/answerCallbackQuery"), "answer_callback_query: method name in URL")
    ok(payload["callback_query_id"] == "cb_123", "answer_callback_query: callback_query_id present")
    ok("text" not in payload, "answer_callback_query: text omitted when None")

    # 6. answer_callback_query with text
    api.answer_callback_query("cb_123", text="alert")
    url, payload, _ = recorded_requests[-1]
    ok(payload.get("text") == "alert", "answer_callback_query: text included when provided")

    # 7. set_my_commands payload shape
    cmds = [("cmd1", "desc1"), ("cmd2", "desc2")]
    api.set_my_commands(cmds)
    url, payload, _ = recorded_requests[-1]
    ok(url.endswith("/setMyCommands"), "set_my_commands: method name in URL")
    expected_cmds = [
        {"command": "cmd1", "description": "desc1"},
        {"command": "cmd2", "description": "desc2"},
    ]
    ok(payload.get("commands") == expected_cmds, "set_my_commands: payload shape matches Telegram schema")


def test_commands_start_detached() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "detached.log"
        proc = commands.start_detached(["/bin/sh", "-c", "echo hi"], log_path)
        rc = proc.wait(timeout=5.0)
        ok(rc == 0, "start_detached: child exited with code 0")
        ok(log_path.exists(), "start_detached: log file created")

        content = log_path.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        ok(len(lines) >= 2, "start_detached: log has header line then stdout")
        ok(lines[0].startswith("===") and "['/bin/sh', '-c', 'echo hi']" in lines[0] and lines[0].endswith("==="),
           "start_detached: log contains proper header line with argv")
        ok(lines[1] == "hi", "start_detached: log contains command stdout 'hi'")


class FakeBotAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 100
        self.lock = threading.Lock()

    def send_message(
        self, chat_id: int, text: str, reply_markup: dict | None = None
    ) -> int:
        with self.lock:
            mid = self._next_id
            self._next_id += 1
            self.calls.append(
                (
                    "sendMessage",
                    {
                        "chat_id": chat_id,
                        "text": text,
                        "reply_markup": reply_markup,
                        "message_id": mid,
                    },
                )
            )
            return mid

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        with self.lock:
            self.calls.append(
                (
                    "editMessageText",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": text,
                        "reply_markup": reply_markup,
                    },
                )
            )

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> None:
        with self.lock:
            self.calls.append(
                (
                    "answerCallbackQuery",
                    {"callback_query_id": callback_query_id, "text": text},
                )
            )

    def delete_message(self, chat_id: int, message_id: int) -> None:
        with self.lock:
            self.calls.append(
                (
                    "deleteMessage",
                    {"chat_id": chat_id, "message_id": message_id},
                )
            )

    def set_my_commands(self, cmds: list[tuple[str, str]]) -> None:
        with self.lock:
            self.calls.append(("setMyCommands", {"commands": cmds}))

    def find_calls(self, method: str) -> list[dict[str, Any]]:
        with self.lock:
            return [payload for m, payload in self.calls if m == method]


def test_actions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        bins = {
            "archiver": "/bin/echo",
            "recorder": "/bin/echo",
            "dispatcher": "/bin/echo",
            "ops": "/bin/echo",
        }
        clock = lambda: 1000.0

        # 1. handle_text safe command runs and edits 'running:' to '✓ ok:'
        api1 = FakeBotAPI()
        pending1 = actions.Pending()
        state1 = BotState(Path(tmpdir) / "state1.sqlite3")
        handled = actions.handle_text(
            api1, state1, pending1, 123, "/a stats", bins, None, clock
        )
        ok(handled is True, "actions: safe command handled -> True")
        sends = api1.find_calls("sendMessage")
        ok(len(sends) == 1 and sends[0]["text"] == "running: archiver stats",
           "actions: safe command sent 'running: archiver stats'")
        initial_mid = sends[0]["message_id"]

        deadline = time.time() + 5.0
        found_ok = False
        while time.time() < deadline:
            edits = [c for c in api1.find_calls("editMessageText") if c["message_id"] == initial_mid]
            if edits and "✓ ok: archiver stats" in edits[-1]["text"]:
                found_ok = True
                break
            time.sleep(0.02)
        ok(found_ok, "actions: safe command edited to '✓ ok: archiver stats'")

        # 2. failing command (/bin/false) edits to ✗ with exit code
        api2 = FakeBotAPI()
        pending2 = actions.Pending()
        state2 = BotState(Path(tmpdir) / "state2.sqlite3")
        bins_fail = dict(bins, archiver="/bin/false")
        actions.handle_text(
            api2, state2, pending2, 123, "/a stats", bins_fail, None, clock
        )
        fail_mid = api2.find_calls("sendMessage")[0]["message_id"]

        deadline = time.time() + 5.0
        found_fail = False
        while time.time() < deadline:
            edits = [c for c in api2.find_calls("editMessageText") if c["message_id"] == fail_mid]
            if edits and "✗ failed (exit 1): archiver stats" in edits[-1]["text"]:
                found_fail = True
                break
            time.sleep(0.02)
        ok(found_fail, "actions: failing command edited to '✗ failed (exit 1): archiver stats'")

        # 3. confirm command sends prompt with Yes/No keyboard and stores token
        api3 = FakeBotAPI()
        pending3 = actions.Pending()
        state3 = BotState(Path(tmpdir) / "state3.sqlite3")
        actions.handle_text(
            api3, state3, pending3, 123, "/a reset all", bins, None, clock
        )
        prompt_call = api3.find_calls("sendMessage")[0]
        ok(prompt_call["text"] == "Run: archiver reset all?",
           "actions: confirm command sent prompt 'Run: archiver reset all?'")
        markup = prompt_call.get("reply_markup")
        ok(isinstance(markup, dict) and "inline_keyboard" in markup,
           "actions: confirm command included inline_keyboard")
        buttons = markup["inline_keyboard"][0]
        ok(buttons[0]["text"] == "Yes" and buttons[1]["text"] == "No",
           "actions: prompt contains Yes and No buttons")
        token_y = buttons[0]["callback_data"].split(":", 1)[1]
        token_n = buttons[1]["callback_data"].split(":", 1)[1]
        ok(token_y == token_n, "actions: buttons share confirmation token")
        prompt_mid = prompt_call["message_id"]

        with pending3._lock:
            ok(token_y in pending3._entries, "actions: confirmation token stored in pending")

        # 4. handle_callback y: executes using prompt's message id
        cb_y = {
            "id": "cb_y_1",
            "from": {"id": 999},
            "message": {"chat": {"id": 123}, "message_id": prompt_mid},
            "data": f"y:{token_y}",
        }
        actions.handle_callback(
            api3, state3, pending3, cb_y, frozenset({999}), bins, None, clock
        )
        answers_y = [c for c in api3.find_calls("answerCallbackQuery") if c["callback_query_id"] == "cb_y_1"]
        ok(len(answers_y) == 1 and answers_y[0]["text"] is None,
           "actions: handle_callback (y) answered callback query")

        deadline = time.time() + 5.0
        confirmed_exec = False
        while time.time() < deadline:
            edits = [c for c in api3.find_calls("editMessageText") if c["message_id"] == prompt_mid]
            if any("running: archiver reset all" in e["text"] for e in edits) and any("✓ ok: archiver reset all" in e["text"] for e in edits):
                confirmed_exec = True
                break
            time.sleep(0.02)
        ok(confirmed_exec, "actions: handle_callback (y) executed plan using prompt's message_id")

        # 5. handle_callback n: edits to cancelled
        actions.handle_text(
            api3, state3, pending3, 123, "/a reset all", bins, None, clock
        )
        prompt_call_2 = api3.find_calls("sendMessage")[-1]
        prompt_mid_2 = prompt_call_2["message_id"]
        token_2 = prompt_call_2["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]

        cb_n = {
            "id": "cb_n_1",
            "from": {"id": 999},
            "message": {"chat": {"id": 123}, "message_id": prompt_mid_2},
            "data": f"n:{token_2}",
        }
        actions.handle_callback(
            api3, state3, pending3, cb_n, frozenset({999}), bins, None, clock
        )
        n_edits = [c for c in api3.find_calls("editMessageText") if c["message_id"] == prompt_mid_2]
        ok(len(n_edits) == 1 and n_edits[0]["text"] == "cancelled: archiver reset all",
           "actions: handle_callback (n) edited prompt to 'cancelled: archiver reset all'")

        # 6. unknown token answers expired
        cb_unk = {
            "id": "cb_unk_1",
            "from": {"id": 999},
            "message": {"chat": {"id": 123}, "message_id": 888},
            "data": "y:unknown_token",
        }
        actions.handle_callback(
            api3, state3, pending3, cb_unk, frozenset({999}), bins, None, clock
        )
        unk_answers = [c for c in api3.find_calls("answerCallbackQuery") if c["callback_query_id"] == "cb_unk_1"]
        ok(len(unk_answers) == 1 and unk_answers[0]["text"] == "expired",
           "actions: unknown token answered callback query with 'expired'")
        unk_edits = [c for c in api3.find_calls("editMessageText") if c["message_id"] == 888]
        ok(len(unk_edits) == 1 and unk_edits[0]["text"] == "confirmation expired",
           "actions: unknown token edited message to 'confirmation expired'")

        # 7. sender outside allowed triggers no execution
        actions.handle_text(
            api3, state3, pending3, 123, "/a reset all", bins, None, clock
        )
        prompt_call_3 = api3.find_calls("sendMessage")[-1]
        prompt_mid_3 = prompt_call_3["message_id"]
        token_3 = prompt_call_3["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
        calls_before = len(api3.calls)

        cb_unauth = {
            "id": "cb_unauth_1",
            "from": {"id": 403},
            "message": {"chat": {"id": 123}, "message_id": prompt_mid_3},
            "data": f"y:{token_3}",
        }
        actions.handle_callback(
            api3, state3, pending3, cb_unauth, frozenset({999}), bins, None, clock
        )
        ok(len(api3.calls) == calls_before,
           "actions: sender outside allowed triggers no execution or api calls")
        with pending3._lock:
            ok(token_3 in pending3._entries, "actions: unauthorized callback left token in pending")

        # 8. expire edits prompts older than CONFIRM_TTL_S and leaves younger ones
        pending_exp = actions.Pending()
        now_ts = 2000.0
        plan_exp = catalog.Plan(tool="archiver", args=("reset", "all"), confirm=True, detach=False, timeout_s=120.0)
        pending_exp.put("tok_old", plan_exp, 123, 701, now_ts - 61.0)
        pending_exp.put("tok_young", plan_exp, 123, 702, now_ts - 10.0)

        actions.expire(api3, pending_exp, now=now_ts)
        exp_edits = [c for c in api3.find_calls("editMessageText") if c["message_id"] == 701]
        ok(len(exp_edits) == 1 and "expired (no answer in 60s): archiver reset all" in exp_edits[0]["text"],
           "actions.expire: edited expired prompt to 'expired (no answer in 60s): ...'")
        young_edits = [c for c in api3.find_calls("editMessageText") if c["message_id"] == 702]
        ok(len(young_edits) == 0, "actions.expire: left younger prompt untouched")
        with pending_exp._lock:
            ok("tok_old" not in pending_exp._entries, "actions.expire: removed expired token")
            ok("tok_young" in pending_exp._entries, "actions.expire: retained active token")

        # 9. detached plan with detached_log=None refuses
        api_det = FakeBotAPI()
        state_det = BotState(Path(tmpdir) / "state_det.sqlite3")
        plan_up = catalog.parse("/update")
        ok(isinstance(plan_up, catalog.Plan), "actions: /update parsed to Plan")
        actions.execute(
            api_det, state_det, 123, plan_up, bins, None, clock
        )
        det_sends = api_det.find_calls("sendMessage")
        ok(len(det_sends) == 1 and det_sends[0]["text"] == "detached commands are not configured",
           "actions: detached plan with detached_log=None refuses")

        # 10. with a log path the reply is sent BEFORE the process starts
        api_det_log = FakeBotAPI()
        det_log_file = Path(tmpdir) / "detached_exec.log"
        order_events: list[str] = []

        orig_start_detached = commands.start_detached
        def spy_start_detached(argv: list[str], log_p: Path) -> subprocess.Popen:
            order_events.append("start_detached")
            return orig_start_detached(["/bin/echo", "hello"], log_p)

        orig_send = api_det_log.send_message
        def spy_send_message(chat_id: int, text: str, reply_markup: dict | None = None) -> int:
            order_events.append("send_message")
            return orig_send(chat_id, text, reply_markup=reply_markup)

        api_det_log.send_message = spy_send_message
        commands.start_detached = spy_start_detached
        try:
            plan_up = catalog.parse("/update")
            ok(isinstance(plan_up, catalog.Plan), "actions: /update parsed to Plan")
            actions.execute(
                api_det_log, state_det, 123, plan_up, bins, det_log_file, clock
            )
        finally:
            commands.start_detached = orig_start_detached

        ok(order_events == ["send_message", "start_detached"],
           "actions: detached command reply is sent BEFORE process starts")


def test_actions_detached_spawn_failure() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        bins = {
            "archiver": "/bin/echo",
            "recorder": "/bin/echo",
            "dispatcher": "/bin/echo",
            "ops": "/bin/echo",
        }
        clock = lambda: 1000.0
        api = FakeBotAPI()
        state = BotState(Path(tmpdir) / "state_fail.sqlite3")
        det_log_file = Path(tmpdir) / "detached_fail.log"

        def fake_start_detached(
            argv: list[str], log_p: Path
        ) -> subprocess.Popen:
            raise OSError("boom")

        orig_start_detached = commands.start_detached
        commands.start_detached = fake_start_detached
        try:
            plan_up = catalog.parse("/update")
            ok(
                isinstance(plan_up, catalog.Plan),
                "actions: /update parsed to Plan",
            )
            actions.execute(
                api, state, 123, plan_up, bins, det_log_file, clock
            )
        finally:
            commands.start_detached = orig_start_detached

        sends = api.find_calls("sendMessage")
        ok(
            len(sends) == 1 and "started in background" in sends[0]["text"],
            "actions: detached spawn failure sent initial start message",
        )
        started_mid = sends[0]["message_id"]

        edits = [
            c
            for c in api.find_calls("editMessageText")
            if c["message_id"] == started_mid
        ]
        ok(
            len(edits) == 1,
            "actions: detached spawn failure edited initial message once",
        )
        edit_text = edits[0]["text"]
        ok(
            edit_text.startswith("✗ failed to start:")
            and "boom" in edit_text,
            "actions: detached spawn failure edit has error marker and boom",
        )


class FakeLoopBotAPI(FakeBotAPI):
    def __init__(self) -> None:
        super().__init__()
        self.pass_count = 0

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        with self.lock:
            self.pass_count += 1
            current_pass = self.pass_count

        if current_pass == 1:
            return [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 10,
                        "date": 1000,
                        "chat": {"id": 123},
                        "from": {"id": 999},
                        "text": "/a reset all",
                    },
                }
            ]
        elif current_pass == 2:
            sends = self.find_calls("sendMessage")
            prompt_call = [s for s in sends if "Run: archiver reset all?" in s["text"]][0]
            markup = prompt_call["reply_markup"]
            token = markup["inline_keyboard"][0][0]["callback_data"].split(":", 1)[1]
            prompt_mid = prompt_call["message_id"]
            return [
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb_loop_pass2",
                        "from": {"id": 999},
                        "message": {"chat": {"id": 123}, "message_id": prompt_mid},
                        "data": f"y:{token}",
                    },
                }
            ]
        return []


def test_loop_run() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        api = FakeLoopBotAPI()
        state = BotState(Path(tmpdir) / "loop_state.sqlite3")
        lock_path = Path(tmpdir) / "bot.lock"
        lock_path.touch()
        bins = {
            "archiver": "/bin/echo",
            "recorder": "/bin/echo",
            "dispatcher": "/bin/echo",
            "ops": "/bin/echo",
        }

        loop.run(
            api,
            state,
            allowed=frozenset({999}),
            lock_path=lock_path,
            bins=bins,
            detached_log=None,
            poll_interval_s=0.01,
            clock=lambda: 1000.0,
            should_stop=lambda: api.pass_count >= 2,
        )

        # 1. set_my_commands called once
        set_cmds = api.find_calls("setMyCommands")
        ok(len(set_cmds) == 1, "loop.run: set_my_commands called exactly once")

        # 2. callback_query update reached confirm flow and executed
        answers = [c for c in api.find_calls("answerCallbackQuery") if c["callback_query_id"] == "cb_loop_pass2"]
        ok(len(answers) == 1 and answers[0]["text"] is None,
           "loop.run: answer_callback_query called for callback update")

        deadline = time.time() + 5.0
        finished = False
        while time.time() < deadline:
            edits = [c for c in api.find_calls("editMessageText") if "✓ ok: archiver reset all" in c["text"]]
            if edits:
                finished = True
                break
            time.sleep(0.02)
        ok(finished, "loop.run: callback_query y: executed confirmed command to ✓ ok")


def main() -> int:
    test_catalog_parse()
    test_catalog_bot_commands()
    test_bot_api_additions()
    test_commands_start_detached()
    test_actions()
    test_actions_detached_spawn_failure()
    test_loop_run()
    print(f"\nAll {_checks} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
