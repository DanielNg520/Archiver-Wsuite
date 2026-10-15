"""
ops.bot.state
─────────────
Persistent state management for the Telegram control bot (ops.bot). Tracks
the getUpdates offset, last recording-started reports per chat, and sent
traffic scheduled for 24h deletion sweep.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from core.heartbeat import write_atomic


class BotState:
    """Thread-safe persistent state store for ops.bot."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._offset_path = self._root / "offset.json"
        self._reports_path = self._root / "reports.json"
        self._sent_path = self._root / "sent.json"
        self._lock = threading.Lock()

    def get_offset(self) -> int:
        with self._lock:
            try:
                data = json.loads(self._offset_path.read_text("utf-8"))
                if isinstance(data, dict):
                    return int(data.get("offset", 0))
                return int(data)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                return 0

    def set_offset(self, offset: int) -> None:
        with self._lock:
            write_atomic(self._offset_path, {"offset": int(offset)})

    def _read_reports(self) -> dict[int, int]:
        try:
            data = json.loads(self._reports_path.read_text("utf-8"))
            if not isinstance(data, dict):
                return {}
            raw = (
                data["reports"]
                if "reports" in data and isinstance(data["reports"], dict)
                else data
            )
            out: dict[int, int] = {}
            for k, v in raw.items():
                try:
                    out[int(k)] = int(v)
                except (ValueError, TypeError):
                    continue
            return out
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}

    def get_report(self, chat_id: int) -> int | None:
        with self._lock:
            return self._read_reports().get(int(chat_id))

    def set_report(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            reports = self._read_reports()
            reports[int(chat_id)] = int(message_id)
            write_atomic(
                self._reports_path,
                {str(k): v for k, v in reports.items()},
            )

    def clear_report(self, chat_id: int) -> None:
        with self._lock:
            reports = self._read_reports()
            cid = int(chat_id)
            if cid in reports:
                del reports[cid]
                write_atomic(
                    self._reports_path,
                    {str(k): v for k, v in reports.items()},
                )

    def _read_sent(self) -> list[tuple[int, int, float]]:
        try:
            data = json.loads(self._sent_path.read_text("utf-8"))
            if isinstance(data, dict):
                raw = (
                    data.get("sent")
                    or data.get("messages")
                    or data.get("entries")
                    or []
                )
            elif isinstance(data, list):
                raw = data
            else:
                return []
            if not isinstance(raw, list):
                return []
            out: list[tuple[int, int, float]] = []
            for item in raw:
                if isinstance(item, (list, tuple)) and len(item) >= 3:
                    try:
                        out.append(
                            (int(item[0]), int(item[1]), float(item[2]))
                        )
                    except (ValueError, TypeError):
                        continue
            return out
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return []

    def track(self, chat_id: int, message_id: int, sent_at: float) -> None:
        with self._lock:
            entries = self._read_sent()
            cid = int(chat_id)
            mid = int(message_id)
            for item in entries:
                if item[0] == cid and item[1] == mid:
                    return
            entries.append((cid, mid, float(sent_at)))
            write_atomic(
                self._sent_path,
                {"sent": [[c, m, t] for c, m, t in entries]},
            )

    def due(
        self, now: float, max_age_s: float
    ) -> list[tuple[int, int, float]]:
        with self._lock:
            entries = self._read_sent()
            return [
                item for item in entries
                if (float(now) - item[2]) >= float(max_age_s)
            ]

    def forget(self, chat_id: int, message_id: int) -> None:
        with self._lock:
            entries = self._read_sent()
            cid = int(chat_id)
            mid = int(message_id)
            remaining = [
                item for item in entries
                if not (item[0] == cid and item[1] == mid)
            ]
            if len(remaining) != len(entries):
                write_atomic(
                    self._sent_path,
                    {"sent": [[c, m, t] for c, m, t in remaining]},
                )
