"""telegram
────
Telegram Bot API client using urllib.request and json.
"""

import json
import urllib.error
import urllib.request
from typing import Any


class TelegramError(Exception):
    """Raised when the Telegram Bot API returns an error or request fails."""


class BotAPI:
    """Telegram Bot API client using urllib.request."""

    def __init__(
        self,
        token: str,
        *,
        urlopen: Any = urllib.request.urlopen,
    ) -> None:
        self._token = token
        self._urlopen = urlopen

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(token='<token>')"

    def _redact(self, text: str) -> str:
        if not self._token:
            return str(text)
        return str(text).replace(self._token, "<token>")

    def _request(
        self,
        method: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            resp = self._urlopen(req, timeout=timeout)
            try:
                raw = resp.read()
            finally:
                if hasattr(resp, "close"):
                    resp.close()
        except urllib.error.HTTPError as exc:
            desc = None
            try:
                read_fn = getattr(exc, "read", None)
                raw_err = read_fn() if callable(read_fn) else None
                if raw_err:
                    err_json = json.loads(
                        raw_err.decode("utf-8", errors="replace")
                    )
                    if isinstance(err_json, dict):
                        desc = err_json.get("description")
            except Exception:
                pass
            msg = str(desc or exc.reason or getattr(exc, "msg", None) or exc)
            raise TelegramError(self._redact(msg)) from None
        except (urllib.error.URLError, OSError) as exc:
            raise TelegramError(self._redact(str(exc))) from None
        except Exception as exc:
            raise TelegramError(self._redact(str(exc))) from None

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise TelegramError(self._redact(str(exc))) from None

        if not isinstance(parsed, dict):
            raise TelegramError("invalid response from Telegram") from None

        if not parsed.get("ok"):
            desc = parsed.get("description", "request failed")
            raise TelegramError(self._redact(str(desc))) from None

        return parsed.get("result")

    def get_updates(self, offset: int, timeout: int = 30) -> list[dict]:
        payload = {"offset": offset, "timeout": timeout}
        result = self._request(
            "getUpdates",
            payload,
            timeout=float(timeout + 10),
        )
        if not isinstance(result, list):
            raise TelegramError(
                "expected list result from getUpdates"
            ) from None
        return result

    def send_message(self, chat_id: int, text: str) -> int:
        payload = {"chat_id": chat_id, "text": text}
        result = self._request("sendMessage", payload, timeout=30.0)
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramError(
                "missing message_id in sendMessage result"
            ) from None
        try:
            return int(result["message_id"])
        except (TypeError, ValueError) as exc:
            raise TelegramError(self._redact(str(exc))) from None

    def delete_message(self, chat_id: int, message_id: int) -> None:
        payload = {"chat_id": chat_id, "message_id": message_id}
        self._request("deleteMessage", payload, timeout=30.0)
