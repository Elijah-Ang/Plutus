# Crypto Decimal sizing and portfolio-risk boundary

This stage adds canonical sizing and portfolio risk for the separate Alpaca
spot-crypto lane. It remains research-only: it cannot create a proposal,
display, approval, intent, reservation, broker request, or fill. BTC/USD and
ETH/USD remain the only configured pairs, and all crypto execution flags remain
disabled.

## Official contract used by the policy

The sizing policy is based only on current Alpaca primary documentation and the
hash-locked `alpaca-py==0.43.4` contract gate:

- Crypto orders accept market, limit, and stop-limit types with `gtc` or `ioc`;
  fractional orders use either quantity or notional, never both.
  [Crypto Orders](https://docs.alpaca.markets/us/docs/crypto-orders)
- Current pair-specific `min_order_size`, `min_trade_increment`, and
  `price_increment` come from Alpaca's authenticated Assets API instead of a
  copied static table. The broker may change those values.
  [Crypto Coin Pair FAQ](https://alpaca.markets/support/alpaca-crypto-coin-pair-faq)
- Buy orders use a one-dollar minimum. Notional requests use at most two
  decimals and quantity requests use at most nine. Exact full-position sells
  are allowed so dust cannot become trapped.
  [Broker API FAQ](https://docs.alpaca.markets/us/docs/broker-api-faq)
- Tier-one crypto fees are conservatively modelled at 25 bps per side. Alpaca
  charges a buy fee in the received crypto and a sell fee in quote proceeds.
  [Crypto Fees](https://docs.alpaca.markets/us/docs/crypto-fees)

The system ceiling is deliberately much smaller than Alpaca's platform order
limit: five US dollars per initial paper order.

## Canonical Decimal sizing

`app/crypto_sizing.py` rejects binary floats at the strategy authority boundary.
Money, price, quantity, fee, slippage, notional, and stop risk are calculated
with `Decimal` and stored as canonical text.

For a long BUY, the maximum-loss denominator includes:

```text
loss per unit
= entry limit × (1 + entry fee rate)
 - adverse stop execution price × (1 - exit fee rate)
```

The adverse stop execution price applies the configured stop slippage. Quantity
is bounded by both stop-risk dollars and every notional capacity, then rounded
down to the current Assets API increment. A BUY limit price rounds up to the
current price increment before sizing so price precision cannot silently
increase approved risk. A notional request rounds down to cents. The formula
never raises a value to satisfy Alpaca's minimum.

SELLs use an exact quantity basis. Pending sell quantity is subtracted once
from current broker holdings. Partial sells round down to the current quantity
increment; an exact full-position exit may close dust, but may not exceed nine
decimal places or the verified sellable holding.

## Trusted portfolio risk

`app/crypto_risk.py` reloads the current immutable capability and market
evidence, then obtains fresh evidence from the paper broker:

- stable paper account identity, active status, USD currency, equity, cash, and
  non-marginable buying power;
- all positions, with crypto positions explicitly classified as crypto;
- all open orders;
- current daily and weekly account loss evidence;
- current hourly US-feed crypto bars for Decimal annualized volatility.

It then opens one `BEGIN IMMEDIATE` transaction and reads durable intents,
active reservations, realized crypto loss, and equity watermarks. Broker I/O is
completed before that transaction, so a network delay never holds SQLite's
writer lock. A broker order and its matching durable reservation are grouped by
client-order identity and counted once; unrelated orders and reservations are
never excluded.

The hard notional ceiling is the minimum remaining capacity across:

- the five-dollar order maximum;
- total portfolio gross exposure;
- the crypto sleeve;
- symbol exposure;
- the BTC/ETH `crypto_major` correlation cluster;
- cash after the configured reserve;
- non-marginable buying power.

The hard stop-risk ceiling is the minimum of per-trade stop risk and remaining
crypto heat. Existing crypto positions use their full market value as a
conservative loss-to-zero bound until a later position-management stage binds a
tighter durable protective stop. Daily and weekly account/crypto loss,
drawdown, and annualized volatility can throttle capacity to zero. ADDs remain
disabled.

Missing, stale, non-finite, mismatched, unsupported, ambiguous, or malformed
evidence produces zero authority. A failed snapshot may be retained for audit,
but both `crypto_sizing_decisions.execution_authorized` and
`crypto_risk_decisions.execution_authorized` are constrained to zero by schema.

## Durable evidence and tamper checks

The additive tables are:

- `crypto_risk_snapshots`;
- `crypto_sizing_decisions`;
- `crypto_risk_decisions`.

Every source family has a separate fingerprint. Reload verifies persisted
columns, relational capability/market/risk identities, config and formula
versions, expiry, sub-fingerprints, classification, and independently
recomputes all derived capacities and risk checks. Runtime schema guards,
release manifests, artifact tests, reports, and integrity counters require the
new tables and versions.

The older float-valued crypto research candidate fields remain explicitly
non-authoritative measurement metadata. No future proposal may use those fields
for quantity, notional, fee, stop-risk, or portfolio authority.

## Current safety boundary

- paper endpoint only;
- research-only crypto mode;
- proposal and paper-trading flags false;
- manual approval required in any later stage;
- long-only spot BTC/USD and ETH/USD;
- no leverage, margin, borrowing, shorting, derivatives, or autonomous action;
- no crypto adapter is present in this stage;
- the equity DAY-order adapter still rejects every crypto symbol before broker
  I/O.
