# Crypto data and broker capability boundary

This stage adds read-only, durable capability evidence for the separate Alpaca
spot-crypto lane. It does **not** enable crypto proposals, approvals, intents,
reservations or broker submission. Configuration remains `research_only`,
`paper_trading_enabled: false`, `proposals_enabled: false`, and
`live_enabled: false`.

## Primary-source contract

The versioned contract `alpaca_spot_crypto_contract_2026_07_17` is based only
on current official Alpaca documentation and the pinned `alpaca-py==0.43.4`
SDK:

- Alpaca documents spot crypto as available all day, seven days a week, and
  directs clients to query the Assets API for the current supported pairs and
  pair-specific precision. Asset precision can change, so the application does
  not treat a copied list as runtime authority. [Crypto Spot Trading](https://docs.alpaca.markets/us/docs/crypto-trading)
- Current supported order types are `market`, `limit`, and `stop_limit`; current
  crypto time-in-force values are `gtc` and `ioc`; and fractional orders accept
  either quantity or notional, not both. [Crypto Orders](https://docs.alpaca.markets/us/docs/crypto-orders)
- Paper trading explicitly simulates crypto. It is not execution-quality proof:
  the simulation omits queue position, market impact, and latency slippage and
  can produce partial fills. [Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading)
- The crypto market-data API exposes bars, trades, quotes, and latest order
  books. This build selects the Alpaca US feed explicitly. [alpaca-py crypto historical client](https://alpaca.markets/sdks/python/api_reference/data/crypto/historical.html)
- Historical crypto bars may use quote midpoint prices when no trade occurs;
  therefore a positive bar price with zero volume is not liquidity evidence.
  [Historical Crypto Data](https://docs.alpaca.markets/us/docs/historical-crypto-data-1)
- Crypto fees are maker/taker and volume-tiered. The conservative tier-one
  contract is 15 bps maker and 25 bps taker; fees can post at end of day and
  are available as `CFEE` or `FEE` activities. [Crypto Spot Trading Fees](https://docs.alpaca.markets/us/docs/crypto-fees)
- Client order identifiers and cancellation use the ordinary Orders API. The
  capability contract records the documented 128-character client-order-ID
  bound, terminal states, and all active or ambiguous states that later
  reconciliation must handle. [Orders at Alpaca](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- The pinned trading model represents spot-pair assets with
  `AssetClass.CRYPTO`, `AssetExchange.CRYPTO`, and `AssetStatus.ACTIVE`.
  [alpaca-py trading enums](https://alpaca.markets/sdks/python/api_reference/trading/enums.html)

Alpaca does not publish a crypto execution calendar that proves the absence of
maintenance. Continuous hours are therefore never interpreted as guaranteed
availability. Missing Assets API evidence, unavailable market data, an inactive
pair, or later reconciliation uncertainty is a fail-closed condition.

## Durable authority

Every research cycle captures one immutable `crypto_capability_snapshots` row
and one `crypto_asset_capabilities` row for each configured initial pair:

- `BTC/USD`;
- `ETH/USD`.

The snapshot binds:

- a verified paper endpoint and stable hashed paper-account identity;
- the current effective configuration hash;
- static contract and formula/schema versions;
- the exact current Assets API response for each pair;
- `active`, `tradable`, `fractionable`, non-marginable, non-shortable and
  non-borrowable flags;
- broker asset identity, exchange and asset class;
- current minimum order size, minimum trade increment and price increment;
- capture and expiry times;
- per-asset, input and complete snapshot fingerprints.

Loading authority recomputes every fingerprint from the persisted evidence and
compares the JSON evidence with the relational columns and child rows. It also
rejects an expired snapshot, configuration change, obsolete contract/formula,
missing pair, duplicate pair, malformed numeric precision, non-USD quote,
stablecoin base, wrong asset class/exchange, or unverified paper account.

Capability evidence is linked to each crypto research run and snapshot. A
non-authoritative capability is recorded as
`crypto_capability_unverified` and cannot support a later proposal stage.

Each symbol also receives an immutable `crypto_market_data_evidence` record.
It binds the current US-feed quote (bid, ask, sizes and timestamp), latest trade
(price, size and timestamp), latest order-book top levels and timestamp,
derived spread, minimum two-sided top-of-book notional, freshness, config and
capability identities, formula/schema versions, failure/warning reasons and a
complete fingerprint. Quote and order-book evidence are mandatory; latest
trade evidence is recorded but may be absent when no trade occurred. Crossed,
stale, malformed or unavailable markets fail authority, while excessive spread
or insufficient displayed depth remains authoritative evidence with
`execution_eligible=false`.

## Data adapter separation

`AlpacaBroker` now has crypto-specific read methods for:

- active crypto Assets API records;
- historical crypto bars;
- latest crypto quote;
- latest crypto trade;
- latest crypto order book.

All market-data calls select the `CryptoFeed.US` feed explicitly. The crypto
client is distinct from the stock data client. The generic equity order method
rejects slash, dash, and configured legacy crypto symbols before request
construction, so its equity `DAY` default cannot leak into crypto.

The fresh-release test runner also executes the offline
`scripts/verify_alpaca_crypto_sdk.py` gate. It requires the installed SDK to
match the hash-locked `alpaca-py==0.43.4` pin and checks the client, request,
paper-constructor, asset-precision, order-state, feed, cancellation and lookup
interfaces bound by this contract. Artifact verification requires this gate to
be present and successful before `tests_verified=true` can be trusted.

The next reviewed stage now implements research-only Decimal sizing/rounding
and crypto portfolio-risk/loss evidence; see
[CRYPTO_SIZING_AND_RISK.md](CRYPTO_SIZING_AND_RISK.md). Proposal/display
authority, manual Telegram approval, crypto-specific intent construction,
final broker revalidation, fills/fees/accounting, and continuous-market
outcomes remain disabled and require later reviewed stages before this lane may
be enabled.
