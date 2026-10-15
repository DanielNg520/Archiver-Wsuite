"""catalog
────
Command catalog, parsing, and classification for the Telegram bot.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

TOOLS: dict[str, str] = {
    "a": "archiver",
    "r": "recorder",
    "d": "dispatcher",
    "o": "ops",
}


@dataclass(frozen=True)
class Plan:
    tool: str
    args: tuple[str, ...]
    confirm: bool
    detach: bool
    timeout_s: float


@dataclass(frozen=True)
class _Alias:
    tool: str
    leading_args: tuple[str, ...]
    allow_extra: bool


_ALIASES: dict[str, _Alias] = {
    "health": _Alias("ops", ("health",), False),
    "qstatus": _Alias("dispatcher", ("status",), False),
    "stats": _Alias("archiver", ("stats",), False),
    "rstatus": _Alias("recorder", ("status",), False),
    "queue": _Alias("archiver", ("queue", "list"), True),
    "restart": _Alias("ops", ("restart",), True),
    "update": _Alias("ops", ("update",), False),
}

_PHASE1_COMMANDS: frozenset[str] = frozenset({
    "record",
    "recstatus",
    "ar_add",
    "ar-add",
    "re_add",
    "re-add",
    "remove",
    "list",
})

_CMD_RE = re.compile(
    r"^/([a-zA-Z0-9_-]+)(?:@[a-zA-Z0-9_]+)?(?:\s+(.*))?$",
    re.DOTALL,
)

_SAFE_PREFIXES: dict[str, tuple[tuple[str, ...], ...]] = {
    "archiver": (
        ("stats",),
        ("health",),
        ("queue", "list"),
        ("config", "list"),
        ("config", "add"),
        ("local", "list"),
        ("local", "add"),
        ("banned", "list"),
        ("cookies", "list"),
    ),
    "recorder": (
        ("status",),
        ("stats",),
        ("config", "list"),
        ("config", "add"),
        ("config", "manual-add"),
        ("banned", "list"),
    ),
    "dispatcher": (
        ("status",),
        ("stats",),
        ("check-routes",),
        ("queue", "list"),
        ("config", "show"),
        ("config", "get"),
        ("config", "list"),
        ("banned-words", "list"),
        ("banned-words", "add"),
        ("burner", "status"),
        ("burner", "chats"),
    ),
    "ops": (
        ("health",),
    ),
}

_SHOW_ONLY_VERBS: dict[str, frozenset[str]] = {
    "archiver": frozenset({
        "auto-ingest",
        "auto-sort",
        "auto-retry",
        "download",
        "platform",
        "run-settings",
        "policy",
        "dedup-policy",
        "safebrake",
        "priority",
        "deleting",
        "banned",
    }),
    "recorder": frozenset({
        "banned",
        "config",
    }),
    "dispatcher": frozenset({
        "config",
    }),
}


def _strip_leading_options(args: tuple[str, ...]) -> tuple[str, ...]:
    for idx, arg in enumerate(args):
        if not arg.startswith("-"):
            return args[idx:]
    return ()


def _is_detach(tool: str, args: tuple[str, ...]) -> bool:
    if tool != "ops" or not args:
        return False
    verb = args[0]
    if verb in ("install", "uninstall", "update"):
        return True
    if verb in ("load", "unload"):
        target = args[1] if len(args) > 1 else None
        return target is None or target == "bot"
    if verb == "restart":
        target = args[1] if len(args) > 1 else None
        return target == "bot"
    return False


def _timeout_s(tool: str, args: tuple[str, ...], detach: bool) -> float:
    if detach:
        return 0.0
    if not args:
        return 120.0
    verb = args[0]
    if tool == "archiver" and verb in (
        "run",
        "stories",
        "reconcile",
        "dedup",
        "backfill",
        "bootstrap",
        "purge-sent",
    ):
        return 3600.0
    if tool == "archiver" and verb == "health":
        return 600.0
    if tool == "dispatcher" and verb == "check-routes":
        return 600.0
    return 120.0


def parse(text: str) -> Plan | str | None:
    if not text.startswith("/"):
        return None

    match = _CMD_RE.match(text)
    if not match:
        return None

    name = match.group(1).lower()
    if name in _PHASE1_COMMANDS:
        return None

    raw_args = match.group(2)
    if raw_args is None or not raw_args.strip():
        tokens: list[str] = []
    else:
        try:
            tokens = shlex.split(raw_args)
        except ValueError as err:
            return f"Command parsing error: {err}"

    if name in TOOLS:
        tool = TOOLS[name]
        cli_args = tuple(tokens) if tokens else ("--help",)
    elif name in _ALIASES:
        alias = _ALIASES[name]
        if not alias.allow_extra and tokens:
            return f"Usage: /{name} takes no additional arguments."
        tool = alias.tool
        cli_args = alias.leading_args + tuple(tokens)
    else:
        return None

    view = _strip_leading_options(cli_args)

    if any(arg in ("-h", "--help") for arg in cli_args):
        confirm = False
    elif tool == "recorder" and view and view[0] == "record":
        return (
            "Refused: 'recorder record' is not supported via bot. "
            "Use /record <handle> instead."
        )
    elif tool == "archiver" and view and view[0] in ("start", "loop"):
        return (
            f"Refused: 'archiver {view[0]}' runs forever or requires "
            "an interactive terminal."
        )
    elif tool == "recorder" and view and view[0] in ("start", "watch"):
        return (
            f"Refused: 'recorder {view[0]}' runs forever or requires "
            "an interactive terminal."
        )
    elif tool == "dispatcher" and view and view[0] == "start":
        return (
            "Refused: 'dispatcher start' runs forever or requires "
            "an interactive terminal."
        )
    elif tool == "ops" and view and view[0] in ("watch", "bot"):
        return (
            f"Refused: 'ops {view[0]}' runs forever or requires "
            "an interactive terminal."
        )
    elif (
        tool == "dispatcher"
        and len(view) >= 2
        and view[0] == "burner"
        and view[1] == "login"
    ):
        return (
            "Refused: 'dispatcher burner login' runs forever or requires "
            "an interactive terminal."
        )
    else:
        if tool == "ops" and view and view[0] == "update":
            if "--no-watch" not in cli_args:
                cli_args = cli_args + ("--no-watch",)
                view = view + ("--no-watch",)

        matched_safe = False
        for prefix in _SAFE_PREFIXES.get(tool, ()):
            if len(view) >= len(prefix) and view[: len(prefix)] == prefix:
                matched_safe = True
                break

        if matched_safe:
            confirm = False
        elif (
            len(view) == 1
            and view[0] in _SHOW_ONLY_VERBS.get(tool, frozenset())
        ):
            confirm = False
        else:
            confirm = True

    detach = _is_detach(tool, view)
    timeout_s = _timeout_s(tool, view, detach)

    return Plan(
        tool=tool,
        args=cli_args,
        confirm=confirm,
        detach=detach,
        timeout_s=timeout_s,
    )


def bot_commands() -> list[tuple[str, str]]:
    return [
        ("record", "Start recording an account"),
        ("recstatus", "Show recording status"),
        ("ar_add", "Add account to archiver"),
        ("re_add", "Add account to recorder"),
        ("remove", "Remove account from monitoring"),
        ("list", "List monitored accounts"),
        ("health", "System health report"),
        ("qstatus", "Dispatcher queue status"),
        ("stats", "Archiver statistics"),
        ("rstatus", "Recorder worker status"),
        ("queue", "List archiver queue"),
        ("restart", "Restart a service worker"),
        ("update", "Update codebase and reload"),
        ("a", "Run archiver CLI command"),
        ("r", "Run recorder CLI command"),
        ("d", "Run dispatcher CLI command"),
        ("o", "Run ops CLI command"),
    ]
