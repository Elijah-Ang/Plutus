from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Mapping


REGISTRY_SCHEMA_VERSION = "strategy_execution_registry_v1"
REGISTRY_FORMULA_VERSION = "strategy_execution_registry_formula_v1"
POLICY_STATES = frozenset(
    {"RESEARCH_ONLY", "PROBE", "EXPLORATION", "THROTTLED", "ACTIVE", "SUSPENDED"}
)
EXECUTABLE_POLICY_STATES = frozenset({"PROBE", "EXPLORATION", "THROTTLED", "ACTIVE"})


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _policy_snapshot(policy: Any) -> dict[str, Any]:
    hard_gates = _value(policy, "hard_gates", {})
    hard_gates = dict(hard_gates) if isinstance(hard_gates, Mapping) else {}
    raw_inputs = _value(policy, "raw_inputs", {})
    raw_inputs = dict(raw_inputs) if isinstance(raw_inputs, Mapping) else {}
    evidence_current = _value(policy, "evidence_current")
    if evidence_current is None:
        evidence_current = hard_gates.get("evidence_fresh")
    evidence_complete = _value(policy, "evidence_version_complete")
    if evidence_complete is None:
        evidence_complete = hard_gates.get("version_complete")
    return {
        "strategy_version": _value(policy, "strategy_version"),
        "state": _value(policy, "state"),
        "enforcement_enabled": _value(policy, "enforcement_enabled"),
        "evidence_current": evidence_current,
        "evidence_version_complete": evidence_complete,
        "evidence_version": (
            _value(policy, "evidence_version")
            or raw_inputs.get("current_evidence_version")
        ),
        "performance_version": _value(policy, "performance_version"),
        "policy_version": _value(policy, "policy_version"),
        "schema_version": _value(policy, "schema_version"),
        "configuration_version": (
            _value(policy, "configuration_version")
            or raw_inputs.get("configuration_version")
            or raw_inputs.get("configuration_schema_version")
        ),
        "config_hash": (
            _value(policy, "config_hash")
            or raw_inputs.get("config_hash")
            or raw_inputs.get("effective_config_hash")
        ),
        "suspended": _value(policy, "suspended", False),
        "fingerprint": _value(policy, "fingerprint") or _value(policy, "input_fingerprint"),
        "decided_at": _value(policy, "decided_at"),
        "hard_gates": dict(sorted(hard_gates.items())),
        "raw_inputs": raw_inputs,
    }


def apply_strategy_registry_schema(conn: Any) -> None:
    """Create the additive registry ledger without rewriting historical rows."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS strategy_registry_snapshots(
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL,
          evaluated_at TEXT NOT NULL,
          registry_schema_version TEXT NOT NULL,
          registry_formula_version TEXT NOT NULL,
          configuration_version TEXT,
          config_hash TEXT,
          authorized_strategies_json TEXT NOT NULL,
          rejected_strategies_json TEXT NOT NULL,
          global_reasons_json TEXT NOT NULL,
          raw_inputs_json TEXT NOT NULL,
          evaluation_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(run_id,evaluation_fingerprint)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_registry_snapshots_run
          ON strategy_registry_snapshots(run_id,evaluated_at);
        CREATE TABLE IF NOT EXISTS strategy_registry_decisions(
          id TEXT PRIMARY KEY,
          snapshot_id TEXT NOT NULL,
          run_id TEXT NOT NULL,
          strategy_name TEXT NOT NULL,
          strategy_version TEXT NOT NULL,
          authorized INTEGER NOT NULL CHECK(authorized IN (0,1)),
          policy_state TEXT NOT NULL,
          reasons_json TEXT NOT NULL,
          reason TEXT NOT NULL,
          decision_json TEXT NOT NULL,
          raw_inputs_json TEXT NOT NULL,
          evidence_version TEXT,
          performance_version TEXT,
          policy_version TEXT,
          policy_schema_version TEXT,
          configuration_version TEXT,
          config_hash TEXT,
          decision_fingerprint TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(snapshot_id,strategy_version),
          FOREIGN KEY(snapshot_id) REFERENCES strategy_registry_snapshots(id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_registry_decisions_strategy
          ON strategy_registry_decisions(strategy_version,authorized,created_at);
        """
    )
    migration_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if migration_table:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (
                REGISTRY_SCHEMA_VERSION,
                datetime.now(UTC).isoformat(),
                "additive deterministic paper strategy execution registry ledger",
            ),
        )


@dataclass(frozen=True)
class StrategyRegistryDecision:
    strategy_name: str
    strategy_version: str
    implementation_id: str
    implementation_version: str
    implementation_available: bool
    execution_eligible: bool
    paper_eligible: bool
    live_eligible: bool
    human_authorized: bool
    config_authorized: bool
    authorization_id: str | None
    suspended: bool
    policy_state: str
    policy_enforcement_enabled: bool
    evidence_current: bool
    evidence_version_complete: bool
    authorized: bool
    reasons: tuple[str, ...]
    effective_at: str | None
    expires_at: str | None
    evidence_version: str | None
    performance_version: str | None
    policy_version: str | None
    policy_schema_version: str | None
    configuration_version: str | None
    configuration_hash: str | None
    registry_schema_version: str | None
    registry_formula_version: str | None
    decision_fingerprint: str

    @property
    def reason(self) -> str:
        return "authorized_for_bounded_paper_execution" if self.authorized else "; ".join(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        result["reason"] = self.reason
        return result


@dataclass(frozen=True)
class StrategyRegistryEvaluation:
    as_of: str
    authorized: tuple[StrategyRegistryDecision, ...]
    rejected: tuple[StrategyRegistryDecision, ...]
    global_reasons: tuple[str, ...]
    raw_inputs: dict[str, Any]
    fingerprint: str

    @property
    def authorized_versions(self) -> tuple[str, ...]:
        return tuple(decision.strategy_version for decision in self.authorized)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "authorized": [decision.as_dict() for decision in self.authorized],
            "rejected": [decision.as_dict() for decision in self.rejected],
            "authorized_versions": list(self.authorized_versions),
            "global_reasons": list(self.global_reasons),
            "raw_inputs": self.raw_inputs,
            "fingerprint": self.fingerprint,
        }


class StrategyExecutionRegistry:
    """Deterministic, fail-closed paper strategy authorization boundary.

    Profitability evidence supplies a policy state, but it cannot add an
    implementation or grant execution authority. Those grants must already be
    explicit in the configuration registry and are checked again here against
    the runtime implementation inventory.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        available_implementations: Mapping[str, str] | None = None,
    ) -> None:
        self.config = dict(config)
        section = config.get("strategy_execution_registry", {})
        self.registry = dict(section) if isinstance(section, Mapping) else {}
        entries = self.registry.get("entries", {})
        self.entries = dict(entries) if isinstance(entries, Mapping) else {}
        self.available_implementations = {
            str(identifier): str(version)
            for identifier, version in (available_implementations or {}).items()
            if identifier and version
        }

    def _global_reasons(self, as_of: datetime | None) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.registry:
            reasons.append("strategy_execution_registry_missing")
        if self.registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            reasons.append("registry_schema_version_mismatch")
        if self.registry.get("formula_version") != REGISTRY_FORMULA_VERSION:
            reasons.append("registry_formula_version_mismatch")
        if self.registry.get("mode") != "paper_only":
            reasons.append("registry_not_paper_only")
        if self.config.get("mode") != "paper":
            reasons.append("runtime_mode_not_paper")
        if self.config.get("live_enabled") is not False:
            reasons.append("live_execution_not_disabled")
        capabilities = self.config.get("execution_capabilities", {})
        if not isinstance(capabilities, Mapping) or capabilities.get("live_execution_enabled") is not False:
            reasons.append("live_execution_capability_not_disabled")
        if self.config.get("auto_execution_enabled", False) is not False:
            reasons.append("autonomous_execution_not_disabled")
        configuration_version = self.config.get("configuration_schema_version")
        required_configuration_version = self.registry.get("required_configuration_version")
        if not configuration_version:
            reasons.append("configuration_version_missing")
        elif required_configuration_version != configuration_version:
            reasons.append("configuration_version_mismatch")
        if not self.config.get("effective_config_hash"):
            reasons.append("configuration_hash_missing")
        for key in (
            "required_evidence_version",
            "required_performance_version",
            "required_policy_version",
            "required_policy_schema_version",
        ):
            if not self.registry.get(key):
                reasons.append(f"{key}_missing")
        if not isinstance(self.registry.get("entries"), Mapping):
            reasons.append("registry_entries_invalid")
        if as_of is None:
            reasons.append("evaluation_time_invalid")
        return tuple(reasons)

    def evaluate(
        self,
        policies: Mapping[str, Any],
        *,
        as_of: datetime | str | None = None,
    ) -> StrategyRegistryEvaluation:
        if as_of is None:
            raise ValueError("strategy registry evaluation requires explicit as_of")
        evaluated_at = _aware_datetime(as_of)
        global_reasons = self._global_reasons(evaluated_at)
        as_of_text = _iso(evaluated_at) if evaluated_at is not None else str(as_of or "invalid")
        normalized_policies = {
            str(strategy): _policy_snapshot(policy)
            for strategy, policy in sorted(policies.items(), key=lambda item: str(item[0]))
        }
        decisions: list[StrategyRegistryDecision] = []
        for registry_key, raw_entry in sorted(self.entries.items(), key=lambda item: str(item[0])):
            entry = dict(raw_entry) if isinstance(raw_entry, Mapping) else {}
            decisions.append(
                self._evaluate_entry(
                    str(registry_key),
                    entry,
                    normalized_policies.get(str(registry_key)),
                    evaluated_at,
                    global_reasons,
                )
            )
        authorized = tuple(decision for decision in decisions if decision.authorized)
        rejected = tuple(decision for decision in decisions if not decision.authorized)
        raw_inputs = {
            "as_of": as_of_text,
            "registry": self.registry,
            "policies": normalized_policies,
            "available_implementations": dict(sorted(self.available_implementations.items())),
            "configuration_version": self.config.get("configuration_schema_version"),
            "configuration_hash": self.config.get("effective_config_hash"),
            "runtime_mode": self.config.get("mode"),
            "live_enabled": self.config.get("live_enabled"),
            "auto_execution_enabled": self.config.get("auto_execution_enabled", False),
            "live_execution_capability": (
                self.config.get("execution_capabilities", {}).get("live_execution_enabled")
                if isinstance(self.config.get("execution_capabilities"), Mapping)
                else None
            ),
        }
        payload = {
            "as_of": as_of_text,
            "authorized": [decision.as_dict() for decision in authorized],
            "rejected": [decision.as_dict() for decision in rejected],
            "global_reasons": list(global_reasons),
            "registry_schema_version": self.registry.get("schema_version"),
            "registry_formula_version": self.registry.get("formula_version"),
            "configuration_version": self.config.get("configuration_schema_version"),
            "configuration_hash": self.config.get("effective_config_hash"),
            "raw_inputs": raw_inputs,
        }
        return StrategyRegistryEvaluation(
            as_of=as_of_text,
            authorized=authorized,
            rejected=rejected,
            global_reasons=global_reasons,
            raw_inputs=raw_inputs,
            fingerprint=_fingerprint(payload),
        )

    def _evaluate_entry(
        self,
        registry_key: str,
        entry: Mapping[str, Any],
        policy: Mapping[str, Any] | None,
        as_of: datetime | None,
        global_reasons: tuple[str, ...],
    ) -> StrategyRegistryDecision:
        reasons = list(global_reasons)
        strategy_name = str(entry.get("strategy_name") or "")
        strategy_version = str(entry.get("strategy_version") or registry_key)
        implementation_id = str(entry.get("implementation_id") or "")
        implementation_version = str(entry.get("implementation_version") or "")
        effective_at = _aware_datetime(entry.get("effective_at"))
        expires_at = _aware_datetime(entry.get("expires_at"))

        if not isinstance(entry, Mapping) or not entry:
            reasons.append("registry_entry_invalid")
        if not strategy_name:
            reasons.append("strategy_name_missing")
        if not strategy_version or strategy_version != registry_key:
            reasons.append("strategy_version_mismatch")
        if not implementation_id:
            reasons.append("implementation_id_missing")
        if not implementation_version:
            reasons.append("implementation_version_missing")
        if entry.get("implementation_available") is not True:
            reasons.append("implementation_not_declared_available")
        runtime_implementation_version = self.available_implementations.get(implementation_id)
        if runtime_implementation_version is None:
            reasons.append("implementation_not_available")
        elif runtime_implementation_version != implementation_version:
            reasons.append("implementation_version_mismatch")
        if entry.get("execution_eligible") is not True:
            reasons.append("execution_eligibility_missing")
        if entry.get("paper_eligible") is not True:
            reasons.append("paper_execution_eligibility_missing")
        if entry.get("live_eligible") is not False:
            reasons.append("live_execution_eligibility_forbidden")
        if entry.get("human_authorized") is not True:
            reasons.append("human_authorization_missing")
        if entry.get("config_authorized") is not True:
            reasons.append("configuration_authorization_missing")
        if not entry.get("authorization_id"):
            reasons.append("authorization_id_missing")
        if entry.get("suspended") is not False:
            reasons.append("registry_entry_suspended")
        if effective_at is None:
            reasons.append("effective_at_invalid")
        elif as_of is not None and as_of < effective_at:
            reasons.append("authorization_not_yet_effective")
        if expires_at is None:
            reasons.append("expires_at_invalid")
        elif as_of is not None and as_of >= expires_at:
            reasons.append("authorization_expired")
        if effective_at is not None and expires_at is not None and expires_at <= effective_at:
            reasons.append("authorization_window_invalid")

        policy_snapshot = dict(policy or {})
        policy_state = str(policy_snapshot.get("state") or "")
        if not policy:
            reasons.append("profitability_policy_missing")
        if policy_snapshot.get("strategy_version") != strategy_version:
            reasons.append("policy_strategy_version_mismatch")
        if policy_state not in POLICY_STATES:
            reasons.append("policy_state_invalid")
        elif policy_state not in EXECUTABLE_POLICY_STATES:
            reasons.append(f"policy_state_not_executable:{policy_state}")
        if policy_snapshot.get("enforcement_enabled") is not True:
            reasons.append("profitability_policy_enforcement_disabled")
        if policy_snapshot.get("suspended") is not False:
            reasons.append("policy_suspended")
        if policy_snapshot.get("evidence_current") is not True:
            reasons.append("evidence_stale_or_unverified")
        if policy_snapshot.get("evidence_version_complete") is not True:
            reasons.append("evidence_version_incomplete")

        expected_versions = {
            "evidence_version": entry.get("evidence_version") or self.registry.get("required_evidence_version"),
            "performance_version": (
                entry.get("performance_version")
                or self.registry.get("required_performance_version")
            ),
            "policy_version": entry.get("policy_version") or self.registry.get("required_policy_version"),
            "schema_version": entry.get("policy_schema_version") or self.registry.get("required_policy_schema_version"),
            "configuration_version": self.config.get("configuration_schema_version"),
        }
        for policy_key, expected in expected_versions.items():
            actual = policy_snapshot.get(policy_key)
            label = "policy_schema_version" if policy_key == "schema_version" else policy_key
            if not actual:
                reasons.append(f"{label}_missing")
            elif not expected or actual != expected:
                reasons.append(f"{label}_mismatch")
        config_hash = str(self.config.get("effective_config_hash") or "")
        if not policy_snapshot.get("config_hash"):
            reasons.append("policy_configuration_hash_missing")
        elif policy_snapshot.get("config_hash") != config_hash:
            reasons.append("policy_configuration_hash_mismatch")
        if not policy_snapshot.get("fingerprint"):
            reasons.append("policy_fingerprint_missing")

        reasons = list(dict.fromkeys(reasons))
        payload = {
            "registry_key": registry_key,
            "entry": dict(entry),
            "policy": policy_snapshot,
            "as_of": _iso(as_of) if as_of is not None else None,
            "available_implementation_version": runtime_implementation_version,
            "configuration_version": self.config.get("configuration_schema_version"),
            "configuration_hash": config_hash,
            "registry_schema_version": self.registry.get("schema_version"),
            "registry_formula_version": self.registry.get("formula_version"),
            "reasons": reasons,
        }
        return StrategyRegistryDecision(
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            implementation_id=implementation_id,
            implementation_version=implementation_version,
            implementation_available=(
                entry.get("implementation_available") is True
                and runtime_implementation_version == implementation_version
            ),
            execution_eligible=entry.get("execution_eligible") is True,
            paper_eligible=entry.get("paper_eligible") is True,
            live_eligible=entry.get("live_eligible") is True,
            human_authorized=entry.get("human_authorized") is True,
            config_authorized=entry.get("config_authorized") is True,
            authorization_id=str(entry.get("authorization_id")) if entry.get("authorization_id") else None,
            suspended=entry.get("suspended") is not False or policy_snapshot.get("suspended") is not False,
            policy_state=policy_state,
            policy_enforcement_enabled=policy_snapshot.get("enforcement_enabled") is True,
            evidence_current=policy_snapshot.get("evidence_current") is True,
            evidence_version_complete=policy_snapshot.get("evidence_version_complete") is True,
            authorized=not reasons,
            reasons=tuple(reasons),
            effective_at=_iso(effective_at) if effective_at is not None else None,
            expires_at=_iso(expires_at) if expires_at is not None else None,
            evidence_version=policy_snapshot.get("evidence_version"),
            performance_version=policy_snapshot.get("performance_version"),
            policy_version=policy_snapshot.get("policy_version"),
            policy_schema_version=policy_snapshot.get("schema_version"),
            configuration_version=self.config.get("configuration_schema_version"),
            configuration_hash=config_hash or None,
            registry_schema_version=self.registry.get("schema_version"),
            registry_formula_version=self.registry.get("formula_version"),
            decision_fingerprint=_fingerprint(payload),
        )


def persist(
    storage: Any,
    run_id: str,
    evaluation: StrategyRegistryEvaluation,
) -> dict[str, Any]:
    """Persist one registry evaluation atomically and idempotently per run."""
    if not str(run_id).strip():
        raise ValueError("run_id is required for strategy registry persistence")
    snapshot_id = _fingerprint(
        {"run_id": str(run_id), "evaluation_fingerprint": evaluation.fingerprint}
    )[:32]
    decisions = (*evaluation.authorized, *evaluation.rejected)
    registry = evaluation.raw_inputs.get("registry", {})
    entries = registry.get("entries", {}) if isinstance(registry, Mapping) else {}
    policies = evaluation.raw_inputs.get("policies", {})
    implementations = evaluation.raw_inputs.get("available_implementations", {})
    decision_ids: list[str] = []
    with storage.connect() as conn:
        apply_strategy_registry_schema(conn)
        conn.execute(
            """INSERT OR IGNORE INTO strategy_registry_snapshots(
                 id,run_id,evaluated_at,registry_schema_version,registry_formula_version,
                 configuration_version,config_hash,authorized_strategies_json,
                 rejected_strategies_json,global_reasons_json,raw_inputs_json,
                 evaluation_fingerprint,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                str(run_id),
                evaluation.as_of,
                str(registry.get("schema_version") or "") if isinstance(registry, Mapping) else "",
                str(registry.get("formula_version") or "") if isinstance(registry, Mapping) else "",
                evaluation.raw_inputs.get("configuration_version"),
                evaluation.raw_inputs.get("configuration_hash"),
                _canonical_json([item.as_dict() for item in evaluation.authorized]),
                _canonical_json([item.as_dict() for item in evaluation.rejected]),
                _canonical_json(list(evaluation.global_reasons)),
                _canonical_json(evaluation.raw_inputs),
                evaluation.fingerprint,
                evaluation.as_of,
            ),
        )
        for decision in decisions:
            decision_id = _fingerprint(
                {
                    "snapshot_id": snapshot_id,
                    "strategy_version": decision.strategy_version,
                    "decision_fingerprint": decision.decision_fingerprint,
                }
            )[:32]
            decision_ids.append(decision_id)
            entry = entries.get(decision.strategy_version, {}) if isinstance(entries, Mapping) else {}
            policy = policies.get(decision.strategy_version, {}) if isinstance(policies, Mapping) else {}
            implementation_version = (
                implementations.get(decision.implementation_id)
                if isinstance(implementations, Mapping)
                else None
            )
            raw_inputs = {
                "as_of": evaluation.as_of,
                "registry_entry": entry,
                "policy": policy,
                "available_implementation_version": implementation_version,
                "configuration_version": evaluation.raw_inputs.get("configuration_version"),
                "configuration_hash": evaluation.raw_inputs.get("configuration_hash"),
                "registry_schema_version": decision.registry_schema_version,
                "registry_formula_version": decision.registry_formula_version,
            }
            conn.execute(
                """INSERT OR IGNORE INTO strategy_registry_decisions(
                     id,snapshot_id,run_id,strategy_name,strategy_version,authorized,
                     policy_state,reasons_json,reason,decision_json,raw_inputs_json,
                     evidence_version,performance_version,policy_version,
                     policy_schema_version,configuration_version,config_hash,
                     decision_fingerprint,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    snapshot_id,
                    str(run_id),
                    decision.strategy_name,
                    decision.strategy_version,
                    int(decision.authorized),
                    decision.policy_state,
                    _canonical_json(list(decision.reasons)),
                    decision.reason,
                    _canonical_json(decision.as_dict()),
                    _canonical_json(raw_inputs),
                    decision.evidence_version,
                    decision.performance_version,
                    decision.policy_version,
                    decision.policy_schema_version,
                    decision.configuration_version,
                    decision.configuration_hash,
                    decision.decision_fingerprint,
                    evaluation.as_of,
                ),
            )
    return {
        "snapshot_id": snapshot_id,
        "decision_ids": tuple(decision_ids),
        "evaluation_fingerprint": evaluation.fingerprint,
    }


__all__ = [
    "EXECUTABLE_POLICY_STATES",
    "POLICY_STATES",
    "REGISTRY_FORMULA_VERSION",
    "REGISTRY_SCHEMA_VERSION",
    "StrategyExecutionRegistry",
    "StrategyRegistryDecision",
    "StrategyRegistryEvaluation",
    "apply_strategy_registry_schema",
    "persist",
]
