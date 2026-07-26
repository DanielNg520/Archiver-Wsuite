"""
core.policy_store
───────────────────────
Shared Repository for config.toml. Same atomic write semantics and same
hierarchical lookup shape everywhere: user → platform → global.

Storage layout:
  ~/.config/archiver-suite/config.toml

Overridable via $ARCHIVER_SUITE_CONFIG for tests / alternate setups.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import Any, Iterator

import tomli_w

from core.platform import paths as _osp

log = logging.getLogger(__name__)


_HEADER = """\
# archiver-suite config — machine-managed but human-readable.
# Edits are safe; the CLI may rewrite this file. Values are preserved on
# rewrite but comments OUTSIDE this header are not. Don't put secrets
# here — those live in .env. Resolution order: user → platform → global.

"""


def default_config_path() -> Path:
    """Canonical config location. Override with $ARCHIVER_SUITE_CONFIG."""
    override = os.environ.get("ARCHIVER_SUITE_CONFIG")
    if override:
        return Path(override).expanduser()
    return _osp.config_dir(_osp.SUITE) / "config.toml"


class PolicyStore:
    """
    Owns config.toml. Thread-safe via a single RLock.

    Public surface (identical to archiver's):
      .get(key, *, platform=None, username=None, default=None)
      .explain(key, *, platform=None, username=None, default=None)
      .set(key, value, *, platform=None, username=None)
      .unset(key, *, platform=None, username=None)
      .list_users(platform)
      .add_user(platform, username)
      .remove_user(platform, username)
      .list_banned(platform)
      .banned_details(platform)
      .ban_user(platform, username, *, reason, detected_at)
      .unban_user(platform, username)
      .iter_user_overrides()
    """

    def __init__(self, path: Path | None = None):
        self._path  = path or default_config_path()
        self._lock  = threading.RLock()
        self._data: dict[str, Any] = self._load()

    # ── Loading / persistence ─────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            log.info("policy_store: %s does not exist — starting empty", self._path)
            return {}
        try:
            with self._path.open("rb") as f:
                return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise RuntimeError(
                f"config.toml is malformed: {e}. Fix or delete {self._path}."
            ) from e

    def _persist(self) -> None:
        """Atomic write: tempfile (same dir) → fsync → os.replace."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(
            mode     = "w",
            dir      = self._path.parent,
            prefix   = ".config.toml.",
            suffix   = ".tmp",
            delete   = False,
            encoding = "utf-8",
        )
        try:
            tmp.write(_HEADER)
            tmp.write(tomli_w.dumps(self._data))
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, self._path)
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

    # ── Hierarchical lookup ───────────────────────────────────────────────

    def get(
        self,
        key:      str,
        *,
        platform: str | None = None,
        username: str | None = None,
        default:  Any        = None,
    ) -> Any:
        with self._lock:
            if platform and username:
                user_section = (
                    self._data.get("platform", {})
                              .get(platform, {})
                              .get("user", {})
                              .get(username, {})
                )
                if key in user_section:
                    return user_section[key]
            if platform:
                plat_section = self._data.get("platform", {}).get(platform, {})
                if key in plat_section:
                    return plat_section[key]
            global_section = self._data.get("global", {})
            if key in global_section:
                return global_section[key]
            return default

    def explain(
        self,
        key:      str,
        *,
        platform: str | None = None,
        username: str | None = None,
        default:  Any        = None,
    ) -> tuple[Any, str]:
        with self._lock:
            if platform and username:
                user_section = (
                    self._data.get("platform", {})
                              .get(platform, {})
                              .get("user", {})
                              .get(username, {})
                )
                if key in user_section:
                    return user_section[key], f"user:{platform}/{username}"
            if platform:
                plat_section = self._data.get("platform", {}).get(platform, {})
                if key in plat_section:
                    return plat_section[key], f"platform:{platform}"
            global_section = self._data.get("global", {})
            if key in global_section:
                return global_section[key], "global"
            return default, "default"

    # ── Mutation ──────────────────────────────────────────────────────────

    def set(
        self,
        key:      str,
        value:    Any,
        *,
        platform: str | None = None,
        username: str | None = None,
    ) -> None:
        with self._lock:
            target = self._resolve_section(platform, username, create=True)
            target[key] = value
            self._persist()

    def unset(
        self,
        key:      str,
        *,
        platform: str | None = None,
        username: str | None = None,
    ) -> bool:
        """Return True iff a key was actually removed."""
        with self._lock:
            target = self._resolve_section(platform, username, create=False)
            if target is None or key not in target:
                return False
            del target[key]
            self._prune_empty(platform, username)
            self._persist()
            return True

    def _resolve_section(
        self,
        platform: str | None,
        username: str | None,
        *,
        create: bool,
    ) -> dict[str, Any] | None:
        if platform and username:
            path: tuple[str, ...] = ("platform", platform, "user", username)
        elif platform:
            path = ("platform", platform)
        else:
            path = ("global",)

        node: dict[str, Any] = self._data
        for seg in path:
            if seg not in node:
                if not create:
                    return None
                node[seg] = {}
            node = node[seg]
        return node

    def _prune_empty(self, platform: str | None, username: str | None) -> None:
        if platform and username:
            user_dict = (
                self._data.get("platform", {})
                          .get(platform, {})
                          .get("user", {})
            )
            if username in user_dict and not user_dict[username]:
                del user_dict[username]

    # ── User-list management ──────────────────────────────────────────────

    def list_users(self, platform: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                self._data.get("platform", {})
                          .get(platform, {})
                          .get("users", [])
            )

    def add_user(self, platform: str, username: str) -> bool:
        with self._lock:
            section = self._resolve_section(platform, None, create=True)
            users = list(section.get("users", []))
            if username in users:
                return False
            users.append(username)
            section["users"] = users
            self._persist()
            return True

    def remove_user(self, platform: str, username: str) -> bool:
        with self._lock:
            section = self._resolve_section(platform, None, create=False)
            if section is None:
                return False
            users = list(section.get("users", []))
            if username not in users:
                return False
            users.remove(username)
            section["users"] = users
            user_dict = section.get("user", {})
            if username in user_dict:
                del user_dict[username]
            self._persist()
            return True

    # ── Banned-account roster ─────────────────────────────────────────────
    #
    # Accounts auto-detected as gone (banned/suspended/deleted) during a run.
    # Stored under `[platform.<name>.banned]` as a table keyed by username →
    # {reason, detected_at}, parallel to the `users` array. Separating the two
    # keeps banned accounts out of the active fetch loop while preserving why
    # and when each was retired, without losing the username.

    def list_banned(self, platform: str) -> tuple[str, ...]:
        with self._lock:
            banned = (self._data.get("platform", {})
                                .get(platform, {})
                                .get("banned", {}))
            return tuple(banned.keys()) if isinstance(banned, dict) else ()

    def banned_details(self, platform: str) -> dict[str, dict[str, Any]]:
        """username → {reason, detected_at} for every banned account on a
        platform. Returns a copy; mutating it does not touch the store."""
        with self._lock:
            banned = (self._data.get("platform", {})
                                .get(platform, {})
                                .get("banned", {}))
            if not isinstance(banned, dict):
                return {}
            return {
                u: (dict(meta) if isinstance(meta, dict) else {})
                for u, meta in banned.items()
            }

    def ban_user(
        self,
        platform: str,
        username: str,
        *,
        reason:      str = "",
        detected_at: str = "",
    ) -> bool:
        """Retire an account: remove it from the active `users` list (and drop
        any per-user overrides), then record it under `banned` with the reason
        and timestamp. Returns True iff it was NOT already banned (i.e. this is
        a newly-detected ban) — lets callers distinguish first detection from a
        repeat. Idempotent: re-banning refreshes reason/detected_at."""
        with self._lock:
            section = self._resolve_section(platform, None, create=True)

            users = list(section.get("users", []))
            if username in users:
                users.remove(username)
                section["users"] = users

            user_dict = section.get("user", {})
            if username in user_dict:
                del user_dict[username]

            banned = section.setdefault("banned", {})
            newly = username not in banned
            entry: dict[str, Any] = {}
            if reason:
                entry["reason"] = reason
            if detected_at:
                entry["detected_at"] = detected_at
            banned[username] = entry

            self._persist()
            return newly

    def unban_user(self, platform: str, username: str) -> bool:
        """Remove an account from the banned roster. Does NOT re-add it to the
        active `users` list — restoring an account is a deliberate two-step
        (unban, then add). Returns True iff a banned entry was removed."""
        with self._lock:
            section = self._resolve_section(platform, None, create=False)
            if section is None:
                return False
            banned = section.get("banned", {})
            if not isinstance(banned, dict) or username not in banned:
                return False
            del banned[username]
            if not banned:
                del section["banned"]
            self._persist()
            return True

    # ── Deletion roster ───────────────────────────────────────────────────
    #
    # Users the operator asked to DELETE (manual, terminal — distinct from the
    # auto-ban quarantine). Stored under `[platform.<name>.deleting]` keyed by
    # username → {requested_at, trashed_at?}, parallel to `banned`. The entry
    # drives the deferred-trash sweeper (core.manual_delete): folder → Recycle
    # Bin once every row is sent, rows GC'd 30 days after the trash.

    def list_deleting(self, platform: str) -> tuple[str, ...]:
        with self._lock:
            d = (self._data.get("platform", {})
                           .get(platform, {})
                           .get("deleting", {}))
            return tuple(d.keys()) if isinstance(d, dict) else ()

    def deleting_details(self, platform: str) -> dict[str, dict[str, Any]]:
        """username → {requested_at, trashed_at?} for every pending deletion.
        Returns a copy; mutating it does not touch the store."""
        with self._lock:
            d = (self._data.get("platform", {})
                           .get(platform, {})
                           .get("deleting", {}))
            if not isinstance(d, dict):
                return {}
            return {
                u: (dict(meta) if isinstance(meta, dict) else {})
                for u, meta in d.items()
            }

    def mark_deleting(self, platform: str, username: str, *,
                      requested_at: str = "") -> bool:
        """Request deletion: record the user under `deleting`. Does NOT touch
        the active `users` list — the caller pairs this with remove_user (the
        same two-step shape as ban_user, kept separate so `cancel` can restore
        cleanly). Returns True iff this is a new request; a repeat is a no-op
        (the original requested_at is authoritative)."""
        with self._lock:
            section = self._resolve_section(platform, None, create=True)
            deleting = section.setdefault("deleting", {})
            if username in deleting:
                return False
            entry: dict[str, Any] = {}
            if requested_at:
                entry["requested_at"] = requested_at
            deleting[username] = entry
            self._persist()
            return True

    def set_deleting_field(self, platform: str, username: str,
                           key: str, value: Any) -> bool:
        """Stamp one field (e.g. trashed_at) onto an existing deletion entry.
        Returns False if the user has no entry."""
        with self._lock:
            section = self._resolve_section(platform, None, create=False)
            deleting = (section or {}).get("deleting", {})
            if not isinstance(deleting, dict) or username not in deleting:
                return False
            entry = deleting[username]
            if not isinstance(entry, dict):
                entry = deleting[username] = {}
            entry[key] = value
            self._persist()
            return True

    def platforms_with_deletions(self) -> tuple[str, ...]:
        """Every platform that has at least one pending deletion entry — the
        sweeper's iteration set (a roster can outlive the platform's presence
        in the configured list, so this comes from the store, not the config)."""
        with self._lock:
            return tuple(
                name for name, data in self._data.get("platform", {}).items()
                if isinstance(data, dict) and isinstance(data.get("deleting"), dict)
                and data["deleting"]
            )

    def unmark_deleting(self, platform: str, username: str) -> bool:
        """Evict a deletion entry (GC done, or `deleting cancel`). Returns
        True iff an entry was removed."""
        with self._lock:
            section = self._resolve_section(platform, None, create=False)
            if section is None:
                return False
            deleting = section.get("deleting", {})
            if not isinstance(deleting, dict) or username not in deleting:
                return False
            del deleting[username]
            if not deleting:
                del section["deleting"]
            self._persist()
            return True

    # ── Diagnostics ───────────────────────────────────────────────────────

    def iter_user_overrides(self) -> Iterator[tuple[str, str, dict[str, Any]]]:
        with self._lock:
            for plat_name, plat_data in self._data.get("platform", {}).items():
                if not isinstance(plat_data, dict):
                    continue
                for user_name, user_data in plat_data.get("user", {}).items():
                    if isinstance(user_data, dict):
                        yield plat_name, user_name, dict(user_data)

    @property
    def path(self) -> Path:
        return self._path
