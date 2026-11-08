# Architecture

Praxis is a closed-loop decision system for a **simulated** API and cloud-compute business. Every
customer, invoice and amount below is synthetic unless the diagram labels it otherwise. The
binding contract is [`docs/architecture.md`](../architecture.md). The decisions behind it are the
ADRs in [`docs/adr/`](../adr/README.md).

## 1. Logical architecture (the decision loop)

```mermaid
flowchart LR
    subgraph Sources
        SIM["Simulator (SYNTHETIC)<br/>seeded customers, latent truth kept apart"]
        EXT["External signals (REAL)<br/>Open-Meteo, NESO, FRED, EIA"]
        PAY["PaymentGateway<br/>Synthetic | Stripe Sandbox"]
    end

    SIM --> PROD["Producer<br/>validate, archive, publish"]
    EXT --> ING["Ingest<br/>contract check, quarantine"]
    PAY --> WH["Webhook inbox<br/>signature on raw body, dedupe"]

    PROD --> RAW[("Raw archive<br/>content-addressed, replayable")]
    ING --> RAW
    PROD --> BUS{{"Event bus<br/>Pub/Sub, at-least-once"}}
    WH --> BUS
    BUS --> OPC["Operational consumer"]
    BUS --> WHC["Warehouse consumer"]
    BUS --> MON["Monitoring consumer"]
    BUS -. "poison / exhausted" .-> DLQ{{"Dead letters + redrive"}}

    OPC --> PG[("Postgres control plane<br/>projections, ledger, decisions, audit")]
    WHC --> DW[("Warehouse<br/>DuckDB locally, BigQuery in cloud")]
    ING --> DW
    DW --> DBT["dbt marts + features"]

    DBT --> FC["Demand forecaster<br/>ridge + calibrated LightGBM quantiles"]
    DBT --> EL["Elasticity<br/>hierarchical Bayes on randomised tests"]
    DBT --> CV["Churn hazard + CLV"]
    DBT --> RM["Recovery model<br/>mixture-cure Weibull"]

    FC --> OPT["Constrained optimiser"]
    EL --> OPT
    CV --> OPT
    OPT --> GR["Hard guardrails + uncertainty freeze"]
    GR --> REC["Decision record (Postgres trigger:<br/>nothing executes unrecorded)"]
    REC --> EXE["shadow | recommend | execute"]

    RM --> DUN["Dunning planner"]
    DUN --> TASKS["Cloud Tasks retries<br/>idempotency key per attempt"]
    TASKS --> PAY

    REG[("Model registry<br/>champion / challenger / shadow")] --> FC
    MLO["MLOps lifecycle<br/>drift, retrain, validate, shadow, probation"] --> REG

    MON --> OBS["Telemetry, SLOs, alerts, incidents"]
    OBS --> AG["AI operator<br/>typed, audited tools"]
    AG -. "PENDING request" .-> CON["Decision console<br/>named human approves"]
    CON --> REG
    CON --> GR
    EXE --> BUS
```

The loop closes when executed decisions and payment outcomes return to the bus as events.
Ground truth from the simulator never enters this loop. Only `praxis.science` joins model
output with truth, and an import contract stops model packages from reaching it (ADR 0012).

## 2. Layers (enforced by `make arch`, import-linter)

```mermaid
flowchart TB
    L1["console"] --> L2["api | science | agent"]
    L2 --> L3["dunning"]
    L3 --> L4["pricing | payments | mlops"]
    L4 --> L5["streaming | forecasting | elasticity | recovery | customer | ops"]
    L5 --> L6["control | data | simulator"]
    L6 --> L7["events"]
    L7 --> L8["domain"]
    L8 --> L9["foundation: config, logging, tracing, provenance, errors, observability, glm, telemetry"]
```

Additional contracts:
- Models never import the simulator.
- The AI operator cannot reach the optimiser, payment execution, MLOps training, streaming,
  the API or `subprocess`.
- The console cannot set prices, execute payments or reach ground truth.
- Nothing in the core imports `praxis.agent`.

## 3. Deployed topology (`praxis-dev-510522`, ADR 0026 / 0027)

```mermaid
flowchart LR
    subgraph us-central1
        subgraph "Cloud Run (min 0, capped)"
            API["praxis-dev-api<br/>private (IAM)"]
            CON["praxis-dev-console<br/>UI + API sidecar, private"]
            GUARD["praxis-dev-guard<br/>spend guard"]
            JOBS["Jobs: ops, streaming,<br/>live (4 vCPU, <= 4 h)"]
        end
        SCHED["Cloud Scheduler<br/>live 03:10 UTC, ops 03:50 UTC"]
        VM[("Postgres 17<br/>Free Tier e2-micro, no external IP")]
        PS{{"Pub/Sub topics + DLQs"}}
        GCS[("GCS raw archive<br/>+ live volume")]
        AR["Artifact Registry"]
        TASKS["Cloud Tasks queue"]
    end
    subgraph europe-west2
        BQ[("BigQuery datasets<br/>(Phase 2 sandbox)")]
    end
    BUDGET["Budget GBP 10<br/>alerts 2 / 5 / 8 / 10"] --> PS
    PS --> GUARD
    GUARD -. "unlink billing at GBP 8" .-> BILL["Billing account"]
    SCHED --> JOBS
    API --> VM
    CON --> VM
    JOBS --> VM
    JOBS --> GCS
    JOBS --> PS
    API --> GCS
```

- Cloud Run reaches the VM over Direct VPC egress. There is no NAT, load balancer or public
  address.
- One service account per process, with resource-level grants only. The one project-level
  grant is the spend guard's `roles/billing.projectManager`.
- Secrets live in Secret Manager. Values never appear in Terraform or its state.

Evidence: [`deployment.md`](../evidence/deployment.md), the
[smoke report](../evidence/deployment-smoke.json) (46 / 46),
[`live-loop.md`](../evidence/live-loop.md) and the
[live smoke](../evidence/live-deployment-smoke.json) (49 / 49).
