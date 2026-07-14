# Phase 4.2 Adaptive Conviction and Adaptive Sizing

Adaptive Conviction is the sole owner of deployment mode, opportunity class,
multipliers, requested risk, permitted risk, confidence and binding reasons:

```text
requested stop risk = base strategy risk
                    × opportunity multiplier
                    × regime multiplier
                    × account-health multiplier
                    × execution-quality multiplier
                    × diversification multiplier
```

The result is bounded in order by formula request, strategy authorisation,
deployment-mode trade cap, 0.35% hard trade cap, portfolio-heat capacity, gross
capacity, symbol capacity and cluster capacity. Calibration alone cannot
expand risk: setup, payoff/stop quality, regime, account, execution, integrity,
liquidity and diversification must independently agree. Uncalibrated score
evidence is neutral and cannot produce expansion.

Adaptive Sizing converts permitted risk with the shared canonical helper:

```text
stop-risk dollars = authoritative equity × permitted stop-risk percent / 100
unconstrained notional = stop-risk dollars × validated entry / stop distance
```

It then takes the minimum of the active canonical ceilings in this order:
stop risk; displayed quantity and displayed stop risk at final approval;
mode heat; mode gross; equity; absolute limit when configured; cash reserve;
cash usage; cash; buying power; symbol; cluster; portfolio; non-ACTIVE policy
allocation; exploration; PROBE; and their aggregate heat/gross/count limits.
The historical initial `$250` and ADD `$100` stage ceilings are retained only
as historical configuration evidence and are excluded from the operational
minimum path. No replacement fixed-dollar bottleneck exists.

At proposal time, the persisted adaptive quantity/notional is the actual paper
proposal and displayed approval ceiling. At approval, fresh account, quote,
stop, exposure, policy, integrity and reservation inputs are recomputed:

```text
final operational notional = min(
    displayed approved adaptive notional,
    approval-time recomputed adaptive notional,
    current Phase 3 capacity,
    reservation-adjusted capacity,
    every hard ceiling
)
```

Displayed quantity and displayed stop-risk dollars are also independent
one-way ceilings. Final validation may preserve, reduce or block, never enlarge.
Executor atomically reserves the resulting operational notional and stop risk;
the engines cannot reserve or submit anything themselves. Exits bypass both
entry engines.

Historical `adaptive_conviction_decisions` and `adaptive_sizing_decisions`
remain report-only evidence. New decisions use additive
`adaptive_conviction_operational_decisions` and
`adaptive_sizing_operational_decisions`, storing full raw inputs, identifiers,
configuration hash, fingerprints and formula/schema/evidence versions.

Configuration/schema/formula versions:

- `plutus_effective_config_v7_release_gate_operational_paper`

Every ordinary or protective paper order requires manual approval. Extreme and
sleep-mode exit detection can prioritise an urgent SELL proposal, but it cannot
start a timer or submit through an approval-bypass path.
- `adaptive_conviction_formula_v2_operational_paper`
- `adaptive_conviction_operational_decisions_v2`
- `adaptive_sizing_formula_v2_operational_paper`
- `adaptive_sizing_operational_decisions_v2`
- `phase1_outcome_v2_exit_session`

Deployment requires an immutable release, verified paper broker identity,
manual Telegram approval, a production-database backup, an isolated successful
migration dry run, all migration ledger entries and a healthy durable integrity
report. Rollback switches both scanner and listener to the prior immutable
release and its compatible configuration. Additive tables may remain; exact
database rollback requires stopped writers and the verified pre-migration copy.
