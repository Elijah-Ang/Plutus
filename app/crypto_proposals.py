"""Immutable, non-executable proposal previews for supervised spot crypto.

This is the proposal-authority half of the crypto strategy roadmap stage.  It
binds an exact research setup to verified broker/capability/market/risk/sizing
evidence and renders the complete future approval surface.  The current stage
intentionally persists ``manual_approval_eligible=0`` and
``execution_authorized=0`` and never writes ``trade_proposals`` or calls
Telegram or Alpaca.  A later separately reviewed execution stage must replace
that boundary; regenerating this local preview is never order authority.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Mapping

from .approval_authority import canonical_json
from .crypto_risk import CryptoRiskStore
from .crypto_sizing import load_verified_crypto_sizing
from .crypto_strategies import CryptoStrategyStore
from .formula_versions import (
    CRYPTO_PROPOSAL_FORMULA_VERSION,
    CRYPTO_PROPOSAL_SCHEMA_VERSION,
)
from .utils import iso_now, json_dumps


ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


class CryptoProposalError(ValueError):
    """Raised when a crypto proposal preview cannot be bound exactly."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _valid_hash(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _decimal(value: Any, label: str, *, minimum: Decimal = ZERO) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise CryptoProposalError(f"{label} must be an exact decimal value")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoProposalError(f"{label} is invalid") from exc
    if not number.is_finite() or number < minimum:
        raise CryptoProposalError(f"{label} must be finite and nonnegative")
    return number


def _trusted_decimal(value: Any, label: str, *, minimum: Decimal = ZERO) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise CryptoProposalError(f"{label} is missing or invalid")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise CryptoProposalError(f"{label} is missing or invalid") from exc
    if not number.is_finite() or number < minimum:
        raise CryptoProposalError(f"{label} must be finite and nonnegative")
    return number


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value.normalize(), "f")


def _utc(value: Any, label: str) -> datetime:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CryptoProposalError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise CryptoProposalError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _policy(config: Mapping[str, Any]) -> dict[str, Any]:
    cfg = config.get("crypto") or {}
    policy = cfg.get("proposal_policy") or {}
    failures: list[str] = []
    if policy.get("mode") != "research_only_preview":
        failures.append("proposal_policy_mode_not_research_only_preview")
    if policy.get("formula_version") != CRYPTO_PROPOSAL_FORMULA_VERSION:
        failures.append("proposal_formula_identity_mismatch")
    if policy.get("schema_version") != CRYPTO_PROPOSAL_SCHEMA_VERSION:
        failures.append("proposal_schema_identity_mismatch")
    if str((config.get("formula_versions") or {}).get("crypto_proposal") or "") != CRYPTO_PROPOSAL_FORMULA_VERSION:
        failures.append("configuration_proposal_formula_mismatch")
    if policy.get("create_trade_proposals") is not False:
        failures.append("ordinary_trade_proposal_creation_not_disabled")
    if policy.get("send_telegram") is not False:
        failures.append("telegram_send_not_disabled")
    if policy.get("manual_approval_enabled") is not False:
        failures.append("manual_approval_not_disabled_in_preview_stage")
    if policy.get("execution_enabled") is not False:
        failures.append("execution_not_disabled_in_preview_stage")
    if cfg.get("mode") != "research_only" or cfg.get("paper_trading_enabled") is not False or cfg.get("proposals_enabled") is not False or cfg.get("live_enabled") is not False:
        failures.append("global_crypto_lane_not_research_only_disabled")
    try:
        expiry_minutes = int(policy.get("preview_expiry_minutes"))
    except (TypeError, ValueError):
        expiry_minutes = 0
    if not 1 <= expiry_minutes <= 5:
        failures.append("preview_expiry_outside_stage_bound")
    if failures:
        raise CryptoProposalError("invalid crypto proposal policy: " + ", ".join(sorted(set(failures))))
    return {**policy, "preview_expiry_minutes": expiry_minutes}


@dataclass(frozen=True)
class CryptoProposalPreview:
    id: str
    run_id: str
    strategy_decision_id: str
    strategy_decision_fingerprint: str
    risk_decision_id: str
    risk_snapshot_id: str
    sizing_decision_id: str
    symbol: str
    strategy: str
    action: str
    request_basis: str
    status: str
    manual_approval_eligible: bool
    execution_authorized: bool
    created_at: str
    expires_at: str
    display_fingerprint: str
    proposal_fingerprint: str
    display: Mapping[str, Any]
    payload: Mapping[str, Any]


def apply_crypto_proposal_schema(conn: Any, *, record_migration: bool = True) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_proposal_previews(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL,
          strategy_decision_id TEXT NOT NULL,strategy_decision_fingerprint TEXT NOT NULL,
          risk_decision_id TEXT NOT NULL,risk_decision_fingerprint TEXT NOT NULL,
          risk_snapshot_id TEXT NOT NULL,risk_snapshot_fingerprint TEXT NOT NULL,
          sizing_decision_id TEXT NOT NULL,sizing_decision_fingerprint TEXT NOT NULL,
          capability_snapshot_id TEXT NOT NULL,capability_snapshot_fingerprint TEXT NOT NULL,
          market_evidence_id TEXT NOT NULL,market_evidence_fingerprint TEXT NOT NULL,
          symbol TEXT NOT NULL,strategy TEXT NOT NULL,action TEXT NOT NULL,request_basis TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status='research_only_preview'),
          manual_approval_eligible INTEGER NOT NULL CHECK(manual_approval_eligible=0),
          execution_authorized INTEGER NOT NULL CHECK(execution_authorized=0),
          created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
          display_json TEXT NOT NULL,display_fingerprint TEXT NOT NULL UNIQUE,
          proposal_json TEXT NOT NULL,proposal_fingerprint TEXT NOT NULL UNIQUE,
          config_hash TEXT NOT NULL,formula_version TEXT NOT NULL,schema_version TEXT NOT NULL,
          FOREIGN KEY(strategy_decision_id) REFERENCES crypto_strategy_decisions(id),
          FOREIGN KEY(risk_decision_id) REFERENCES crypto_risk_decisions(id),
          FOREIGN KEY(risk_snapshot_id) REFERENCES crypto_risk_snapshots(id),
          FOREIGN KEY(sizing_decision_id) REFERENCES crypto_sizing_decisions(id),
          FOREIGN KEY(capability_snapshot_id) REFERENCES crypto_capability_snapshots(id),
          FOREIGN KEY(market_evidence_id) REFERENCES crypto_market_data_evidence(id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_crypto_preview_symbol_time ON crypto_proposal_previews(symbol,created_at)")
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(crypto_paper_watch_candidates)").fetchall()}
    if "proposal_preview_id" not in existing:
        conn.execute("ALTER TABLE crypto_paper_watch_candidates ADD COLUMN proposal_preview_id TEXT")
    if record_migration:
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,applied_at,detail) VALUES(?,?,?)",
            (CRYPTO_PROPOSAL_SCHEMA_VERSION, iso_now(), "immutable non-executable crypto proposal previews"),
        )


def _build_payload(
    *,
    preview_id: str,
    strategy: Any,
    risk_decision: Mapping[str, Any],
    risk_snapshot: Mapping[str, Any],
    sizing: Any,
    config: Mapping[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    policy = _policy(config)
    config_hash = str(config.get("effective_config_hash") or "").strip().lower()
    if not _valid_hash(config_hash):
        raise CryptoProposalError("current configuration hash is missing or invalid")
    if not strategy.signal_eligible or strategy.selected_strategy is None or strategy.action != "entry":
        raise CryptoProposalError("crypto strategy decision is not eligible for an entry preview")
    if strategy.proposal_authorized or strategy.execution_authorized:
        raise CryptoProposalError("crypto strategy unexpectedly carries proposal or execution authority")
    if risk_decision.get("risk_eligible") is not True or risk_decision.get("execution_authorized") is not False:
        raise CryptoProposalError("crypto risk decision is not eligible research authority")
    if not sizing.eligible or not sizing.authoritative or sizing.execution_authorized:
        raise CryptoProposalError("crypto sizing is not authoritative research sizing")
    request = sizing.payload.get("request") or {}
    if request.get("source_type") != "crypto_strategy_decision" or request.get("source_id") != strategy.id or request.get("source_fingerprint") != strategy.decision_fingerprint:
        raise CryptoProposalError("crypto sizing does not bind the exact strategy decision")
    if sizing.symbol != strategy.symbol or sizing.action != strategy.action or sizing.side != "buy":
        raise CryptoProposalError("crypto strategy and sizing candidate identities differ")
    if sizing.stop_price != strategy.stop_price:
        raise CryptoProposalError("crypto sizing stop differs from the strategy decision")
    if risk_decision.get("sizing_decision_id") != sizing.id or risk_decision.get("snapshot_id") != sizing.risk_snapshot_id:
        raise CryptoProposalError("crypto risk decision and sizing relationship differs")
    if any(
        str(value or "") != strategy.run_id
        for value in (risk_decision.get("run_id"), risk_snapshot.get("run_id"), sizing.run_id)
    ):
        raise CryptoProposalError("crypto proposal run identities differ")
    if any(value != config_hash for value in (strategy.config_hash, sizing.config_hash, risk_snapshot.get("config_hash"), risk_decision.get("config_hash"))):
        raise CryptoProposalError("crypto proposal configuration identity differs")
    limit_price = _decimal(sizing.limit_price, "canonical limit price")
    stop_price = _decimal(sizing.stop_price, "canonical stop price")
    strategy_target_price = _decimal(strategy.target_price, "strategy target price")
    target_reward_r = _decimal(strategy.expected_reward_r, "strategy expected reward R")
    quantity = _decimal(sizing.canonical_quantity, "canonical quantity")
    notional = _decimal(sizing.canonical_notional, "canonical notional")
    maximum_loss = _decimal(sizing.canonical_stop_risk, "canonical maximum loss")
    estimated_fees = _decimal(sizing.estimated_fees, "estimated fees")
    estimated_slippage = _decimal(sizing.estimated_stop_slippage, "estimated stop slippage")
    stop_execution_price = _decimal(sizing.stop_execution_price, "conservative stop execution price")
    if not (stop_price < limit_price) or quantity <= ZERO or notional <= ZERO or maximum_loss <= ZERO:
        raise CryptoProposalError("crypto proposal economics are not a valid bounded long entry")
    price_increment = _decimal(sizing.price_increment, "current price increment", minimum=Decimal("0.000000001"))
    fee_bps = _trusted_decimal(
        ((config.get("crypto") or {}).get("sizing_policy") or {}).get("conservative_taker_fee_bps_per_side"),
        "conservative crypto fee bps per side",
    )
    fee_rate = fee_bps / BPS
    if fee_rate >= ONE:
        raise CryptoProposalError("conservative crypto fee rate is outside its finite policy range")
    independently_recomputed_stop_fees = quantity * (limit_price + stop_execution_price) * fee_rate
    if independently_recomputed_stop_fees != estimated_fees:
        raise CryptoProposalError("crypto sizing fee evidence does not independently reconcile")
    # Solve for a target whose proceeds after both entry and target-exit fees
    # retain at least the configured R multiple of the cost-inclusive maximum
    # stop loss.  A gross-price target would otherwise overstate net reward.
    cost_adjusted_target = (
        limit_price * (ONE + fee_rate) + maximum_loss / quantity * target_reward_r
    ) / (ONE - fee_rate)
    raw_target = max(strategy_target_price, cost_adjusted_target)
    target_price = (raw_target / price_increment).to_integral_value(rounding=ROUND_CEILING) * price_increment
    entry_fee = quantity * limit_price * fee_rate
    target_exit_fee = quantity * target_price * fee_rate
    expected_execution_cost = entry_fee + target_exit_fee
    gross_reward = quantity * (target_price - limit_price)
    net_reward = gross_reward - expected_execution_cost
    net_reward_r = net_reward / maximum_loss
    gross_reward_r = gross_reward / maximum_loss
    if target_price < strategy_target_price or net_reward <= ZERO or net_reward_r < target_reward_r:
        raise CryptoProposalError("canonical target does not preserve the minimum net reward authority")
    # Risk snapshots bind the market by ID/fingerprint; quote values live in
    # the separately verified market-evidence row and are added by the caller.
    aggregate = risk_snapshot.get("aggregate") or {}
    account = risk_snapshot.get("account") or {}
    current_crypto = _decimal(aggregate.get("crypto_position_gross"), "current crypto exposure")
    current_total = _decimal(aggregate.get("all_position_gross"), "current total exposure")
    equity = _decimal(account.get("equity"), "paper account equity")
    volatility = _decimal((risk_snapshot.get("volatility_evidence") or {}).get("annualized_volatility"), "annualized volatility")
    expires = min(
        created_at + timedelta(minutes=policy["preview_expiry_minutes"]),
        _utc(risk_snapshot.get("expires_at"), "risk snapshot expiry"),
    )
    if expires <= created_at:
        raise CryptoProposalError("crypto risk authority expired before preview creation")
    display = {
        "header": "CRYPTO PAPER RESEARCH PREVIEW — NOT APPROVABLE",
        "symbol": strategy.symbol,
        "strategy": strategy.selected_strategy,
        "strategy_lifecycle": strategy.lifecycle,
        "action": "BUY ENTRY",
        "request_basis": sizing.request_basis,
        "quantity": _text(quantity),
        "notional_usd": _text(notional),
        "current_bid": None,
        "current_ask": None,
        "spread_bps": None,
        "annualized_volatility": _text(volatility),
        "limit_price": _text(limit_price),
        "stop_price": _text(stop_price),
        "strategy_signal_target_price": _text(strategy_target_price),
        "target_price": _text(target_price),
        "expected_gross_reward_usd": _text(gross_reward),
        "expected_reward_usd": _text(net_reward),
        "expected_net_reward_after_estimated_cost_usd": _text(net_reward),
        "gross_reward_r": _text(gross_reward_r),
        "expected_reward_r": _text(net_reward_r),
        "minimum_target_reward_r": _text(target_reward_r),
        "maximum_loss_usd": _text(maximum_loss),
        "expected_entry_fee_usd": _text(entry_fee),
        "expected_target_exit_fee_usd": _text(target_exit_fee),
        "expected_execution_cost_usd": _text(expected_execution_cost),
        "maximum_loss_fee_component_usd": _text(estimated_fees),
        "adverse_stop_slippage_usd": _text(estimated_slippage),
        "current_crypto_exposure_usd": _text(current_crypto),
        "projected_crypto_exposure_usd": _text(current_crypto + notional),
        "current_total_portfolio_exposure_usd": _text(current_total),
        "projected_total_portfolio_exposure_usd": _text(current_total + notional),
        "paper_account_equity_usd": _text(equity),
        "expires_at": expires.isoformat(),
        "would_be_approval_command": f"YES CRYPTO {preview_id}",
        "approval_command_enabled": False,
        "approval_instruction": "Research preview only. This command is not accepted and cannot create an order.",
        "paper_only_warning": "PAPER ONLY • MANUAL APPROVAL REQUIRED IN A LATER ENABLED STAGE • NO ORDER AUTHORITY",
    }
    return {
        "id": preview_id,
        "run_id": strategy.run_id,
        "strategy_decision_id": strategy.id,
        "strategy_decision_fingerprint": strategy.decision_fingerprint,
        "risk_decision_id": risk_decision["id"],
        "risk_decision_fingerprint": _hash(risk_decision),
        "risk_snapshot_id": risk_snapshot["id"],
        "risk_snapshot_fingerprint": _hash(risk_snapshot),
        "sizing_decision_id": sizing.id,
        "sizing_decision_fingerprint": sizing.decision_fingerprint,
        "capability_snapshot_id": sizing.capability_snapshot_id,
        "capability_snapshot_fingerprint": sizing.capability_snapshot_fingerprint,
        "market_evidence_id": sizing.market_evidence_id,
        "market_evidence_fingerprint": sizing.market_evidence_fingerprint,
        "symbol": strategy.symbol,
        "strategy": strategy.selected_strategy,
        "action": strategy.action,
        "request_basis": sizing.request_basis,
        "status": "research_only_preview",
        "manual_approval_eligible": False,
        "execution_authorized": False,
        "display": display,
        "display_fingerprint": _hash(display),
        "config_hash": config_hash,
        "formula_version": CRYPTO_PROPOSAL_FORMULA_VERSION,
        "schema_version": CRYPTO_PROPOSAL_SCHEMA_VERSION,
        "created_at": created_at.isoformat(),
        "expires_at": expires.isoformat(),
    }


def format_crypto_proposal_preview(preview: CryptoProposalPreview) -> str:
    display = preview.display
    return (
        "🧪 CRYPTO PAPER RESEARCH PREVIEW — NOT APPROVABLE\n\n"
        f"{display['action']} {display['symbol']}\n"
        f"Strategy: {display['strategy']} ({display['strategy_lifecycle']})\n"
        f"Basis: {display['request_basis']} | qty {display['quantity']} | ${display['notional_usd']}\n"
        f"Alpaca US bid ${display['current_bid']} | ask ${display['current_ask']} | spread {display['spread_bps']} bps\n"
        f"Annualized volatility: {display['annualized_volatility']}\n"
        f"Limit ${display['limit_price']} | stop ${display['stop_price']} | target ${display['target_price']}\n"
        f"Expected net reward ${display['expected_reward_usd']} ({display['expected_reward_r']}R; gross ${display['expected_gross_reward_usd']} / {display['gross_reward_r']}R)\n"
        f"Maximum loss ${display['maximum_loss_usd']} | expected execution cost ${display['expected_execution_cost_usd']} (target round trip) | adverse-stop slippage ${display['adverse_stop_slippage_usd']}\n"
        f"Crypto exposure ${display['current_crypto_exposure_usd']} → ${display['projected_crypto_exposure_usd']}\n"
        f"Total exposure ${display['current_total_portfolio_exposure_usd']} → ${display['projected_total_portfolio_exposure_usd']}\n"
        f"Expires: {display['expires_at']}\n"
        f"Would-be command: {display['would_be_approval_command']} (DISABLED)\n\n"
        f"{display['paper_only_warning']}"
    )


class CryptoProposalStore:
    def __init__(self, storage: Any) -> None:
        self.storage = storage

    def create_preview(
        self,
        config: Mapping[str, Any],
        strategy_decision_id: str,
        risk_decision_id: str,
        *,
        now: datetime | None = None,
    ) -> CryptoProposalPreview:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            apply_crypto_proposal_schema(conn, record_migration=False)
            # Acquire the SQLite writer reservation before loading authority so
            # every independently verified relationship belongs to one coherent
            # durable state.  Other writers cannot replace evidence between
            # verification, display construction, and insertion.
            strategy = CryptoStrategyStore(self.storage).load_verified(strategy_decision_id, config)
            risk_store = CryptoRiskStore(self.storage)
            risk_decision = risk_store.load_verified_decision(risk_decision_id, config, now=current)
            risk_snapshot = risk_store.load_verified(risk_decision["snapshot_id"], config, now=current)
            sizing = load_verified_crypto_sizing(self.storage, risk_decision["sizing_decision_id"], config)
            payload = _build_payload(
                preview_id=str(uuid.uuid4()), strategy=strategy, risk_decision=risk_decision,
                risk_snapshot=risk_snapshot, sizing=sizing, config=config, created_at=current,
            )
            market_row = conn.execute(
                "SELECT bid_price,ask_price,spread_bps,evidence_fingerprint FROM crypto_market_data_evidence WHERE id=?",
                (sizing.market_evidence_id,),
            ).fetchone()
            if market_row is None or market_row["evidence_fingerprint"] != sizing.market_evidence_fingerprint:
                raise CryptoProposalError("crypto proposal market evidence is missing or changed")
            display = dict(payload["display"])
            display["current_bid"] = market_row["bid_price"]
            display["current_ask"] = market_row["ask_price"]
            display["spread_bps"] = market_row["spread_bps"]
            payload["display"] = display
            payload["display_fingerprint"] = _hash(display)
            proposal_fingerprint = _hash(payload)
            conn.execute(
                """INSERT INTO crypto_proposal_previews(
                  id,run_id,strategy_decision_id,strategy_decision_fingerprint,
                  risk_decision_id,risk_decision_fingerprint,risk_snapshot_id,risk_snapshot_fingerprint,
                  sizing_decision_id,sizing_decision_fingerprint,capability_snapshot_id,
                  capability_snapshot_fingerprint,market_evidence_id,market_evidence_fingerprint,
                  symbol,strategy,action,request_basis,status,manual_approval_eligible,
                  execution_authorized,created_at,expires_at,display_json,display_fingerprint,
                  proposal_json,proposal_fingerprint,config_hash,formula_version,schema_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    payload["id"], payload["run_id"], payload["strategy_decision_id"], payload["strategy_decision_fingerprint"],
                    payload["risk_decision_id"], payload["risk_decision_fingerprint"], payload["risk_snapshot_id"], payload["risk_snapshot_fingerprint"],
                    payload["sizing_decision_id"], payload["sizing_decision_fingerprint"], payload["capability_snapshot_id"], payload["capability_snapshot_fingerprint"],
                    payload["market_evidence_id"], payload["market_evidence_fingerprint"], payload["symbol"], payload["strategy"], payload["action"],
                    payload["request_basis"], payload["status"], 0, 0, payload["created_at"], payload["expires_at"], json_dumps(display),
                    payload["display_fingerprint"], json_dumps(payload), proposal_fingerprint, payload["config_hash"], payload["formula_version"], payload["schema_version"],
                ),
            )
        return self.load_verified(payload["id"], config, now=current)

    def load_verified(
        self,
        preview_id: str,
        config: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> CryptoProposalPreview:
        rows = self.storage.fetch_all("SELECT * FROM crypto_proposal_previews WHERE id=?", (preview_id,))
        if len(rows) != 1:
            raise CryptoProposalError("crypto proposal preview is missing or duplicated")
        row = rows[0]
        try:
            payload = json.loads(row["proposal_json"])
            display = json.loads(row["display_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CryptoProposalError("crypto proposal preview JSON is invalid") from exc
        if not isinstance(payload, dict) or not isinstance(display, dict):
            raise CryptoProposalError("crypto proposal preview shape is invalid")
        if _hash(payload) != row["proposal_fingerprint"] or _hash(display) != row["display_fingerprint"]:
            raise CryptoProposalError("crypto proposal preview fingerprint mismatch")
        if payload.get("display") != display or payload.get("display_fingerprint") != row["display_fingerprint"]:
            raise CryptoProposalError("crypto proposal display binding mismatch")
        scalar = (
            "id", "run_id", "strategy_decision_id", "strategy_decision_fingerprint",
            "risk_decision_id", "risk_decision_fingerprint", "risk_snapshot_id", "risk_snapshot_fingerprint",
            "sizing_decision_id", "sizing_decision_fingerprint", "capability_snapshot_id",
            "capability_snapshot_fingerprint", "market_evidence_id", "market_evidence_fingerprint",
            "symbol", "strategy", "action", "request_basis", "status", "created_at", "expires_at",
            "config_hash", "formula_version", "schema_version",
        )
        for key in scalar:
            if row[key] != payload.get(key):
                raise CryptoProposalError(f"crypto proposal persisted column mismatch: {key}")
        if bool(row["manual_approval_eligible"]) or bool(row["execution_authorized"]) or payload.get("manual_approval_eligible") is not False or payload.get("execution_authorized") is not False:
            raise CryptoProposalError("crypto proposal preview escaped research-only authority")
        if row["status"] != "research_only_preview":
            raise CryptoProposalError("crypto proposal preview status is invalid")
        if row["config_hash"] != str(config.get("effective_config_hash") or ""):
            raise CryptoProposalError("crypto proposal preview configuration identity changed")
        _policy(config)
        if row["formula_version"] != CRYPTO_PROPOSAL_FORMULA_VERSION or row["schema_version"] != CRYPTO_PROPOSAL_SCHEMA_VERSION:
            raise CryptoProposalError("crypto proposal preview version is obsolete")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        if current > _utc(row["expires_at"], "proposal preview expiry"):
            raise CryptoProposalError("crypto proposal preview expired")
        strategy = CryptoStrategyStore(self.storage).load_verified(row["strategy_decision_id"], config)
        risk_store = CryptoRiskStore(self.storage)
        risk_decision = risk_store.load_verified_decision(row["risk_decision_id"], config, now=current)
        risk_snapshot = risk_store.load_verified(row["risk_snapshot_id"], config, now=current)
        sizing = load_verified_crypto_sizing(self.storage, row["sizing_decision_id"], config)
        recomputed = _build_payload(
            preview_id=row["id"], strategy=strategy, risk_decision=risk_decision,
            risk_snapshot=risk_snapshot, sizing=sizing, config=config,
            created_at=_utc(row["created_at"], "proposal preview creation"),
        )
        recomputed_display = dict(recomputed["display"])
        market_rows = self.storage.fetch_all(
            "SELECT bid_price,ask_price,spread_bps,evidence_fingerprint FROM crypto_market_data_evidence WHERE id=?",
            (row["market_evidence_id"],),
        )
        if len(market_rows) != 1 or market_rows[0]["evidence_fingerprint"] != row["market_evidence_fingerprint"]:
            raise CryptoProposalError("crypto proposal market evidence relationship mismatch")
        recomputed_display.update({
            "current_bid": market_rows[0]["bid_price"],
            "current_ask": market_rows[0]["ask_price"],
            "spread_bps": market_rows[0]["spread_bps"],
        })
        recomputed["display"] = recomputed_display
        recomputed["display_fingerprint"] = _hash(recomputed_display)
        if recomputed != payload or _hash(recomputed) != row["proposal_fingerprint"]:
            raise CryptoProposalError("crypto proposal independent recomputation mismatch")
        return CryptoProposalPreview(
            id=row["id"], run_id=row["run_id"], strategy_decision_id=row["strategy_decision_id"],
            strategy_decision_fingerprint=row["strategy_decision_fingerprint"],
            risk_decision_id=row["risk_decision_id"], risk_snapshot_id=row["risk_snapshot_id"],
            sizing_decision_id=row["sizing_decision_id"], symbol=row["symbol"], strategy=row["strategy"],
            action=row["action"], request_basis=row["request_basis"], status=row["status"],
            manual_approval_eligible=False, execution_authorized=False, created_at=row["created_at"],
            expires_at=row["expires_at"], display_fingerprint=row["display_fingerprint"],
            proposal_fingerprint=row["proposal_fingerprint"], display=display, payload=payload,
        )


__all__ = [
    "CryptoProposalError",
    "CryptoProposalPreview",
    "CryptoProposalStore",
    "apply_crypto_proposal_schema",
    "format_crypto_proposal_preview",
]
