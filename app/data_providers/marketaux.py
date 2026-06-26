from __future__ import annotations

import os
import subprocess
from typing import Any

from app.data_providers.base import ProviderResponse


class MarketauxNewsProvider:
    """Optional future news fallback for shortlisted symbols only.

    The provider is disabled by default. It intentionally does not perform
    network calls until a key is configured and a caller explicitly enables it.
    """

    name = "marketaux"

    def __init__(self, config: dict[str, Any], api_key: str | None = None) -> None:
        self.config = config
        self.cfg = config.get("news_providers", {}).get("marketaux", {})
        self.api_key = api_key if api_key is not None else self._load_api_key()

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False)) and bool(self.api_key)

    def health(self) -> ProviderResponse:
        if not self.cfg.get("enabled", False):
            return ProviderResponse(self.name, "health", "disabled", None, "disabled_until_key_exists")
        if not self.api_key:
            return ProviderResponse(self.name, "health", "disabled_missing_key", None, "missing_api_key")
        return ProviderResponse(self.name, "health", "ok", {"enabled": True})

    def get_news(self, symbol: str | None = None, topic: str | None = None, limit: int = 10) -> ProviderResponse:
        if not self.enabled():
            return ProviderResponse(self.name, "news", "disabled_missing_key", [], "missing_api_key")
        return ProviderResponse(self.name, "news", "disabled", [], "network_implementation_not_enabled")

    def _load_api_key(self) -> str | None:
        env_key = os.getenv("MARKETAUX_API_KEY")
        if env_key:
            return env_key
        secret_name = self.cfg.get("api_key_secret_name", "TradingAgent.MARKETAUX_API_KEY")
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", str(secret_name), "-w"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            return None
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
