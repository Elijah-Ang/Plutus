from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.storage import Storage
from app.utils import iso_now, json_dumps


def cache_key(provider: str, endpoint: str, params: dict[str, Any]) -> str:
    material = json_dumps({"endpoint": endpoint, "params": params, "provider": provider})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ProviderCache:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def get(self, provider: str, endpoint: str, params: dict[str, Any]) -> Any | None:
        key = cache_key(provider, endpoint, params)
        rows = self.storage.fetch_all(
            "SELECT payload, expires_at FROM data_provider_cache_index WHERE cache_key=? AND provider=? AND endpoint=?",
            (key, provider, endpoint),
        )
        if not rows:
            return None
        expires_at = rows[0].get("expires_at")
        if expires_at and datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).astimezone(UTC) <= datetime.now(UTC):
            return None
        try:
            return json.loads(rows[0].get("payload") or "null")
        except json.JSONDecodeError:
            return None

    def set(self, provider: str, endpoint: str, params: dict[str, Any], payload: Any, ttl_minutes: int, status: str = "ok") -> None:
        key = cache_key(provider, endpoint, params)
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=max(1, int(ttl_minutes)))
        symbol = params.get("symbol") or params.get("query") or params.get("exchange")
        self.storage.execute(
            """
            INSERT INTO data_provider_cache_index(
                id,provider,endpoint,cache_key,symbol,fetched_at,expires_at,status,payload
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cache_key) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                expires_at=excluded.expires_at,
                status=excluded.status,
                payload=excluded.payload
            """,
            (key, provider, endpoint, key, symbol, now.isoformat(), expires.isoformat(), status, json_dumps(payload)),
        )

    def record_health(self, provider: str, status: str, run_id: str | None = None, detail: dict[str, Any] | None = None) -> None:
        detail = detail or {}
        self.storage.execute(
            "INSERT INTO data_provider_health(id,run_id,provider,status,checked_at,rate_limit_remaining,error,detail) VALUES(?,?,?,?,?,?,?,?)",
            (
                f"{provider}-{iso_now()}",
                run_id,
                provider,
                status,
                iso_now(),
                detail.get("rate_limit_remaining"),
                detail.get("error"),
                json_dumps(detail),
            ),
        )
