# Runbook: Business KPI shock or unusual price volatility

Alerts: business_kpi_shock, price_volatility

## Trigger

A KPI deviates more than 30% from its trailing median, or more than half of the products changed price in one cycle.

## Impact

Either the world changed (real shock) or a model or data fault is moving prices. Revenue, churn and payment loss may follow.

## Diagnose

1. List the cycle's decisions: `uv run python -m praxis.pricing show --database-url <url> --decision-id <id>`; look at reason codes and policy version.
2. Check the guardrail bounds (`max_step`, cooldown) held; a fault shows as many CHANGE decisions with similar reasons.
3. Check for a data or model incident at the same time (`GET /v1/ops/health`): stale or broken inputs first, then a recent promotion.

## Mitigate

1. Set the policy to `shadow` (config change, then redeploy); no price is executed from a shadow cycle.
2. Pause execution of recorded decisions you do not trust; decisions are append-only audit rows, do not edit them.
3. If the shock is real demand change, let the monitors and retraining run their course.

## Verify

1. Deviation or change fraction back under threshold across two evaluations.
2. Post-incident note: what changed, which signal caught it, how long it took.

## Escalate

Owner before any resumption of `execute` mode.
