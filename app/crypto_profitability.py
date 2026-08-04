"""Exact, append-only profitability authority for crypto candidates.

Crypto research is allowed to produce a signal, but a signal is not a
profitability estimate.  This module turns only persisted, replayable crypto
outcomes plus an explicitly verified portfolio-correlation snapshot into the
evidence consumed by the cross-asset allocator.  Missing evidence is a
rejection, never a default probability or holding period.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .crypto_outcomes import (
    BetaPrior,
    VerifiedCorrelationSnapshot,
    canonical_decimal_text,
    canonical_json,
    derive_aggregate_metrics,
    validate_actual_evidence,
)
from .profitability_validation import (
    PROFITABILITY_VALIDATION_FORMULA_VERSION,
    ProfitabilityValidationDecision,
    ProfitabilityValidationPolicy,
    ProfitabilityValidationStore,
    ValidationHypothesis,
    ValidationObservation,
    policy_from_config,
    validate_profitability_family,
)
from .crypto_strategies import SUPPORTED_STRATEGIES
from .formula_versions import (
    CRYPTO_PROFITABILITY_FORMULA_VERSION,
    CRYPTO_PROFITABILITY_SCHEMA_VERSION,
)
from .utils import iso_now


TABLE_NAME = "crypto_profitability_decisions"


class CryptoProfitabilityError(ValueError):
    """Raised when crypto profitability authority is missing or inconsistent."""


def _text(value: Decimal | None) -> str | None:
    return None if value is None else canonical_decimal_text(value)


def _decimal(value: Any, field: str, *, minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CryptoProfitabilityError(f"{field} is missing")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoProfitabilityError(f"{field} is invalid") from exc
    if not result.is_finite() or (minimum is not None and result < minimum):
        raise CryptoProfitabilityError(f"{field} is outside its safe range")
    return result


def _required(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise CryptoProfitabilityError(f"{field} is required")
    return result


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc(value: Any, field: str) -> datetime:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError) as exc:
        raise CryptoProfitabilityError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise CryptoProfitabilityError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _validation_hypothesis_id(symbol: str, strategy_version: str) -> str:
    return f"crypto:{symbol.upper()}:{strategy_version}"


def _validation_policy(value: Any) -> ProfitabilityValidationPolicy:
    if value is None:
        return ProfitabilityValidationPolicy()
    if isinstance(value, ProfitabilityValidationPolicy):
        return value
    if isinstance(value, Mapping):
        # Accept either the complete application config or the policy mapping
        # itself.  The production path passes the complete config so the
        # crypto lane uses the same explicit policy as Performance Lab.
        if "profitability_validation" in value:
            return policy_from_config(value)
        allowed = {
            field.name
            for field in ProfitabilityValidationPolicy.__dataclass_fields__.values()
        }
        return ProfitabilityValidationPolicy(
            **{key: value[key] for key in allowed if key in value}
        )
    raise CryptoProfitabilityError("validation_policy must be a validation policy or mapping")


@dataclass(frozen=True, slots=True)
class CryptoProfitabilityDecision:
    decision_id: str
    symbol: str
    strategy_version: str
    strategy_decision_id: str
    strategy_decision_fingerprint: str
    config_hash: str
    sample_count: int
    unavailable_count: int
    win_probability: Decimal | None
    severe_loss_rate: Decimal | None
    uncertainty: Decimal | None
    mean_net_return: Decimal | None
    average_holding_hours: Decimal | None
    correlation_to_portfolio: Decimal | None
    correlation_snapshot_id: str | None
    correlation_snapshot_fingerprint: str | None
    minimum_samples: int
    severe_loss_threshold: Decimal
    minimum_mean_net_return: Decimal
    require_verified_correlation: bool
    validation_family_id: str
    validation_decision_id: str
    validation_decision_fingerprint: str
    walk_forward_status: str
    validation_sample_count: int
    validation_fold_count: int
    validation_lower_net_r: Decimal | None
    validation_fdr_q_value: Decimal
    validation_reason: str
    validation_status: str
    rejection_reasons: tuple[str, ...]
    observation_fingerprints: tuple[str, ...]
    input_fingerprint: str
    decision_fingerprint: str
    formula_version: str = CRYPTO_PROFITABILITY_FORMULA_VERSION
    schema_version: str = CRYPTO_PROFITABILITY_SCHEMA_VERSION

    @property
    def eligible(self) -> bool:
        return self.validation_status == "validated" and not self.rejection_reasons

    def metrics(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "unavailable_count": self.unavailable_count,
            "win_probability": _text(self.win_probability),
            "severe_loss_rate": _text(self.severe_loss_rate),
            "uncertainty": _text(self.uncertainty),
            "mean_net_return": _text(self.mean_net_return),
            "average_holding_hours": _text(self.average_holding_hours),
            "correlation_to_portfolio": _text(self.correlation_to_portfolio),
            "minimum_samples": self.minimum_samples,
            "severe_loss_threshold": _text(self.severe_loss_threshold),
            "minimum_mean_net_return": _text(self.minimum_mean_net_return),
            "require_verified_correlation": self.require_verified_correlation,
            "walk_forward_validation": {
                "family_id": self.validation_family_id,
                "decision_id": self.validation_decision_id,
                "decision_fingerprint": self.validation_decision_fingerprint,
                "status": self.walk_forward_status,
                "sample_count": self.validation_sample_count,
                "fold_count": self.validation_fold_count,
                "bootstrap_lower_net_r": _text(self.validation_lower_net_r),
                "fdr_q_value": _text(self.validation_fdr_q_value),
                "reason": self.validation_reason,
            },
            "validation_status": self.validation_status,
            "rejection_reasons": list(self.rejection_reasons),
        }


_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME}(
  decision_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  strategy_decision_id TEXT NOT NULL,
  strategy_decision_fingerprint TEXT NOT NULL,
  config_hash TEXT NOT NULL,
  sample_count INTEGER NOT NULL CHECK(sample_count >= 0),
  unavailable_count INTEGER NOT NULL CHECK(unavailable_count >= 0),
  win_probability TEXT,
  severe_loss_rate TEXT,
  uncertainty TEXT,
  mean_net_return TEXT,
  average_holding_hours TEXT,
  correlation_to_portfolio TEXT,
  correlation_snapshot_id TEXT,
  correlation_snapshot_fingerprint TEXT,
  minimum_samples INTEGER NOT NULL CHECK(minimum_samples > 0),
  severe_loss_threshold TEXT NOT NULL,
  minimum_mean_net_return TEXT NOT NULL,
  require_verified_correlation INTEGER NOT NULL CHECK(require_verified_correlation IN (0,1)),
  validation_family_id TEXT NOT NULL,
  validation_decision_id TEXT NOT NULL,
  validation_decision_fingerprint TEXT NOT NULL,
  walk_forward_status TEXT NOT NULL CHECK(walk_forward_status IN ('validated','failed','insufficient')),
  validation_sample_count INTEGER NOT NULL CHECK(validation_sample_count >= 0),
  validation_fold_count INTEGER NOT NULL CHECK(validation_fold_count >= 0),
  validation_lower_net_r TEXT,
  validation_fdr_q_value TEXT NOT NULL,
  validation_reason TEXT NOT NULL,
  validation_status TEXT NOT NULL CHECK(validation_status IN ('validated','rejected')),
  rejection_reasons_json TEXT NOT NULL,
  observation_fingerprints_json TEXT NOT NULL,
  input_fingerprint TEXT NOT NULL UNIQUE,
  decision_fingerprint TEXT NOT NULL UNIQUE,
  formula_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crypto_profitability_decisions_lookup
  ON {TABLE_NAME}(symbol,strategy_version,config_hash,created_at);
CREATE TRIGGER IF NOT EXISTS trg_crypto_profitability_decisions_append_only_update
  BEFORE UPDATE ON {TABLE_NAME}
BEGIN
  SELECT RAISE(ABORT, 'crypto profitability decisions are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_crypto_profitability_decisions_append_only_delete
  BEFORE DELETE ON {TABLE_NAME}
BEGIN
  SELECT RAISE(ABORT, 'crypto profitability decisions are append-only');
END;
"""


def apply_crypto_profitability_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    conn.executescript(_SCHEMA_SQL)
    # The table was introduced before walk-forward/FDR evidence became a
    # binding candidate gate.  Keep old rows immutable and unusable rather
    # than backfilling unverifiable validation identities.
    existing = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{TABLE_NAME}")')}
    additive = {
        "validation_family_id": "TEXT",
        "validation_decision_id": "TEXT",
        "validation_decision_fingerprint": "TEXT",
        "walk_forward_status": "TEXT",
        "validation_sample_count": "INTEGER",
        "validation_fold_count": "INTEGER",
        "validation_lower_net_r": "TEXT",
        "validation_fdr_q_value": "TEXT",
        "validation_reason": "TEXT",
    }
    for column, definition in additive.items():
        if column not in existing:
            conn.execute(f'ALTER TABLE "{TABLE_NAME}" ADD COLUMN "{column}" {definition}')
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                CRYPTO_PROFITABILITY_SCHEMA_VERSION,
                iso_now(),
                "append-only cost-adjusted crypto profitability decisions bound to walk-forward/FDR validation",
            ),
        )


def _row_to_decision(row: Mapping[str, Any]) -> CryptoProfitabilityDecision:
    try:
        reasons = tuple(json.loads(row["rejection_reasons_json"]))
        fingerprints = tuple(json.loads(row["observation_fingerprints_json"]))
        metrics = json.loads(row["metrics_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CryptoProfitabilityError("persisted crypto profitability JSON is invalid") from exc
    if not isinstance(reasons, tuple) or not all(isinstance(item, str) for item in reasons):
        raise CryptoProfitabilityError("persisted crypto profitability rejection reasons are invalid")
    if not isinstance(fingerprints, tuple) or not all(isinstance(item, str) for item in fingerprints):
        raise CryptoProfitabilityError("persisted crypto profitability observation evidence is invalid")
    if not isinstance(metrics, dict):
        raise CryptoProfitabilityError("persisted crypto profitability metrics are invalid")
    result = CryptoProfitabilityDecision(
        decision_id=str(row["decision_id"]),
        symbol=str(row["symbol"]),
        strategy_version=str(row["strategy_version"]),
        strategy_decision_id=str(row["strategy_decision_id"]),
        strategy_decision_fingerprint=str(row["strategy_decision_fingerprint"]),
        config_hash=str(row["config_hash"]),
        sample_count=int(row["sample_count"]),
        unavailable_count=int(row["unavailable_count"]),
        win_probability=None if row["win_probability"] is None else Decimal(str(row["win_probability"])),
        severe_loss_rate=None if row["severe_loss_rate"] is None else Decimal(str(row["severe_loss_rate"])),
        uncertainty=None if row["uncertainty"] is None else Decimal(str(row["uncertainty"])),
        mean_net_return=None if row["mean_net_return"] is None else Decimal(str(row["mean_net_return"])),
        average_holding_hours=None if row["average_holding_hours"] is None else Decimal(str(row["average_holding_hours"])),
        correlation_to_portfolio=None if row["correlation_to_portfolio"] is None else Decimal(str(row["correlation_to_portfolio"])),
        correlation_snapshot_id=None if row["correlation_snapshot_id"] is None else str(row["correlation_snapshot_id"]),
        correlation_snapshot_fingerprint=None if row["correlation_snapshot_fingerprint"] is None else str(row["correlation_snapshot_fingerprint"]),
        minimum_samples=int(row["minimum_samples"]),
        severe_loss_threshold=Decimal(str(row["severe_loss_threshold"])),
        minimum_mean_net_return=Decimal(str(row["minimum_mean_net_return"])),
        require_verified_correlation=bool(row["require_verified_correlation"]),
        validation_family_id=_required(row.get("validation_family_id"), "validation_family_id"),
        validation_decision_id=_required(row.get("validation_decision_id"), "validation_decision_id"),
        validation_decision_fingerprint=_required(
            row.get("validation_decision_fingerprint"),
            "validation_decision_fingerprint",
        ),
        walk_forward_status=_required(row.get("walk_forward_status"), "walk_forward_status"),
        validation_sample_count=int(row["validation_sample_count"]),
        validation_fold_count=int(row["validation_fold_count"]),
        validation_lower_net_r=(
            None
            if row.get("validation_lower_net_r") is None
            else Decimal(str(row["validation_lower_net_r"]))
        ),
        validation_fdr_q_value=Decimal(str(row["validation_fdr_q_value"])),
        validation_reason=_required(row.get("validation_reason"), "validation_reason"),
        validation_status=str(row["validation_status"]),
        rejection_reasons=reasons,
        observation_fingerprints=fingerprints,
        input_fingerprint=str(row["input_fingerprint"]),
        decision_fingerprint=str(row["decision_fingerprint"]),
        formula_version=str(row["formula_version"]),
        schema_version=str(row["schema_version"]),
    )
    expected_metrics = result.metrics()
    if metrics != expected_metrics:
        raise CryptoProfitabilityError("persisted crypto profitability metrics drifted")
    if result.formula_version != CRYPTO_PROFITABILITY_FORMULA_VERSION or result.schema_version != CRYPTO_PROFITABILITY_SCHEMA_VERSION:
        raise CryptoProfitabilityError("crypto profitability decision version is obsolete")
    return result


class CryptoProfitabilityStore:
    """Build, persist, and independently verify crypto profitability evidence."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def _observation_rows(
        self,
        *,
        symbol: str,
        strategy_version: str,
        config_hash: str,
    ) -> list[dict[str, Any]]:
        rows = self.storage.fetch_all(
            """
            SELECT o.*, d.selected_strategy, d.decision_fingerprint AS persisted_strategy_fingerprint,
                   d.config_hash AS strategy_config_hash
            FROM crypto_profitability_observations o
            JOIN crypto_strategy_decisions d ON d.id=o.strategy_decision_id
            WHERE o.symbol=? AND d.symbol=? AND d.selected_strategy=? AND d.config_hash=?
            ORDER BY o.research_timestamp,o.observation_id
            """,
            (symbol, symbol, strategy_version, config_hash),
        )
        verified: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("strategy_decision_fingerprint") or "") != str(row.get("persisted_strategy_fingerprint") or ""):
                raise CryptoProfitabilityError("crypto outcome strategy fingerprint does not match strategy authority")
            if str(row.get("strategy_config_hash") or "") != config_hash:
                raise CryptoProfitabilityError("crypto outcome strategy configuration is not current")
            if str(row.get("evidence_type") or "").lower() == "actual":
                try:
                    lineage = json.loads(row.get("actual_lineage_json") or "")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CryptoProfitabilityError("actual crypto outcome lineage is invalid") from exc
                validation = validate_actual_evidence(lineage)
                if not validation.valid:
                    raise CryptoProfitabilityError("actual crypto outcome lineage is not verified")
            verified.append(row)
        return verified

    def _validation_rows(
        self,
        *,
        symbol: str,
        config_hash: str,
    ) -> list[dict[str, Any]]:
        """Load every crypto hypothesis row in the current FDR family."""

        rows = self.storage.fetch_all(
            """
            SELECT o.*, d.selected_strategy,
                   d.decision_fingerprint AS persisted_strategy_fingerprint,
                   d.config_hash AS strategy_config_hash
            FROM crypto_profitability_observations o
            JOIN crypto_strategy_decisions d ON d.id=o.strategy_decision_id
            WHERE o.symbol=? AND d.symbol=? AND d.config_hash=?
              AND d.selected_strategy IS NOT NULL
            ORDER BY o.research_timestamp,o.observation_id
            """,
            (symbol, symbol, config_hash),
        )
        verified: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("strategy_decision_fingerprint") or "") != str(
                row.get("persisted_strategy_fingerprint") or ""
            ):
                raise CryptoProfitabilityError(
                    "crypto outcome strategy fingerprint does not match strategy authority"
                )
            if str(row.get("strategy_config_hash") or "") != config_hash:
                raise CryptoProfitabilityError(
                    "crypto outcome strategy configuration is not current"
                )
            if str(row.get("evidence_type") or "").lower() == "actual":
                try:
                    lineage = json.loads(row.get("actual_lineage_json") or "")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CryptoProfitabilityError(
                        "actual crypto outcome lineage is invalid"
                    ) from exc
                validation = validate_actual_evidence(lineage)
                if not validation.valid:
                    raise CryptoProfitabilityError(
                        "actual crypto outcome lineage is not verified"
                    )
            verified.append(row)
        return verified

    def _build_validation_family(
        self,
        *,
        symbol: str,
        strategy_version: str,
        config_hash: str,
        as_of_limit: datetime,
        policy: ProfitabilityValidationPolicy,
        validation_configuration: Mapping[str, Any] | None,
    ) -> tuple[Any, ProfitabilityValidationDecision]:
        """Build the immutable crypto validation family and current member."""

        rows = self._validation_rows(symbol=symbol, config_hash=config_hash)
        # The crypto evaluator predeclares this complete strategy family even
        # when only one member is selected for a particular research run.
        # Otherwise BH correction would silently shrink to whichever member
        # happened to produce a row first.
        strategy_versions = set(SUPPORTED_STRATEGIES)
        strategy_versions.add(strategy_version)
        declared_rows = self.storage.fetch_all(
            """
            SELECT DISTINCT selected_strategy
            FROM crypto_strategy_decisions
            WHERE symbol=? AND config_hash=? AND selected_strategy IS NOT NULL
              AND created_at<=?
            """,
            (symbol, config_hash, as_of_limit.isoformat()),
        )
        strategy_versions.update(
            _required(row.get("selected_strategy"), "selected_strategy")
            for row in declared_rows
        )
        observations: list[ValidationObservation] = []
        required_economic_fields = (
            "quantity",
            "exit_timestamp",
            "exit_price",
            "holding_hours",
            "gross_return",
            "net_return",
            "gross_pnl",
            "net_pnl",
            "risk_amount",
            "gross_r_multiple",
            "net_r_multiple",
        )
        for row in rows:
            selected = _required(row.get("selected_strategy"), "selected_strategy")
            strategy_versions.add(selected)
            if str(row.get("status") or "").lower() != "completed":
                continue
            if any(row.get(field) in (None, "") for field in required_economic_fields):
                # A closed row without complete quantity, P&L, risk, return,
                # and lifecycle evidence is unavailable.  It must not become
                # a validation sample merely because net_return exists.
                continue
            if row.get("exit_timestamp") is None:
                raise CryptoProfitabilityError(
                    "completed crypto validation outcome is missing exit_timestamp"
                )
            observed_at = _utc(row.get("research_timestamp"), "research_timestamp")
            outcome_end_at = _utc(row.get("exit_timestamp"), "exit_timestamp")
            if observed_at > as_of_limit or outcome_end_at > as_of_limit:
                # Point-in-time validation must not consume evidence that was
                # not closed at the decision's evaluation time.
                continue
            evidence_type = str(row.get("evidence_type") or "").lower()
            evidence_class = {
                "shadow": "shadow_oos",
                "actual": "actual_paper",
            }.get(evidence_type)
            if evidence_class is None:
                raise CryptoProfitabilityError(
                    "crypto validation evidence type is unsupported"
                )
            observations.append(
                ValidationObservation(
                    id=_required(row.get("observation_id"), "observation_id"),
                    hypothesis_id=_validation_hypothesis_id(symbol, selected),
                    strategy_version=selected,
                    observed_at=observed_at.isoformat(),
                    outcome_end_at=outcome_end_at.isoformat(),
                    net_r=row.get("net_r_multiple"),
                    evidence_class=evidence_class,
                    source_id=_required(row.get("input_fingerprint"), "input_fingerprint"),
                )
            )

        hypotheses = tuple(
            ValidationHypothesis(
                hypothesis_id=_validation_hypothesis_id(symbol, version),
                strategy_version=version,
                # Crypto strategies are separate predeclared hypotheses.  The
                # family-level BH correction still controls multiplicity while
                # a singleton does not pretend to prove parameter stability.
                stability_group=_validation_hypothesis_id(symbol, version),
            )
            for version in sorted(strategy_versions)
        )
        family_as_of = (
            max(_utc(item.outcome_end_at, "outcome_end_at") for item in observations)
            if observations
            else datetime(1970, 1, 1, tzinfo=UTC)
        )
        configuration = dict(validation_configuration or {})
        configuration_version = str(
            configuration.get("configuration_schema_version")
            or "crypto_profitability_standalone"
        )
        formulas = dict(configuration.get("formula_versions") or {})
        formulas.setdefault(
            "profitability_validation", PROFITABILITY_VALIDATION_FORMULA_VERSION
        )
        family = validate_profitability_family(
            family_key=f"crypto_profitability:{symbol}:{config_hash}",
            as_of=family_as_of,
            hypotheses=hypotheses,
            observations=observations,
            policy=policy,
            configuration_version=configuration_version,
            config_hash=config_hash,
            formula_versions=formulas,
        )
        current_id = _validation_hypothesis_id(symbol, strategy_version)
        try:
            decision = next(
                item for item in family.decisions if item.hypothesis_id == current_id
            )
        except StopIteration as exc:
            raise CryptoProfitabilityError(
                "crypto validation family omitted the selected strategy"
            ) from exc
        return family, decision

    def build_for_strategy(
        self,
        *,
        symbol: str,
        strategy_version: str,
        strategy_decision_id: str,
        strategy_decision_fingerprint: str,
        config_hash: str,
        minimum_samples: int,
        severe_loss_threshold: Any,
        minimum_mean_net_return: Any,
        require_verified_correlation: bool,
        correlation_snapshot: VerifiedCorrelationSnapshot | Mapping[str, Any] | None,
        now: datetime | None = None,
        validation_policy: ProfitabilityValidationPolicy | Mapping[str, Any] | None = None,
        validation_configuration: Mapping[str, Any] | None = None,
    ) -> CryptoProfitabilityDecision:
        symbol = _required(symbol, "symbol").upper()
        strategy_version = _required(strategy_version, "strategy_version")
        decision_id = _required(strategy_decision_id, "strategy_decision_id")
        decision_fp = _required(strategy_decision_fingerprint, "strategy_decision_fingerprint")
        config_hash = _required(config_hash, "config_hash")
        if isinstance(minimum_samples, bool) or not isinstance(minimum_samples, int) or minimum_samples <= 0:
            raise CryptoProfitabilityError("minimum_samples must be a positive integer")
        threshold = _decimal(severe_loss_threshold, "severe_loss_threshold")
        minimum_mean = _decimal(minimum_mean_net_return, "minimum_mean_net_return")
        rows = self._observation_rows(symbol=symbol, strategy_version=strategy_version, config_hash=config_hash)
        observation_fingerprints = tuple(sorted(str(row["input_fingerprint"]) for row in rows))
        evaluation_time = (now or datetime.now(UTC)).astimezone(UTC)
        validation_family, validation_decision = self._build_validation_family(
            symbol=symbol,
            strategy_version=strategy_version,
            config_hash=config_hash,
            as_of_limit=evaluation_time,
            policy=_validation_policy(validation_policy),
            validation_configuration=validation_configuration,
        )
        correlation = None
        correlation_id = None
        correlation_fp = None
        if correlation_snapshot is not None:
            if isinstance(correlation_snapshot, VerifiedCorrelationSnapshot):
                correlation = correlation_snapshot.correlation
                correlation_id = correlation_snapshot.snapshot_id
                correlation_fp = correlation_snapshot.snapshot_fingerprint
            elif isinstance(correlation_snapshot, Mapping):
                if correlation_snapshot.get("verified") is True or str(correlation_snapshot.get("verification_status") or "").lower() == "verified":
                    raw_correlation = correlation_snapshot.get("correlation")
                    if raw_correlation is not None:
                        correlation = _decimal(raw_correlation, "correlation")
                        if correlation < Decimal("-1") or correlation > Decimal("1"):
                            raise CryptoProfitabilityError("correlation is outside [-1,1]")
                        correlation_id = _required(correlation_snapshot.get("snapshot_id"), "correlation snapshot id")
                        correlation_fp = _required(correlation_snapshot.get("snapshot_fingerprint"), "correlation snapshot fingerprint")
        metrics = derive_aggregate_metrics(
            rows,
            severe_loss_threshold=threshold,
            prior=BetaPrior(alpha=Decimal("1"), beta=Decimal("1")),
            verified_snapshot=(
                None
                if correlation is None
                else VerifiedCorrelationSnapshot(
                    snapshot_id=correlation_id or "correlation",
                    snapshot_fingerprint=correlation_fp or _fingerprint({"correlation": _text(correlation)}),
                    correlation=correlation,
                )
            ),
        )
        reasons: list[str] = []
        if len(rows) < minimum_samples:
            reasons.append("crypto_profitability_minimum_samples_not_met")
        if metrics["sample_count"] < minimum_samples:
            reasons.append("crypto_profitability_completed_samples_not_met")
        if metrics["mean_net_return"] is None:
            reasons.append("crypto_profitability_mean_return_unavailable")
        elif metrics["mean_net_return"] <= minimum_mean:
            reasons.append("crypto_profitability_mean_return_not_positive")
        if require_verified_correlation and correlation is None:
            reasons.append("crypto_profitability_verified_correlation_unavailable")
        if validation_decision.status == "insufficient":
            reasons.append("crypto_profitability_walk_forward_validation_insufficient")
        elif validation_decision.status == "failed":
            reasons.append("crypto_profitability_walk_forward_validation_failed")
        status = "validated" if not reasons else "rejected"
        validation_lower = (
            None
            if validation_decision.bootstrap_lower_net_r is None
            else Decimal(validation_decision.bootstrap_lower_net_r)
        )
        validation_q = Decimal(validation_decision.fdr_q_value)
        unfingerprinted = CryptoProfitabilityDecision(
            decision_id="",
            symbol=symbol,
            strategy_version=strategy_version,
            strategy_decision_id=decision_id,
            strategy_decision_fingerprint=decision_fp,
            config_hash=config_hash,
            sample_count=int(metrics["sample_count"]),
            unavailable_count=int(metrics["unavailable_count"]),
            win_probability=metrics["posterior_win_probability"],
            severe_loss_rate=metrics["severe_loss_rate"],
            uncertainty=metrics["posterior_uncertainty"],
            mean_net_return=metrics["mean_net_return"],
            average_holding_hours=metrics["average_holding_hours"],
            correlation_to_portfolio=correlation,
            correlation_snapshot_id=correlation_id,
            correlation_snapshot_fingerprint=correlation_fp,
            minimum_samples=minimum_samples,
            severe_loss_threshold=threshold,
            minimum_mean_net_return=minimum_mean,
            require_verified_correlation=bool(require_verified_correlation),
            validation_family_id=validation_family.id,
            validation_decision_id=validation_decision.id,
            validation_decision_fingerprint=validation_decision.decision_fingerprint,
            walk_forward_status=validation_decision.status,
            validation_sample_count=validation_decision.sample_count,
            validation_fold_count=validation_decision.fold_count,
            validation_lower_net_r=validation_lower,
            validation_fdr_q_value=validation_q,
            validation_reason=validation_decision.reason,
            validation_status=status,
            rejection_reasons=tuple(sorted(set(reasons))),
            observation_fingerprints=observation_fingerprints,
            input_fingerprint="",
            decision_fingerprint="",
        )
        body = {
            "symbol": symbol,
            "strategy_version": strategy_version,
            "strategy_decision_id": decision_id,
            "strategy_decision_fingerprint": decision_fp,
            "config_hash": config_hash,
            "minimum_samples": minimum_samples,
            "severe_loss_threshold": _text(threshold),
            "minimum_mean_net_return": _text(minimum_mean),
            "observation_fingerprints": list(observation_fingerprints),
            "correlation_snapshot_id": correlation_id,
            "correlation_snapshot_fingerprint": correlation_fp,
            "correlation": _text(correlation),
            "require_verified_correlation": bool(require_verified_correlation),
            "validation_family_id": validation_family.id,
            "validation_decision_id": validation_decision.id,
            "validation_decision_fingerprint": validation_decision.decision_fingerprint,
            "walk_forward_status": validation_decision.status,
            "validation_sample_count": validation_decision.sample_count,
            "validation_fold_count": validation_decision.fold_count,
            "validation_lower_net_r": _text(validation_lower),
            "validation_fdr_q_value": _text(validation_q),
            "validation_reason": validation_decision.reason,
            "metrics": unfingerprinted.metrics(),
        }
        input_fingerprint = _fingerprint(body)
        decision_fingerprint = _fingerprint({**body, "input_fingerprint": input_fingerprint, "formula_version": CRYPTO_PROFITABILITY_FORMULA_VERSION})
        decision = replace(
            unfingerprinted,
            decision_id=decision_fingerprint[:32],
            input_fingerprint=input_fingerprint,
            decision_fingerprint=decision_fingerprint,
        )
        values = {
            "decision_id": decision.decision_id,
            "symbol": decision.symbol,
            "strategy_version": decision.strategy_version,
            "strategy_decision_id": decision.strategy_decision_id,
            "strategy_decision_fingerprint": decision.strategy_decision_fingerprint,
            "config_hash": decision.config_hash,
            "sample_count": decision.sample_count,
            "unavailable_count": decision.unavailable_count,
            "win_probability": _text(decision.win_probability),
            "severe_loss_rate": _text(decision.severe_loss_rate),
            "uncertainty": _text(decision.uncertainty),
            "mean_net_return": _text(decision.mean_net_return),
            "average_holding_hours": _text(decision.average_holding_hours),
            "correlation_to_portfolio": _text(decision.correlation_to_portfolio),
            "correlation_snapshot_id": decision.correlation_snapshot_id,
            "correlation_snapshot_fingerprint": decision.correlation_snapshot_fingerprint,
            "minimum_samples": decision.minimum_samples,
            "severe_loss_threshold": _text(decision.severe_loss_threshold),
            "minimum_mean_net_return": _text(decision.minimum_mean_net_return),
            "require_verified_correlation": int(decision.require_verified_correlation),
            "validation_family_id": decision.validation_family_id,
            "validation_decision_id": decision.validation_decision_id,
            "validation_decision_fingerprint": decision.validation_decision_fingerprint,
            "walk_forward_status": decision.walk_forward_status,
            "validation_sample_count": decision.validation_sample_count,
            "validation_fold_count": decision.validation_fold_count,
            "validation_lower_net_r": _text(decision.validation_lower_net_r),
            "validation_fdr_q_value": _text(decision.validation_fdr_q_value),
            "validation_reason": decision.validation_reason,
            "validation_status": decision.validation_status,
            "rejection_reasons_json": canonical_json(list(decision.rejection_reasons)),
            "observation_fingerprints_json": canonical_json(list(decision.observation_fingerprints)),
            "input_fingerprint": decision.input_fingerprint,
            "decision_fingerprint": decision.decision_fingerprint,
            "formula_version": decision.formula_version,
            "schema_version": decision.schema_version,
            "metrics_json": canonical_json(decision.metrics()),
            "created_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        }
        columns = tuple(values)
        with self.storage.connect() as conn:
            # ``executescript`` used by the additive schema installer may
            # close an implicit SQLite transaction.  Install/verify schema
            # first, then begin the single transaction that binds the
            # validation family and crypto decision together.
            apply_crypto_profitability_schema(conn, record_migration=False)
            conn.execute("BEGIN IMMEDIATE")
            ProfitabilityValidationStore(self.storage).persist(
                validation_family,
                conn=conn,
            )
            conn.execute(
                f"INSERT OR IGNORE INTO {TABLE_NAME}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        return self.load_verified(decision.decision_id)

    def load_verified(self, decision_id: str) -> CryptoProfitabilityDecision:
        rows = self.storage.fetch_all(
            f"SELECT * FROM {TABLE_NAME} WHERE decision_id=?",
            (_required(decision_id, "decision_id"),),
        )
        if len(rows) != 1:
            raise CryptoProfitabilityError("crypto profitability decision is missing or duplicated")
        decision = _row_to_decision(rows[0])
        validation_family = ProfitabilityValidationStore(self.storage).load_verified(
            decision.validation_family_id
        )
        if validation_family.config_hash != decision.config_hash:
            raise CryptoProfitabilityError(
                "crypto profitability validation configuration identity changed"
            )
        expected_hypothesis_id = _validation_hypothesis_id(
            decision.symbol, decision.strategy_version
        )
        validation_decisions = [
            item
            for item in validation_family.decisions
            if item.hypothesis_id == expected_hypothesis_id
        ]
        if len(validation_decisions) != 1:
            raise CryptoProfitabilityError(
                "crypto profitability validation member is missing or duplicated"
            )
        validation_decision = validation_decisions[0]
        if (
            validation_decision.id != decision.validation_decision_id
            or validation_decision.decision_fingerprint
            != decision.validation_decision_fingerprint
            or validation_decision.status != decision.walk_forward_status
            or validation_decision.sample_count != decision.validation_sample_count
            or validation_decision.fold_count != decision.validation_fold_count
            or (
                None
                if validation_decision.bootstrap_lower_net_r is None
                else Decimal(validation_decision.bootstrap_lower_net_r)
            )
            != decision.validation_lower_net_r
            or Decimal(validation_decision.fdr_q_value)
            != decision.validation_fdr_q_value
            or validation_decision.reason != decision.validation_reason
        ):
            raise CryptoProfitabilityError(
                "crypto profitability validation authority does not match decision"
            )
        # The persisted decision fingerprint includes the complete input
        # fingerprint and formula identity.  Rebuild it from the row rather
        # than trusting the JSON columns.
        expected_body = {
            "symbol": decision.symbol,
            "strategy_version": decision.strategy_version,
            "strategy_decision_id": decision.strategy_decision_id,
            "strategy_decision_fingerprint": decision.strategy_decision_fingerprint,
            "config_hash": decision.config_hash,
            "minimum_samples": decision.minimum_samples,
            "severe_loss_threshold": _text(decision.severe_loss_threshold),
            "minimum_mean_net_return": _text(decision.minimum_mean_net_return),
            "observation_fingerprints": list(decision.observation_fingerprints),
            "correlation_snapshot_id": decision.correlation_snapshot_id,
            "correlation_snapshot_fingerprint": decision.correlation_snapshot_fingerprint,
            "correlation": _text(decision.correlation_to_portfolio),
            "require_verified_correlation": decision.require_verified_correlation,
            "validation_family_id": decision.validation_family_id,
            "validation_decision_id": decision.validation_decision_id,
            "validation_decision_fingerprint": decision.validation_decision_fingerprint,
            "walk_forward_status": decision.walk_forward_status,
            "validation_sample_count": decision.validation_sample_count,
            "validation_fold_count": decision.validation_fold_count,
            "validation_lower_net_r": _text(decision.validation_lower_net_r),
            "validation_fdr_q_value": _text(decision.validation_fdr_q_value),
            "validation_reason": decision.validation_reason,
            "metrics": decision.metrics(),
        }
        if decision.input_fingerprint != _fingerprint(expected_body):
            # Older rows may carry a different decision-body shape only if
            # this code changed; refusing them is safer than reinterpreting
            # persisted authority.
            raise CryptoProfitabilityError("crypto profitability input fingerprint mismatch")
        expected_decision = {
            **expected_body,
            "input_fingerprint": decision.input_fingerprint,
            "formula_version": decision.formula_version,
        }
        if decision.decision_id != decision.decision_fingerprint[:32] or decision.decision_fingerprint != _fingerprint(expected_decision):
            raise CryptoProfitabilityError("crypto profitability decision fingerprint mismatch")
        return decision


__all__ = [
    "CRYPTO_PROFITABILITY_FORMULA_VERSION",
    "CRYPTO_PROFITABILITY_SCHEMA_VERSION",
    "CryptoProfitabilityDecision",
    "CryptoProfitabilityError",
    "CryptoProfitabilityStore",
    "apply_crypto_profitability_schema",
]
