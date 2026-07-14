from __future__ import annotations

import json
from copy import deepcopy

import numpy as np
import pytest

from app.formula_versions import (
    EVIDENCE_VERSION,
    STRATEGY_PERFORMANCE_SCHEMA_VERSION,
    STRATEGY_PERFORMANCE_VERSION,
    STRATEGY_POLICY_VERSION,
)
from app.phase4_allocator import AdaptiveAllocator, allocate_candidates_to_sleeves
from app.storage import Storage
from app.strategy_execution_registry import REGISTRY_FORMULA_VERSION, REGISTRY_SCHEMA_VERSION
from app.utils import load_config


AS_OF = "2026-07-14T08:00:00+00:00"


def _entry(strategy: str) -> dict:
    return {
        "strategy_name": strategy,
        "strategy_version": strategy,
        "implementation_id": f"implementation:{strategy}",
        "implementation_version": "implementation_v1",
        "implementation_available": True,
        "execution_eligible": True,
        "paper_eligible": True,
        "live_eligible": False,
        "human_authorized": True,
        "config_authorized": True,
        "authorization_id": f"paper:{strategy}",
        "effective_at": "2026-07-01T00:00:00+00:00",
        "expires_at": "2027-07-01T00:00:00+00:00",
        "suspended": False,
    }


def _policy(config: dict, strategy: str, state: str = "ACTIVE", quality: float = 85.0) -> dict:
    return {
        "strategy_version": strategy,
        "state": state,
        "quality_score": quality,
        "enforcement_enabled": True,
        "evidence_current": True,
        "evidence_version_complete": True,
        "evidence_version": EVIDENCE_VERSION,
        "performance_version": STRATEGY_PERFORMANCE_VERSION,
        "policy_version": STRATEGY_POLICY_VERSION,
        "schema_version": STRATEGY_PERFORMANCE_SCHEMA_VERSION,
        "configuration_version": config["configuration_schema_version"],
        "config_hash": config["effective_config_hash"],
        "suspended": False,
        "fingerprint": f"policy:{strategy}:{state}",
        "reason": f"test {state}",
    }


def _config(*strategies: str) -> dict:
    config = deepcopy(load_config())
    config["strategy_execution_registry"] = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "formula_version": REGISTRY_FORMULA_VERSION,
        "mode": "paper_only",
        "required_configuration_version": config["configuration_schema_version"],
        "required_evidence_version": EVIDENCE_VERSION,
        "required_performance_version": STRATEGY_PERFORMANCE_VERSION,
        "required_policy_version": STRATEGY_POLICY_VERSION,
        "required_policy_schema_version": STRATEGY_PERFORMANCE_SCHEMA_VERSION,
        "entries": {strategy: _entry(strategy) for strategy in strategies},
    }
    return config


def _allocator(tmp_path, config: dict, strategies: tuple[str, ...], run_id: str = "phase43") -> tuple[Storage, AdaptiveAllocator]:
    storage = Storage(tmp_path / f"{run_id}.sqlite3")
    storage.initialize()
    implementations = {f"implementation:{strategy}": "implementation_v1" for strategy in strategies}
    return storage, AdaptiveAllocator(
        storage, config, run_id, available_implementations=implementations,
    )


def test_zero_authorized_strategies_preserves_cash_through_empty_vector_path(tmp_path) -> None:
    strategies = ("alpha_v1",)
    config = _config(*strategies)
    storage, allocator = _allocator(tmp_path, config, strategies, "zero")

    result = allocator.run(
        regime="normal",
        drawdown_pct=0.0,
        strategy_policy_map={"alpha_v1": _policy(config, "alpha_v1", "SUSPENDED")},
        portfolio_snapshot={"phase3_available_risk_pct": 1.0},
        as_of=AS_OF,
    )

    assert result["decision"] == "PRESERVE_CASH"
    assert result["authorized_strategies"] == []
    assert result["strategy_sleeves"] == {}
    assert result["covariance"]["strategy_order"] == []
    assert result["covariance"]["dimensions"] == [0, 0]
    assert result["phase3_available_risk"] == 1.0
    assert result["unallocated_available_risk"] == 1.0
    assert result["risk_reconciliation_residual"] == 0.0
    assert storage.fetch_all("SELECT payload FROM phase4_allocation_decisions")


def test_one_authorized_strategy_uses_valid_degenerate_covariance_and_exact_sleeve(tmp_path) -> None:
    strategies = ("alpha_v1",)
    config = _config(*strategies)
    _storage, allocator = _allocator(tmp_path, config, strategies, "one")

    result = allocator.run(
        regime="normal",
        drawdown_pct=0.0,
        strategy_policy_map={"alpha_v1": _policy(config, "alpha_v1")},
        portfolio_snapshot={
            "phase3_available_risk_pct": 1.0,
            "reserved_risk_by_strategy": {"alpha_v1": 0.01},
        },
        as_of=AS_OF,
    )

    covariance = result["covariance"]
    assert result["authorized_strategies"] == ["alpha_v1"]
    assert covariance["strategy_order"] == ["alpha_v1"]
    assert covariance["dimensions"] == [1, 1]
    assert covariance["matrix_finite"] is True
    assert covariance["matrix_psd"] is True
    assert covariance["aligned_pairwise_inputs"] == {}
    sleeve = result["strategy_sleeves"]["alpha_v1"]
    assert sleeve["allocated_risk"] > 0
    assert sleeve["remaining_risk"] == pytest.approx(sleeve["allocated_risk"] - 0.01)
    assert sum(item["allocated_risk"] for item in result["strategy_sleeves"].values()) + result["unallocated_available_risk"] == pytest.approx(1.0)
    assert result["risk_reconciliation_residual"] == 0.0


def test_multiple_authorized_strategies_have_deterministic_psd_order_and_replay_payload(tmp_path) -> None:
    strategies = ("zeta_v1", "alpha_v1")
    config = _config(*strategies)
    storage, allocator = _allocator(tmp_path, config, strategies, "many")
    policies = {strategy: _policy(config, strategy) for strategy in strategies}

    result = allocator.run(
        regime="normal", drawdown_pct=0.0, strategy_policy_map=policies,
        portfolio_snapshot={"phase3_available_risk_pct": 1.0, "as_of": AS_OF},
        as_of=AS_OF,
    )

    assert result["authorized_strategies"] == ["alpha_v1", "zeta_v1"]
    assert all(result["weights"][strategy] > 0 for strategy in strategies)
    covariance = result["covariance"]
    assert covariance["strategy_order"] == ["alpha_v1", "zeta_v1"]
    assert covariance["dimensions"] == [2, 2]
    assert covariance["matrix_finite"] and covariance["matrix_symmetric"] and covariance["matrix_psd"]
    assert covariance["covariance"][0][1] > 0.0
    allocated = sum(item["allocated_risk"] for item in result["strategy_sleeves"].values())
    assert allocated + result["unallocated_available_risk"] == pytest.approx(1.0)
    persisted = json.loads(storage.fetch_all(
        "SELECT payload FROM phase4_allocation_decisions WHERE id=?", (result["allocation_id"],)
    )[0]["payload"])
    assert persisted["raw_replay_inputs"] == result["raw_replay_inputs"]
    assert persisted["registry_evaluation"]["authorized_versions"] == ["alpha_v1", "zeta_v1"]


def test_covariance_persists_exact_aligned_inputs_and_repairs_invalid_rows(tmp_path) -> None:
    config = _config("alpha_v1", "beta_v1")
    _storage, allocator = _allocator(tmp_path, config, ("alpha_v1", "beta_v1"), "covariance")
    evidence = {
        "beta_v1": [
            {"id": f"b-{index}", "exit_session": f"2026-07-{index + 1:02d}", "cost_adjusted_return": value}
            for index, value in enumerate((0.01, 0.02, -0.01, 0.03, 0.015))
        ],
        "alpha_v1": [
            {"id": f"a-{index}", "exit_session": f"2026-07-{index + 1:02d}", "cost_adjusted_return": value}
            for index, value in enumerate((0.02, 0.01, -0.005, 0.025, 0.01))
        ] + [{"id": "invalid", "exit_session": "2026-07-20", "cost_adjusted_return": float("nan")}],
    }

    first, fallback, counts = allocator.covariance(evidence, ("alpha_v1", "beta_v1"))
    first_payload = deepcopy(allocator._last_covariance_payload)
    second, second_fallback, second_counts = allocator.covariance(dict(reversed(list(evidence.items()))), ("alpha_v1", "beta_v1"))

    assert np.array_equal(first, second)
    assert fallback is True and second_fallback is True
    assert counts == second_counts == {"alpha_v1": 5, "beta_v1": 5}
    assert np.isfinite(first).all()
    assert np.linalg.eigvalsh(first).min() >= -1e-12
    assert len(first_payload["aligned_pairwise_inputs"]["alpha_v1|beta_v1"]) == 5
    assert first_payload["invalid_rows"]["alpha_v1"][0]["id"] == "invalid"


def test_candidate_allocation_ranks_globally_but_cannot_cross_sleeves_and_exits_bypass() -> None:
    sleeves = {
        "alpha_v1": {"remaining_risk": 0.30, "allocated_risk": 0.30, "state": "ACTIVE"},
        "beta_v1": {"remaining_risk": 0.20, "allocated_risk": 0.20, "state": "ACTIVE"},
    }
    base = {
        "symbol": "SPY", "action": "entry", "side": "buy", "evidence_quality": 90,
        "regime": "favorable", "execution_fill_rate": 0.98, "execution_shortfall_bps": 2,
        "average_dollar_volume": 20_000_000, "spread_bps": 2, "stop_quality": 95,
        "reward_to_risk": 3, "uncertainty": 0.05, "deterioration_score": 0,
        "symbol_exposure_pct": 0, "cluster_exposure_pct": 0, "stop_risk_pct": 0.10,
    }
    candidates = [
        {**base, "strategy_version": "alpha_v1", "setup_id": "strong", "setup_score": 98, "requested_stop_risk": 0.20},
        {**base, "strategy_version": "beta_v1", "setup_id": "second", "setup_score": 90, "requested_stop_risk": 0.20},
        {**base, "strategy_version": "alpha_v1", "setup_id": "weak", "setup_score": 45, "requested_stop_risk": 0.20, "uncertainty": 0.9},
        {"strategy_version": "suspended_v1", "symbol": "QQQ", "action": "exit", "side": "sell"},
    ]

    first = allocate_candidates_to_sleeves(candidates, sleeves, global_available_risk=0.40)
    second = allocate_candidates_to_sleeves(deepcopy(candidates), deepcopy(sleeves), global_available_risk=0.40)
    entries = [decision for decision in first["decisions"] if decision["decision"] != "EXIT_BYPASS"]

    assert first == second
    assert first["decisions"][0]["decision"] == "EXIT_BYPASS"
    assert [decision["strategy_version"] for decision in entries[:2]] == ["alpha_v1", "beta_v1"]
    assert entries[0]["candidate_id"] != entries[1]["candidate_id"]
    assert entries[2]["decision"] == "REJECT"
    assert first["allocated_by_strategy"] == {"alpha_v1": 0.20, "beta_v1": 0.20}
    assert first["allocated_risk"] == 0.40
    assert first["global_remaining_risk"] == 0.0
    assert first["reconciliation_residual"] == 0.0
