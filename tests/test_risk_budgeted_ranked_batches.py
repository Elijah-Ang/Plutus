from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from app.service import TradingService
from app.storage import Storage
from app.risk_engine import RiskEngine


class BatchTelegram:
    def __init__(self, allowed_user_id: str = "7777") -> None:
        self.allowed_user_id = allowed_user_id
        self.chat_id = "123"
        self.messages: list[str] = []
        self.updates: list[dict] = []
        self.is_mock = True

    def send_message(self, text, chat_id=None):
        self.messages.append(text)
        return {"message_id": 4242}

    def is_authorized(self, sender_id):
        return str(sender_id) == str(self.allowed_user_id)

    def is_available(self, force=False):
        return True

    def get_updates(self, timeout=0, offset=None):
        updates = list(self.updates)
        self.updates.clear()
        return updates

    def handle_command(self, text, sender):
        return "ok"


class BatchBroker:
    def __init__(self) -> None:
        self.submitted: list[dict] = []

    def get_account(self):
        return {
            "equity": 10000.0,
            "cash": 10000.0,
            "buying_power": 10000.0,
            "last_equity": 10000.0,
            "long_market_value": 0.0,
            "short_market_value": 0.0,
        }

    def get_positions(self):
        return []

    def get_open_orders(self):
        return []

    def get_loss_metrics(self):
        return {"daily_loss": 0.0, "weekly_loss": 0.0}

    def is_market_open(self):
        return True

    def get_latest_price(self, symbol):
        return {"price": 100.0, "timestamp": datetime.now(UTC)}

    def get_historical_bars(self, symbol, timeframe, limit=250):
        import pandas as pd

        return pd.DataFrame({
            "open": [100.0] * limit,
            "high": [101.0] * limit,
            "low": [99.0] * limit,
            "close": [100.0] * limit,
            "volume": [10000.0] * limit,
            "volatility_20": [0.15] * limit,
        })

    def submit_order(self, symbol, side, order_args, order_type, limit_price, client_order_id):
        self.submitted.append({
            "symbol": symbol,
            "side": side,
            "order_args": order_args,
            "client_order_id": client_order_id,
        })
        return type("Order", (), {"id": "broker-order", "status": "submitted"})()


def config() -> dict:
    return {
        "mode": "paper",
        "live_enabled": False,
        "portfolio_execution_mode": "risk_budgeted",
        "proposal_mode": {
            "type": "ranked_batch",
            "allow_yes_all_for_paper": True,
            "yes_all_requires_each_trade_final_revalidation": True,
        },
        "risk_budget": {
            "risk_per_trade_pct": 0.05,
            "max_open_risk_pct": 0.30,
            "max_daily_realized_loss_pct": 0.25,
            "max_total_portfolio_exposure_pct": 6.0,
            "max_single_symbol_exposure_pct": 2.5,
            "max_cluster_exposure_pct": 5.0,
        },
        "position_sizing": {"enabled": True, "min_paper_notional": 5.0, "risk_per_trade_pct": 0.05},
        "portfolio_behavior": {
            "max_total_portfolio_exposure_pct": 6.0,
            "max_single_symbol_exposure_pct": 2.5,
            "max_correlated_us_equity_exposure_pct": 5.0,
        },
        "portfolio_optimizer": {"clusters": {"broad": ["SPY", "IWM"]}, "max_same_cluster_exposure_pct": 5.0},
        "telegram": {"approval_price_refresh_required": False},
        "risk": {
            "max_trade_notional_paper": 50,
            "max_trades_per_day": 99,
            "max_open_positions": 99,
            "allow_margin": False,
            "allow_shorting": False,
            "allowed_order_types": ["limit"],
            "max_price_age_seconds": 120,
            "min_historical_bars": 50,
            "max_price_gap_pct": 15,
            "stop_if_daily_loss_pct_exceeds": 5,
            "stop_if_weekly_loss_pct_exceeds": 10,
            "stop_if_daily_loss_dollars_exceeds": None,
            "stop_if_weekly_loss_dollars_exceeds": None,
            "require_final_revalidation": True,
        },
        "approved_strategy_versions": ["rule_based_v1"],
    }


def make_service(tmp_path):
    storage = Storage(tmp_path / "batch.db")
    storage.initialize()
    service = TradingService(config(), storage, BatchBroker(), "run")
    service.telegram = BatchTelegram()
    return service, storage


def test_ranked_batch_tables_created(tmp_path):
    storage = Storage(tmp_path / "schema.db")
    storage.initialize()
    tables = {
        row["name"]
        for row in storage.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "proposal_batches" in tables
    assert "proposal_batch_candidates" in tables
    assert "candidate_risk_budget_decisions" in tables
    assert "ranked_opportunity_sets" in tables


def test_risk_budget_allows_multiple_candidates_without_fixed_count_cap(tmp_path):
    service, storage = make_service(tmp_path)
    now = datetime.now(UTC)
    candidates = service._rank_candidates([
        {"symbol": "SPY", "score": 88, "final_notional": 20.0, "price": 100.0, "stop_distance_pct": 5.0},
        {"symbol": "IWM", "score": 84, "final_notional": 20.0, "price": 100.0, "stop_distance_pct": 5.0},
    ], {
        "portfolio_equity": 10000.0,
        "total_exposure_pct": 0.0,
        "single_exposures": {},
        "cluster_exposures": {},
        "cluster_counts": {},
    })

    allowed, reasons = service._apply_risk_budget_to_ranked_candidates(
        candidates,
        {"portfolio_equity": 10000.0, "total_exposure_pct": 0.0, "single_exposures": {}, "cluster_exposures": {}, "cluster_counts": {}},
        service.broker.get_account(),
        now,
    )

    assert allowed == {"SPY", "IWM"}
    assert "passes ranked risk budget" in reasons["SPY"]
    assert len(storage.fetch_all("SELECT * FROM ranked_opportunity_sets")) == 2


def test_lower_ranked_candidate_blocked_when_open_risk_budget_exhausted(tmp_path):
    service, storage = make_service(tmp_path)
    service.config["risk_budget"]["max_open_risk_pct"] = 0.08
    now = datetime.now(UTC)
    candidates = service._rank_candidates([
        {"symbol": "SPY", "score": 90, "final_notional": 30.0, "price": 100.0, "stop_distance_pct": 8.0},
        {"symbol": "IWM", "score": 80, "final_notional": 30.0, "price": 100.0, "stop_distance_pct": 8.0},
    ], {"portfolio_equity": 1000.0, "total_exposure_pct": 0.0, "single_exposures": {}, "cluster_exposures": {}, "cluster_counts": {}})

    allowed, reasons = service._apply_risk_budget_to_ranked_candidates(
        candidates,
        {"portfolio_equity": 1000.0, "total_exposure_pct": 0.0, "single_exposures": {}, "cluster_exposures": {}, "cluster_counts": {}},
        service.broker.get_account(),
        now,
    )

    assert "SPY" in allowed
    assert "IWM" not in allowed
    assert "insufficient risk budget" in reasons["IWM"]
    blocked = storage.fetch_all("SELECT * FROM candidate_risk_budget_decisions WHERE passed=0")
    assert blocked


def test_preproposal_risk_block_is_recorded_as_non_actionable(tmp_path):
    service, storage = make_service(tmp_path)
    candidate = {
        "symbol": "SPY",
        "score": 90,
        "final_notional": 20.0,
        "price": 100.0,
        "stop_distance_pct": 5.0,
        "preproposal_block_reason": "new buy blocked because an exit is pending",
    }

    allowed, reasons = service._apply_risk_budget_to_ranked_candidates(
        service._rank_candidates([candidate], {"portfolio_equity": 10000.0, "total_exposure_pct": 0.0, "single_exposures": {}, "cluster_exposures": {}, "cluster_counts": {}}),
        {"portfolio_equity": 10000.0, "total_exposure_pct": 0.0, "single_exposures": {}, "cluster_exposures": {}, "cluster_counts": {}},
        service.broker.get_account(),
        datetime.now(UTC),
    )

    assert allowed == set()
    assert "pre-proposal risk check failed" in reasons["SPY"]
    rows = storage.fetch_all("SELECT actionable, reason FROM ranked_opportunity_sets")
    assert rows == [{"actionable": 0, "reason": reasons["SPY"]}]


def test_ranked_batch_message_contains_symbol_specific_and_yes_all_instructions(tmp_path):
    service, _ = make_service(tmp_path)
    message = service._format_ranked_batch_message(
        [
            {"id": "p1", "symbol": "SPY", "side": "buy", "action": "entry", "notional": 18.4, "qty": 0.03, "score": 88, "selection_reason": "strongest active setup", "created_at": datetime.now(UTC).isoformat(), "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()},
            {"id": "p2", "symbol": "IWM", "side": "buy", "action": "entry", "notional": 12.6, "qty": 0.10, "score": 84, "selection_reason": "passes risk budget", "created_at": datetime.now(UTC).isoformat(), "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()},
        ],
        [{"symbol": "XLV", "risk_budget_block_reason": "observation-only"}],
        {"total_exposure_pct": 4.8, "open_risk_pct": 0.22, "buying_power": 1000.0},
    )
    assert "Paper position and trade opportunity set" in message
    assert "Created:" in message
    assert "Expires:" in message
    assert "No reply before expiry = no order." in message
    assert "yes SPY = approve SPY only" in message
    assert "yes all = approve all actionable candidates after final checks" in message
    assert "Plain yes is ambiguous" in message


def test_ranked_batch_message_formats_tiny_positive_exposure_without_zeroing(tmp_path):
    service, _ = make_service(tmp_path)
    now = datetime.now(UTC)
    message = service._format_ranked_batch_message(
        [
            {"id": "p1", "symbol": "SPY", "side": "buy", "action": "entry", "notional": 18.4, "qty": 0.03, "score": 88, "created_at": now.isoformat(), "expires_at": (now + timedelta(minutes=10)).isoformat()},
        ],
        [],
        {"total_exposure_pct": 0.001, "open_risk_pct": 0.005, "buying_power": 1000.0},
    )
    assert "Total exposure after proposed trades: <0.01% / 6.0%" in message
    assert "Open risk after proposed trades: <0.01% / 0.30%" in message


def test_ranked_batch_message_uses_rank_specific_reason_wording(tmp_path):
    service, _ = make_service(tmp_path)
    now = datetime.now(UTC)
    message = service._format_ranked_batch_message(
        [
            {
                "id": "p1",
                "symbol": "SPY",
                "side": "buy",
                "action": "entry",
                "notional": 20.0,
                "qty": 0.02,
                "score": 95,
                "selection_reason": "Selected as the strongest eligible candidate.",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
            },
            {
                "id": "p2",
                "symbol": "IWM",
                "side": "buy",
                "action": "entry",
                "notional": 15.0,
                "qty": 0.05,
                "score": 85,
                "selection_reason": "Selected as the strongest eligible candidate.",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=10)).isoformat(),
            },
        ],
        [],
        {"total_exposure_pct": 0.5, "open_risk_pct": 0.05, "buying_power": 1000.0},
    )
    assert "Reason: Selected as the strongest eligible candidate." in message
    assert "Reason: Included as ranked eligible candidate #2 after risk-budget checks." in message


def insert_batch(storage: Storage, proposals: list[tuple[str, str] | tuple[str, str, str, str]]) -> str:
    now = datetime.now(UTC)
    expiry = (now + timedelta(minutes=10)).isoformat()
    batch_id = "batch-1"
    storage.execute(
        "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
        (batch_id, "run", "4242", "pending", now.isoformat(), expiry, "{}"),
    )
    for idx, item in enumerate(proposals, start=1):
        if len(item) == 2:
            proposal_id, symbol = item
            side = "buy"
            action = "entry"
            candidate_action = "BUY"
        else:
            proposal_id, symbol, side, action = item
            candidate_action = "ADD" if action == "add" else ("EXIT" if action == "exit" else side.upper())
        payload = {
            "id": proposal_id,
            "symbol": symbol,
            "side": side,
            "action": action,
            "notional": 5.0,
            "qty": 0.05,
            "latest_price": 100.0,
            "price_at": now.isoformat(),
            "historical_bars": 250,
            "volume": 10000,
            "price_gap_pct": 0,
            "created_at": now.isoformat(),
            "expires_at": expiry,
            "strategy_version": "rule_based_v1",
            "reason": "ranked batch test",
            "order_type": "market",
            "asset_class": "equity",
        }
        storage.execute(
            "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,telegram_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, "run", f"sig-{idx}", symbol, side, 5.0, "pending", now.isoformat(), expiry, "rule_based_v1", json.dumps(payload), "4242"),
        )
        storage.execute(
            "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,rank,reason,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"cand-{idx}", batch_id, proposal_id, "4242", symbol, side, candidate_action, "pending", idx, "ranked", now.isoformat(), expiry, json.dumps(payload)),
        )
    return batch_id


def test_no_all_rejects_all_batch_candidates(tmp_path):
    service, storage = make_service(tmp_path)
    insert_batch(storage, [("p1", "SPY"), ("p2", "IWM")])

    handled = service._handle_batch_approval_command("no all", "7777", "no", "all", "4242")

    assert handled is True
    assert {r["status"] for r in storage.fetch_all("SELECT status FROM trade_proposals")} == {"rejected"}
    assert {r["candidate_status"] for r in storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates")} == {"rejected"}


def test_yes_all_blocked_outside_paper_mode(tmp_path):
    service, storage = make_service(tmp_path)
    service.config["mode"] = "live"
    insert_batch(storage, [("p1", "SPY"), ("p2", "IWM")])

    handled = service._handle_batch_approval_command("yes all", "7777", "yes", "all", "4242")

    assert handled is True
    assert "YES ALL is blocked" in service.telegram.messages[-1]
    assert {r["status"] for r in storage.fetch_all("SELECT status FROM trade_proposals")} == {"pending"}


def test_batch_buy_approval_blocked_when_sleep_mode_active(tmp_path):
    service, storage = make_service(tmp_path)
    storage.execute("INSERT OR REPLACE INTO control_state(key, value) VALUES('sleep_mode_active', '1')")
    insert_batch(storage, [("p1", "SPY")])

    handled = service._handle_batch_approval_command("yes SPY", "7777", "yes", "spy", "4242")

    assert handled is True
    assert "Sleep mode is ON" in service.telegram.messages[-1]
    assert service.broker.submitted == []
    assert storage.fetch_all("SELECT status FROM trade_proposals WHERE id='p1'")[0]["status"] == "pending"
    assert storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE proposal_id='p1'")[0]["candidate_status"] == "pending"
    assert storage.fetch_all("SELECT COUNT(*) AS cnt FROM orders")[0]["cnt"] == 0


def test_batch_approval_command_parser_accepts_case_and_punctuation(tmp_path):
    service, _ = make_service(tmp_path)
    assert service._parse_batch_approval_command("YES SPY") == ("yes", "SPY")
    assert service._parse_batch_approval_command("yes, spy") == ("yes", "SPY")
    assert service._parse_batch_approval_command("no SPY.") == ("no", "SPY")


def test_batch_add_approval_blocked_when_sleep_mode_active(tmp_path):
    service, storage = make_service(tmp_path)
    storage.execute("INSERT OR REPLACE INTO control_state(key, value) VALUES('sleep_mode_active', '1')")
    insert_batch(storage, [("p1", "SPY", "buy", "add")])

    handled = service._handle_batch_approval_command("yes SPY", "7777", "yes", "spy", "4242")

    assert handled is True
    assert "Sleep mode is ON" in service.telegram.messages[-1]
    assert service.broker.submitted == []
    assert storage.fetch_all("SELECT status FROM trade_proposals WHERE id='p1'")[0]["status"] == "pending"
    assert storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE proposal_id='p1'")[0]["candidate_status"] == "pending"


def test_yes_all_blocks_buy_candidates_during_sleep_mode(tmp_path):
    service, storage = make_service(tmp_path)
    storage.execute("INSERT OR REPLACE INTO control_state(key, value) VALUES('sleep_mode_active', '1')")
    insert_batch(storage, [("p1", "SPY"), ("p2", "IWM", "buy", "add")])

    handled = service._handle_batch_approval_command("yes all", "7777", "yes", "all", "4242")

    assert handled is True
    assert any("YES ALL" in msg for msg in service.telegram.messages)
    assert sum("Sleep mode is ON" in msg for msg in service.telegram.messages) == 2
    assert service.broker.submitted == []
    assert {r["status"] for r in storage.fetch_all("SELECT status FROM trade_proposals")} == {"pending"}
    assert {r["candidate_status"] for r in storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates")} == {"pending"}


def test_batch_exit_approval_allowed_during_sleep_mode(tmp_path):
    service, storage = make_service(tmp_path)
    service.broker.get_positions = lambda: [{"symbol": "SPY", "qty": 0.05, "avg_entry_price": 100.0, "market_value": 5.0}]
    storage.execute("INSERT OR REPLACE INTO control_state(key, value) VALUES('sleep_mode_active', '1')")
    insert_batch(storage, [("p1", "SPY", "sell", "exit")])

    handled = service._handle_batch_approval_command("yes SPY", "7777", "yes", "spy", "4242")

    assert handled is True
    assert not any("Sleep mode is ON" in msg for msg in service.telegram.messages)
    assert service.broker.submitted and service.broker.submitted[0]["side"] == "sell"
    assert storage.fetch_all("SELECT status FROM trade_proposals WHERE id='p1'")[0]["status"] == "submitted"
    assert storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE proposal_id='p1'")[0]["candidate_status"] == "submitted"


def test_bad_symbol_gets_batch_aware_fallback_message(tmp_path):
    service, storage = make_service(tmp_path)
    insert_batch(storage, [("p1", "SPY"), ("p2", "IWM")])

    handled = service._handle_batch_approval_command("yes QQQ", "7777", "yes", "QQQ", "4242")

    assert handled is True
    assert service.telegram.messages[-1] == (
        "I found an active proposal batch, but I could not match your reply to a pending candidate. "
        "Use one of: yes SPY, yes IWM, yes all, no SPY, no IWM, no all."
    )


def test_expired_batch_candidate_cannot_be_approved(tmp_path):
    service, storage = make_service(tmp_path)
    now = datetime.now(UTC)
    expired = (now - timedelta(minutes=1)).isoformat()
    storage.execute(
        "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
        ("batch-1", "run", "4242", "pending", now.isoformat(), expired, "{}"),
    )
    payload = {
        "id": "p1",
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "qty": 0.05,
        "latest_price": 100.0,
        "price_at": now.isoformat(),
        "created_at": now.isoformat(),
        "expires_at": expired,
        "strategy_version": "rule_based_v1",
        "reason": "expired batch",
        "order_type": "limit",
        "asset_class": "equity",
    }
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,telegram_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("p1", "run", "sig-1", "SPY", "buy", 5.0, "pending", now.isoformat(), expired, "rule_based_v1", json.dumps(payload), "4242"),
    )
    storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,rank,reason,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cand-1", "batch-1", "p1", "4242", "SPY", "buy", "BUY", "pending", 1, "expired", now.isoformat(), expired, json.dumps(payload)),
    )

    handled = service._handle_batch_approval_command("yes SPY", "7777", "yes", "SPY", "4242")

    assert handled is True
    assert service.telegram.messages[-1] == "That candidate has expired, so I did not take action. I will not submit an order from an expired proposal."
    assert storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE proposal_id='p1'")[0]["candidate_status"] == "pending"


def test_listener_side_batch_expiry_notification_is_one_shot(tmp_path):
    service, storage = make_service(tmp_path)
    now = datetime.now(UTC)
    expiry = (now - timedelta(minutes=1)).isoformat()
    insert_batch(storage, [("p1", "SPY"), ("p2", "IWM")])
    storage.execute("UPDATE proposal_batches SET expires_at=?, expiry_notified=0 WHERE id='batch-1'", (expiry,))
    storage.execute("UPDATE proposal_batch_candidates SET expires_at=? WHERE batch_id='batch-1'", (expiry,))
    storage.execute("UPDATE trade_proposals SET expires_at=?, status='expired', expiry_notified=1", (expiry,))

    service._expire_pending_batches(notify=True)
    service._expire_pending_batches(notify=True)

    assert sum("Proposal batch expired" in msg for msg in service.telegram.messages) == 1
    assert storage.fetch_all("SELECT status, expiry_notified FROM proposal_batches WHERE id='batch-1'")[0] == {
        "status": "expired",
        "expiry_notified": 1,
    }
    assert {r["candidate_status"] for r in storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE batch_id='batch-1'")} == {"expired"}


def test_process_telegram_batch_route_stops_before_old_single_proposal_fallback(tmp_path, monkeypatch):
    service, storage = make_service(tmp_path)
    insert_batch(storage, [("p1", "SPY"), ("p2", "IWM")])
    service.listener_started_at = 0
    service.telegram.updates = [
        {
            "update_id": 101,
            "message": {
                "message_id": 5001,
                "text": "yes SPY",
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 7777},
                "chat": {"id": "123"},
                "reply_to_message": {"message_id": 4242},
            },
        }
    ]

    def fake_approve(*args, **kwargs):
        return False, "blocked", "blocked for test"

    monkeypatch.setattr(service, "_approve_batch_candidate", fake_approve)

    service.process_telegram()

    assert any("Received: YES for SPY paper buy candidate" in msg for msg in service.telegram.messages)
    assert not any("could not match your reply to a single pending proposal" in msg for msg in service.telegram.messages)
    assert storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE proposal_id='p1'")[0]["candidate_status"] == "blocked"
    audits = storage.fetch_all("SELECT event_type, detail FROM audit_events WHERE event_type='telegram_approval_route'")
    assert audits
    detail = json.loads(audits[-1]["detail"])
    assert detail["approval_intent"] == "yes"
    assert detail["target_symbol"] == "SPY"
    assert detail["route_chosen"] == "batch"
    assert detail["stopped_processing"] is True


def test_process_telegram_yes_symbol_matches_one_candidate_batch_without_reply_to(tmp_path, monkeypatch):
    service, storage = make_service(tmp_path)
    insert_batch(storage, [("p1", "SPY")])
    service.listener_started_at = 0
    service.telegram.updates = [
        {
            "update_id": 103,
            "message": {
                "message_id": 5003,
                "text": "yes SPY",
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 7777},
                "chat": {"id": "123"},
            },
        }
    ]

    def fake_approve(*args, **kwargs):
        return False, "blocked", "blocked for test"

    monkeypatch.setattr(service, "_approve_batch_candidate", fake_approve)

    service.process_telegram()

    assert any("Received: YES for SPY paper buy candidate" in msg for msg in service.telegram.messages)
    assert not any("could not match your reply to a single pending proposal" in msg for msg in service.telegram.messages)
    assert service.broker.submitted == []
    assert storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE proposal_id='p1'")[0]["candidate_status"] == "blocked"
    audits = storage.fetch_all("SELECT detail FROM audit_events WHERE event_type='telegram_approval_route'")
    detail = json.loads(audits[-1]["detail"])
    assert detail["target_symbol"] == "SPY"
    assert detail["active_batch_count"] == 1
    assert detail["active_batch_candidate_symbols"] == ["SPY"]
    assert detail["route_chosen"] == "batch"


def test_process_telegram_plain_yes_is_batch_ambiguous(tmp_path):
    service, storage = make_service(tmp_path)
    insert_batch(storage, [("p1", "SPY"), ("p2", "IWM")])
    service.listener_started_at = 0
    service.telegram.updates = [
        {
            "update_id": 102,
            "message": {
                "message_id": 5002,
                "text": "yes",
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 7777},
                "chat": {"id": "123"},
            },
        }
    ]

    service.process_telegram()

    assert service.telegram.messages[-1] == "Plain yes is ambiguous because more than one candidate is pending. Use yes SPY, yes IWM, yes all, no SPY, no IWM, no all."


def test_process_telegram_plain_yes_single_batch_candidate_is_documented_ambiguous(tmp_path):
    service, storage = make_service(tmp_path)
    insert_batch(storage, [("p1", "SPY")])
    service.listener_started_at = 0
    service.telegram.updates = [
        {
            "update_id": 104,
            "message": {
                "message_id": 5004,
                "text": "yes",
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 7777},
                "chat": {"id": "123"},
            },
        }
    ]

    service.process_telegram()

    assert service.telegram.messages[-1] == "Plain yes is ambiguous because a ranked batch is pending. Use yes SPY, yes all, no SPY, no all."
    assert storage.fetch_all("SELECT candidate_status FROM proposal_batch_candidates WHERE proposal_id='p1'")[0]["candidate_status"] == "pending"


def test_process_telegram_active_batch_bad_symbol_uses_batch_fallback(tmp_path):
    service, storage = make_service(tmp_path)
    insert_batch(storage, [("p1", "SPY")])
    service.listener_started_at = 0
    service.telegram.updates = [
        {
            "update_id": 105,
            "message": {
                "message_id": 5005,
                "text": "yes QQQ",
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 7777},
                "chat": {"id": "123"},
            },
        }
    ]

    service.process_telegram()

    assert service.telegram.messages[-1] == "I found an active proposal batch, but I could not match your reply to a pending candidate. Use one of: yes SPY, yes all, no SPY, no all."
    assert not any("single pending proposal" in msg for msg in service.telegram.messages)


def test_process_telegram_no_active_batch_keeps_single_proposal_behavior(tmp_path):
    service, storage = make_service(tmp_path)
    now = datetime.now(UTC)
    expiry = (now + timedelta(minutes=10)).isoformat()
    payload = {
        "id": "p-single",
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "notional": 5.0,
        "qty": 0.05,
        "latest_price": 100.0,
        "price_at": now.isoformat(),
        "created_at": now.isoformat(),
        "expires_at": expiry,
        "strategy_version": "rule_based_v1",
        "reason": "single proposal",
        "order_type": "market",
        "asset_class": "equity",
    }
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,telegram_message_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("p-single", "run", "sig-single", "SPY", "buy", 5.0, "pending", now.isoformat(), expiry, "rule_based_v1", json.dumps(payload), "9999"),
    )
    service.listener_started_at = 0
    service.telegram.updates = [
        {
            "update_id": 106,
            "message": {
                "message_id": 5006,
                "text": "maybe",
                "date": int(datetime.now(UTC).timestamp()),
                "from": {"id": 7777},
                "chat": {"id": "123"},
            },
        }
    ]

    service.process_telegram()

    assert "could not tell whether you meant yes or no" in service.telegram.messages[-1]


def insert_exit_proposal(storage: Storage, proposal_id: str, symbol: str, status: str, expires_at: str, emergency: int = 0) -> None:
    now = datetime.now(UTC)
    storage.execute(
        "INSERT INTO trade_proposals(id,run_id,signal_id,symbol,side,notional,status,created_at,expires_at,strategy_version,payload,emergency_exit_triggered) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (proposal_id, "run", f"sig-{proposal_id}", symbol, "sell", 5.0, status, now.isoformat(), expires_at, "rule_based_v1", "{}", emergency),
    )


def test_stale_approved_expired_sell_proposal_does_not_block_buys(tmp_path):
    service, storage = make_service(tmp_path)
    expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    insert_exit_proposal(storage, "old-spy-exit", "SPY", "approved", expired)

    blocker = service._exit_blocker_context([])

    assert blocker["active"] is False
    assert blocker["stale"] is True
    assert blocker["symbol"] == "SPY"
    assert blocker["reason"] == "stale SPY exit flag ignored"


def test_active_pending_exit_proposal_blocks_buys(tmp_path):
    service, storage = make_service(tmp_path)
    future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    insert_exit_proposal(storage, "dia-exit", "DIA", "pending", future)

    blocker = service._exit_blocker_context([])

    assert blocker["active"] is True
    assert blocker["symbol"] == "DIA"
    assert blocker["reason"] == "DIA EXIT proposal pending"


def test_active_emergency_exit_review_blocks_buys(tmp_path):
    service, storage = make_service(tmp_path)
    future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    insert_exit_proposal(storage, "dia-emergency", "DIA", "pending", future, emergency=1)

    blocker = service._exit_blocker_context([])

    assert blocker["active"] is True
    assert blocker["symbol"] == "DIA"
    assert blocker["reason"] == "DIA emergency exit review active"


def test_expired_exit_proposal_does_not_block_buys(tmp_path):
    service, storage = make_service(tmp_path)
    expired = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    insert_exit_proposal(storage, "dia-expired", "DIA", "pending", expired)

    blocker = service._exit_blocker_context([])

    assert blocker["active"] is False
    assert blocker["stale"] is True
    assert blocker["status"] == "pending"


def test_submitted_and_filled_exit_proposals_do_not_block_buys(tmp_path):
    for status in ("submitted", "filled"):
        service, storage = make_service(tmp_path / status)
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        insert_exit_proposal(storage, f"dia-{status}", "DIA", status, future)

        blocker = service._exit_blocker_context([])

        assert blocker["active"] is False
        assert blocker["stale"] is True
        assert blocker["status"] == status


def test_pending_exit_batch_candidate_blocks_buys(tmp_path):
    service, storage = make_service(tmp_path)
    now = datetime.now(UTC)
    expiry = (now + timedelta(minutes=10)).isoformat()
    storage.execute(
        "INSERT INTO proposal_batches(id,run_id,telegram_message_id,status,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?)",
        ("batch-exit", "run", "4242", "pending", now.isoformat(), expiry, "{}"),
    )
    storage.execute(
        "INSERT INTO proposal_batch_candidates(id,batch_id,proposal_id,telegram_message_id,candidate_symbol,candidate_side,candidate_action,candidate_status,rank,reason,created_at,expires_at,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("cand-exit", "batch-exit", "proposal-exit", "4242", "DIA", "sell", "EXIT", "pending", 1, "ranked", now.isoformat(), expiry, "{}"),
    )

    blocker = service._exit_blocker_context([])

    assert blocker["active"] is True
    assert blocker["symbol"] == "DIA"
    assert blocker["reason"] == "DIA EXIT batch candidate pending"


def test_risk_context_does_not_block_buy_on_stale_exit(tmp_path):
    service, storage = make_service(tmp_path)
    expired = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    insert_exit_proposal(storage, "old-spy-exit", "SPY", "approved", expired)
    proposal = risk_proposal()

    context = service._portfolio_context(proposal)
    decision = service._risk_engine("risk-test", "proposal").evaluate(proposal, context)

    assert context["exit_pending"] is False
    assert context["exit_pending_stale"] is True
    assert not any("exit" in reason.lower() for reason in decision.reasons)


def risk_proposal() -> dict:
    now = datetime.now(UTC)
    return {
        "symbol": "SPY",
        "side": "buy",
        "action": "entry",
        "latest_price": 100.0,
        "price_at": now.isoformat(),
        "historical_bars": 250,
        "volume": 10000,
        "price_gap_pct": 0,
        "notional": 5.0,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=10)).isoformat(),
        "strategy_version": "rule_based_v1",
        "reason": "test",
        "order_type": "limit",
        "asset_class": "equity",
    }


def risk_context() -> dict:
    return {
        "power_connected": True,
        "internet_available": True,
        "database_writable": True,
        "broker_available": True,
        "telegram_available": True,
        "market_open": True,
        "kill_switch": False,
        "open_positions": 99,
        "buy_trades_today": 99,
        "duplicate_order": False,
        "same_symbol_position": False,
        "uses_margin": False,
        "daily_loss": 0,
        "weekly_loss": 0,
        "daily_loss_dollars": 0,
        "weekly_loss_dollars": 0,
        "daily_loss_pct": 0,
        "weekly_loss_pct": 0,
        "daily_loss_confidence": "verified",
        "weekly_loss_confidence": "verified",
        "loss_metrics_version": "loss_controls_v2",
        "buying_power": 10000,
        "proposed_total_exposure_pct": 1.0,
        "proposed_symbol_exposure_pct": 1.0,
        "proposed_cluster_positions_count": 1,
        "proposed_cluster_exposure_pct": 1.0,
        "approval_valid": True,
    }


def test_risk_budgeted_mode_ignores_fixed_position_and_daily_buy_count_caps():
    cfg = config()
    cfg["portfolio_behavior"]["max_open_positions"] = 1
    cfg["portfolio_behavior"]["max_new_buy_orders_per_day"] = 1
    decision = RiskEngine(cfg).evaluate(risk_proposal(), risk_context())

    assert decision.passed


def test_legacy_mode_still_enforces_fixed_position_and_daily_buy_count_caps():
    cfg = config()
    cfg.pop("portfolio_execution_mode")
    cfg["portfolio_behavior"]["max_open_positions"] = 1
    cfg["portfolio_behavior"]["max_new_buy_orders_per_day"] = 1
    decision = RiskEngine(cfg).evaluate(risk_proposal(), risk_context())

    assert not decision.passed
    assert "open-position limit" in decision.reasons
    assert "daily buy order limit" in decision.reasons
