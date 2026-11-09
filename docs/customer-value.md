# Customer value

`praxis.customer` estimates each segment's churn hazard and lifetime value, so the pricing
optimiser can weigh a price rise against the customers it would lose. Decision: ADR 0016.
Policy: `configs/customer/value.toml` (pinned).

```text
marts (point in time: rows strictly before the snapshot date T)
  -> praxis.customer.warehouse   subscribers active at T, labels after T
  -> praxis.customer.features    customer_features.v1
  -> praxis.customer.dataset     non-overlapping 28-day windows
  -> praxis.customer.hazard      Poisson GLM (piecewise exponential) | tier rate table
  -> praxis.customer.train       rolling-origin backtest -> champion -> artifact
  -> praxis.customer.value       calibrated hazards, CLV and segment value at a decision date
  -> praxis.customer.monitor     segment report and alerts
  -> praxis.pricing              CLV penalty and the segment churn guardrails
```

## Definitions

| Term | Definition |
| --- | --- |
| Customer | Subscribed before T and not churned before T |
| Churn label | Voluntary churn in [T, T + 28 days). Involuntary (payment) churn ends exposure as a competing risk |
| Segment | The tier in force at T, never the current tier |
| Daily margin | (usage value - marginal cost) / 28 over the last 28 days, plus tier fee / 30 |
| CLV | margin x (1 - payment loss) x sum over t = 1..365 of 1.10^(-t/365) exp(-(h_vol + h_inv) t) |
| Bill share s_ip | Product p's share of the customer's usage value |
| Bill response gamma_p | d log(voluntary hazard) / d (s_ip x log price of p), from the randomised tests |

The champion between the GLM and the rate table is chosen by rolling-origin deviance, and every
backtest window is fitted on earlier windows only.

## Commands

```bash
make churn-response EL_SEED=1                # bill response from the experiment world
make pricing-data customer-train PX_SEED=1   # customer artifact on the shadow world's prefix
make customer-monitor PX_SEED=1              # segment report (exit 1 on an alert)
python -m praxis.customer --db WH train --start D --cutoff D --out ROOT
python -m praxis.customer --db WH monitor --model DIR --as-of D
```

## What the tests cover

- CLV against hand-calculated fixtures and the closed form.
- Labels, censoring, competing risks and point-in-time tier and fee, on a hand-built warehouse
  whose future rows are poisoned: deleting every row on or after T changes no feature.
- Artifact lineage and tamper detection.
- Bill response recovered on a known synthetic factorial.
- The long-horizon guardrails reject or shrink a raise that only improves immediate
  contribution (`tests/pricing/test_long_term.py`).
