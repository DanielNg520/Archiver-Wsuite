"""loop
────
Main loop and background runner for the Telegram control bot.
"""

import logging
import subprocess
import threading
import time
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any

import core.heartbeat
from . import commands
from .telegram import BotAPI, TelegramError

DELETE_AFTER_S = 24 * 3600
# Retried once per loop pass so a Telegram outage never blocks polling.
BROADCAST_TRIES = 5

log = logging.getLogger(__name__)


def _send_and_track(
    api: Any,
    state: Any,
    chat_id: int,
    text: str,
    clock: Callable[[], float],
) -> int:
    msg_id = api.send_message(chat_id, text)
    state.track(chat_id, msg_id, clock())
    return msg_id


def _format_failure(action: str, rc: int, out: str) -> str:
    tail = out[-500:]
    if tail:
        return f"{action} failed (exit {rc}): {tail}"
    return f"{action} failed (exit {rc})"


def _wait_record_thread(
    api: Any,
    state: Any,
    proc: subprocess.Popen,
    log_file: typing.IO[bytes],
    chat_id: int,
    handle: str,
    clock: Callable[[], float],
) -> None:
    rc, out = commands.wait_record(proc, log_file)
    if rc == 0:
        return
    if rc == 3:
        reply = f"@{handle} is not live right now."
    else:
        reply = _format_failure("record", rc, out)

    _send_with_retry(api, state, chat_id, reply, clock)


def _send_with_retry(
    api: Any,
    state: Any,
    chat_id: int,
    text: str,
    clock: Callable[[], float],
) -> int | None:
    delay = 1.0
    for attempt in range(5):
        try:
            return _send_and_track(api, state, chat_id, text, clock)
        except TelegramError as err:
            log.warning(
                "Record waiter send failed (attempt %d/5): %s",
                attempt + 1,
                err,
            )
            if attempt < 4:
                time.sleep(min(delay, 30.0))
                delay *= 2.0
    return None


def _handle_command(
    api: Any,
    state: Any,
    chat_id: int,
    cmd_name: str,
    arg: str | None,
    bins: dict[str, str],
    lock_path: Path,
    clock: Callable[[], float],
) -> None:
    if cmd_name == "status":
        live_data = core.heartbeat.read_live(lock_path)
        if live_data and live_data.get("username"):
            reply = f"Recording: @{live_data['username']}"
        else:
            reply = "Not recording anyone."
        _send_and_track(api, state, chat_id, reply, clock)
        return

    handle = arg.strip().lstrip("@") if arg else ""

    if cmd_name == "ar_add":
        argv = [
            bins["archiver"],
            "config",
            "add",
            "--platform",
            "tiktok",
            "--user",
            handle,
        ]
        rc, out = commands.run_cli(argv)
        if rc == 0:
            reply = f"Added @{handle} to archiver (tiktok)."
        else:
            reply = _format_failure("ar_add", rc, out)
        _send_and_track(api, state, chat_id, reply, clock)
    elif cmd_name == "re_add":
        argv = [bins["recorder"], "config", "add", "--user", handle]
        rc, out = commands.run_cli(argv)
        if rc == 0:
            reply = f"Added @{handle} to recorder."
        else:
            reply = _format_failure("re_add", rc, out)
        _send_and_track(api, state, chat_id, reply, clock)
    elif cmd_name == "remove":
        argv = [bins["recorder"], "config", "remove", "--user", handle]
        rc, out = commands.run_cli(argv)
        if rc == 0:
            reply = f"Removed @{handle} from recorder."
        else:
            reply = _format_failure("remove", rc, out)
        _send_and_track(api, state, chat_id, reply, clock)
    elif cmd_name == "list":
        argv = [bins["recorder"], "config", "list"]
        rc, out = commands.run_cli(argv)
        if rc == 0:
            stripped = out.strip()
            reply = stripped[:4000] if stripped else "(empty)"
        else:
            reply = _format_failure("list", rc, out)
        _send_and_track(api, state, chat_id, reply, clock)
    elif cmd_name == "record":
        proc, log_file = commands.start_record(bins["recorder"], handle)
        for _ in range(20):
            if proc.poll() is not None:
                break
            live = core.heartbeat.read_live(lock_path)
            if live and live.get("username") == handle:
                break
            time.sleep(0.5)

        if proc.poll() is not None:
            rc, out = commands.wait_record(proc, log_file)
            if rc == 0:
                reply = f"Recorded @{handle}."
            elif rc == 3:
                reply = f"@{handle} is not live right now."
            else:
                reply = _format_failure("record", rc, out)
            _send_and_track(api, state, chat_id, reply, clock)
        else:
            thread = threading.Thread(
                target=_wait_record_thread,
                args=(api, state, proc, log_file, chat_id, handle, clock),
                daemon=True,
            )
            thread.start()


def _process_updates(
    api: Any,
    state: Any,
    allowed: frozenset[int],
    bins: dict[str, str],
    lock_path: Path,
    clock: Callable[[], float],
) -> None:
    updates = api.get_updates(state.get_offset())
    for update in updates:
        update_id = update.get("update_id")
        if update_id is not None:
            state.set_offset(update_id + 1)
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        sender = message.get("from")
        if not isinstance(sender, dict) or sender.get("id") not in allowed:
            continue
        text = message.get("text", "")
        chat_id = message["chat"]["id"]
        message_id = message["message_id"]
        parsed = commands.parse_command(text)
        if parsed is None:
            hint = commands.usage_hint(text)
            if hint is None:
                continue
            state.track(chat_id, message_id, float(message["date"]))
            _send_and_track(api, state, chat_id, hint, clock)
            continue
        state.track(chat_id, message_id, float(message["date"]))
        cmd_name, arg = parsed
        _handle_command(
            api, state, chat_id, cmd_name, arg, bins, lock_path, clock
        )


def _broadcast(
    api: Any,
    state: Any,
    chats: frozenset[int],
    current_user: str | None,
    prev_user: str | None,
    clock: Callable[[], float],
) -> frozenset[int]:
    failed: set[int] = set()
    if current_user is not None:
        text = f"\U0001F534 recording started: @{current_user}"
        for chat_id in chats:
            try:
                prev_msg_id = state.get_report(chat_id)
                if prev_msg_id is not None:
                    try:
                        api.delete_message(chat_id, prev_msg_id)
                    except TelegramError:
                        pass
                    state.forget(chat_id, prev_msg_id)
                msg_id = _send_and_track(
                    api, state, chat_id, text, clock
                )
                if msg_id is not None:
                    state.set_report(chat_id, msg_id)
            except TelegramError as err:
                failed.add(chat_id)
                log.warning(
                    "Broadcast start failed for chat %d: %s", chat_id, err
                )
    elif prev_user is not None:
        text = f"⚪ recording ended: @{prev_user}"
        for chat_id in chats:
            try:
                msg_id = _send_and_track(
                    api, state, chat_id, text, clock
                )
                if msg_id is not None:
                    state.set_report(chat_id, msg_id)
            except TelegramError as err:
                failed.add(chat_id)
                log.warning(
                    "Broadcast end failed for chat %d: %s", chat_id, err
                )
    return frozenset(failed)


def _sweep_deletions(
    api: Any,
    state: Any,
    now: float,
) -> None:
    for chat_id, message_id, sent_at in state.due(now, DELETE_AFTER_S):
        try:
            api.delete_message(chat_id, message_id)
            state.forget(chat_id, message_id)
        except TelegramError:
            if now - sent_at >= 47 * 3600:
                state.forget(chat_id, message_id)


def run(
    api: BotAPI,
    state: Any,
    *,
    allowed: frozenset[int],
    lock_path: Path,
    bins: dict[str, str],
    poll_interval_s: float = 15.0,
    clock: Callable[[], float] = time.time,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    prev_user: str | None = None
    last_sweep: float | None = None
    pending: tuple[str | None, str | None, frozenset[int]] | None = None
    pending_tries = 0

    while not should_stop():
        try:
            _process_updates(api, state, allowed, bins, lock_path, clock)

            live_data = core.heartbeat.read_live(lock_path)
            current_user = (
                live_data.get("username")
                if isinstance(live_data, dict)
                else None
            )
            if current_user != prev_user:
                pending = (current_user, prev_user, allowed)
                pending_tries = 0
                prev_user = current_user

            if pending is not None:
                pending_tries += 1
                failed = _broadcast(
                    api, state, pending[2], pending[0], pending[1], clock
                )
                if failed and pending_tries < BROADCAST_TRIES:
                    pending = (pending[0], pending[1], failed)
                else:
                    if failed:
                        log.warning(
                            "Broadcast gave up for chats: %s",
                            sorted(failed),
                        )
                    pending = None

            now = clock()
            if last_sweep is None or now - last_sweep >= 60.0:
                _sweep_deletions(api, state, now)
                last_sweep = now
        except Exception as exc:
            log.warning("%s: %s", type(exc).__name__, exc)
            time.sleep(poll_interval_s)
