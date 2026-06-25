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

    def get_capability(self, provider: str, endpoint_name: str) -> dict[str, Any] | None:
        rows = self.storage.fetch_all(
            "SELECT * FROM data_provider_capabilities WHERE provider=? AND endpoint_name=?",
            (provider, endpoint_name),
        )
        return rows[0] if rows else None

    def capability_disabled(self, provider: str, endpoint_name: str) -> bool:
        row = self.get_capability(provider, endpoint_name)
        if row and row.get("last_error_category") == "no_data":
            return False
        disabled_until = row.get("disabled_until") if row else None
        if not disabled_until:
            return False
        try:
            return datetime.fromisoformat(str(disabled_until).replace("Z", "+00:00")).astimezone(UTC) > datetime.now(UTC)
        except Exception:
            return False

    def record_capability(
        self,
        provider: str,
        endpoint_name: str,
        *,
        status: str,
        run_id: str | None = None,
        status_code: int | None = None,
        error_category: str | None = None,
        used_for_scoring: bool = False,
        cooldown_minutes: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        current = self.get_capability(provider, endpoint_name)
        failure_count = int(current.get("failure_count") or 0) if current else 0
        available = status == "ok"
        plan_limited = status == "plan_limited" or error_category in {"forbidden", "not_found", "http_402"}
        if available:
            failure_count = 0
            last_success_at = now.isoformat()
            last_failure_at = current.get("last_failure_at") if current else None
            disabled_until = None
        else:
            failure_count += 1
            last_success_at = current.get("last_success_at") if current else None
            last_failure_at = now.isoformat()
            disabled_until = (now + timedelta(minutes=cooldown_minutes)).isoformat() if cooldown_minutes else current.get("disabled_until") if current else None
        self.storage.execute(
            """
            INSERT INTO data_provider_capabilities(
                id,run_id,provider,endpoint_name,available,plan_limited,last_success_at,last_failure_at,
                failure_count,last_status_code,last_error_category,disabled_until,retry_after,used_for_scoring,updated_at,detail
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(endpoint_name) DO UPDATE SET
                run_id=excluded.run_id,
                provider=excluded.provider,
                available=excluded.available,
                plan_limited=excluded.plan_limited,
                last_success_at=excluded.last_success_at,
                last_failure_at=excluded.last_failure_at,
                failure_count=excluded.failure_count,
                last_status_code=excluded.last_status_code,
                last_error_category=excluded.last_error_category,
                disabled_until=excluded.disabled_until,
                retry_after=excluded.retry_after,
                used_for_scoring=excluded.used_for_scoring,
                updated_at=excluded.updated_at,
                detail=excluded.detail
            """,
            (
                f"{provider}-{endpoint_name}",
                run_id,
                provider,
                endpoint_name,
                int(available),
                int(plan_limited),
                last_success_at,
                last_failure_at,
                failure_count,
                status_code,
                error_category,
                disabled_until,
                disabled_until,
                int(used_for_scoring),
                now.isoformat(),
                json_dumps(detail or {}),
            ),
        )
