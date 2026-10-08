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

Add a new ADR whenever an architectural decision changes system behaviour.
