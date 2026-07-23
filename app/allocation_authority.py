"""Verified persistence boundary for current Phase 4 sleeve allocation authority."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from .formula_versions import (
    PHASE4_ALLOCATION_VERSION,
    PHASE4_ALLOCATOR_VERSION,
    PHASE4_SCHEMA_VERSION,
)
from .strategy_execution_registry import (
    REGISTRY_FORMULA_VERSION,
    StrategyRegistryIntegrityError,
    StrategyRegistryStore,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALLOCATION_AUTHORITY_VERSION = (
    "phase4_allocation_authority_v1_exact_registry"
)


class AllocationAuthorityError(ValueError):
    """Raised when a persisted allocation cannot prove its exact authority."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def allocation_identity(
    run_id: str,
    replay_inputs: Mapping[str, Any],
    strategy_weights: Mapping[str, Any],
    strategy_sleeves: Mapping[str, Any],
    *,
    authority_payload: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Return the canonical evidence fingerprint and immutable allocation ID."""
    evidence_fingerprint = _fingerprint(dict(replay_inputs))
    identity: list[Any] = [
        str(run_id),
        evidence_fingerprint,
        dict(strategy_weights),
        dict(strategy_sleeves),
    ]
    if authority_payload is not None:
        identity.append(allocation_authority_fingerprint(authority_payload))
    allocation_id = _fingerprint(identity)[:32]
    return evidence_fingerprint, allocation_id


def allocation_authority_fingerprint(
    payload: Mapping[str, Any],
) -> str:
    """Bind every current allocation payload field except this digest."""
    authority = dict(payload)
    authority.pop("allocation_authority_fingerprint", None)
    return _fingerprint(authority)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AllocationAuthorityError(f"{label} is invalid")
    return value


def _unique_texts(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AllocationAuthorityError(f"{label} is invalid")
    result = tuple(str(item or "").strip() for item in value)
    if any(not item for item in result) or len(set(result)) != len(result):
        raise AllocationAuthorityError(f"{label} is invalid or duplicated")
    return result


@dataclass(frozen=True)
class VerifiedAllocationAuthority:
    allocation_id: str
    run_id: str
    registry_snapshot_id: str
    authorized_strategies: tuple[str, ...]
    strategy_weights: Mapping[str, Any]
    strategy_sleeves: Mapping[str, Any]
    payload: Mapping[str, Any]
    row: Mapping[str, Any]
    authority_version: str | None
    registry_binding_exact: bool
    executable: bool


class Phase4AllocationStore:
    """Reload and cryptographically replay immutable allocation identities."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def load_verified(
        self,
        allocation_id: str,
        *,
        conn: sqlite3.Connection | None = None,
        expected_run_id: str | None = None,
        expected_registry_snapshot_id: str | None = None,
        expected_strategy_version: str | None = None,
        expected_config_hash: str | None = None,
        require_executable: bool = False,
    ) -> VerifiedAllocationAuthority:
        identifier = str(allocation_id or "").strip()
        if not identifier:
            raise AllocationAuthorityError("allocation ID is required")
        if conn is None:
            rows = self.storage.fetch_all(
                "SELECT * FROM phase4_allocation_decisions WHERE id=?", (identifier,)
            )
        else:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM phase4_allocation_decisions WHERE id=?", (identifier,)
                ).fetchall()
            ]
        if len(rows) != 1:
            raise AllocationAuthorityError(
                "allocation authority is missing or duplicated"
            )
        row = dict(rows[0])
        run_id = str(row.get("run_id") or "")
        if not run_id or (
            expected_run_id is not None and run_id != str(expected_run_id)
        ):
            raise AllocationAuthorityError("allocation run identity is inconsistent")
        try:
            payload = json.loads(row["payload"])
            weights = json.loads(row["strategy_weights_json"])
            evidence_versions = json.loads(row["evidence_versions_json"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise AllocationAuthorityError("persisted allocation JSON is invalid") from exc
        payload = _mapping(payload, "allocation payload")
        weights = _mapping(weights, "allocation strategy weights")
        _mapping(evidence_versions, "allocation evidence versions")
        authority_version = (
            str(payload.get("allocation_authority_version"))
            if payload.get("allocation_authority_version")
            else None
        )
        if authority_version not in (None, ALLOCATION_AUTHORITY_VERSION):
            raise AllocationAuthorityError(
                "allocation authority version is invalid"
            )
        if authority_version == ALLOCATION_AUTHORITY_VERSION:
            persisted_authority_fingerprint = str(
                payload.get("allocation_authority_fingerprint") or ""
            )
            if (
                not _SHA256.fullmatch(persisted_authority_fingerprint)
                or persisted_authority_fingerprint
                != allocation_authority_fingerprint(payload)
            ):
                raise AllocationAuthorityError(
                    "allocation authority fingerprint is inconsistent"
                )
        replay = _mapping(payload.get("raw_replay_inputs"), "allocation replay inputs")
        portfolio = _mapping(
            replay.get("portfolio_snapshot"), "allocation portfolio snapshot"
        )
        sleeves = _mapping(payload.get("strategy_sleeves"), "allocation sleeves")
        registry = _mapping(replay.get("registry"), "allocation registry replay")

        config_hash = str(row.get("config_hash") or "").lower()
        if not _SHA256.fullmatch(config_hash):
            raise AllocationAuthorityError("allocation config hash is invalid")
        if expected_config_hash is not None and config_hash != str(
            expected_config_hash
        ).lower():
            raise AllocationAuthorityError(
                "allocation config hash does not match current authority"
            )
        decision = str(row.get("decision") or "")
        legacy_non_authoritative = bool(
            not require_executable
            and decision == "PRESERVE_CASH"
            and payload.get("schema_version") is None
            and payload.get("authorized_strategies") == []
            and not sleeves
        )
        if (
            row.get("mode") != "ACTIVE_ADAPTIVE_PAPER"
            or row.get("allocator_version") != PHASE4_ALLOCATOR_VERSION
            or row.get("formula_version") != PHASE4_ALLOCATION_VERSION
            or (
                not decision.startswith("ALLOCATE")
                and decision != "PRESERVE_CASH"
            )
            or (require_executable and not decision.startswith("ALLOCATE"))
            or (
                payload.get("schema_version") != PHASE4_SCHEMA_VERSION
                and not legacy_non_authoritative
            )
            or payload.get("formula_version") != PHASE4_ALLOCATION_VERSION
            or payload.get("config_hash") != config_hash
            or replay.get("formula_version") != PHASE4_ALLOCATION_VERSION
            or replay.get("configuration_hash") != config_hash
        ):
            raise AllocationAuthorityError(
                "allocation version, mode, decision, or configuration is inconsistent"
            )
        evidence_fingerprint, expected_id = allocation_identity(
            run_id,
            replay,
            weights,
            sleeves,
            authority_payload=(
                payload
                if authority_version == ALLOCATION_AUTHORITY_VERSION
                else None
            ),
        )
        if row.get("evidence_fingerprint") != evidence_fingerprint:
            raise AllocationAuthorityError(
                "allocation evidence fingerprint is inconsistent"
            )
        if identifier != expected_id:
            raise AllocationAuthorityError("allocation identity is inconsistent")

        registry_snapshot_id = str(payload.get("registry_snapshot_id") or "")
        if (
            not registry_snapshot_id
            or registry_snapshot_id
            != str(portfolio.get("strategy_registry_snapshot_id") or "")
            or (
                expected_registry_snapshot_id is not None
                and registry_snapshot_id != str(expected_registry_snapshot_id)
            )
            or payload.get("registry_evaluation") != registry
        ):
            raise AllocationAuthorityError(
                "allocation registry authority is inconsistent"
            )
        try:
            verified_registry = StrategyRegistryStore(self.storage).load_verified(
                registry_snapshot_id,
                conn=conn,
                expected_run_id=run_id,
            )
        except (StrategyRegistryIntegrityError, sqlite3.Error) as exc:
            raise AllocationAuthorityError(
                "allocation references invalid registry authority"
            ) from exc
        registry_binding_exact = (
            payload.get("registry_evaluation")
            == verified_registry.evaluation.as_dict()
        )
        registry_config = verified_registry.evaluation.raw_inputs.get(
            "registry", {}
        )
        if (
            authority_version == ALLOCATION_AUTHORITY_VERSION
            and (
                not isinstance(registry_config, Mapping)
                or registry_config.get("formula_version")
                != REGISTRY_FORMULA_VERSION
            )
        ):
            raise AllocationAuthorityError(
                "allocation registry formula lacks full policy authority"
            )
        if authority_version == ALLOCATION_AUTHORITY_VERSION and not registry_binding_exact:
            raise AllocationAuthorityError(
                "allocation is not bound to the exact persisted registry evaluation"
            )
        if require_executable and (
            authority_version != ALLOCATION_AUTHORITY_VERSION
            or not registry_binding_exact
        ):
            raise AllocationAuthorityError(
                "allocation lacks exact persisted registry execution authority"
            )
        authorized = _unique_texts(
            payload.get("authorized_strategies"),
            "allocation authorized strategy set",
        )
        strategy_order = _unique_texts(
            replay.get("strategy_order"),
            "allocation replay strategy order",
        )
        if payload.get("strategy_order") != list(strategy_order):
            raise AllocationAuthorityError(
                "allocation strategy order replay is inconsistent"
            )
        replay_authorized = _unique_texts(
            replay.get("authorized_strategy_order"),
            "allocation replay authorized strategy set",
        )
        if authorized != replay_authorized:
            raise AllocationAuthorityError(
                "allocation authorized strategy replay is inconsistent"
            )
        if authority_version == ALLOCATION_AUTHORITY_VERSION:
            if set(weights) != set(strategy_order):
                raise AllocationAuthorityError(
                    "allocation weight strategy family is inconsistent"
                )
            try:
                weight_values = [float(value) for value in weights.values()]
            except (TypeError, ValueError) as exc:
                raise AllocationAuthorityError(
                    "allocation weights are invalid"
                ) from exc
            if any(
                not math.isfinite(value) or not 0.0 <= value <= 1.0
                for value in weight_values
            ) or sum(weight_values) > 1.0 + 1e-9:
                raise AllocationAuthorityError(
                    "allocation weights are not finite and bounded"
                )
            if set(sleeves) != set(authorized):
                raise AllocationAuthorityError(
                    "allocation sleeve strategy family is inconsistent"
                )
            for strategy, raw_sleeve in sleeves.items():
                if (
                    not isinstance(raw_sleeve, Mapping)
                    or str(raw_sleeve.get("strategy_version") or "")
                    != strategy
                    or raw_sleeve.get("risk_unit")
                    not in {"stop_risk_dollars", "pct_equity"}
                ):
                    raise AllocationAuthorityError(
                        "allocation sleeve identity or unit is invalid"
                    )
                try:
                    remaining_risk = float(raw_sleeve.get("remaining_risk"))
                    remaining_notional = float(
                        raw_sleeve.get("remaining_notional")
                    )
                except (TypeError, ValueError) as exc:
                    raise AllocationAuthorityError(
                        "allocation sleeve capacity is invalid"
                    ) from exc
                if any(
                    not math.isfinite(value) or value < 0.0
                    for value in (remaining_risk, remaining_notional)
                ):
                    raise AllocationAuthorityError(
                        "allocation sleeve capacity is not finite and nonnegative"
                    )
        if registry_binding_exact and authorized != tuple(
            verified_registry.evaluation.authorized_versions
        ):
            raise AllocationAuthorityError(
                "allocation authorized strategies differ from registry authority"
            )
        if expected_strategy_version is not None:
            strategy = str(expected_strategy_version)
            sleeve = sleeves.get(strategy)
            if strategy not in authorized or not isinstance(sleeve, Mapping):
                raise AllocationAuthorityError(
                    "allocation does not authorize the requested strategy sleeve"
                )
            if str(sleeve.get("strategy_version") or "") != strategy:
                raise AllocationAuthorityError(
                    "allocation strategy sleeve identity is inconsistent"
                )
        if payload.get("evidence_versions") != evidence_versions:
            raise AllocationAuthorityError(
                "allocation evidence-version inventory is inconsistent"
            )
        if set(evidence_versions) != set(strategy_order):
            raise AllocationAuthorityError(
                "allocation evidence-version strategy family is inconsistent"
            )
        if registry_binding_exact:
            registry_evidence_versions = {
                decision.strategy_version: decision.evidence_version
                for decision in (
                    *verified_registry.evaluation.authorized,
                    *verified_registry.evaluation.rejected,
                )
            }
            if any(
                evidence_versions.get(strategy) != version
                for strategy, version in registry_evidence_versions.items()
            ):
                raise AllocationAuthorityError(
                    "allocation evidence versions differ from registry authority"
                )
        executable = bool(
            decision.startswith("ALLOCATE")
            and authority_version == ALLOCATION_AUTHORITY_VERSION
            and registry_binding_exact
        )
        return VerifiedAllocationAuthority(
            allocation_id=identifier,
            run_id=run_id,
            registry_snapshot_id=registry_snapshot_id,
            authorized_strategies=authorized,
            strategy_weights=dict(weights),
            strategy_sleeves=dict(sleeves),
            payload=dict(payload),
            row=row,
            authority_version=authority_version,
            registry_binding_exact=registry_binding_exact,
            executable=executable,
        )


def allocation_authority_integrity_report(storage: Any) -> dict[str, int]:
    """Count replay failures without mutating or repairing persisted authority."""
    rows = storage.fetch_all(
        "SELECT id FROM phase4_allocation_decisions WHERE formula_version=?",
        (PHASE4_ALLOCATION_VERSION,),
    )
    invalid = 0
    store = Phase4AllocationStore(storage)
    for row in rows:
        try:
            store.load_verified(str(row["id"]))
        except (AllocationAuthorityError, sqlite3.Error):
            invalid += 1
    return {"invalid_phase4_allocation_authority": invalid}


__all__ = [
    "ALLOCATION_AUTHORITY_VERSION",
    "AllocationAuthorityError",
    "Phase4AllocationStore",
    "VerifiedAllocationAuthority",
    "allocation_authority_fingerprint",
    "allocation_identity",
    "allocation_authority_integrity_report",
]
