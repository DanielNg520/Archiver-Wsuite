"""
recorder.cli
────────────
  recorder start                         foreground (watch the priority list)
  recorder start --daemon                deprecated no-op (use `ops install`)
  recorder record --user <u|alias>       ONE-SHOT: if the user is live, record
                                         (auto `ops load recorder` on exit; --no-reload to skip)
                                         it once and exit (no listening loop)
  recorder stop                          terminate via pid file
  recorder status                        state + queue depth + lock
  recorder config add --user <u> [--alias <name>]
  recorder config remove --user <u|alias>
  recorder config list                   both lists, with aliases
  recorder config priority --user <u|alias> --rank N
  recorder config alias --user <u|alias> --alias <name>
  recorder config manual-add --user <u> [--alias <name>]
  recorder config manual-remove --user <u|alias>

config writes go to ~/.config/recorder/config.toml (the priority-ordered
user list). The ordering of the `users` array IS the priority.

Aliases (username → display name) are stamped into the upload caption and
accepted anywhere a `--user` value is given. The `manual` roster is a second
list that is NEVER polled — it just keeps alias-bearing accounts on hand for
`recorder record --user <alias>`.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import tomli_w

from core import ItemStore
from core import cli as core_cli
from core import heartbeat
from core.platform import procgroup as _procgroup
from core.platform import signals as _signals

from . import ui, watch
from .config import CONFIG_TOML, RecorderConfig

log = logging.getLogger(__name__)


def cmd_stats(args: argparse.Namespace) -> int:
    """Shared `stats` noun — counts from the one suite DB, identical output to
    archiver/dispatcher `stats`."""
    config = RecorderConfig.load()
    store = ItemStore.open(config.db_path)
    try:
        return core_cli.handle_stats(store, args)
    finally:
        store.close()


def _setup_logging(verbose: bool) -> None:
    ui.setup_logging(verbose)


def _pid_path(config: RecorderConfig) -> Path:
    return Path(config.state_dir).expanduser() / "pid"


def _resolve_ops_bin() -> str | None:
    """Absolute path to the `ops` CLI. Prefer PATH (pipx puts it there), fall
    back to ~/.local/bin/ops. Mirrors ops.cli._resolve_bin so a manual record
    can hand the recorder service back to the OS service manager on exit."""
    found = shutil.which("ops")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "ops"
    return str(fallback) if fallback.exists() else None


def _reload_recorder_service() -> None:
    """After a one-shot manual record ends, bring the automated recorder back
    up via `ops load recorder`. Manual mode only runs when the service is NOT
    running (cmd_record bails otherwise), so on exit the listening recorder is
    down until someone reloads it — this closes that gap automatically.

    Best-effort: a missing `ops`, or a machine where the tasks were never
    installed, must never turn a good recording into a failure. We only log."""
    ops_bin = _resolve_ops_bin()
    if ops_bin is None:
        log.warning("manual record done, but 'ops' not found on PATH — "
                    "run `ops load recorder` yourself to resume the service")
        return
    log.info("manual record done — reloading the recorder service "
             "(`ops load recorder`)", extra={"ev": "reload"})
    try:
        rc = subprocess.run([ops_bin, "load", "recorder"]).returncode
        if rc != 0:
            log.warning("`ops load recorder` exited %d — resume the service "
                        "manually if it did not come back up", rc)
    except OSError as e:
        log.warning("could not reload recorder service: %s", e)


def _recorder_running(pid_path: Path) -> bool:
    """True iff the pid file names a live process. Liveness via the suite's one
    primitive (core.heartbeat.pid_alive): a dead/stale pid reads false, one alive
    but owned by another user reads true."""
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return False
    return heartbeat.pid_alive(pid)


# ── start ─────────────────────────────────────────────────────────────────

def cmd_start(args: argparse.Namespace) -> int:
    config = RecorderConfig.load()
    if not config.tiktok_users:
        log.error("no tiktok users configured. Add some: "
                  "recorder config add --user <username>")
        return 1

    # Lazy imports: only needed to actually run, and they pull in
    # TikTokLive / subprocess machinery.
    from .capture import StreamCapture
    from .enqueue import EnqueueClient
    from .lock import TikTokLock
    from .platforms.tiktok import TikTokLivePlatform
    from .state import StateMachine

    if args.daemon:
        # Backgrounding is the service manager's job (see `ops install`), not a
        # hand-rolled double-fork — which POSIX-forked and did not exist on
        # Windows at all. Kept as an accepted no-op so old invocations/scripts
        # don't break; it just runs in the foreground.
        log.warning("--daemon is a no-op; use `ops install` + `ops load` to run "
                    "the recorder under the OS service manager. Running in "
                    "foreground.")

    ui.banner(config)

    pid_path = _pid_path(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    # Startup reconciliation: clear stale logs, requeue un-uploaded recordings,
    # delete already-uploaded leftovers, prune empty folders. Best-effort — a
    # failed sweep must never stop the recorder from coming up.
    from .startup_sweep import sweep
    try:
        report = sweep(config.output_dir, config.db_path,
                       split_threshold_bytes=config.split_threshold_bytes)
        log.info("startup sweep — %s", report, extra={"ev": "sweep"})
    except Exception as e:
        log.warning("startup sweep skipped after error: %s", e)

    platform = TikTokLivePlatform(config.tiktok_cookies_file, config.state_dir)
    capture  = StreamCapture(config.output_dir, config.tiktok_cookies_file)
    enqueue_client = EnqueueClient(
        config.db_path, split_threshold_bytes=config.split_threshold_bytes)
    lock = TikTokLock(config.lock_path, os.getpid())

    def _enqueue(platform_name, username, file_path, caption,
                 group_key=None, alias=None):
        enqueue_client.enqueue(
            platform=platform_name, username=username,
            file_path=file_path, caption=caption, group_key=group_key,
            alias=alias,
        )

    machine = StateMachine(config, platform, capture, _enqueue, lock)

    def _on_signal(signum, _frame):
        # First signal → graceful stop. A second one (the user mashing Ctrl-C
        # because an in-flight network probe / browser fallback hasn't yielded
        # yet) → hard exit, so the terminal is always recoverable.
        if machine._stop.is_set():
            log.warning("second signal — forcing exit", extra={"ev": "stop"})
            os._exit(130)
        log.info("signal %s — requesting stop (Ctrl-C again to force)", signum,
                 extra={"ev": "stop"})
        machine.request_stop()

    _signals.install_sync(_on_signal)

    try:
        machine.run_forever()
    finally:
        pid_path.unlink(missing_ok=True)
    return 0


# ── record (one-shot manual mode) ──────────────────────────────────────────

def cmd_record(args: argparse.Namespace) -> int:
    """Manual one-shot: check whether one username is live and, if so, record
    that stream to the end, then exit. No listening loop, no priority list —
    just this user, just once.

    Exit codes: 0 recorded, 1 conflict/error, 3 user not live.
    """
    config = RecorderConfig.load()
    # Accept an alias (from either roster) as well as a raw username.
    username = _resolve_user(_load_toml(), args.user)

    # A running recorder owns the TikTok lockfile; a second one would stomp it
    # (the lock is a soft signal file, not a mutex) and could double-record.
    # Manual mode is for ad-hoc grabs when the service isn't running.
    pid_path = _pid_path(config)
    if _recorder_running(pid_path):
        log.error("a recorder is already running (pid file %s). Manual record "
                  "would conflict with its TikTok lock — `recorder stop` first, "
                  "or add @%s to the list and let the service catch it.",
                  pid_path, username)
        return 1

    from .capture import StreamCapture
    from .enqueue import EnqueueClient
    from .lock import TikTokLock
    from .platforms.tiktok import TikTokLivePlatform
    from .state import StateMachine

    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))

    platform = TikTokLivePlatform(config.tiktok_cookies_file, config.state_dir)
    capture  = StreamCapture(config.output_dir, config.tiktok_cookies_file)
    enqueue_client = EnqueueClient(
        config.db_path, split_threshold_bytes=config.split_threshold_bytes)
    lock = TikTokLock(config.lock_path, os.getpid())

    def _enqueue(platform_name, username, file_path, caption,
                 group_key=None, alias=None):
        enqueue_client.enqueue(
            platform=platform_name, username=username,
            file_path=file_path, caption=caption, group_key=group_key,
            alias=alias,
        )

    machine = StateMachine(config, platform, capture, _enqueue, lock)

    def _on_signal(signum, _frame):
        # First signal → graceful stop; second → hard exit (see cmd_start).
        if machine._stop.is_set():
            log.warning("second signal — forcing exit", extra={"ev": "stop"})
            os._exit(130)
        log.info("signal %s — requesting stop (Ctrl-C again to force)", signum,
                 extra={"ev": "stop"})
        machine.request_stop()

    _signals.install_sync(_on_signal)

    try:
        recorded = machine.record_once(username)
    finally:
        pid_path.unlink(missing_ok=True)
        if not args.no_reload:
            _reload_recorder_service()
    if recorded:
        return 0
    print(f"@{username} is not live — nothing recorded")
    return 3


# ── stop ──────────────────────────────────────────────────────────────────

def cmd_stop(args: argparse.Namespace) -> int:
    config = RecorderConfig.load()
    pid_path = _pid_path(config)
    if not pid_path.exists():
        log.error("no pid file at %s — recorder not running?", pid_path)
        return 1
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        log.error("pid file unreadable: %s", pid_path)
        return 1
    if not heartbeat.pid_alive(pid):
        log.warning("pid %d not running — clearing stale pid file", pid)
        pid_path.unlink(missing_ok=True)
        return 1
    if _procgroup.terminate_pid(pid):
        log.info("requested stop of recorder pid=%d", pid)
        return 0
    log.error("could not signal recorder pid=%d", pid)
    return 1


# ── status ────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    config = RecorderConfig.load()
    pid_path = _pid_path(config)

    state_line, accent = "not running", "dim"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            if not heartbeat.pid_alive(pid):
                raise ProcessLookupError
            state_line, accent = f"running · pid {pid}", "green"
        except (OSError, ValueError, ProcessLookupError):
            state_line, accent = "not running (stale pid file)", "yellow"

    lock_held = Path(config.lock_path).expanduser().exists()
    roster = "  ".join(_label(u, config.tiktok_aliases)
                       for u in config.tiktok_users) or "(none)"

    print()
    ui.field("recorder", state_line, accent=accent)
    ui.field("recording", "yes — tiktok.lock held" if lock_held else "no",
             accent="green" if lock_held else None)
    ui.field("users", roster)
    if config.tiktok_manual_users:
        manual = "  ".join(_label(u, config.tiktok_aliases)
                           for u in config.tiktok_manual_users)
        ui.field("manual", manual)
    ui.field("output", str(config.output_dir))
    ui.field("queue", str(config.db_path))
    print()
    return 0


# ── watch ─────────────────────────────────────────────────────────────────

def cmd_watch(args: argparse.Namespace) -> int:
    """Live dashboard: clear + re-render every `interval` seconds, the same
    loop as `ops watch`. Successive snapshots are diffed to show the active
    recording's live write-rate."""
    config = RecorderConfig.load()
    prev: tuple[str, int, float] | None = None    # (path, size, wall-clock)
    try:
        while True:
            snap = watch.snapshot(config)
            now = time.monotonic()
            rate = None
            if snap.active and prev and prev[0] == snap.active.path:
                dt = now - prev[2]
                if dt > 0:
                    rate = max(0.0, (snap.active.size - prev[1]) / dt)
            prev = (snap.active.path, snap.active.size, now) if snap.active else None

            print("\033[2J\033[H", end="")        # clear screen, home cursor
            print(f"recorder watch  ({time.strftime('%H:%M:%S')})\n")
            print(watch.render(snap, rate_bps=rate))
            footer = f"refreshing every {args.interval:.0f}s · Ctrl-C to exit"
            print(f"\n  {ui._paint(footer, 'dim', on=ui.color_enabled())}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


# ── config ────────────────────────────────────────────────────────────────

def _load_toml() -> dict:
    if CONFIG_TOML.exists():
        with CONFIG_TOML.open("rb") as f:
            return tomllib.load(f)
    return {}


def _save_toml(data: dict) -> None:
    CONFIG_TOML.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_TOML.with_suffix(".toml.tmp")
    with tmp.open("wb") as f:
        f.write(tomli_w.dumps(data).encode("utf-8"))
    os.replace(tmp, CONFIG_TOML)


def _get_users(data: dict) -> list[str]:
    return list(data.get("recorder", {}).get("tiktok", {}).get("users", []))


def _set_users(data: dict, users: list[str]) -> None:
    data.setdefault("recorder", {}).setdefault("tiktok", {})["users"] = users


def _get_manual(data: dict) -> list[str]:
    """The secondary, never-polled roster (manual on-demand recording)."""
    return list(data.get("recorder", {}).get("tiktok", {}).get("manual", []))


def _set_manual(data: dict, users: list[str]) -> None:
    data.setdefault("recorder", {}).setdefault("tiktok", {})["manual"] = users


def _get_aliases(data: dict) -> dict[str, str]:
    """username → display alias, covering both rosters."""
    a = data.get("recorder", {}).get("tiktok", {}).get("aliases", {})
    return dict(a) if isinstance(a, dict) else {}


def _set_alias(data: dict, username: str, alias: str | None) -> None:
    """Assign (or, with a blank alias, clear) the display alias for a username.
    Drops the whole `aliases` table when it empties so config.toml stays tidy."""
    tt = data.setdefault("recorder", {}).setdefault("tiktok", {})
    aliases = dict(tt.get("aliases", {}) or {})
    if alias and alias.strip():
        aliases[username] = alias.strip()
    else:
        aliases.pop(username, None)
    if aliases:
        tt["aliases"] = aliases
    else:
        tt.pop("aliases", None)


def _resolve_user(data: dict, raw: str) -> str:
    """Map a `--user` value — a real username OR an alias, with or without a
    leading @ — to the real username. Alias matching is case-insensitive. An
    unrecognized value passes through unchanged (stripped of @) so `add` can
    register a brand-new account. Aliases work anywhere a username does because
    every CLI command routes its `--user` through here."""
    val = raw.lstrip("@")
    known = set(_get_users(data)) | set(_get_manual(data))
    if val in known:
        return val
    for username, alias in _get_aliases(data).items():
        if alias.lower() == val.lower():
            return username
    return val


def _label(username: str, aliases: dict[str, str]) -> str:
    """`@user` or `@user (Alias)` for listings/status."""
    alias = aliases.get(username)
    return f"@{username}" + (f" ({alias})" if alias else "")


def cmd_config_add(args: argparse.Namespace) -> int:
    data = _load_toml()
    users = _get_users(data)
    u = args.user.lstrip("@")
    alias = getattr(args, "alias", None)
    if u in users:
        # Already listed — still honour an alias supplied on a re-add.
        if alias:
            _set_alias(data, u, alias)
            _save_toml(data)
            print(f"@{u} already in list — alias set to \"{alias.strip()}\"")
            return 0
        print(f"@{u} already in list")
        return 0
    users.append(u)
    _set_users(data, users)
    if alias:
        _set_alias(data, u, alias)
    _save_toml(data)
    suffix = f' as "{alias.strip()}"' if alias else ""
    print(f"added @{u}{suffix} (priority rank {len(users)})")
    return 0


def cmd_config_remove(args: argparse.Namespace) -> int:
    data = _load_toml()
    users = _get_users(data)
    u = _resolve_user(data, args.user)
    if u not in users:
        print(f"@{u} not in list")
        return 1
    users.remove(u)
    _set_users(data, users)
    if u not in _get_manual(data):     # alias no longer referenced by any roster
        _set_alias(data, u, None)
    _save_toml(data)
    print(f"removed @{u}")
    return 0


def cmd_config_list(args: argparse.Namespace) -> int:
    data = _load_toml()
    aliases = _get_aliases(data)
    users = _get_users(data)
    manual = _get_manual(data)
    if not users and not manual:
        print("(no users configured)")
        return 0
    print("active (listened, priority order):")
    if users:
        for i, u in enumerate(users, 1):
            print(f"  {i}. {_label(u, aliases)}")
    else:
        print("  (none)")
    print("manual (on-demand, not listened):")
    if manual:
        for u in manual:
            print(f"  - {_label(u, aliases)}")
    else:
        print("  (none)")
    return 0


def cmd_config_alias(args: argparse.Namespace) -> int:
    """Set or clear a user's display alias (applies to whichever roster the
    user is on). An empty `--alias ""` clears it."""
    data = _load_toml()
    u = _resolve_user(data, args.user)
    if u not in _get_users(data) and u not in _get_manual(data):
        print(f"@{u} not in either list — add it first")
        return 1
    _set_alias(data, u, args.alias)
    _save_toml(data)
    if args.alias and args.alias.strip():
        print(f"@{u} alias set to \"{args.alias.strip()}\"")
    else:
        print(f"@{u} alias cleared")
    return 0


def cmd_config_manual_add(args: argparse.Namespace) -> int:
    data = _load_toml()
    manual = _get_manual(data)
    u = args.user.lstrip("@")
    alias = getattr(args, "alias", None)
    if u in manual:
        if alias:
            _set_alias(data, u, alias)
            _save_toml(data)
            print(f"@{u} already on the manual list — alias set to "
                  f"\"{alias.strip()}\"")
            return 0
        print(f"@{u} already on the manual list")
        return 0
    manual.append(u)
    _set_manual(data, manual)
    if alias:
        _set_alias(data, u, alias)
    _save_toml(data)
    suffix = f' as "{alias.strip()}"' if alias else ""
    print(f"added @{u}{suffix} to the manual list "
          f"(record with `recorder record --user {alias.strip() if alias else u}`)")
    return 0


def cmd_config_manual_remove(args: argparse.Namespace) -> int:
    data = _load_toml()
    manual = _get_manual(data)
    u = _resolve_user(data, args.user)
    if u not in manual:
        print(f"@{u} not on the manual list")
        return 1
    manual.remove(u)
    _set_manual(data, manual)
    if u not in _get_users(data):      # alias no longer referenced by any roster
        _set_alias(data, u, None)
    _save_toml(data)
    print(f"removed @{u} from the manual list")
    return 0


def cmd_config_priority(args: argparse.Namespace) -> int:
    data = _load_toml()
    users = _get_users(data)
    u = _resolve_user(data, args.user)
    if u not in users:
        print(f"@{u} not in list — add it first")
        return 1
    rank = args.rank
    if not (1 <= rank <= len(users)):
        print(f"rank must be 1..{len(users)}")
        return 1
    users.remove(u)
    users.insert(rank - 1, u)
    _set_users(data, users)
    _save_toml(data)
    print(f"@{u} moved to rank {rank}")
    return 0


# ── banned (auto-retired accounts) ───────────────────────────────────────────

def cmd_banned(args: argparse.Namespace) -> int:
    """List auto-banned TikTok accounts or restore one — the recorder-side
    mirror of `archiver banned`. Bans are written by the recorder's two-stage
    unstartable gate (state._maybe_ban_unstartable) onto this app's
    config.toml roster via core.PolicyStore; unban reverses the roster entry
    AND brings the quarantined folder back out of .deleted/."""
    from core import ItemStore, PolicyStore, restore_user

    store  = PolicyStore(CONFIG_TOML)
    action = getattr(args, "banned_cmd", None)

    if action == "unban":
        username = _resolve_user(_load_toml(), args.user)
        if not store.unban_user("tiktok", username):
            log.error("@%s is not on the banned list.", username)
            return 1
        log.info("Removed @%s from the banned list.", username)
        config = RecorderConfig.load()
        # Recordings live directly under output_dir (no platform segment);
        # repoint any still-queued rows back to the restored location.
        db = ItemStore.open(config.db_path)
        try:
            restored = restore_user(config.output_dir, "", username, db=db)
        finally:
            db.close()
        if restored is not None:
            log.info("Restored quarantined folder → %s", restored)
        else:
            log.info("No quarantined folder to restore (none was moved, or a "
                     "live folder already exists).")
        if args.re_add:
            data = _load_toml()
            users = _get_users(data)
            if username not in users:
                users.append(username)
                _set_users(data, users)
                _save_toml(data)
                log.info("Re-added @%s to the priority list (rank %d).",
                         username, len(users))
            else:
                log.info("@%s already in the priority list.", username)
        else:
            log.info("Not re-added to the priority list — "
                     "`recorder config add --user %s` to resume recording "
                     "(or re-run unban with --re-add).", username)
        log.info("Note: a running recorder won't see this until it restarts.")
        return 0

    # Default / "list": show the banned roster.
    details = store.banned_details("tiktok")
    if not details:
        print("(no banned users)")
        return 0
    for u, meta in details.items():
        line = f"@{u}"
        if meta.get("detected_at"):
            line += f"  [{meta['detected_at']}]"
        if meta.get("reason"):
            line += f"  {meta['reason']}"
        print(line)
    return 0


# ── parser ──────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="recorder",
                                description="TikTok live recorder.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="run the recorder")
    p_start.add_argument("--daemon", action="store_true",
                         help="deprecated no-op — use `ops install` to run under "
                              "the OS service manager")
    p_record = sub.add_parser(
        "record",
        help="one-shot: record a single user's live now, then exit (no loop)")
    p_record.add_argument("--user", required=True,
                          help="username or alias to check and record once")
    p_record.add_argument("--no-reload", action="store_true",
                          help="don't auto-run `ops load recorder` on exit")
    sub.add_parser("stop", help="stop a running recorder via pid file")
    sub.add_parser("status", help="show state + lock + user list")
    p_watch = sub.add_parser("watch", help="live auto-refreshing dashboard")
    p_watch.add_argument("--interval", type=float, default=2.0,
                         help="seconds between refreshes (default 2)")
    core_cli.add_stats_parser(sub)   # shared `stats` noun (DB counts)

    p_ban = sub.add_parser("banned", help="list/restore auto-banned accounts")
    ban_sub = p_ban.add_subparsers(dest="banned_cmd", required=False)
    ban_sub.add_parser("list", help="show the banned roster (default)")
    p_unban = ban_sub.add_parser("unban", help="remove a user from the roster "
                                               "and restore their folder")
    p_unban.add_argument("--user", required=True)
    p_unban.add_argument("--re-add", action="store_true",
                         help="also re-add to the priority list")

    p_cfg = sub.add_parser("config",
                           help="manage the user lists (aliases, manual roster)")
    cfg_sub = p_cfg.add_subparsers(dest="config_command", required=True)

    p_add = cfg_sub.add_parser("add", help="add a user to the listened list")
    p_add.add_argument("--user", required=True)
    p_add.add_argument("--alias", help="friendly display name (shown in the "
                                       "upload caption; usable as a --user value)")
    p_rm  = cfg_sub.add_parser("remove", help="remove a user (accepts an alias)")
    p_rm.add_argument("--user", required=True)
    cfg_sub.add_parser("list", help="show both lists with aliases")
    p_pri = cfg_sub.add_parser("priority")
    p_pri.add_argument("--user", required=True)
    p_pri.add_argument("--rank", type=int, required=True)

    p_alias = cfg_sub.add_parser("alias",
                                 help="set/clear a user's display alias")
    p_alias.add_argument("--user", required=True)
    p_alias.add_argument("--alias", required=True,
                         help='new alias (pass "" to clear)')

    p_madd = cfg_sub.add_parser("manual-add",
                                help="add a user to the manual (on-demand) list")
    p_madd.add_argument("--user", required=True)
    p_madd.add_argument("--alias", help="friendly display name")
    p_mrm = cfg_sub.add_parser("manual-remove",
                               help="remove a user from the manual list")
    p_mrm.add_argument("--user", required=True)

    return p


_DISPATCH = {
    ("start", None):              cmd_start,
    ("record", None):             cmd_record,
    ("stop", None):               cmd_stop,
    ("status", None):             cmd_status,
    ("watch", None):              cmd_watch,
    ("stats", None):              cmd_stats,
    ("banned", None):             cmd_banned,
    ("config", "add"):            cmd_config_add,
    ("config", "remove"):         cmd_config_remove,
    ("config", "list"):           cmd_config_list,
    ("config", "priority"):       cmd_config_priority,
    ("config", "alias"):          cmd_config_alias,
    ("config", "manual-add"):     cmd_config_manual_add,
    ("config", "manual-remove"):  cmd_config_manual_remove,
}


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    sub = getattr(args, "config_command", None)
    handler = _DISPATCH.get((args.command, sub))
    if handler is None:
        log.error("no handler for %s/%s", args.command, sub)
        return 2
    try:
        # Single-instance guard: only one `recorder start` may run at a time
        # (two would fight over the same TikTok session + capture dirs).
        # InstanceAlreadyRunning is a RuntimeError, so the handler below reports
        # it cleanly. Other subcommands (config, stop, watch, …) are not gated.
        if args.command == "start":
            from core import InstanceLock
            with InstanceLock("recorder"):
                return handler(args)
        return handler(args)
    except RuntimeError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
