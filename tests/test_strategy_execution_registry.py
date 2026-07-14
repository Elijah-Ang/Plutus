from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from app.strategy_execution_registry import (
    EXECUTABLE_POLICY_STATES,
    REGISTRY_FORMULA_VERSION,
    REGISTRY_SCHEMA_VERSION,
    StrategyExecutionRegistry,
    apply_strategy_registry_schema,
    persist,
)
from app.strategy_performance import StrategyRiskPolicy


AS_OF = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
CONFIGURATION_VERSION = "plutus_test_config_v1"
CONFIG_HASH = "a" * 64
EVIDENCE_VERSION = "phase1_outcome_v2_exit_session"
PERFORMANCE_VERSION = "strategy_performance_v2_2_probe"
POLICY_VERSION = "strategy_policy_v2_2_probe"
POLICY_SCHEMA_VERSION = "strategy_profitability_engine_v1"


def _entry(strategy: str = "rule_based_v2") -> dict:
    return {
        "strategy_name": "Rule Based",
        "strategy_version": strategy,
        "implementation_id": f"implementation:{strategy}",
        "implementation_version": "implementation_v1",
        "implementation_available": True,
        "execution_eligible": True,
        "paper_eligible": True,
        "live_eligible": False,
        "human_authorized": True,
        "config_authorized": True,
        "authorization_id": f"paper-authorization:{strategy}",
        "effective_at": "2026-07-01T00:00:00+00:00",
        "expires_at": "2027-07-01T00:00:00+00:00",
        "suspended": False,
    }


def _config(*strategies: str) -> dict:
    strategies = strategies or ("rule_based_v2",)
    return {
        "configuration_schema_version": CONFIGURATION_VERSION,
        "effective_config_hash": CONFIG_HASH,
        "mode": "paper",
        "live_enabled": False,
        "auto_execution_enabled": False,
        "execution_capabilities": {"live_execution_enabled": False},
        "strategy_execution_registry": {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "formula_version": REGISTRY_FORMULA_VERSION,
            "mode": "paper_only",
            "required_configuration_version": CONFIGURATION_VERSION,
            "required_evidence_version": EVIDENCE_VERSION,
            "required_performance_version": PERFORMANCE_VERSION,
            "required_policy_version": POLICY_VERSION,
            "required_policy_schema_version": POLICY_SCHEMA_VERSION,
            "entries": {strategy: _entry(strategy) for strategy in strategies},
        },
    }


def _policy(strategy: str = "rule_based_v2", state: str = "ACTIVE") -> dict:
    return {
        "strategy_version": strategy,
        "state": state,
        "enforcement_enabled": True,
        "evidence_current": True,
        "evidence_version_complete": True,
        "evidence_version": EVIDENCE_VERSION,
        "performance_version": PERFORMANCE_VERSION,
        "policy_version": POLICY_VERSION,
        "schema_version": POLICY_SCHEMA_VERSION,
        "configuration_version": CONFIGURATION_VERSION,
        "config_hash": CONFIG_HASH,
        "suspended": False,
        "fingerprint": f"policy:{strategy}:{state}",
    }


def _implementations(*strategies: str) -> dict[str, str]:
    strategies = strategies or ("rule_based_v2",)
    return {f"implementation:{strategy}": "implementation_v1" for strategy in strategies}


def _evaluate(config: dict, policies: dict, implementations: dict[str, str] | None = None):
    return StrategyExecutionRegistry(
        config,
        available_implementations=(
            implementations
            or _implementations(*config["strategy_execution_registry"]["entries"])
        ),
    ).evaluate(policies, as_of=AS_OF)


class _MemoryStorage:
    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    @contextmanager
    def connect(self):
        try:
            yield self.conn
        except Exception:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()


@pytest.mark.parametrize("state", sorted(EXECUTABLE_POLICY_STATES))
def test_all_executable_policy_states_require_and_receive_explicit_authorization(state: str) -> None:
    result = _evaluate(_config(), {"rule_based_v2": _policy(state=state)})

    assert result.authorized_versions == ("rule_based_v2",)
    assert result.rejected == ()
    assert result.authorized[0].policy_state == state
    assert result.authorized[0].reason == "authorized_for_bounded_paper_execution"


@pytest.mark.parametrize("state", ["RESEARCH_ONLY", "SUSPENDED"])
def test_non_executable_policy_states_fail_closed(state: str) -> None:
    result = _evaluate(_config(), {"rule_based_v2": _policy(state=state)})

    assert result.authorized == ()
    assert result.rejected[0].reasons == (f"policy_state_not_executable:{state}",)


def test_research_evidence_without_current_enforced_policy_cannot_authorize() -> None:
    config = _config()
    config["strategy_execution_registry"]["entries"]["rule_based_v2"]["research_evidence"] = {
        "expectancy": 1.0,
        "qualified": True,
    }

    result = _evaluate(config, {})

    assert result.authorized == ()
    assert "profitability_policy_missing" in result.rejected[0].reasons
    assert "policy_state_invalid" in result.rejected[0].reasons


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("implementation_available", False, "implementation_not_declared_available"),
        ("execution_eligible", False, "execution_eligibility_missing"),
        ("paper_eligible", False, "paper_execution_eligibility_missing"),
        ("live_eligible", True, "live_execution_eligibility_forbidden"),
        ("human_authorized", False, "human_authorization_missing"),
        ("config_authorized", False, "configuration_authorization_missing"),
        ("authorization_id", "", "authorization_id_missing"),
        ("suspended", True, "registry_entry_suspended"),
    ],
)
def test_every_registry_authorization_gate_is_fail_closed(field: str, value: object, reason: str) -> None:
    config = _config()
    config["strategy_execution_registry"]["entries"]["rule_based_v2"][field] = value

    result = _evaluate(config, {"rule_based_v2": _policy()})

    assert result.authorized == ()
    assert reason in result.rejected[0].reasons


def test_runtime_implementation_inventory_is_authoritative() -> None:
    missing = StrategyExecutionRegistry(_config(), available_implementations={}).evaluate(
        {"rule_based_v2": _policy()}, as_of=AS_OF
    )
    mismatched = StrategyExecutionRegistry(
        _config(), available_implementations={"implementation:rule_based_v2": "wrong_version"}
    ).evaluate({"rule_based_v2": _policy()}, as_of=AS_OF)

    assert missing.rejected[0].reasons == ("implementation_not_available",)
    assert mismatched.rejected[0].reasons == ("implementation_version_mismatch",)


@pytest.mark.parametrize(
    ("effective_at", "expires_at", "reason"),
    [
        ("2026-08-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00", "authorization_not_yet_effective"),
        ("2025-01-01T00:00:00+00:00", "2026-07-14T08:00:00+00:00", "authorization_expired"),
        ("not-a-date", "2027-01-01T00:00:00+00:00", "effective_at_invalid"),
        ("2026-07-01T00:00:00", "2027-01-01T00:00:00+00:00", "effective_at_invalid"),
        ("2027-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "authorization_window_invalid"),
    ],
)
def test_effective_and_expiry_windows_are_timezone_aware_and_fail_closed(
    effective_at: str, expires_at: str, reason: str
) -> None:
    config = _config()
    entry = config["strategy_execution_registry"]["entries"]["rule_based_v2"]
    entry["effective_at"], entry["expires_at"] = effective_at, expires_at

    result = _evaluate(config, {"rule_based_v2": _policy()})

    assert result.authorized == ()
    assert reason in result.rejected[0].reasons


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("evidence_current", False, "evidence_stale_or_unverified"),
        ("evidence_version_complete", False, "evidence_version_incomplete"),
        ("enforcement_enabled", False, "profitability_policy_enforcement_disabled"),
        ("suspended", True, "policy_suspended"),
        ("evidence_version", "old", "evidence_version_mismatch"),
        ("performance_version", "old", "performance_version_mismatch"),
        ("policy_version", "old", "policy_version_mismatch"),
        ("schema_version", "old", "policy_schema_version_mismatch"),
        ("configuration_version", "old", "configuration_version_mismatch"),
        ("config_hash", "old", "policy_configuration_hash_mismatch"),
        ("fingerprint", "", "policy_fingerprint_missing"),
    ],
)
def test_policy_freshness_suspension_and_all_versions_fail_closed(
    field: str, value: object, reason: str
) -> None:
    policy = _policy()
    policy[field] = value

    result = _evaluate(_config(), {"rule_based_v2": policy})

    assert result.authorized == ()
    assert reason in result.rejected[0].reasons


def test_strategy_order_and_fingerprint_are_deterministic_and_replayable() -> None:
    strategies = ("zeta_v1", "alpha_v1")
    config = _config(*strategies)
    policies = {strategy: _policy(strategy) for strategy in strategies}
    implementations = _implementations(*strategies)

    first = _evaluate(config, policies, implementations)
    second = _evaluate(
        deepcopy(config),
        {strategy: policies[strategy] for strategy in reversed(strategies)},
        dict(reversed(list(implementations.items()))),
    )

    assert first.authorized_versions == ("alpha_v1", "zeta_v1")
    assert first.as_dict() == second.as_dict()
    changed = deepcopy(policies)
    changed["alpha_v1"]["state"] = "THROTTLED"
    changed_result = _evaluate(config, changed, implementations)
    assert changed_result.fingerprint != first.fingerprint


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("mode",), "live", "runtime_mode_not_paper"),
        (("live_enabled",), True, "live_execution_not_disabled"),
        (("auto_execution_enabled",), True, "autonomous_execution_not_disabled"),
        (("execution_capabilities", "live_execution_enabled"), True, "live_execution_capability_not_disabled"),
        (("strategy_execution_registry", "mode"), "paper_and_live", "registry_not_paper_only"),
    ],
)
def test_runtime_and_registry_can_never_be_live_or_autonomous(
    path: tuple[str, ...], value: object, reason: str
) -> None:
    config = _config()
    target = config
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value

    result = _evaluate(config, {"rule_based_v2": _policy()})

    assert result.authorized == ()
    assert reason in result.global_reasons
    assert reason in result.rejected[0].reasons


def test_global_version_or_hash_omission_rejects_every_entry_with_exact_reason() -> None:
    config = _config()
    config["effective_config_hash"] = ""
    config["strategy_execution_registry"]["required_policy_schema_version"] = ""

    result = _evaluate(config, {"rule_based_v2": _policy()})

    assert result.authorized == ()
    assert result.global_reasons == (
        "configuration_hash_missing",
        "required_policy_schema_version_missing",
    )
    assert result.rejected[0].reasons[:2] == result.global_reasons


def test_actual_strategy_risk_policy_fields_supply_strict_evidence_metadata() -> None:
    policy = StrategyRiskPolicy(
        strategy_version="rule_based_v2",
        state="ACTIVE",
        enforcement_enabled=True,
        performance_version=PERFORMANCE_VERSION,
        policy_version=POLICY_VERSION,
        schema_version=POLICY_SCHEMA_VERSION,
        fingerprint="persisted-policy-fingerprint",
        hard_gates={"evidence_fresh": True, "version_complete": True},
        raw_inputs={
            "current_evidence_version": EVIDENCE_VERSION,
            "configuration_schema_version": CONFIGURATION_VERSION,
            "effective_config_hash": CONFIG_HASH,
        },
    )

    result = _evaluate(_config(), {"rule_based_v2": policy})

    assert result.authorized_versions == ("rule_based_v2",)
    decision = result.authorized[0]
    assert decision.evidence_current is True
    assert decision.evidence_version_complete is True
    assert decision.evidence_version == EVIDENCE_VERSION
    assert result.raw_inputs["policies"]["rule_based_v2"]["raw_inputs"] == policy.raw_inputs


def test_strategy_registry_schema_is_additive_and_idempotent() -> None:
    storage = _MemoryStorage()
    storage.conn.execute(
        "CREATE TABLE schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL,detail TEXT)"
    )

    apply_strategy_registry_schema(storage.conn)
    apply_strategy_registry_schema(storage.conn)

    tables = {
        row[0]
        for row in storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"strategy_registry_snapshots", "strategy_registry_decisions"} <= tables
    assert storage.conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version=?", (REGISTRY_SCHEMA_VERSION,)
    ).fetchone()[0] == 1
    snapshot_columns = {
        row[1] for row in storage.conn.execute("PRAGMA table_info(strategy_registry_snapshots)")
    }
    decision_columns = {
        row[1] for row in storage.conn.execute("PRAGMA table_info(strategy_registry_decisions)")
    }
    assert {"raw_inputs_json", "evaluation_fingerprint", "global_reasons_json"} <= snapshot_columns
    assert {"reasons_json", "raw_inputs_json", "decision_fingerprint"} <= decision_columns


def test_persist_is_idempotent_and_retains_exact_authorized_and_rejected_inputs() -> None:
    strategies = ("rule_based_v2", "shadow_v1")
    config = _config(*strategies)
    policies = {
        "rule_based_v2": _policy("rule_based_v2", "ACTIVE"),
        "shadow_v1": _policy("shadow_v1", "RESEARCH_ONLY"),
    }
    evaluation = _evaluate(config, policies, _implementations(*strategies))
    storage = _MemoryStorage()

    first = persist(storage, "run-1", evaluation)
    second = persist(storage, "run-1", evaluation)

    assert first == second
    assert storage.conn.execute("SELECT COUNT(*) FROM strategy_registry_snapshots").fetchone()[0] == 1
    assert storage.conn.execute("SELECT COUNT(*) FROM strategy_registry_decisions").fetchone()[0] == 2
    snapshot = storage.conn.execute("SELECT * FROM strategy_registry_snapshots").fetchone()
    assert snapshot["evaluation_fingerprint"] == evaluation.fingerprint
    assert json.loads(snapshot["raw_inputs_json"]) == evaluation.raw_inputs
    assert json.loads(snapshot["authorized_strategies_json"]) == [
        evaluation.authorized[0].as_dict()
    ]
    assert json.loads(snapshot["rejected_strategies_json"]) == [
        evaluation.rejected[0].as_dict()
    ]
    rejected = storage.conn.execute(
        "SELECT * FROM strategy_registry_decisions WHERE authorized=0"
    ).fetchone()
    assert json.loads(rejected["reasons_json"]) == [
        "policy_state_not_executable:RESEARCH_ONLY"
    ]
    rejected_raw = json.loads(rejected["raw_inputs_json"])
    assert rejected_raw["registry_entry"] == config["strategy_execution_registry"]["entries"]["shadow_v1"]
    assert rejected_raw["policy"] == evaluation.raw_inputs["policies"]["shadow_v1"]


def test_replayed_evaluation_has_byte_identical_fingerprint_and_persists_per_run() -> None:
    config = _config()
    policy = _policy()
    first = _evaluate(config, {"rule_based_v2": policy})
    replay = _evaluate(deepcopy(config), {"rule_based_v2": deepcopy(policy)})
    storage = _MemoryStorage()

    assert first.as_dict() == replay.as_dict()
    persist(storage, "run-1", first)
    persist(storage, "run-1", replay)
    persist(storage, "run-2", replay)

    rows = storage.conn.execute(
        "SELECT run_id,evaluation_fingerprint,raw_inputs_json FROM strategy_registry_snapshots ORDER BY run_id"
    ).fetchall()
    assert len(rows) == 2
    assert {row["evaluation_fingerprint"] for row in rows} == {first.fingerprint}
    assert rows[0]["raw_inputs_json"] == rows[1]["raw_inputs_json"]
    assert storage.conn.execute("SELECT COUNT(*) FROM strategy_registry_decisions").fetchone()[0] == 2
