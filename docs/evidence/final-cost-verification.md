# Cloud cost verification (2026-11-05, read-only)

Project `praxis-dev-510522`, billing account linked (`billingEnabled: true`). Everything below
was read with `gcloud` (no write, no new resource, no spend).

| Check | Source | Result |
| --- | --- | --- |
| Budget exists, scoped to this project only | `gcloud billing budgets list` | `praxis-dev-monthly`, GBP 10, thresholds 20 / 50 / 80 / 100 % of current spend + 100 % forecast, notifications to `praxis-dev-budget-alerts` |
| Spend reported by Google Billing | the budget notifications received by `praxis-dev-spend-guard` (Cloud Logging, `jsonPayload.cost_gbp`) | **GBP 0.65 month-to-date** (November 2026) at 2026-11-05 22:39 UTC; previous notifications 0.64 (21:57), 0.65 (22:18) |
| Spend guard decision | same log lines | `action: hold`, `cost GBP 0.65 < guard GBP 8` (guard unlinks billing at GBP 8; ADR 0026) |
| Scheduled jobs ran | `gcloud run jobs executions list` | `praxis-dev-live` 2026-11-05 03:13 UTC succeeded; `praxis-dev-ops` 03:52 succeeded |

Limits of this check: the figure is Google's budget-notification `costAmount` (it lags actual
usage by hours and is not an invoice). No BigQuery billing export is configured, so there is no
per-service breakdown; the GBP 10 ceiling and the GBP 8 guard are the controls. A budget is not
a hard cap; the spend guard is the enforcement (ADR 0026).
