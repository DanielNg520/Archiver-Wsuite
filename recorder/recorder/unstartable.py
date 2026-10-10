"""
recorder.unstartable
────────────────────
Persistent escalation counter behind the recorder's auto-ban gate.

The in-memory cooldown bench (state._skipped) handles the fast path: a user we
can't START recording is skipped for _SKIP_COOLDOWN_S and retried. But ban
escalation needs a LONG window — "this user has been unstartable for days" —
and that must survive restarts, so each cooldown is also tallied here, in a
small JSON file in state_dir (~/.recorder/unstartable.json):

    {username: {"cooldown_cycles": int,
                "first_seen": iso-utc, "last_seen": iso-utc}}

One successful capture start EVICTS the entry (mirrors _consec_fail.pop): a
user who records fine is, by definition, not on the road to a ban.

Best-effort persistence: a corrupt/unwritable file must never stop the
recorder — it degrades to an empty tally (escalation restarts from zero, which
only DELAYS a ban, never causes a wrong one).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_FILENAME = "unstartable.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class UnstartableTracker:
    def __init__(self, state_dir: str | Path):
        self._path = Path(state_dir).expanduser() / _FILENAME
        self._data: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("unstartable: %s unreadable (%s) — starting empty",
                        self._path, e)
            return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("unstartable: could not persist %s: %s", self._path, e)

    # ── tally ──────────────────────────────────────────────────────────────

    def note_cooldown(self, username: str) -> None:
        """One more cooldown bench for `username` (called when _deactivate_user
        fires). Creates the entry on first sight; stamps last_seen always."""
        now = _now_iso()
        e = self._data.get(username)
        if e is None:
            e = {"cooldown_cycles": 0, "first_seen": now}
        e["cooldown_cycles"] = int(e.get("cooldown_cycles", 0)) + 1
        e["last_seen"] = now
        self._data[username] = e
        self._save()

    def clear(self, username: str) -> None:
        """Successful start → the user is startable; forget the history.
        Also the ban path's evict (the roster carries the record from there)."""
        if self._data.pop(username, None) is not None:
            self._save()

    # ── reads (for the ban gate) ───────────────────────────────────────────

    def cycles(self, username: str) -> int:
        return int(self._data.get(username, {}).get("cooldown_cycles", 0))

    def age_seconds(self, username: str) -> float:
        """Seconds since first_seen, or 0.0 if unknown/unparseable — an
        unknown age can only DELAY a ban (the age floor won't be met)."""
        raw = self._data.get(username, {}).get("first_seen")
        if not raw:
            return 0.0
        try:
            first = datetime.fromisoformat(raw)
        except ValueError:
            return 0.0
        return (datetime.now(timezone.utc) - first).total_seconds()
