# Praxis documentation

Start with [architecture.md](architecture.md) for how the pieces fit, and
[results.md](results.md) for what was measured, including what failed.

| Document | Covers |
| --- | --- |
| [architecture.md](architecture.md) | The decision loop, state, layers, boundaries, lineage, logging |
| [results.md](results.md) | Every experiment, scale run and failure, linked to its evidence |
| [development.md](development.md) | Setup, the CI gate, tests, seeds, reproducing results |
| [simulator.md](simulator.md) | The synthetic world and its hidden ground truth |
| [data-platform.md](data-platform.md) | Warehouse, dbt, external data and provenance |
| [event-backbone.md](event-backbone.md) | Pub/Sub, idempotent consumers, dead letters, replay |
| [forecasting.md](forecasting.md) | Demand forecasting and its serving API |
| [elasticity.md](elasticity.md) | Price elasticity from randomised tests |
| [customer-value.md](customer-value.md) | Churn hazard and lifetime value |
| [pricing.md](pricing.md) | The constrained optimiser, guardrails and decision records |
| [payments.md](payments.md) | `PaymentGateway`, Stripe webhooks, the payments inbox |
| [recovery.md](recovery.md) | Failed-payment retries planned by a survival model |
| [mlops.md](mlops.md) | Model registry, drift, retraining and promotion |
| [observability.md](observability.md) | Metrics, SLOs, alerts and incidents |
| [agent.md](agent.md) | The AI operator and its permission layer |
| [dashboard.md](dashboard.md) | The decision console and its design |
| [deployment.md](deployment.md) | GCP runbook, the live loop, security posture |
| [cost-guard.md](cost-guard.md) | Cost rules, safeguards and what it costs |

Reference material:

- [adr/](adr/README.md): architecture decision records, including every amendment made after a
  failed result.
- [runbooks/](runbooks/README.md): one runbook per alert.
- [evidence/](evidence/): the raw reports behind every number, kept as written at the time.
  Older evidence files may mention documents that have since been merged.
