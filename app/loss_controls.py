"""Explicit, versioned loss-control metrics used by proposal and final risk."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .lot_ledger import KNOWN_CONFIDENCE

LOSS_METRICS_VERSION = "loss_controls_v2"


@dataclass(frozen=True)
class LossMetrics:
    daily_loss_dollars: float | None
    weekly_loss_dollars: float | None
    daily_loss_pct: float | None
    weekly_loss_pct: float | None
    loss_reference_equity: float | None
    daily_loss_confidence: str
    weekly_loss_confidence: str
    loss_provenance: str
    metrics_version: str = LOSS_METRICS_VERSION

    def as_context(self) -> dict[str, Any]:
        return {
            "daily_loss_dollars": self.daily_loss_dollars,
            "weekly_loss_dollars": self.weekly_loss_dollars,
            "daily_loss_pct": self.daily_loss_pct,
            "weekly_loss_pct": self.weekly_loss_pct,
            "loss_reference_equity": self.loss_reference_equity,
            "daily_loss_confidence": self.daily_loss_confidence,
            "weekly_loss_confidence": self.weekly_loss_confidence,
            "loss_provenance": self.loss_provenance,
            "loss_metrics_version": self.metrics_version,
        }


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def build_loss_metrics(
    broker_metrics: Mapping[str, Any] | None,
    *,
    account_equity: Any,
    daily_realized_pl: Any = None,
    weekly_realized_pl: Any = None,
    daily_confidence: str = "unavailable",
    weekly_confidence: str = "unavailable",
    realized_provenance: str | None = None,
) -> LossMetrics:
    broker = broker_metrics or {}
    broker_reference = _number(broker.get("reference_equity"))
    account_reference = _number(account_equity)
    reference = broker_reference if broker_reference and broker_reference > 0 else account_reference

    daily_candidates: list[tuple[float, str, str]] = []
    weekly_candidates: list[tuple[float, str, str]] = []
    daily_broker = _number(broker.get("daily_loss_dollars"))
    weekly_broker = _number(broker.get("weekly_loss_dollars"))
    if daily_broker is not None and daily_broker >= 0 and reference and reference > 0:
        daily_candidates.append((daily_broker, str(broker.get("daily_loss_confidence") or "verified"), "broker_account"))
    if weekly_broker is not None and weekly_broker >= 0 and reference and reference > 0:
        weekly_candidates.append((weekly_broker, str(broker.get("weekly_loss_confidence") or "verified"), "broker_history"))

    daily_pl = _number(daily_realized_pl)
    weekly_pl = _number(weekly_realized_pl)
    if daily_pl is not None and daily_confidence in KNOWN_CONFIDENCE and reference and reference > 0:
        daily_candidates.append((max(0.0, -daily_pl), daily_confidence, realized_provenance or "lot_ledger"))
    if weekly_pl is not None and weekly_confidence in KNOWN_CONFIDENCE and reference and reference > 0:
        weekly_candidates.append((max(0.0, -weekly_pl), weekly_confidence, realized_provenance or "lot_ledger"))

    def combine(candidates: list[tuple[float, str, str]]) -> tuple[float | None, float | None, str, list[str]]:
        if not candidates or reference is None or reference <= 0:
            return None, None, "unavailable", []
        dollars = max(item[0] for item in candidates)
        confidence = "verified" if any(item[1] == "verified" for item in candidates) else "reconstructed"
        provenance = [item[2] for item in candidates]
        return dollars, dollars / reference * 100.0, confidence, provenance

    daily_dollars, daily_pct, daily_result_confidence, daily_sources = combine(daily_candidates)
    weekly_dollars, weekly_pct, weekly_result_confidence, weekly_sources = combine(weekly_candidates)
    sources = list(dict.fromkeys(daily_sources + weekly_sources))
    return LossMetrics(
        daily_loss_dollars=daily_dollars,
        weekly_loss_dollars=weekly_dollars,
        daily_loss_pct=daily_pct,
        weekly_loss_pct=weekly_pct,
        loss_reference_equity=reference,
        daily_loss_confidence=daily_result_confidence,
        weekly_loss_confidence=weekly_result_confidence,
        loss_provenance=";".join(sources) if sources else "unavailable",
    )
