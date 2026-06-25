from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderHealth:
    provider: str
    status: str
    error: str | None = None
    calls_this_run: int = 0
    max_calls_per_run: int | None = None

    @property
    def healthy(self) -> bool:
        return self.status == "ok"
