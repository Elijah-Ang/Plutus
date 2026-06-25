from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

from app.data_providers.base import ProviderResponse
from app.data_providers.cache import ProviderCache
from app.storage import Storage
from app.utils import get_secret


class EODHDProvider:
    name = "eodhd"

    def __init__(self, config: dict[str, Any], storage: Storage, run_id: str | None = None, api_key: str | None = None) -> None:
        self.config = config
        self.storage = storage
        self.run_id = run_id
        self.provider_cfg = config.get("eodhd", {})
        secret_name = self.provider_cfg.get("api_key_secret_name", "TradingAgent.EODHD_API_KEY")
        env_name = str(secret_name).replace("TradingAgent.", "")
        self.api_key = api_key or get_secret(env_name) or get_secret(str(secret_name))
        self.base_url = str(self.provider_cfg.get("base_url", "https://eodhd.com/api")).rstrip("/")
        self.timeout = float(self.provider_cfg.get("timeout_seconds", 10))
        self.max_retries = int(self.provider_cfg.get("max_retries", 2))
        self.backoff = float(self.provider_cfg.get("retry_backoff_seconds", 2))
        self.cache = ProviderCache(storage)
        self.calls_this_run = 0
        self.max_calls_per_run = int(self.provider_cfg.get("rate_limit", {}).get("max_calls_per_run", 80))
        self.ttls = self.provider_cfg.get("cache_ttl_minutes", {})

    def _ttl(self, name: str, default: int) -> int:
        return int(self.ttls.get(name, default))

    def _request(self, endpoint: str, params: dict[str, Any], cache_ttl: int) -> ProviderResponse:
        if not self.api_key:
            self.cache.record_health(self.name, "provider_unavailable", self.run_id, {"error": "missing_api_key"})
            return ProviderResponse(self.name, endpoint, "provider_unavailable", None, "missing_api_key")

        clean_params = {k: v for k, v in params.items() if v is not None}
        cached = self.cache.get(self.name, endpoint, clean_params)
        if cached is not None:
            return ProviderResponse(self.name, endpoint, "ok", cached, cached=True)

        if self.calls_this_run >= self.max_calls_per_run:
            self.cache.record_health(self.name, "rate_limited", self.run_id, {"max_calls_per_run": self.max_calls_per_run})
            return ProviderResponse(self.name, endpoint, "rate_limited", None, "max_calls_per_run_exceeded")

        request_params = {**clean_params, "api_token": self.api_key, "fmt": "json"}
        url = f"{self.base_url}/{endpoint.lstrip('/')}?{urllib.parse.urlencode(request_params, doseq=True)}"
        last_error = None
        for attempt in range(max(1, self.max_retries + 1)):
            self.calls_this_run += 1
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "TradingAgent/1.0"})
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                data = json.loads(raw) if raw else None
                self.cache.set(self.name, endpoint, clean_params, data, cache_ttl)
                self.cache.record_health(self.name, "ok", self.run_id, {"endpoint": endpoint})
                return ProviderResponse(self.name, endpoint, "ok", data)
            except Exception as exc:
                last_error = type(exc).__name__
                if attempt < self.max_retries:
                    time.sleep(self.backoff)
        self.cache.record_health(self.name, "provider_unavailable", self.run_id, {"endpoint": endpoint, "error": last_error})
        return ProviderResponse(self.name, endpoint, "provider_unavailable", None, last_error)

    def health(self) -> ProviderResponse:
        if not self.api_key:
            return ProviderResponse(self.name, "health", "provider_unavailable", None, "missing_api_key")
        return self._request("user", {}, self._ttl("symbols", 1440))

    def list_symbols(self, exchange: str = "US", limit: int | None = None) -> ProviderResponse:
        res = self._request(f"exchange-symbol-list/{exchange}", {}, self._ttl("symbols", 1440))
        if res.status == "ok" and isinstance(res.data, list) and limit:
            return ProviderResponse(res.provider, res.endpoint, res.status, res.data[:limit], cached=res.cached)
        return res

    def search_symbols(self, query: str, limit: int | None = None) -> ProviderResponse:
        res = self._request("search", {"q": query, "limit": limit}, self._ttl("symbols", 1440))
        return res

    def get_historical_bars(self, symbol: str, period: str = "d", limit: int = 250) -> ProviderResponse:
        res = self._request(f"eod/{symbol}", {"period": period, "order": "d"}, self._ttl("daily_bars", 720))
        if res.status == "ok" and isinstance(res.data, list):
            return ProviderResponse(res.provider, res.endpoint, res.status, res.data[-limit:], cached=res.cached)
        return res

    def get_intraday_bars(self, symbol: str, interval: str = "5m", limit: int = 100) -> ProviderResponse:
        res = self._request(f"intraday/{symbol}", {"interval": interval}, self._ttl("intraday_bars", 15))
        if res.status == "ok" and isinstance(res.data, list):
            return ProviderResponse(res.provider, res.endpoint, res.status, res.data[-limit:], cached=res.cached)
        return res

    def get_latest_quote(self, symbol: str) -> ProviderResponse:
        return self._request(f"real-time/{symbol}", {}, self._ttl("intraday_bars", 15))

    def get_news(self, symbol: str | None = None, topic: str | None = None, limit: int = 10) -> ProviderResponse:
        return self._request("news", {"s": symbol, "t": topic, "limit": limit}, self._ttl("news", 60))

    def get_fundamentals(self, symbol: str) -> ProviderResponse:
        return self._request(f"fundamentals/{symbol}", {}, self._ttl("fundamentals", 1440))

    def get_technical_indicators(self, symbol: str, function: str = "sma", period: int = 50) -> ProviderResponse:
        return self._request(f"technical/{symbol}", {"function": function, "period": period}, self._ttl("technicals", 60))

    def get_screener_results(self, filters: dict[str, Any] | None = None, limit: int = 100) -> ProviderResponse:
        params = {"limit": limit}
        if filters:
            params.update(filters)
        return self._request("screener", params, self._ttl("screener", 60))
