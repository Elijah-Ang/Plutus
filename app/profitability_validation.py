"""Durable time-series profitability validation and false-discovery control.

The strategy scorecard consumes only canonical, completed observations.  This
module adds a separate immutable authority for the statistical question:
"which members of this predeclared hypothesis family still show positive net
expectancy after chronology, overlapping-label, dependence, and multiplicity
controls?"

It deliberately does not optimize parameters or choose a winner.  A family is
validated as supplied, with every hypothesis retained in the false-discovery
correction, including hypotheses with insufficient evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .formula_versions import (
    EVIDENCE_VERSION,
    PROFITABILITY_VALIDATION_FORMULA_VERSION,
    PROFITABILITY_VALIDATION_SCHEMA_VERSION,
)
from .utils import iso_now


ZERO = Decimal("0")
ONE = Decimal("1")
VALIDATION_STATUSES = frozenset({"validated", "failed", "insufficient"})


class ProfitabilityValidationError(ValueError):
    """Raised when validation evidence or persisted authority is malformed."""


def _utc(value: Any, label: str) -> datetime:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError) as exc:
        raise ProfitabilityValidationError(
            f"{label} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise ProfitabilityValidationError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ProfitabilityValidationError(f"{label} is required")
    return result


def _decimal(
    value: Any,
    label: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ProfitabilityValidationError(f"{label} must be finite")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ProfitabilityValidationError(f"{label} must be finite") from exc
    if not result.is_finite():
        raise ProfitabilityValidationError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise ProfitabilityValidationError(f"{label} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ProfitabilityValidationError(f"{label} must be at most {maximum}")
    return result


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidationObservation:
    id: str
    hypothesis_id: str
    strategy_version: str
    observed_at: str
    outcome_end_at: str
    net_r: Any
    evidence_class: str
    source_id: str

    def canonical(self) -> dict[str, str]:
        observed = _utc(self.observed_at, "observation.observed_at")
        outcome_end = _utc(self.outcome_end_at, "observation.outcome_end_at")
        if outcome_end < observed:
            raise ProfitabilityValidationError(
                "observation outcome_end_at cannot precede observed_at"
            )
        evidence_class = _text(
            self.evidence_class, "observation.evidence_class"
        )
        if evidence_class not in {"shadow_oos", "actual_paper"}:
            raise ProfitabilityValidationError(
                "validation accepts only shadow_oos or actual_paper evidence"
            )
        return {
            "id": _text(self.id, "observation.id"),
            "hypothesis_id": _text(
                self.hypothesis_id, "observation.hypothesis_id"
            ),
            "strategy_version": _text(
                self.strategy_version, "observation.strategy_version"
            ),
            "observed_at": observed.isoformat(),
            "outcome_end_at": outcome_end.isoformat(),
            "net_r": _decimal_text(_decimal(self.net_r, "observation.net_r")),
            "evidence_class": evidence_class,
            "source_id": _text(self.source_id, "observation.source_id"),
        }


@dataclass(frozen=True)
class ValidationHypothesis:
    hypothesis_id: str
    strategy_version: str
    stability_group: str

    def canonical(self) -> dict[str, str]:
        return {
            "hypothesis_id": _text(
                self.hypothesis_id, "hypothesis.hypothesis_id"
            ),
            "strategy_version": _text(
                self.strategy_version, "hypothesis.strategy_version"
            ),
            "stability_group": _text(
                self.stability_group, "hypothesis.stability_group"
            ),
        }


@dataclass(frozen=True)
class ProfitabilityValidationPolicy:
    minimum_samples: int = 50
    minimum_folds: int = 2
    minimum_train_observations: int = 30
    test_observations: int = 10
    embargo_periods: int = 1
    block_length: int = 5
    bootstrap_draws: int = 2000
    fdr_alpha: Any = Decimal("0.10")
    minimum_positive_fold_ratio: Any = Decimal("0.60")
    minimum_parameter_stability_ratio: Any = Decimal("0.60")

    def canonical(self) -> dict[str, Any]:
        integer_values = {
            "minimum_samples": self.minimum_samples,
            "minimum_folds": self.minimum_folds,
            "minimum_train_observations": self.minimum_train_observations,
            "test_observations": self.test_observations,
            "embargo_periods": self.embargo_periods,
            "block_length": self.block_length,
            "bootstrap_draws": self.bootstrap_draws,
        }
        result: dict[str, Any] = {}
        for name, value in integer_values.items():
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ProfitabilityValidationError(
                    f"policy.{name} must be an integer"
                ) from exc
            minimum = 0 if name == "embargo_periods" else 1
            if parsed < minimum:
                raise ProfitabilityValidationError(
                    f"policy.{name} must be at least {minimum}"
                )
            result[name] = parsed
        if result["minimum_samples"] < result["minimum_train_observations"]:
            raise ProfitabilityValidationError(
                "policy.minimum_samples cannot be below minimum_train_observations"
            )
        if result["block_length"] > result["minimum_samples"]:
            raise ProfitabilityValidationError(
                "policy.block_length cannot exceed minimum_samples"
            )
        if result["bootstrap_draws"] < 100:
            raise ProfitabilityValidationError(
                "policy.bootstrap_draws must be at least 100"
            )
        for name in (
            "fdr_alpha",
            "minimum_positive_fold_ratio",
            "minimum_parameter_stability_ratio",
        ):
            result[name] = _decimal_text(
                _decimal(
                    getattr(self, name),
                    f"policy.{name}",
                    minimum=ZERO,
                    maximum=ONE,
                )
            )
        return result


@dataclass(frozen=True)
class PurgedFold:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    train_ids: tuple[str, ...]
    test_ids: tuple[str, ...]
    raw_train_count: int
    purged_train_count: int
    embargo_group_count: int
    test_mean_r: str
    test_positive: bool
    fingerprint: str


@dataclass(frozen=True)
class ProfitabilityValidationDecision:
    id: str
    family_id: str
    hypothesis_id: str
    strategy_version: str
    stability_group: str
    status: str
    reason: str
    sample_count: int
    fold_count: int
    mean_net_r: str | None
    bootstrap_lower_net_r: str | None
    bootstrap_upper_net_r: str | None
    bootstrap_p_value: str
    fdr_q_value: str
    fdr_accepted: bool
    positive_fold_ratio: str | None
    parameter_stability_ratio: str
    parameter_stability_status: str
    folds: tuple[PurgedFold, ...] = field(default_factory=tuple)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    input_fingerprint: str = ""
    decision_fingerprint: str = ""
    formula_version: str = PROFITABILITY_VALIDATION_FORMULA_VERSION
    schema_version: str = PROFITABILITY_VALIDATION_SCHEMA_VERSION


@dataclass(frozen=True)
class ProfitabilityValidationFamily:
    id: str
    family_key: str
    as_of: str
    hypotheses: tuple[Mapping[str, str], ...]
    observations: tuple[Mapping[str, str], ...]
    policy: Mapping[str, Any]
    decisions: tuple[ProfitabilityValidationDecision, ...]
    configuration_version: str
    config_hash: str
    evidence_version: str
    formula_versions: Mapping[str, str]
    input_fingerprint: str
    family_fingerprint: str
    formula_version: str = PROFITABILITY_VALIDATION_FORMULA_VERSION
    schema_version: str = PROFITABILITY_VALIDATION_SCHEMA_VERSION


def _grouped_periods(
    observations: Sequence[Mapping[str, str]],
) -> list[tuple[datetime, list[Mapping[str, str]]]]:
    groups: dict[datetime, list[Mapping[str, str]]] = {}
    for row in observations:
        groups.setdefault(
            _utc(row["observed_at"], "observation.observed_at"), []
        ).append(row)
    return [
        (timestamp, sorted(rows, key=lambda row: (row["id"], row["source_id"])))
        for timestamp, rows in sorted(groups.items())
    ]


def purged_walk_forward_folds(
    observations: Sequence[ValidationObservation | Mapping[str, Any]],
    *,
    minimum_train_observations: int,
    test_observations: int,
    embargo_periods: int,
) -> tuple[PurgedFold, ...]:
    """Create expanding chronological folds with exact label-overlap purging.

    Period groups are never split.  The embargo is an explicit number of
    observation-time groups between the raw training boundary and the test
    start.  Training labels whose outcome interval reaches the test start are
    then purged, regardless of their observation timestamp.
    """

    canonical = [
        row.canonical() if isinstance(row, ValidationObservation) else ValidationObservation(
            id=row["id"],
            hypothesis_id=row["hypothesis_id"],
            strategy_version=row["strategy_version"],
            observed_at=row["observed_at"],
            outcome_end_at=row["outcome_end_at"],
            net_r=row["net_r"],
            evidence_class=row["evidence_class"],
            source_id=row["source_id"],
        ).canonical()
        for row in observations
    ]
    canonical.sort(key=lambda row: (row["observed_at"], row["id"]))
    groups = _grouped_periods(canonical)
    folds: list[PurgedFold] = []
    raw_boundary = 1
    while raw_boundary < len(groups):
        raw_train = [
            row for _timestamp, rows in groups[:raw_boundary] for row in rows
        ]
        if len(raw_train) < int(minimum_train_observations):
            raw_boundary += 1
            continue
        test_group_start = raw_boundary + int(embargo_periods)
        if test_group_start >= len(groups):
            break
        selected_test_groups: list[
            tuple[datetime, list[Mapping[str, str]]]
        ] = []
        selected_test_count = 0
        cursor = test_group_start
        while cursor < len(groups) and selected_test_count < int(test_observations):
            selected_test_groups.append(groups[cursor])
            selected_test_count += len(groups[cursor][1])
            cursor += 1
        if selected_test_count < int(test_observations):
            break
        test_start = selected_test_groups[0][0]
        test_rows = [
            row for _timestamp, rows in selected_test_groups for row in rows
        ]
        train_rows = [
            row
            for row in raw_train
            if _utc(row["outcome_end_at"], "observation.outcome_end_at")
            < test_start
        ]
        if len(train_rows) >= int(minimum_train_observations):
            values = [Decimal(row["net_r"]) for row in test_rows]
            mean = sum(values, ZERO) / Decimal(len(values))
            body = {
                "fold": len(folds) + 1,
                "train_ids": [row["id"] for row in train_rows],
                "test_ids": [row["id"] for row in test_rows],
                "raw_train_count": len(raw_train),
                "purged_train_count": len(raw_train) - len(train_rows),
                "embargo_group_count": int(embargo_periods),
                "test_start": test_start.isoformat(),
                "test_end": selected_test_groups[-1][0].isoformat(),
                "test_mean_r": _decimal_text(mean),
            }
            folds.append(
                PurgedFold(
                    fold=len(folds) + 1,
                    train_start=train_rows[0]["observed_at"],
                    train_end=train_rows[-1]["observed_at"],
                    test_start=test_start.isoformat(),
                    test_end=selected_test_groups[-1][0].isoformat(),
                    train_ids=tuple(row["id"] for row in train_rows),
                    test_ids=tuple(row["id"] for row in test_rows),
                    raw_train_count=len(raw_train),
                    purged_train_count=len(raw_train) - len(train_rows),
                    embargo_group_count=int(embargo_periods),
                    test_mean_r=_decimal_text(mean) or "0",
                    test_positive=mean > ZERO,
                    fingerprint=_fingerprint(body),
                )
            )
        raw_boundary = cursor
    return tuple(folds)


def circular_block_bootstrap(
    values: Sequence[Any],
    *,
    block_length: int,
    draws: int,
    seed: int,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return percentile bounds and a one-sided centered-null p-value."""

    data = np.asarray(
        [float(_decimal(value, "bootstrap.value")) for value in values],
        dtype=np.float64,
    )
    if data.size < 2:
        raise ProfitabilityValidationError(
            "block bootstrap requires at least two values"
        )
    if not np.isfinite(data).all():
        raise ProfitabilityValidationError("bootstrap values must be finite")
    length = min(max(1, int(block_length)), int(data.size))
    draws_i = int(draws)
    if draws_i < 100:
        raise ProfitabilityValidationError(
            "block bootstrap requires at least 100 draws"
        )
    blocks_needed = math.ceil(data.size / length)
    rng = np.random.default_rng(int(seed))
    starts = rng.integers(0, data.size, size=(draws_i, blocks_needed))
    offsets = np.arange(length, dtype=np.int64)
    indexes = (starts[:, :, None] + offsets[None, None, :]) % data.size
    indexes = indexes.reshape(draws_i, -1)[:, : data.size]
    sampled_means = data[indexes].mean(axis=1)
    observed_mean = float(data.mean())
    centered = data - observed_mean
    centered_means = centered[indexes].mean(axis=1)
    lower, upper = np.quantile(sampled_means, [0.025, 0.975])
    p_value = (1 + int(np.count_nonzero(centered_means >= observed_mean))) / (
        draws_i + 1
    )
    return (
        Decimal(str(float(lower))),
        Decimal(str(float(upper))),
        Decimal(str(float(p_value))),
    )


def benjamini_hochberg(
    p_values: Mapping[str, Any], *, alpha: Any
) -> tuple[dict[str, Decimal], dict[str, bool]]:
    """Return monotone BH q-values and step-up rejections."""

    if not p_values:
        raise ProfitabilityValidationError(
            "false-discovery family must contain at least one hypothesis"
        )
    alpha_d = _decimal(alpha, "alpha", minimum=ZERO, maximum=ONE)
    parsed = {
        _text(key, "hypothesis_id"): _decimal(
            value, f"p_values.{key}", minimum=ZERO, maximum=ONE
        )
        for key, value in p_values.items()
    }
    ordered = sorted(parsed.items(), key=lambda item: (item[1], item[0]))
    count = Decimal(len(ordered))
    q_values: dict[str, Decimal] = {}
    running = ONE
    for index in range(len(ordered), 0, -1):
        key, p_value = ordered[index - 1]
        running = min(running, p_value * count / Decimal(index))
        q_values[key] = min(ONE, running)
    largest_rejected_rank = 0
    for index, (_key, p_value) in enumerate(ordered, start=1):
        if p_value <= alpha_d * Decimal(index) / count:
            largest_rejected_rank = index
    accepted = {
        key: index <= largest_rejected_rank
        for index, (key, _p_value) in enumerate(ordered, start=1)
    }
    return q_values, accepted


def _decision_body(decision: ProfitabilityValidationDecision) -> dict[str, Any]:
    return {
        name: value
        for name, value in asdict(decision).items()
        if name not in {"id", "decision_fingerprint"}
    }


def validate_profitability_family(
    *,
    family_key: str,
    as_of: Any,
    hypotheses: Sequence[ValidationHypothesis],
    observations: Sequence[ValidationObservation],
    policy: ProfitabilityValidationPolicy,
    configuration_version: str,
    config_hash: str,
    formula_versions: Mapping[str, str],
    evidence_version: str = EVIDENCE_VERSION,
) -> ProfitabilityValidationFamily:
    """Validate one predeclared family without dropping weak members."""

    family_key_s = _text(family_key, "family_key")
    as_of_s = _utc(as_of, "as_of").isoformat()
    configuration_version_s = _text(
        configuration_version, "configuration_version"
    )
    config_hash_s = _text(config_hash, "config_hash")
    evidence_version_s = _text(evidence_version, "evidence_version")
    if evidence_version_s != EVIDENCE_VERSION:
        raise ProfitabilityValidationError(
            "validation evidence version is not current"
        )
    formulas = {
        str(key): _text(value, f"formula_versions.{key}")
        for key, value in sorted(formula_versions.items())
    }
    if (
        formulas.get("profitability_validation")
        != PROFITABILITY_VALIDATION_FORMULA_VERSION
    ):
        raise ProfitabilityValidationError(
            "profitability validation formula version is not current"
        )
    canonical_policy = policy.canonical()
    canonical_hypotheses = tuple(
        sorted(
            (hypothesis.canonical() for hypothesis in hypotheses),
            key=lambda item: item["hypothesis_id"],
        )
    )
    if not canonical_hypotheses:
        raise ProfitabilityValidationError(
            "validation family must contain at least one hypothesis"
        )
    ids = [item["hypothesis_id"] for item in canonical_hypotheses]
    if len(ids) != len(set(ids)):
        raise ProfitabilityValidationError(
            "validation hypothesis IDs must be unique"
        )
    canonical_observations = tuple(
        sorted(
            (observation.canonical() for observation in observations),
            key=lambda item: (
                item["hypothesis_id"],
                item["observed_at"],
                item["id"],
            ),
        )
    )
    hypothesis_map = {
        item["hypothesis_id"]: item for item in canonical_hypotheses
    }
    observation_ids: set[str] = set()
    for row in canonical_observations:
        hypothesis = hypothesis_map.get(row["hypothesis_id"])
        if hypothesis is None:
            raise ProfitabilityValidationError(
                "observation references an undeclared hypothesis"
            )
        if row["strategy_version"] != hypothesis["strategy_version"]:
            raise ProfitabilityValidationError(
                "observation strategy does not match hypothesis"
            )
        if row["id"] in observation_ids:
            raise ProfitabilityValidationError(
                "validation observation IDs must be unique"
            )
        observation_ids.add(row["id"])
        if _utc(row["outcome_end_at"], "observation.outcome_end_at") > _utc(
            as_of_s, "as_of"
        ):
            raise ProfitabilityValidationError(
                "validation observation contains future outcome evidence"
            )

    family_input = {
        "family_key": family_key_s,
        "as_of": as_of_s,
        "hypotheses": canonical_hypotheses,
        "observations": canonical_observations,
        "policy": canonical_policy,
        "configuration_version": configuration_version_s,
        "config_hash": config_hash_s,
        "evidence_version": evidence_version_s,
        "formula_versions": formulas,
        "formula_version": PROFITABILITY_VALIDATION_FORMULA_VERSION,
        "schema_version": PROFITABILITY_VALIDATION_SCHEMA_VERSION,
    }
    input_fingerprint = _fingerprint(family_input)
    family_id = input_fingerprint[:32]

    provisional: dict[str, dict[str, Any]] = {}
    p_values: dict[str, Decimal] = {}
    for hypothesis in canonical_hypotheses:
        hypothesis_id = hypothesis["hypothesis_id"]
        rows = [
            row
            for row in canonical_observations
            if row["hypothesis_id"] == hypothesis_id
        ]
        values = [Decimal(row["net_r"]) for row in rows]
        folds = purged_walk_forward_folds(
            rows,
            minimum_train_observations=canonical_policy[
                "minimum_train_observations"
            ],
            test_observations=canonical_policy["test_observations"],
            embargo_periods=canonical_policy["embargo_periods"],
        )
        enough_samples = len(values) >= canonical_policy["minimum_samples"]
        enough_folds = len(folds) >= canonical_policy["minimum_folds"]
        mean = (
            sum(values, ZERO) / Decimal(len(values)) if values else None
        )
        lower: Decimal | None = None
        upper: Decimal | None = None
        p_value = ONE
        if enough_samples:
            lower, upper, p_value = circular_block_bootstrap(
                values,
                block_length=canonical_policy["block_length"],
                draws=canonical_policy["bootstrap_draws"],
                seed=int(
                    _fingerprint(
                        {
                            "family": input_fingerprint,
                            "hypothesis": hypothesis_id,
                        }
                    )[:16],
                    16,
                ),
            )
        positive_fold_ratio = (
            Decimal(sum(fold.test_positive for fold in folds))
            / Decimal(len(folds))
            if folds
            else None
        )
        provisional[hypothesis_id] = {
            "hypothesis": hypothesis,
            "rows": rows,
            "values": values,
            "folds": folds,
            "enough_samples": enough_samples,
            "enough_folds": enough_folds,
            "mean": mean,
            "lower": lower,
            "upper": upper,
            "p_value": p_value,
            "positive_fold_ratio": positive_fold_ratio,
        }
        # Insufficient hypotheses stay in the predeclared family with p=1.
        p_values[hypothesis_id] = p_value if enough_samples else ONE

    q_values, accepted = benjamini_hochberg(
        p_values, alpha=canonical_policy["fdr_alpha"]
    )
    stability_groups: dict[str, list[str]] = {}
    for hypothesis in canonical_hypotheses:
        stability_groups.setdefault(hypothesis["stability_group"], []).append(
            hypothesis["hypothesis_id"]
        )

    decisions: list[ProfitabilityValidationDecision] = []
    for hypothesis_id in ids:
        item = provisional[hypothesis_id]
        hypothesis = item["hypothesis"]
        group_ids = stability_groups[hypothesis["stability_group"]]
        if len(group_ids) == 1:
            stability_ratio = ONE
            stability_status = "not_applicable_predeclared_singleton"
            stability_passed = True
        else:
            positive_members = sum(
                bool(
                    provisional[group_id]["mean"] is not None
                    and provisional[group_id]["mean"] > ZERO
                    and provisional[group_id]["lower"] is not None
                    and provisional[group_id]["lower"] > ZERO
                )
                for group_id in group_ids
            )
            stability_ratio = Decimal(positive_members) / Decimal(
                len(group_ids)
            )
            stability_passed = stability_ratio >= Decimal(
                canonical_policy["minimum_parameter_stability_ratio"]
            )
            stability_status = (
                "passed" if stability_passed else "failed_narrow_parameter_support"
            )
        fold_ratio = item["positive_fold_ratio"]
        enough = bool(item["enough_samples"] and item["enough_folds"])
        gates = {
            "minimum_samples": bool(item["enough_samples"]),
            "minimum_folds": bool(item["enough_folds"]),
            "positive_bootstrap_lower_bound": bool(
                item["lower"] is not None and item["lower"] > ZERO
            ),
            "false_discovery_accepted": bool(accepted[hypothesis_id]),
            "positive_fold_ratio": bool(
                fold_ratio is not None
                and fold_ratio
                >= Decimal(canonical_policy["minimum_positive_fold_ratio"])
            ),
            "parameter_stability": stability_passed,
        }
        if not enough:
            status = "insufficient"
            missing = [
                name
                for name in ("minimum_samples", "minimum_folds")
                if not gates[name]
            ]
            reason = "insufficient validation evidence: " + ", ".join(missing)
        elif all(gates.values()):
            status = "validated"
            reason = (
                "positive dependence-aware lower bound, chronological folds, "
                "parameter stability, and family false-discovery control passed"
            )
        else:
            status = "failed"
            reason = "validation gate failed: " + ", ".join(
                name for name, passed in gates.items() if not passed
            )
        metrics = {
            "gates": gates,
            "family_size": len(canonical_hypotheses),
            "stability_group_size": len(group_ids),
            "fdr_alpha": canonical_policy["fdr_alpha"],
            "block_length": canonical_policy["block_length"],
            "bootstrap_draws": canonical_policy["bootstrap_draws"],
            "evidence_classes": sorted(
                {row["evidence_class"] for row in item["rows"]}
            ),
        }
        decision_input_fingerprint = _fingerprint(
            {
                "family_input_fingerprint": input_fingerprint,
                "hypothesis": hypothesis,
                "observation_ids": [row["id"] for row in item["rows"]],
            }
        )
        placeholder = ProfitabilityValidationDecision(
            id="",
            family_id=family_id,
            hypothesis_id=hypothesis_id,
            strategy_version=hypothesis["strategy_version"],
            stability_group=hypothesis["stability_group"],
            status=status,
            reason=reason,
            sample_count=len(item["values"]),
            fold_count=len(item["folds"]),
            mean_net_r=_decimal_text(item["mean"]),
            bootstrap_lower_net_r=_decimal_text(item["lower"]),
            bootstrap_upper_net_r=_decimal_text(item["upper"]),
            bootstrap_p_value=_decimal_text(item["p_value"]) or "1",
            fdr_q_value=_decimal_text(q_values[hypothesis_id]) or "1",
            fdr_accepted=accepted[hypothesis_id],
            positive_fold_ratio=_decimal_text(fold_ratio),
            parameter_stability_ratio=_decimal_text(stability_ratio) or "0",
            parameter_stability_status=stability_status,
            folds=item["folds"],
            metrics=metrics,
            input_fingerprint=decision_input_fingerprint,
            decision_fingerprint="",
        )
        decision_fingerprint = _fingerprint(_decision_body(placeholder))
        decisions.append(
            ProfitabilityValidationDecision(
                **{
                    **asdict(placeholder),
                    "id": decision_fingerprint[:32],
                    "decision_fingerprint": decision_fingerprint,
                    "folds": item["folds"],
                }
            )
        )
    decisions_tuple = tuple(decisions)
    family_fingerprint = _fingerprint(
        {
            **family_input,
            "decisions": [
                {
                    "id": decision.id,
                    "decision_fingerprint": decision.decision_fingerprint,
                }
                for decision in decisions_tuple
            ],
        }
    )
    return ProfitabilityValidationFamily(
        id=family_id,
        family_key=family_key_s,
        as_of=as_of_s,
        hypotheses=canonical_hypotheses,
        observations=canonical_observations,
        policy=canonical_policy,
        decisions=decisions_tuple,
        configuration_version=configuration_version_s,
        config_hash=config_hash_s,
        evidence_version=evidence_version_s,
        formula_versions=formulas,
        input_fingerprint=input_fingerprint,
        family_fingerprint=family_fingerprint,
    )


def apply_profitability_validation_schema(
    conn: sqlite3.Connection, *, record_migration: bool = True
) -> None:
    """Install immutable validation-family authority."""

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS profitability_validation_families(
          id TEXT PRIMARY KEY,
          family_key TEXT NOT NULL,
          as_of TEXT NOT NULL,
          hypotheses_json TEXT NOT NULL,
          observations_json TEXT NOT NULL,
          policy_json TEXT NOT NULL,
          configuration_version TEXT NOT NULL,
          config_hash TEXT NOT NULL,
          evidence_version TEXT NOT NULL,
          formula_versions_json TEXT NOT NULL,
          input_fingerprint TEXT NOT NULL UNIQUE,
          family_fingerprint TEXT NOT NULL UNIQUE,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS profitability_validation_decisions(
          id TEXT PRIMARY KEY,
          family_id TEXT NOT NULL,
          hypothesis_id TEXT NOT NULL,
          strategy_version TEXT NOT NULL,
          stability_group TEXT NOT NULL,
          status TEXT NOT NULL
            CHECK(status IN ('validated','failed','insufficient')),
          reason TEXT NOT NULL,
          sample_count INTEGER NOT NULL CHECK(sample_count>=0),
          fold_count INTEGER NOT NULL CHECK(fold_count>=0),
          mean_net_r TEXT,
          bootstrap_lower_net_r TEXT,
          bootstrap_upper_net_r TEXT,
          bootstrap_p_value TEXT NOT NULL,
          fdr_q_value TEXT NOT NULL,
          fdr_accepted INTEGER NOT NULL CHECK(fdr_accepted IN (0,1)),
          positive_fold_ratio TEXT,
          parameter_stability_ratio TEXT NOT NULL,
          parameter_stability_status TEXT NOT NULL,
          metrics_json TEXT NOT NULL,
          input_fingerprint TEXT NOT NULL,
          decision_fingerprint TEXT NOT NULL UNIQUE,
          formula_version TEXT NOT NULL,
          schema_version TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(family_id,hypothesis_id));
        CREATE TABLE IF NOT EXISTS profitability_validation_folds(
          id TEXT PRIMARY KEY,
          decision_id TEXT NOT NULL,
          fold INTEGER NOT NULL,
          train_start TEXT NOT NULL,
          train_end TEXT NOT NULL,
          test_start TEXT NOT NULL,
          test_end TEXT NOT NULL,
          train_ids_json TEXT NOT NULL,
          test_ids_json TEXT NOT NULL,
          raw_train_count INTEGER NOT NULL,
          purged_train_count INTEGER NOT NULL,
          embargo_group_count INTEGER NOT NULL,
          test_mean_r TEXT NOT NULL,
          test_positive INTEGER NOT NULL CHECK(test_positive IN (0,1)),
          fold_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(decision_id,fold));
        CREATE INDEX IF NOT EXISTS idx_profitability_validation_family
          ON profitability_validation_decisions(family_id,hypothesis_id);
        CREATE INDEX IF NOT EXISTS idx_profitability_validation_strategy
          ON profitability_validation_decisions(
            strategy_version,status,created_at);
        """
    )
    if record_migration:
        conn.execute(
            """INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail)
               VALUES(?,?,?)""",
            (
                PROFITABILITY_VALIDATION_SCHEMA_VERSION,
                iso_now(),
                "immutable purged walk-forward, block-bootstrap, parameter-stability, and family false-discovery authority",
            ),
        )


def _family_columns(family: ProfitabilityValidationFamily) -> dict[str, Any]:
    return {
        "id": family.id,
        "family_key": family.family_key,
        "as_of": family.as_of,
        "hypotheses_json": _canonical_json(list(family.hypotheses)),
        "observations_json": _canonical_json(list(family.observations)),
        "policy_json": _canonical_json(dict(family.policy)),
        "configuration_version": family.configuration_version,
        "config_hash": family.config_hash,
        "evidence_version": family.evidence_version,
        "formula_versions_json": _canonical_json(dict(family.formula_versions)),
        "input_fingerprint": family.input_fingerprint,
        "family_fingerprint": family.family_fingerprint,
        "formula_version": family.formula_version,
        "schema_version": family.schema_version,
    }


def _decision_columns(
    decision: ProfitabilityValidationDecision,
) -> dict[str, Any]:
    return {
        "id": decision.id,
        "family_id": decision.family_id,
        "hypothesis_id": decision.hypothesis_id,
        "strategy_version": decision.strategy_version,
        "stability_group": decision.stability_group,
        "status": decision.status,
        "reason": decision.reason,
        "sample_count": decision.sample_count,
        "fold_count": decision.fold_count,
        "mean_net_r": decision.mean_net_r,
        "bootstrap_lower_net_r": decision.bootstrap_lower_net_r,
        "bootstrap_upper_net_r": decision.bootstrap_upper_net_r,
        "bootstrap_p_value": decision.bootstrap_p_value,
        "fdr_q_value": decision.fdr_q_value,
        "fdr_accepted": int(decision.fdr_accepted),
        "positive_fold_ratio": decision.positive_fold_ratio,
        "parameter_stability_ratio": decision.parameter_stability_ratio,
        "parameter_stability_status": decision.parameter_stability_status,
        "metrics_json": _canonical_json(dict(decision.metrics)),
        "input_fingerprint": decision.input_fingerprint,
        "decision_fingerprint": decision.decision_fingerprint,
        "formula_version": decision.formula_version,
        "schema_version": decision.schema_version,
    }


def _fold_columns(
    fold: PurgedFold, decision_id: str
) -> dict[str, Any]:
    return {
        "id": _fingerprint(
            {
                "decision_id": decision_id,
                "fold_fingerprint": fold.fingerprint,
            }
        )[:32],
        "decision_id": decision_id,
        "fold": fold.fold,
        "train_start": fold.train_start,
        "train_end": fold.train_end,
        "test_start": fold.test_start,
        "test_end": fold.test_end,
        "train_ids_json": _canonical_json(list(fold.train_ids)),
        "test_ids_json": _canonical_json(list(fold.test_ids)),
        "raw_train_count": fold.raw_train_count,
        "purged_train_count": fold.purged_train_count,
        "embargo_group_count": fold.embargo_group_count,
        "test_mean_r": fold.test_mean_r,
        "test_positive": int(fold.test_positive),
        "fold_fingerprint": fold.fingerprint,
    }


class ProfitabilityValidationStore:
    """Persist and independently recompute validation-family authority."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    @staticmethod
    def _verify_columns(
        row: Mapping[str, Any], expected: Mapping[str, Any], label: str
    ) -> None:
        for name, value in expected.items():
            if row.get(name) != value:
                raise ProfitabilityValidationError(
                    f"persisted {label} column is inconsistent: {name}"
                )

    def persist(
        self,
        family: ProfitabilityValidationFamily,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> str:
        if conn is not None:
            if not conn.in_transaction:
                raise ProfitabilityValidationError(
                    "external validation persistence requires an active transaction"
                )
            self._persist_in_connection(conn, family)
            return family.id
        with self.storage.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._persist_in_connection(connection, family)
        return family.id

    def _persist_in_connection(
        self, conn: sqlite3.Connection, family: ProfitabilityValidationFamily
    ) -> None:
        now = iso_now()
        values = {**_family_columns(family), "created_at": now}
        columns = tuple(values)
        conn.execute(
            f"""INSERT OR IGNORE INTO profitability_validation_families(
                   {",".join(columns)}) VALUES({",".join("?" for _ in columns)})""",
            tuple(values[name] for name in columns),
        )
        row = conn.execute(
            "SELECT * FROM profitability_validation_families WHERE id=?",
            (family.id,),
        ).fetchone()
        if row is None:
            raise ProfitabilityValidationError(
                "validation family persistence failed"
            )
        self._verify_columns(
            dict(row), _family_columns(family), "validation family"
        )
        for decision in family.decisions:
            decision_values = {
                **_decision_columns(decision),
                "created_at": now,
            }
            decision_columns = tuple(decision_values)
            conn.execute(
                f"""INSERT OR IGNORE INTO profitability_validation_decisions(
                       {",".join(decision_columns)})
                       VALUES({",".join("?" for _ in decision_columns)})""",
                tuple(decision_values[name] for name in decision_columns),
            )
            decision_row = conn.execute(
                "SELECT * FROM profitability_validation_decisions WHERE id=?",
                (decision.id,),
            ).fetchone()
            if decision_row is None:
                raise ProfitabilityValidationError(
                    "validation decision persistence failed"
                )
            self._verify_columns(
                dict(decision_row),
                _decision_columns(decision),
                "validation decision",
            )
            for fold in decision.folds:
                fold_values = {
                    **_fold_columns(fold, decision.id),
                    "created_at": now,
                }
                fold_columns = tuple(fold_values)
                conn.execute(
                    f"""INSERT OR IGNORE INTO profitability_validation_folds(
                           {",".join(fold_columns)})
                           VALUES({",".join("?" for _ in fold_columns)})""",
                    tuple(fold_values[name] for name in fold_columns),
                )
                fold_row = conn.execute(
                    "SELECT * FROM profitability_validation_folds WHERE id=?",
                    (_fold_columns(fold, decision.id)["id"],),
                ).fetchone()
                if fold_row is None:
                    raise ProfitabilityValidationError(
                        "validation fold persistence failed"
                    )
                self._verify_columns(
                    dict(fold_row),
                    _fold_columns(fold, decision.id),
                    "validation fold",
                )

    def load_verified(
        self,
        family_id: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> ProfitabilityValidationFamily:
        family_id = _text(family_id, "family_id")
        rows = (
            [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM profitability_validation_families WHERE id=?",
                    (family_id,),
                ).fetchall()
            ]
            if conn is not None
            else self.storage.fetch_all(
                "SELECT * FROM profitability_validation_families WHERE id=?",
                (family_id,),
            )
        )
        if not rows:
            raise ProfitabilityValidationError(
                "validation family authority is missing"
            )
        row = rows[0]
        try:
            hypotheses = [
                ValidationHypothesis(**item)
                for item in json.loads(row["hypotheses_json"])
            ]
            observations = [
                ValidationObservation(**item)
                for item in json.loads(row["observations_json"])
            ]
            policy = ProfitabilityValidationPolicy(
                **json.loads(row["policy_json"])
            )
            formulas = json.loads(row["formula_versions_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProfitabilityValidationError(
                "persisted validation family JSON is invalid"
            ) from exc
        recomputed = validate_profitability_family(
            family_key=row["family_key"],
            as_of=row["as_of"],
            hypotheses=hypotheses,
            observations=observations,
            policy=policy,
            configuration_version=row["configuration_version"],
            config_hash=row["config_hash"],
            formula_versions=formulas,
            evidence_version=row["evidence_version"],
        )
        self._verify_columns(
            row, _family_columns(recomputed), "validation family"
        )
        decisions = (
            [
                dict(decision)
                for decision in conn.execute(
                    """SELECT * FROM profitability_validation_decisions
                       WHERE family_id=? ORDER BY hypothesis_id""",
                    (family_id,),
                ).fetchall()
            ]
            if conn is not None
            else self.storage.fetch_all(
                """SELECT * FROM profitability_validation_decisions
                   WHERE family_id=? ORDER BY hypothesis_id""",
                (family_id,),
            )
        )
        expected = {
            decision.hypothesis_id: decision
            for decision in recomputed.decisions
        }
        if set(expected) != {
            str(decision["hypothesis_id"]) for decision in decisions
        }:
            raise ProfitabilityValidationError(
                "persisted validation decision family is incomplete"
            )
        for decision_row in decisions:
            decision = expected[str(decision_row["hypothesis_id"])]
            self._verify_columns(
                decision_row,
                _decision_columns(decision),
                "validation decision",
            )
            fold_rows = (
                [
                    dict(fold)
                    for fold in conn.execute(
                        """SELECT * FROM profitability_validation_folds
                           WHERE decision_id=? ORDER BY fold""",
                        (decision.id,),
                    ).fetchall()
                ]
                if conn is not None
                else self.storage.fetch_all(
                    """SELECT * FROM profitability_validation_folds
                       WHERE decision_id=? ORDER BY fold""",
                    (decision.id,),
                )
            )
            expected_folds = {
                fold.fold: fold for fold in decision.folds
            }
            if set(expected_folds) != {
                int(fold_row["fold"]) for fold_row in fold_rows
            }:
                raise ProfitabilityValidationError(
                    "persisted validation fold family is incomplete"
                )
            for fold_row in fold_rows:
                fold = expected_folds[int(fold_row["fold"])]
                self._verify_columns(
                    fold_row,
                    _fold_columns(fold, decision.id),
                    "validation fold",
                )
        return recomputed


def policy_from_config(
    config: Mapping[str, Any],
) -> ProfitabilityValidationPolicy:
    values = dict(config.get("profitability_validation", {}) or {})
    allowed = {
        field.name
        for field in ProfitabilityValidationPolicy.__dataclass_fields__.values()
    }
    return ProfitabilityValidationPolicy(
        **{key: values[key] for key in allowed if key in values}
    )


def observations_from_strategy_records(
    rows: Iterable[Mapping[str, Any]],
) -> list[ValidationObservation]:
    observations: list[ValidationObservation] = []
    for row in rows:
        if row.get("r_multiple") is None:
            continue
        if str(row.get("attribution_status") or "") not in {
            "complete",
            "partial",
        }:
            continue
        entry = row.get("entry_session")
        exit_at = row.get("exit_session")
        if not entry or not exit_at:
            continue
        strategy = _text(row.get("strategy_version"), "strategy_version")
        source_id = _text(row.get("source_id"), "source_id")
        observations.append(
            ValidationObservation(
                id=_fingerprint(
                    {
                        "source_key": row.get("source_key"),
                        "strategy_version": strategy,
                        "r_multiple": row.get("r_multiple"),
                        "entry_session": entry,
                        "exit_session": exit_at,
                    }
                )[:32],
                hypothesis_id=strategy,
                strategy_version=strategy,
                observed_at=str(entry),
                outcome_end_at=str(exit_at),
                net_r=row["r_multiple"],
                evidence_class=str(row.get("evidence_class") or ""),
                source_id=source_id,
            )
        )
    return observations


__all__ = [
    "ProfitabilityValidationDecision",
    "ProfitabilityValidationError",
    "ProfitabilityValidationFamily",
    "ProfitabilityValidationPolicy",
    "ProfitabilityValidationStore",
    "PurgedFold",
    "ValidationHypothesis",
    "ValidationObservation",
    "apply_profitability_validation_schema",
    "benjamini_hochberg",
    "circular_block_bootstrap",
    "observations_from_strategy_records",
    "policy_from_config",
    "purged_walk_forward_folds",
    "validate_profitability_family",
]
