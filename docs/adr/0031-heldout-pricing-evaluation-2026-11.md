# ADR 0031: Fresh held-out pricing evaluation (2026-11): registration and result

Status: Accepted (2026-11-05, owner-delegated: "a fresh pre-registered pricing evaluation").
Result: **FAIL (H1, H2), PASS (H3)**. Evidence: `docs/evidence/final-pricing-evaluation.md`,
`docs/evidence/final-pricing-heldout-2026-11.json`.

## Context
Phase 9 passed only on "safe" worlds: no held-out price moved. Phase 16 left a fresh evaluation
of whether the method beats static pricing as an open decision.

## Decision (written before any world was built)
* **Method:** the Phase 9 final method, unchanged. `configs/pricing/policy.toml` is pinned
  (sha256 0931da35...).
* **Development sensitivity:** five variants on development worlds 1 and 101-124. A selection
  rule was written before the runs (`data/sweep-p17/SELECTION_RULE.md`). The rule would have
  picked a 10 % max step. That variant was **excluded** because it loosens a guardrail, which
  the standing owner instruction forbids, so the base method was kept. The exclusion is
  disclosed.
* **Registration:** `configs/pricing/evaluation_2026_11.toml`, pinned by
  `tests/pricing/test_evaluation.py`. It covers seeds 901-905, all fresh, and three hypotheses:
  * **H1 safety:** every world passes the unchanged per-world acceptance.
  * **H2 value:** the mean true objective delta per world-day is above 0, and the lower
    bound of its 95 % bootstrap CI is above 0.
  * **H3 decisions:** at least 3 of the 5 worlds change a price.
* Each world was built once and evaluated once.

## Result
| Seed | Changes | Right direction | True delta (GBP/day, SYNTHETIC) | Oracle | Per-world gate |
| --- | --- | --- | --- | --- | --- |
| 901 | 4 | 3 (0.75) | +63.5 | 487.6 | FAIL (direction < 0.80) |
| 902 | 5 | 5 | +318.7 | 1,029.7 | PASS |
| 903 | 0 | - | 0.0 | 218.2 | PASS |
| 904 | 0 | - | 0.0 | 158.0 | PASS |
| 905 | 6 | 1 | **-210.9** | 205.8 | FAIL (no harm, direction) |

* H1: FAIL (901, 905).
* H2: mean +34.3 GBP/day, 95 % CI [-113.9, +203.9]. FAIL: not shown to beat static.
* H3: 3 worlds changed a price. PASS.

Pooled over the five worlds: 15 changes, 9 in the right direction, +171 GBP/day of a
2,099 GBP/day oracle. The naive unconstrained point-estimate optimum scores about
-1.95M GBP/day, so the guardrails remove almost all of the naive method's damage.

**Cause of the 905 loss.** All six changes are +4 % raises on `data_transfer_gb`. Each raised
true contribution by 155 to 200 GBP/day. The objective was nonetheless negative in five of
the six, because the true churn cost was about 200 GBP/day and the predicted churn cost was
about 0. This is the failure seen on Phase 9 seeds 44, 45 and 202: price-driven churn is
under-identified, and this world's churn response is stronger than the churn models' range.

## Consequences
* The optimiser is **not** shown to be profitable on held-out worlds, and it harmed one of
  five. It stays in shadow mode. Execution stays disabled, which is the pinned default.
* Seeds 901-905 are spent. Any method change needs dev-only development and new fresh
  seeds (906+ are reserved as replacement seeds of this registration and are not used).
* Candidate fixes for a future registration (not done here):
  * a churn-response identification design per product (a churn-aware experiment arm);
  * a predicted-churn floor tied to the experiment's churn-response CI rather than its
    point estimate.
