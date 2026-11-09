# Simulator

The simulator is the instrument every model is judged against. It generates a usage-billed API
and compute business day by day from per-customer latent parameters that it keeps to itself.
Models only ever see the events; `praxis.science` compares their estimates with the hidden
truth afterwards. All of its output is synthetic.

```bash
python -m praxis.simulator --customers 1000 --days 56 --seed 42 --validate --out data/sim
```

A run writes `events.ndjson` (one event envelope per line, in canonical order),
`ground_truth.npz`, and `manifest.json` with the config hash, NumPy version, code revision and
a checksum of the stream. Runs are deterministic for a given seed and config; a golden checksum
test catches any unintended change.

## World model

For customer *i*, product *p* and day *t*:

```text
lambda_ipt = base_load_i * mix_ip / load_weight_p
             * season_i(t) * (1 + growth_i)^t * service_factor_i(t-1)
             * (price_ipt / ref_price_p)^elasticity_i * regional_spike(t)
requested ~ Poisson(Gamma(shape = dispersion_i, mean = lambda))
served    ~ Binomial(requested, throttle_ratio_region(t) * available_rp(t))
```

- Seasonality is a weekly cosine whose amplitude and peak day depend on the customer's
  industry.
- Regions have capacity. Utilisation drives latency, errors, throttling and marginal cost, and
  a bad service day lowers the next day's demand in proportion to the customer's sensitivity.
- Churn hazard depends on tier, price, service quality and payment failures. A final failed
  retry causes involuntary churn.
- Billing runs every 30 days. The first attempt succeeds with the customer's reliability, and
  retries on days +3 and +7 succeed with `0.35 + 0.5 * reliability`.
- Elasticity is a log-log slope applied causally, which is what makes it recoverable by a
  randomised experiment.

Latent parameters include base demand, elasticity, churn and service sensitivity, payment
reliability, growth, seasonality, dispersion, product mix and payment method. Two difficult
cohorts (`noisy_volume`, `risky_payer`) are built in deliberately.

## Events

| Event | Meaning |
| --- | --- |
| `customer.created` | A customer appears, at day 0 or later |
| `price.exposed` | A customer sees a price, with experiment id and arm when in a test |
| `conversion.observed` | A prospect converts or not |
| `subscription.started` / `.changed` | Tier and product set, or a tier change |
| `usage.observed` | Served and throttled units per product per day |
| `request.completed` | Daily requests, errors and p50 / p95 latency per customer |
| `invoice.created` | A monthly invoice in integer pence |
| `payment.attempted` / `.failed` / `.succeeded` | The attempt lifecycle; the last failure is marked `final` |
| `churn.observed` | Voluntary or involuntary churn |
| `service.metric_observed` | Hourly regional utilisation, latency, errors, availability, cost |

Envelopes are versioned (schemas in `schemas/events/`). `causation_id` chains a payment attempt
to its invoice, a result to its attempt, and involuntary churn to the final failure.

## Scenarios

Scenario files in `configs/simulator/scenarios/` add price changes, randomised price tests,
capacity shocks, demand spikes and outages on top of the default world. Test arms come from
`praxis.domain.experiments.assign_arm`, the same function the elasticity analysis uses to audit
logged arms. A `contamination_fraction` charges part of the control arm the treatment price
while logging it as control, to test the experiment-validity checks.

## Simplifications

- Customer dynamics are daily, with no intra-day price response.
- The simulator emits a clean, ordered stream; late, duplicate and out-of-order delivery is
  injected by the streaming layer's fault plan.
- Payment failure does not depend on amount or time of month.
- One currency (GBP), and customers never change region.
- Elasticity is constant and there are no cross-price effects.
