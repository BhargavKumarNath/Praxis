# Praxis Simulator (Phase 1)

**All output is synthetic.** Run it with
`python -m praxis.simulator --customers 1000 --days 56 --seed 42 --validate --out data/sim`.
Outputs: `events.ndjson` (canonical, one envelope per line), `ground_truth.npz`,
`manifest.json` (provenance, checksum, config hash, numpy version, code revision).

## World model

Per customer *i*, product *p*, day *t* (served units are what is billed):

```text
lambda_ipt = base_load_i * mix_ip / load_weight_p
             * season_i(t) * (1 + growth_i)^t * service_factor_i(t-1)
             * (price_ipt / ref_price_p)^elasticity_i * regional_spike(t)
requested ~ Poisson(Gamma(shape = dispersion_i, mean = lambda))
served    ~ Binomial(requested, throttle_ratio_region(t) * available_rp(t))
```

* `season_i`: weekly cosine with per-customer amplitude and peak day (by industry).
* `service_factor`: demand falls with the previous day's latency degradation, scaled by
  `service_sensitivity`.
* Regional utilisation = load / capacity, evaluated hourly with a diurnal profile.
  Latency, error rate, throttling and marginal cost are functions of utilisation.
  Capacity is provisioned once from expected load and `target_utilization`.
* Churn hazard = tier base x exp(price, service and payment-failure terms scaled by the
  customer's churn / service sensitivity). Payment failures raise the hazard; a final
  failed retry causes involuntary churn.
* Billing every 30 days; first attempt succeeds with the customer's reliability; retries
  (days +3, +7) succeed with `0.35 + 0.5 * reliability`.
* Elasticity is a ground-truth log-log slope. It is the *causal* parameter the simulator
  applies, and the target for Phase 5 recovery.

## Ground truth (latent, never in events)

base demand, price elasticity, churn sensitivity, service sensitivity, payment
reliability, growth trend, seasonal amplitude and peak, dispersion, compute intensity,
product mix, payment method, tenure, and difficult-cohort flags (`noisy_volume`,
`risky_payer`).

## Event catalogue (envelope v1, payload v1)

| Event | Entity | Meaning |
| --- | --- | --- |
| `customer.created` | customer | Customer appears (existing at day 0, or joins later) |
| `price.exposed` | customer | Customer sees a unit price; carries experiment id and arm when in an experiment |
| `conversion.observed` | customer | New customer converts or not |
| `subscription.started` / `.changed` | customer | Tier and product set / tier change |
| `usage.observed` | customer | Served and throttled units per product per day |
| `request.completed` | customer | Daily request count, errors, p50 / p95 latency |
| `invoice.created` | customer | Monthly invoice (integer pence) |
| `payment.attempted` / `.failed` / `.succeeded` | customer | Attempt lifecycle, `final` flag on last failure |
| `churn.observed` | customer | Voluntary or involuntary (payment) churn |
| `service.metric_observed` | region | Hourly utilisation, latency, errors, availability, marginal cost |

Causation: `payment.attempted.causation_id` is the invoice (or previous failure);
results point at the attempt; involuntary churn points at the final failure.
Trace / correlation IDs are shared by a business flow (customer lifecycle, usage day,
invoice, region-day).

## State machines

See `src/praxis/domain/states.py`. Customer: `prospect -> converted -> active ->
churned`, `prospect -> lost`, and `prospect -> active` for pre-existing customers.
Invoice: `open -> attempting -> {open (retry), paid, uncollectible}`. Payment events may
complete after a customer churns (settlement); usage may not. Exact dunning states for
Phase 8 are left to a later ADR.

## Scenarios

`pricing.interventions`, `pricing.price_changes`, `infrastructure.capacity_shocks`,
`demand_spikes` and `product_outages` are empty in the default world; tests and later
phases add them. Overlapping interventions on one product are rejected.

## Known simplifications

* Customer dynamics are daily; no intra-day price response.
* `request.completed` is aggregated per customer-day, not per request.
* No late, duplicate or out-of-order delivery yet (Phase 3).
* Payment failures do not depend on invoice amount or time of month.
* A single currency (GBP); customers do not move region.
* Control-arm exposure is logged at experiment start; treated customers' price revert
  is logged as an ordinary price change.
