# Customer value and long-horizon guardrails (Phase 9)

**All data is SYNTHETIC** (Phase 1 simulator). Decision: ADR 0016 (and ADR 0013, third
amendment). Evidence: `docs/evidence/phase-9.md`.

```text
marts (dim_customer, fct_subscriptions*, fct_usage_daily, fct_marginal_cost_daily,
       fct_payments, fct_service_metrics_hourly, fct_customer_price_changes*)   * new in Phase 9
        |  point in time: rows strictly before the snapshot date T
praxis.customer.warehouse   Snapshot (subscribers active at T) / Outcomes (labels after T)
praxis.customer.features    customer_features.v1
praxis.customer.dataset     panel of non-overlapping 28-day windows
praxis.customer.hazard      Poisson GLM (piecewise exponential) | tier rate table (baseline)
praxis.customer.train       rolling-origin backtest -> champion rule -> artifact
praxis.customer.value       at a decision date: calibrated hazards, CLV, segment value
praxis.customer.monitor     segment monitoring report + alerts
        |
praxis.pricing.inputs       churn exposure + CLV per segment, price-change histogram
praxis.pricing.optimiser    CLV penalty + segment churn / ceiling / frequency guardrails

experiment world marts -> praxis.elasticity.churn -> churn_response.json (bill response)
```

## Definitions

| Term | Definition |
| --- | --- |
| customer | subscribed before T (a prospect that never converted is not one) and not churned before T |
| churn label | VOLUNTARY churn in [T, T + 28 days); involuntary (payment) churn ends exposure (competing risk) |
| days at risk | until the churn day (inclusive) or the end of the window |
| segment | tier in force at T (`fct_subscriptions`; never `dim_customer.current_tier`) |
| daily margin | (usage value - marginal cost) / 28 over the last 28 days + tier fee / 30 |
| CLV | margin x (1 - payment loss) x sum_{t=1..365} 1.10^(-t/365) exp(-(h_vol + h_inv) t) |
| bill share s_ip | product p's share of the customer's usage value (pre-period in the experiment, last 28 days at a decision) |
| bill response gamma_p | d log(voluntary hazard) / d (s_ip x log price of p), randomised tests |

## Commands

```bash
make churn-response EL_SEED=1                   # after elasticity-analyze: churn_response.json
make pricing-data customer-train PX_SEED=1      # customer artifact on the shadow world's prefix
make customer-monitor PX_SEED=1                 # segment report at the first cycle (exit 1 = alert)
make pricing-shadow PX_SEED=1                   # now needs churn_response.json + the customer model
make pricing-compare PX_SEED=42                 # Phase 6 vs Phase 9 decisions on seed 42
.venv/bin/python -m praxis.customer --db WH train --start D --cutoff D --out ROOT
.venv/bin/python -m praxis.customer --db WH monitor --model DIR --as-of D
```

## What the tests prove

* CLV on hand-calculated fixtures (halving survival, closed form = explicit sum, a year of
  discounting = 1 / (1 + r)), sensitivity grid, invalid inputs (`tests/customer/test_clv.py`).
* Label definition, observation window, censoring, competing risks, point-in-time tier and fee,
  exact integer sums, and leakage: deleting every row on or after T changes no feature
  (`tests/customer/test_warehouse.py`, a hand-built warehouse whose future is poisoned).
* Champion rule follows the backtest; every backtest window is fitted on earlier windows only;
  artifact lineage and tamper detection (`tests/customer/test_models.py`).
* Calibration, segment values, monitoring alerts, CLI (`tests/customer/test_value_monitor.py`).
* Bill response recovered on a known synthetic factorial; eligibility and arms recomputed from
  the salt (`tests/elasticity/test_churn.py`).
* Long-term guardrails reject or shrink a raise that improves immediate contribution
  (`tests/pricing/test_long_term.py`); Hypothesis properties include the new constraints.
