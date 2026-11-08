# Final pricing evaluation (2026-11-05): development sensitivity + fresh held-out worlds

All worlds are SYNTHETIC and every GBP figure is simulated. "True" means the change in the
objective, scored against the simulator's ground truth (contribution minus the cost of
churn).

## 1. Development (seeds 1 and 101-124; never held-out)
Code: the current method, including the Phase 4 loader fix. The selection rule was written
before the variants ran ([final-pricing-selection-rule.md](final-pricing-selection-rule.md)).
Data: [final-pricing-dev-sensitivity.json](final-pricing-dev-sensitivity.json).

| Variant (23 dev worlds each) | True delta, sum GBP/day | Changes | Right direction | Harmed worlds | Per-world failures |
| --- | --- | --- | --- | --- | --- |
| **base** (pinned policy) | +526.4 | 12 | 10 (0.83) | none | 102 (direction) |
| p80 (P(improve) >= 0.80) | +2,462.6 | 43 | 39 (0.91) | **123** | 102, 123 |
| rn (risk aversion 0) | +526.4 | 12 | 10 | none | 102 (identical to base) |
| step10 (max step 10 %) | +770.8 | 12 | 10 | none | 102 |
| robust3 (3 churn models) | +979.8 | 18 | 15 | **111** | 102, 111 |

The rule would have adopted step10. It was **excluded after the runs** because `max_step` is a
guardrail, and the owner's standing instruction (pinned as `PHASE6_LIMITS` in
`tests/pricing/test_config.py`) forbids loosening guardrails to make prices move. It should
not have been on the list. Because of this, **the method under test is the unchanged base**.

## 2. Held-out (seeds 901-905, registered and pinned before any was built, each run once)
Registration: `configs/pricing/evaluation_2026_11.toml` (ADR 0031). Scores:
[final-pricing-heldout-2026-11.json](final-pricing-heldout-2026-11.json), produced by
`scripts/pricing_heldout_report.py`. Per-world reports:
`data/pricing/seed90N-c1000/shadow/shadow.json`. Logs: `data/heldout-2026-11/`.

| Hypothesis | Result | Verdict |
| --- | --- | --- |
| H1 every world passes the per-world acceptance | 901 FAIL (direction 0.75), 905 FAIL (no harm and direction) | **FAIL** |
| H2 mean true delta > 0 with 95 % bootstrap CI lower bound > 0 | +34.3 GBP/day, CI [-113.9, +203.9]; 35 % of resamples <= 0 | **FAIL** |
| H3 at least 3 / 5 worlds change a price | 3 / 5 | PASS |

Per world: 901 +63.5 (4 changes), 902 +318.7 (5, all right), 903 0 and 904 0 (hold / freeze
only), 905 **-210.9** (6 changes, 1 right). The method captured 171 of 2,099 GBP/day of oracle
value. The naive unconstrained optimum would have lost about 1.95M GBP/day on the same
decisions.

**Why 905 lost.** All six changes were +4 % raises on `data_transfer_gb`. Each raised
contribution, but cost about 200 GBP/day in churn that the model predicted as about 0. This is
the price-driven churn identification problem from Phase 9 (seeds 44, 45 and 202), now on a
fresh world.

**Conclusion.** The pricing optimiser is safe against its hard constraints: 0 violations and
0 executions on every world. **It is not shown to beat static pricing, and it harmed 1 of 5
fresh worlds.** It stays in shadow mode. Nothing was tuned after this result. Seeds 901-905
are spent.
