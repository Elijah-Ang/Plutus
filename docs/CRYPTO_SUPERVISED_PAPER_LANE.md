# Supervised crypto paper lane

The ordinary crypto research path remains isolated from equity proposals and
the equity `DAY` order adapter.  The separate `CryptoPaperLaneStore` is an
explicit, opt-in stage for the two initially supported pairs, `BTC/USD` and
`ETH/USD`.

The lane was enabled by the separately reviewed configuration change in PR
#26 (merge commit `9f3ca6e30fce0a3df58a26e28c4a851b37048a47`), after the
paper-only capability and safety prechecks. The active configuration keeps
`crypto.supervised_paper_lane.enabled` and `execution_enabled` true while
the separate `crypto.proposal_policy` remains a non-executable research
preview. The lane still requires all of the following:

- Alpaca paper identity and a current Assets API capability snapshot;
- current US quote, trade, order-book and risk evidence;
- a fresh point-in-time strategy decision and canonical Decimal sizing;
- a displayed, fingerprinted proposal with a short expiry;
- an authorized human approval whose command and reply target bind to that
  exact display;
- a durable intent and one reservation committed before broker I/O;
- final account, quote, open-order, loss, kill-switch, risk-snapshot, formula
  and health checks immediately before invocation.

The lane is long-only spot crypto: BUY entry/add orders may use a quantity or
notional basis, while risk-reducing SELL exit/reduce orders must use an exact
quantity and are checked against current holdings immediately before I/O.  It
permits only limit orders with Alpaca's documented `gtc` or `ioc` time-in-force
values and caps risk-increasing orders at USD 5,000.  Live trading, margin,
shorting, autonomous execution and equity-adapter fall-through are rejected.

`retryable_pre_submission` means the adapter was unavailable or rejected the
request before broker invocation.  Once `broker_invocation_occurred=1`, a
timeout is `unknown` and is never automatically resubmitted.  Fill records,
fees, FIFO lots, realized P&L and reservation release are kept in the isolated
crypto ledger and shared fixed-point FIFO accounting.  The
`portfolio_metrics()` view reports crypto exposure, realized/unrealized P&L and
strategy attribution separately from equity-session metrics. Crypto realized
loss sessions are keyed in UTC, while equity outcomes retain their New York
session timezone, so continuous crypto activity cannot cross-contaminate equity
session controls;
`integrity_report()` verifies the lane's orphan, duplicate, reservation and
ambiguous-retry invariants.

No proposal is created or approved by deployment smoke tests.  Normal market
data plus a real operator approval is required for a future paper order.
