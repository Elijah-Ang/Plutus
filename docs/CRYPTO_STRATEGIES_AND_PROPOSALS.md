# Crypto strategies and proposal authority

This stage adds independently verifiable BTC/USD and ETH/USD strategy
decisions and an immutable crypto proposal-preview record. It does **not**
enable crypto proposals, Telegram approvals, intents, reservations, or broker
submission. The installed configuration remains `research_only`, and the
existing broker adapter continues to reject every crypto order before I/O.

## Official Alpaca contract

The boundary continues to use current primary Alpaca documentation:

- Alpaca crypto supports market, limit, and stop-limit order types, with `gtc`
  and `ioc` time in force. Crypto notional may be used with any supported order
  type. [Orders at Alpaca](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- `alpaca-py` builds submissions from explicit request objects and exposes
  current assets, positions, and orders through the trading client.
  [alpaca-py trading API](https://alpaca.markets/sdks/python/trading.html)
- Pair-specific minimum order size, trade increment, and price increment remain
  bound from the authenticated Assets API rather than copied into strategy
  code. [Trading models](https://alpaca.markets/sdks/python/api_reference/trading/models.html)

These protocol facts are evidence only. They do not grant order authority.

## Point-in-time strategies

`app/crypto_strategies.py` evaluates four crypto-specific research strategies:

1. time-series trend;
2. breakout continuation;
3. pullback in trend;
4. volatility-adjusted momentum.

The evaluator uses exact Decimal OHLC values from a contiguous series of
UTC-hour-aligned, one-hour bars at or before the bound Alpaca quote timestamp.
The newest bar must be no more than two hours behind that quote. Future,
duplicate, misaligned, gapped, or stale timestamps; malformed OHLC
relationships; insufficient history; an unverified market snapshot; or an
identity mismatch fail closed before persistence. The persisted input binds
the `1Hour` timeframe, 3,600-second cadence, newest-bar age, exact bars, and
their fingerprint. Equity market clocks, gap rules, thresholds, and session
labels are never used.

The reviewed configuration fixes all lookbacks and thresholds. EMA, return,
ATR-like distance, breakout range, pullback, and annualized volatility metrics
are persisted with the exact canonical bar set and a bar fingerprint. Reload
recomputes the complete strategy decision from those bars and the verified
market record; replacing local JSON and its digest cannot legitimize changed
metrics.

An eligible signal receives a bounded stop and a target of at least 1.5R. It
still records:

```text
lifecycle = RESEARCH_ONLY
proposal_authorized = false
execution_authorized = false
```

The hourly crypto research loop records these decisions automatically when its
Alpaca quote/order-book evidence is authoritative. Fresh but temporarily wide
or shallow evidence still records the selected research setup with an explicit
market-execution blocker, preserving suppressed opportunity evidence without
granting proposal authority. Stale, malformed, or otherwise unauthoritative
market evidence cannot create a strategy row. A failed strategy evaluation
remains research evidence and never creates a proposal.

## Immutable proposal previews

`app/crypto_proposals.py` creates a preview only when all of the following bind
exactly:

- the strategy decision and its fingerprint;
- the capability snapshot and current pair precision;
- Alpaca quote, spread, order-book, and market fingerprints;
- a current paper account and portfolio-risk snapshot;
- the risk decision;
- canonical Decimal sizing whose source ID and fingerprint are the exact
  strategy decision;
- current configuration, schema, and formula identities.

The preview independently recomputes its economics and shows the future manual
approval surface:

- symbol, strategy, lifecycle, and action;
- quantity/notional basis;
- bid, ask, spread, and annualized volatility;
- limit, stop, target, gross reward, cost-adjusted net reward, maximum loss,
  and expected costs;
- existing/projected crypto exposure and total portfolio exposure;
- expiry and the would-be exact approval command;
- an explicit paper-only, non-approvable warning.

The would-be command is displayed for UX and audit design only. It is marked
disabled and is not accepted by the Telegram listener.

The database constrains every preview to:

```text
status = research_only_preview
manual_approval_eligible = 0
execution_authorized = 0
```

Preview creation never writes `trade_proposals`, `approvals`, `order_intents`,
or `risk_reservations`; it never calls Telegram or Alpaca. Integrity checks
detect orphaned relationships or any attempt to turn strategy, sizing, risk,
or preview evidence into execution authority.

The target is solved from the canonical limit price so that proceeds after the
configured entry and target-exit fees retain at least the displayed net R
multiple of the cost-inclusive maximum stop loss. The preview separately shows
gross reward, net reward, gross R, net R, target round-trip fees, the stop-loss
fee component, and adverse stop slippage. Authority is loaded after a SQLite
`BEGIN IMMEDIATE`, so verified strategy, risk, sizing, capability, and market
evidence cannot change between display construction and preview persistence.

## Enabling a later paper proposal stage

An executable crypto proposal requires a separate reviewed change that adds
the complete ordinary authority chain: immutable Telegram display, reply
target binding, one-use approval, final fresh broker/risk revalidation, durable
intent and reservation, crypto-specific limit/GTC adapter, idempotent client
identity, reconciliation, fills, fees, lot accounting, and Performance Lab
outcomes. That later stage must remain paper-only, manual-only, long-only,
cash-funded, and unable to retry an ambiguous submission automatically.
