# Dev-only policy sensitivity: selection rule (written 2026-11-05 BEFORE any variant ran)

Worlds: development pricing worlds 1, 101-105, 107, 109-124 (23), leave-one-out churn archives,
current code (fixed Phase 4 loader). Held-out worlds are not touched.

Variants (one parameter each, from configs/pricing/policy.toml):
  base: unchanged
  p80:  [uncertainty] min_prob_improvement 0.90 -> 0.80
  rn:   [uncertainty] risk_aversion 0.5 -> 0.0
  step10: [constraints] max_step 0.05 -> 0.10
  robust3: [evidence] churn_models without "no_pooling"

Rule: adopt the variant with the largest total dev true objective delta per day, subject to
(a) no dev world with true delta < 0 (no harm, per world), (b) pooled direction rate >= 0.80 over
all dev changes, (c) zero constraint violations / shadow executions / incomplete records in
every dev world, (d) stress fail-safe in every dev world. If no variant beats base on total
delta under (a)-(d), keep base. The adopted method is then frozen and pre-registered before any
fresh held-out world is built; base is evaluated alongside it on the same worlds as the
pre-registered comparison.

## Outcome (2026-11-05, after the runs)
Per the rule, step10 would have been adopted (+GBP 770.8/day vs 526.4, no harmed world,
direction 0.833). It is NOT adopted: max_step is a business guardrail, and
tests/pricing/test_config.py pins the standing owner instruction "Phase 9 may only ADD
constraints ... do NOT loosen policy.toml to make prices move" (PHASE6_LIMITS). The variant list
should not have contained a guardrail loosening; that was an error in the rule, found after
the results. Excluding it keeps the existing method (the conservative direction). It is
disclosed in docs/evidence/final-pricing-evaluation.md. p80 and robust3 each harmed one dev
world (123 and 111); rn is identical to base (risk aversion never binds). Adopted: base,
unchanged.
