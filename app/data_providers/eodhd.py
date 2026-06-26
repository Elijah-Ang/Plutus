from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError
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
        self.plan_limited_cooldown_minutes = int(self.provider_cfg.get("plan_limited_cooldown_minutes", 1440))
        self.plan_limited_reprobe_minutes = int(self.provider_cfg.get("plan_limited_reprobe_minutes", 60))

    def _ttl(self, name: str, default: int) -> int:
        return int(self.ttls.get(name, default))

    def _capability_name(self, endpoint: str) -> str:
        endpoint = endpoint.strip("/")
        if endpoint.startswith("exchange-symbol-list"):
            return "exchange_symbol_list"
        if endpoint.startswith("eod/"):
            return "eod_bars"
        if endpoint.startswith("real-time/"):
            return "realtime_quote"
        if endpoint.startswith("news"):
            return "news"
        if endpoint.startswith("intraday/"):
            return "intraday_bars"
        if endpoint.startswith("fundamentals/"):
            return "fundamentals"
        if endpoint.startswith("technical/"):
            return "technicals"
        if endpoint.startswith("screener"):
            return "screener"
        if endpoint == "user":
            return "health"
        return endpoint.replace("/", "_")

    def _classify_http_error(self, status_code: int) -> tuple[str, str]:
        if status_code in {401, 402, 403, 404, 451}:
            return "plan_limited", {401: "unauthorized", 402: "http_402", 403: "forbidden", 404: "not_found", 451: "unavailable_for_legal_reasons"}.get(status_code, "permission")
        if status_code == 429:
            return "rate_limited", "per_minute_rate_limited"
        if status_code == 422:
            return "provider_unavailable", "no_data"
        return "provider_unavailable", f"http_{status_code}"

    def _request(self, endpoint: str, params: dict[str, Any], cache_ttl: int, *, used_for_scoring: bool = False) -> ProviderResponse:
        capability = self._capability_name(endpoint)
        if not self.api_key:
            self.cache.record_health(self.name, "provider_unavailable", self.run_id, {"error": "missing_api_key"})
            self.cache.record_capability(self.name, capability, status="missing_api_key", run_id=self.run_id, error_category="missing_api_key", used_for_scoring=used_for_scoring, detail={"error": "missing_api_key"})
            return ProviderResponse(self.name, endpoint, "provider_unavailable", None, "missing_api_key")

        clean_params = {k: v for k, v in params.items() if v is not None}
        cached = self.cache.get(self.name, endpoint, clean_params)
        if cached is not None:
            return ProviderResponse(self.name, endpoint, "ok", cached, cached=True)
        if self.cache.capability_disabled(self.name, capability, reprobe_after_minutes=self.plan_limited_reprobe_minutes, current_run_id=self.run_id):
            row = self.cache.get_capability(self.name, capability) or {}
            last_error = str(row.get("last_error_category") or "capability_disabled")
            if int(row.get("plan_limited") or 0):
                status = "plan_limited"
            elif last_error in {"rate_limited", "per_minute_rate_limited", "cooldown_active"}:
                status = "rate_limited"
            else:
                status = "provider_unavailable"
            return ProviderResponse(self.name, endpoint, status, None, last_error)

        if self.calls_this_run >= self.max_calls_per_run:
            self.cache.record_health(self.name, "rate_limited", self.run_id, {"max_calls_per_run": self.max_calls_per_run})
            self.cache.record_capability(self.name, capability, status="rate_limited", run_id=self.run_id, error_category="cooldown_active", used_for_scoring=used_for_scoring, cooldown_minutes=60, detail={"max_calls_per_run": self.max_calls_per_run})
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
                self.cache.record_capability(self.name, capability, status="ok", run_id=self.run_id, used_for_scoring=used_for_scoring, detail={"endpoint": endpoint})
                return ProviderResponse(self.name, endpoint, "ok", data)
            except HTTPError as exc:
                status, category = self._classify_http_error(exc.code)
                last_error = category
                if status in {"plan_limited", "rate_limited"}:
                    cooldown = self.plan_limited_cooldown_minutes if status == "plan_limited" else 60
                    self.cache.record_health(self.name, status, self.run_id, {"endpoint": endpoint, "error": category, "status_code": exc.code})
                    self.cache.record_capability(
                        self.name,
                        capability,
                        status=status,
                        run_id=self.run_id,
                        status_code=exc.code,
                        error_category=category,
                        used_for_scoring=used_for_scoring,
                        cooldown_minutes=cooldown,
                        detail={"endpoint": endpoint, "status_code": exc.code},
                    )
                    return ProviderResponse(self.name, endpoint, status, None, category)
                if attempt < self.max_retries:
                    time.sleep(self.backoff)
            except Exception as exc:
                last_error = type(exc).__name__
                if attempt < self.max_retries:
                    time.sleep(self.backoff)
        self.cache.record_health(self.name, "provider_unavailable", self.run_id, {"endpoint": endpoint, "error": last_error})
        cooldown = None if last_error == "no_data" else 15
        self.cache.record_capability(self.name, capability, status="provider_unavailable", run_id=self.run_id, error_category=str(last_error), used_for_scoring=used_for_scoring, cooldown_minutes=cooldown, detail={"endpoint": endpoint})
        return ProviderResponse(self.name, endpoint, "provider_unavailable", None, last_error)

    def health(self) -> ProviderResponse:
        if not self.api_key:
            return ProviderResponse(self.name, "health", "provider_unavailable", None, "missing_api_key")
        return self._request("user", {}, self._ttl("symbols", 1440))

    def list_symbols(self, exchange: str = "US", limit: int | None = None) -> ProviderResponse:
        res = self._request(f"exchange-symbol-list/{exchange}", {}, self._ttl("symbols", 1440), used_for_scoring=True)
        if res.status == "ok" and isinstance(res.data, list) and limit:
            return ProviderResponse(res.provider, res.endpoint, res.status, res.data[:limit], cached=res.cached)
        return res

    def search_symbols(self, query: str, limit: int | None = None) -> ProviderResponse:
        res = self._request("search", {"q": query, "limit": limit}, self._ttl("symbols", 1440))
        return res

    def get_historical_bars(self, symbol: str, period: str = "d", limit: int = 250) -> ProviderResponse:
        res = self._request(f"eod/{symbol}", {"period": period, "order": "d"}, self._ttl("daily_bars", 720), used_for_scoring=True)
        if res.status == "ok" and isinstance(res.data, list):
            return ProviderResponse(res.provider, res.endpoint, res.status, res.data[-limit:], cached=res.cached)
        return res

    def get_intraday_bars(self, symbol: str, interval: str = "5m", limit: int = 100) -> ProviderResponse:
        res = self._request(f"intraday/{symbol}", {"interval": interval}, self._ttl("intraday_bars", 15), used_for_scoring=True)
        if res.status == "ok" and isinstance(res.data, list):
            return ProviderResponse(res.provider, res.endpoint, res.status, res.data[-limit:], cached=res.cached)
        return res

    def get_latest_quote(self, symbol: str) -> ProviderResponse:
        return self._request(f"real-time/{symbol}", {}, self._ttl("intraday_bars", 15), used_for_scoring=True)

    def get_news(self, symbol: str | None = None, topic: str | None = None, limit: int = 10) -> ProviderResponse:
        return self._request("news", {"s": symbol, "t": topic, "limit": limit}, self._ttl("news", 60), used_for_scoring=True)

    def get_fundamentals(self, symbol: str) -> ProviderResponse:
        return self._request(f"fundamentals/{symbol}", {}, self._ttl("fundamentals", 1440), used_for_scoring=True)

    def get_technical_indicators(self, symbol: str, function: str = "sma", period: int = 50) -> ProviderResponse:
        return self._request(f"technical/{symbol}", {"function": function, "period": period}, self._ttl("technicals", 60), used_for_scoring=True)

    def get_screener_results(self, filters: dict[str, Any] | None = None, limit: int = 100) -> ProviderResponse:
        params = {"limit": limit}
        if filters:
            params.update(filters)
        return self._request("screener", params, self._ttl("screener", 60), used_for_scoring=True)
