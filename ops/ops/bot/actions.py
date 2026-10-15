"""actions
───────
Catalog command execution, confirmation flow, and background dispatch.
"""

from __future__ import annotations

import logging
import secrets
import shlex
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import catalog, commands
from .telegram import TelegramError

CONFIRM_TTL_S = 60.0

log = logging.getLogger(__name__)


def _command_str(plan: catalog.Plan) -> str:
    return shlex.join([plan.tool, *plan.args])


def _send_or_edit(
    api: Any,
    state: Any,
    chat_id: int,
    text: str,
    clock: Callable[[], float],
    message_id: int | None = None,
) -> int:
    if message_id is not None:
        api.edit_message_text(chat_id, message_id, text)
        return message_id
    return send_and_track(api, state, chat_id, text, clock)


def send_and_track(
    api: Any,
    state: Any,
    chat_id: int,
    text: str,
    clock: Callable[[], float],
    reply_markup: dict | None = None,
) -> int:
    if reply_markup is not None:
        msg_id = api.send_message(chat_id, text, reply_markup=reply_markup)
    else:
        msg_id = api.send_message(chat_id, text)
    state.track(chat_id, msg_id, clock())
    return msg_id


class Pending:
    """In-memory confirmations awaiting Yes/No, thread-safe."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[catalog.Plan, int, int, float]] = {}
        self._lock = threading.Lock()

    def put(
        self,
        token: str,
        plan: catalog.Plan,
        chat_id: int,
        message_id: int,
        now: float,
    ) -> None:
        with self._lock:
            self._entries[token] = (plan, chat_id, message_id, now)

    def pop(
        self, token: str
    ) -> tuple[catalog.Plan, int, int, float] | None:
        with self._lock:
            return self._entries.pop(token, None)

    def expired(
        self, now: float
    ) -> list[tuple[catalog.Plan, int, int, float]]:
        with self._lock:
            expired_entries: list[tuple[catalog.Plan, int, int, float]] = []
            expired_tokens: list[str] = []
            for token, entry in self._entries.items():
                _, _, _, created_at = entry
                if (now - created_at) >= CONFIRM_TTL_S:
                    expired_entries.append(entry)
                    expired_tokens.append(token)
            for token in expired_tokens:
                del self._entries[token]
            return expired_entries


def handle_text(
    api: Any,
    state: Any,
    pending: Pending,
    chat_id: int,
    text: str,
    bins: dict[str, str],
    detached_log: Path | None,
    clock: Callable[[], float],
) -> bool:
    parsed = catalog.parse(text)
    if parsed is None:
        return False

    if isinstance(parsed, str):
        send_and_track(api, state, chat_id, parsed, clock)
        return True

    if parsed.confirm:
        token = secrets.token_hex(8)
        cmd = _command_str(parsed)
        prompt = f"Run: {cmd}?"
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Yes", "callback_data": f"y:{token}"},
                    {"text": "No", "callback_data": f"n:{token}"},
                ]
            ]
        }
        msg_id = send_and_track(
            api, state, chat_id, prompt, clock, reply_markup=markup
        )
        pending.put(token, parsed, chat_id, msg_id, clock())
        return True

    execute(api, state, chat_id, parsed, bins, detached_log, clock)
    return True


def handle_callback(
    api: Any,
    state: Any,
    pending: Pending,
    callback: dict,
    allowed: frozenset[int],
    bins: dict[str, str],
    detached_log: Path | None,
    clock: Callable[[], float],
) -> None:
    sender = callback.get("from")
    sender_id = sender.get("id") if isinstance(sender, dict) else None
    if sender_id not in allowed:
        return

    cb_id = str(callback.get("id", ""))
    data = callback.get("data")
    msg = callback.get("message")
    chat_id = None
    msg_id = None
    if isinstance(msg, dict):
        chat = msg.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        msg_id = msg.get("message_id")

    action = ""
    token = ""
    if isinstance(data, str) and ":" in data:
        action, _, token = data.partition(":")

    popped = pending.pop(token) if token and action in ("y", "n") else None

    if popped is None:
        if cb_id:
            try:
                api.answer_callback_query(cb_id, text="expired")
            except TelegramError as err:
                log.warning("Failed to answer callback query %s: %s", cb_id, err)
        if chat_id is not None and msg_id is not None:
            try:
                api.edit_message_text(chat_id, msg_id, "confirmation expired")
            except TelegramError as err:
                log.warning(
                    "Failed to edit expired confirmation message %d: %s",
                    msg_id,
                    err,
                )
        return

    plan, orig_chat_id, orig_msg_id, _ = popped
    target_chat = chat_id if chat_id is not None else orig_chat_id
    target_msg = msg_id if msg_id is not None else orig_msg_id

    if cb_id:
        try:
            api.answer_callback_query(cb_id)
        except TelegramError as err:
            log.warning("Failed to answer callback query %s: %s", cb_id, err)

    cmd = _command_str(plan)
    if action == "n":
        try:
            api.edit_message_text(target_chat, target_msg, f"cancelled: {cmd}")
        except TelegramError as err:
            log.warning("Failed to edit cancelled prompt %d: %s", target_msg, err)
    elif action == "y":
        try:
            execute(
                api,
                state,
                target_chat,
                plan,
                bins,
                detached_log,
                clock,
                message_id=target_msg,
            )
        except TelegramError as err:
            log.warning("Failed executing confirmed plan: %s", err)


def expire(api: Any, pending: Pending, now: float) -> None:
    for plan, chat_id, message_id, _ in pending.expired(now):
        cmd = _command_str(plan)
        try:
            api.edit_message_text(
                chat_id,
                message_id,
                f"expired (no answer in 60s): {cmd}",
            )
        except TelegramError as err:
            log.warning(
                "Failed to edit expired confirm prompt %d in chat %d: %s",
                message_id,
                chat_id,
                err,
            )


def execute(
    api: Any,
    state: Any,
    chat_id: int,
    plan: catalog.Plan,
    bins: dict[str, str],
    detached_log: Path | None,
    clock: Callable[[], float],
    message_id: int | None = None,
) -> None:
    cmd = _command_str(plan)
    if plan.tool not in bins:
        _send_or_edit(
            api,
            state,
            chat_id,
            f"{plan.tool} CLI not found",
            clock,
            message_id,
        )
        return

    if plan.detach:
        if detached_log is None:
            _send_or_edit(
                api,
                state,
                chat_id,
                "detached commands are not configured",
                clock,
                message_id,
            )
            return
        _send_or_edit(
            api,
            state,
            chat_id,
            f"started in background: {cmd} (log: {detached_log})",
            clock,
            message_id,
        )
        commands.start_detached([bins[plan.tool], *plan.args], detached_log)
        return

    target_msg_id = _send_or_edit(
        api,
        state,
        chat_id,
        f"running: {cmd}",
        clock,
        message_id,
    )

    def _worker() -> None:
        try:
            rc, out = commands.run_cli(
                [bins[plan.tool], *plan.args], plan.timeout_s
            )
            stripped = out.strip()
            tail = stripped[-3500:] if stripped else "(no output)"
            if rc == 0:
                result_text = f"✓ ok: {cmd}\n{tail}"
            else:
                result_text = f"✗ failed (exit {rc}): {cmd}\n{tail}"

            try:
                api.edit_message_text(chat_id, target_msg_id, result_text)
            except TelegramError as edit_err:
                log.warning(
                    "Failed to edit final result message %d in chat %d: %s",
                    target_msg_id,
                    chat_id,
                    edit_err,
                )
                try:
                    send_and_track(
                        api, state, chat_id, result_text, clock
                    )
                except TelegramError as send_err:
                    log.warning(
                        "Failed fallback send_and_track in chat %d: %s",
                        chat_id,
                        send_err,
                    )
        except Exception as exc:
            log.exception(
                "Unexpected error in background runner for %s: %s",
                cmd,
                exc,
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
