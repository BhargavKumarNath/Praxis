# Console access through the Cloud Run proxy (verified 2026-11-05)

Phase 16 left this open: `docs/deployment.md` marked the browser path as unverified, and it was
unknown whether the proxy passes the approver's `Authorization: Bearer` header through.

Command: `gcloud run services proxy praxis-dev-console --region us-central1 --project
praxis-dev-510522 --port 3017` (gcloud 591.0.0, `cloud-run-proxy` component installed). The
proxy authenticates to the private service with the caller's Google identity. Probes
(`curl` to `http://127.0.0.1:3017`):

| Request | Result | Meaning |
| --- | --- | --- |
| `GET /` | 307, 15.5 s (cold start) | the Next.js UI is served |
| `GET /api/console/executive` | 200, 5.4 s | read panels work (SYNTHETIC data label present) |
| `GET /api/console/system` | 200, 0.30 s | |
| `GET /api/console/mlops` | 200, 0.31 s | |
| `GET /api/console/whoami` with no token | 401 `unauthorized` | the admin boundary is enforced |
| `GET /api/console/whoami` with a wrong token | 401 `unauthorized` | |
| `GET /api/console/whoami` with the approver token from Secret Manager | **200 `{"approver": "owner"}`** | **the proxy passes `Authorization` through unchanged**, so approve / reject from the browser work |
| `GET /api/console/operator` | 200 | lists the PENDING promotion request `req_a680c42f35b5a82a` (target `demand-hybrid-84d53ff98b99`, requested by `praxis-agent`, 2026-11-04 18:40 UTC) |

No write was made. Nothing was approved, rejected or lifted. The token was held in a shell
variable for the one `whoami` call and never printed or stored. The proxy was stopped afterwards.
