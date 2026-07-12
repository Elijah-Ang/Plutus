from __future__ import annotations

import time
from typing import Any

import requests

from .utils import get_secret


class TelegramBot:
    def __init__(self, token: str | None = None, chat_id: str | None = None, allowed_user_id: str | None = None, timeout: int = 15) -> None:
        self.token = token or get_secret("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or get_secret("TELEGRAM_CHAT_ID")
        self.allowed_user_id = allowed_user_id or get_secret("TELEGRAM_ALLOWED_USER_ID")
        self.timeout = timeout
        if not self.token:
            raise RuntimeError("Telegram token is not configured")
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._health_checked_at = 0.0
        self._health_ok = False

    def _request(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = requests.post(f"{self.base_url}/{method}", json=payload or {}, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError("Telegram API request failed")
            return data.get("result")
        except Exception as e:
            from .utils import redact_sensitive_url
            msg = redact_sensitive_url(str(e))
            raise RuntimeError(f"Telegram API request failed: {msg}") from None

    def send_message(self, text: str, chat_id: str | None = None) -> Any:
        target = chat_id or self.chat_id
        if not target:
            raise RuntimeError("Telegram chat ID is not configured")
        return self._request("sendMessage", {"chat_id": target, "text": text})

    def send_test_message(self) -> Any:
        return self.send_message("TradingAgent paper-mode Telegram test successful. No order was placed.")

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        return self._request("getUpdates", payload)

    def is_available(self, force: bool = False, cache_seconds: float = 30.0) -> bool:
        now = time.monotonic()
        if not force and self._health_checked_at and now - self._health_checked_at <= cache_seconds:
            return self._health_ok
        try:
            self._health_ok = bool(self._request("getMe")) and bool(self.chat_id) and bool(self.allowed_user_id)
        except Exception:
            self._health_ok = False
        self._health_checked_at = now
        return self._health_ok

    def is_authorized(self, sender_id: Any) -> bool:
        return bool(self.allowed_user_id) and str(sender_id) == str(self.allowed_user_id)

    def handle_command(self, command: str, sender_id: Any | None = None) -> str:
        raw = " ".join(command.strip().split())
        command = raw.split()[0].lower() if raw else ""
        if command in {"/pause", "/resume", "/killswitch"} and not self.is_authorized(sender_id):
            return "Unauthorized command rejected and should be audited."
        if command in {"/pause", "/killswitch"}:
            from .utils import PROJECT_ROOT
            (PROJECT_ROOT / "config" / "KILL_SWITCH").touch(exist_ok=True)
            return "Local kill switch enabled. No new proposal or execution is allowed."
        if command == "/resume":
            if raw != "/resume CONFIRM PAPER RESUME":
                return "Resume rejected. Exact phrase required: /resume CONFIRM PAPER RESUME"
            from .utils import PROJECT_ROOT, load_config
            config = load_config()
            if config.get("mode") != "paper" or config.get("live_enabled") is not False:
                return "Resume blocked: local configuration is not paper-only."
            (PROJECT_ROOT / "config" / "KILL_SWITCH").unlink(missing_ok=True)
            return "Paper-mode kill switch cleared locally."
        messages = {
            "/status": "Status is available from the latest run and audit log.",
            "/report": "Run scripts/export_excel.sh for the current report.",
            "/cashout": "Cash-out is recommendation-only; no funds will move.",
            "/pending": "Pending proposals are read from the local database.",
            "/performance": "Strategy performance is available from the latest persisted report-only scorecard.",
            "/help": "/status /performance /pause /resume /killswitch /report /cashout /pending /help",
        }
        return messages.get(command, "Unknown command. Use /help.")


def redact_telegram_update(update: dict[str, Any], include_raw: bool = False) -> dict[str, Any]:
    """Return a diagnostic-safe update; raw user data requires explicit opt-in."""
    if include_raw:
        return update
    message = update.get("message") or update.get("edited_message") or {}
    return {
        "update_id": update.get("update_id"),
        "message_present": bool(message),
        "text_present": bool(message.get("text")),
        "sender_id": "[REDACTED]" if message.get("from") else None,
        "chat_id": "[REDACTED]" if message.get("chat") else None,
        "text": "[REDACTED]" if message.get("text") else None,
    }
