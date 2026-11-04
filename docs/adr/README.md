# Architecture Decision Records

| ADR | Title |
| --- | --- |
| [0001](0001-pubsub-instead-of-kafka.md) | Pub/Sub instead of Kafka |
| [0002](0002-postgres-control-plane-bigquery-gcs-analytical-plane.md) | Postgres control plane, BigQuery and GCS analytical plane |
| [0003](0003-payment-gateway-abstraction.md) | Stripe behind a `PaymentGateway` abstraction |
| [0004](0004-llm-does-not-own-pricing.md) | The LLM does not own pricing decisions |
| [0005](0005-continuous-training-without-automatic-promotion.md) | Continuous training does not imply automatic promotion |
| [0006](0006-separate-synthetic-scale-from-stripe-integration.md) | Separate synthetic scale from real Stripe integration |
| [0007](0007-deterministic-daily-step-simulator.md) | Deterministic daily-step simulator with retained ground truth |
| [0008](0008-local-first-data-platform.md) | Local-first data platform (DuckDB + dbt), raw archive first |
| [0009](0009-order-independent-event-processing.md) | Order-independent, idempotent event processing |
| [0010](0010-daily-demand-forecasting.md) | Daily probabilistic demand forecasting with pre-registered acceptance |
| [0011](0011-hybrid-demand-champion.md) | Hybrid demand champion: ridge mean + calibrated LightGBM quantiles |
| [0012](0012-randomised-price-tests-and-hierarchical-elasticity.md) | Elasticity from randomised price tests, log-log slopes and a hierarchical Bayesian model |
| [0013](0013-constrained-pricing-optimiser.md) | Constrained pricing optimiser: causal response, hard constraints, audited shadow decisions |
| [0014](0014-stripe-webhooks-inbox-and-state-refetch.md) | Stripe integration: signed webhook inbox, state re-fetch, shared event contracts |
| [0015](0015-dunning-states-recovery-models-and-retry-scheduling.md) | Dunning states, recovery models and scheduled retries |
| [0016](0016-customer-value-and-long-horizon-guardrails.md) | Customer value, churn hazard and long-horizon pricing guardrails |
| [0017](0017-model-registry-continuous-training-and-safe-promotion.md) | Model registry (control plane, MLflow mirror), continuous training, drift and safe promotion |
| [0018](0018-telemetry-slos-alerts-and-incidents.md) | Telemetry (OpenTelemetry), SLOs, alert rules, incident flow and runbooks |
| [0019](0019-constrained-ai-operator.md) | Constrained AI Revenue Operator: typed tools, permission tiers, guarded requests, price freeze |
| [0020](0020-cross-process-metrics-and-ops-monitor.md) | Cross-process metric snapshots, pull collectors, the ops monitor process |
| [0021](0021-decision-console-and-admin-boundary.md) | Decision console: separate process, panel read model, server-side approver boundary |
| [0022](0022-scale-protocol-sharded-consumers-and-batching.md) | Scale protocol (archive + sharded replay + oracle), consumer shards, sink micro-batching, counter aggregation |
| [0023](0023-resilience-suite-and-two-window-alerting.md) | Pre-registered resilience suite, two-window alerting, detection fixes, redrive_all; amendments: remediation suite on fault seeds 802, 803 and 804 |
| [0024](0024-cloud-security-and-cost-posture.md) | Per-process identities, secret isolation, private console, Cloud Run caps, budget and BigQuery quota, mocked destroy/recreate |
| [0025](0025-crash-isolating-consumer-flow-control.md) | Crash-isolating consumer flow control: slow-start lease window, evidence tiers, repeated redeliveries leased alone; am. 1: lookup + plain-insert warehouse writes (amends 0009) |
| [0026](0026-gcp-deployment-topology-and-spend-guard.md) | GCP deployment: us-central1 compute + europe-west2 BigQuery, Free Tier e2-micro Postgres (no external IP), Direct VPC egress, private services, jobs, GBP 10 budget + spend guard |
| [0027](0027-live-loop.md) | The live loop: one synthetic day per real day, daily scheduled tick (world rebuild, warehouse, model lifecycle), registry-served champion, live GCS volume; promotion stays human |

Add a new ADR whenever an architectural decision changes system behaviour.
