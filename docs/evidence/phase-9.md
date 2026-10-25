# Phase 9 Evidence Report

```text
Phase: 9 - Customer Value and Long-Horizon Guardrails
Date: 2026-10-10 (final: 2026-10-11)
Code revision: 9526243 + working tree (uncommitted; no commits made by Claude)
Environment: Linux, Python 3.12, numpy 2.5.3, DuckDB + dbt (2 new marts), Postgres 17 (local
             Docker), Pub/Sub emulator (local Docker), PyMC (Phase 5 analyses of seed 44)
All customers, prices, churn and values are SYNTHETIC. No cloud resource was created or used.
No price was executed outside tests.
```

Design: `docs/customer-value.md`, ADR 0016 (+ ADR 0013 third amendment), `docs/pricing.md`.
Raw reports: `phase-9-eval-seed44-shadow.json` (held-out, run once: FAIL),
`phase-9-seed42-controlled-shadow.json` + `phase-9-seed42-controlled-compare.json` (controlled
example), `phase-9-dev-seed1-shadow.json` (development), `phase-9-eval-seed44-churn-response.json`,
`phase-9-eval-seed44-customer-train.json`, `phase-9-eval-seed44-customer-monitor.json`,
`phase-9-artifact-manifest.json`, `phase-9-seed44-elasticity-recovery.json`.

## Disclosure added 2026-10-25: Phase 4 artifact loader defect (found in Phase 10)

The results below were measured with a defective Phase 4 demand-artifact loader:
`RidgeBaseline.from_state` rebuilt the one-hot blocks in sorted JSON key order instead of fit
order, so every LOADED demand artifact applied its categorical coefficients to the wrong
columns. The pricing input builder loads that artifact, so this phase's shadow evaluations
used the affected forecasts. Size on the development world (seed 1, 8-week pricing window):
loaded-artifact vWAPE 0.1623 with the defect vs 0.1558 fixed; bias -0.3% in both. Fixed in
Phase 10 (ADR 0017 "Defect found in Phase 4 serving"; regression test, `serving_parity`).

Not measured: whether any held-out pricing decision would differ with the fixed loader. The
spent held-out seeds were NOT re-run (that would turn them into a tuning set). Owner decision
2026-10-25: disclose, do not re-evaluate now; the gate below stands AS MEASURED WITH THE
DEFECT. A fresh pre-registered evaluation on new seeds is required before these results are
relied on for live pricing.

## Gate (final, 2026-10-11, third amendment): PASS

| Binding gate | Source | Result |
| --- | --- | --- |
| CLV calculation validated on controlled examples | project_plan | PASS (`tests/customer/test_clv.py`; details below) |
| churn leakage checks pass | project_plan | PASS (poisoned-future warehouse; `tests/customer/`) |
| long-term guardrails demonstrably alter unsafe short-term decisions | project_plan | PASS (unit cases + seed-42 controlled example re-run with the FINAL method: 17 / 17 Phase 6 changes altered, 13 / 13 losing; 32 hold, 8 frozen, all gated checks pass; `phase-9-seed42-controlled-*.json`) |
| segment monitoring exists | project_plan | PASS (`make customer-monitor`) |
| required_test.md s15 (CLV fixtures, label, window, leakage, segments, sensitivity, decision test) | required_test | PASS |
| held-out shadow re-evaluation, unchanged `shadow_acceptance.toml`, all of 301 / 302 / 303 | owner (MUST FIX) + ADR 0016 pre-registration | **PASS 3 / 3** |

| Held-out world (run once) | Result | Decisions | Best feasible, true GBP / day | Captured |
| --- | --- | --- | --- | --- |
| 301 | PASS | 32 hold, 8 frozen | 162 | 0 |
| 302 | PASS | 23 hold, 17 frozen | 291 | 0 |
| 303 | PASS | 21 hold, 19 frozen | 1,038 | 0 |

**Read this before quoting the result.** All three held-out passes are holds and freezes: no
held-out price moved, and none of the GBP 162-1,038 / day available was captured. Change share
and captured share are reported, not gated (ADR 0013 amendment, owner-approved). Value-adding
moves are demonstrated on DEVELOPMENT worlds only (leave-one-out, 22 worlds: 21 / 22 pass,
15 changes, 13 right, +GBP 804 / day; `phase-9-dev-loo-sweep.json`). The owner's MUST FIX hope
of "confident, value-adding changes" is therefore met in development, not shown held-out.

Method (ADR 0016 third amendment): the 202 failure was a single noisy churn test read at face
value (optimizer's curse). A robust meta-analytic-predictive prior from the 25 development
experiments' ESTIMATES (never truth, never an evaluation seed) joined the robust churn class,
so a raise must also be confident under the archive. Strictly more conservative; no threshold
changed. Raw: `phase-9-heldout-seed{301,302,303}-{shadow,churn-response}.json`,
`phase-9-heldout3-summary.json`, `phase-9-dev-loo-sweep.json` (dev runs used per-world
leave-one-out archives, otherwise the adopted policy).

## Earlier gate (2026-10-11, second amendment): FAIL

**FAIL.** After the owner asked for a full fix, the method was rebuilt (ADR 0016 second
amendment: full-Bayes pooling, robust maxmin over pooling models, Laspeyres exposure, exact bill
regressor, book-level elasticity uncertainty, trend hazard calibration), developed on 22 fresh
worlds (21 / 22 pass, 22 changes / 19 right, +GBP 1,076 / day, response z sd 0.6-0.7, 90%
coverage 1.00), frozen, and evaluated once on three pre-registered held-out worlds:

| Held-out world | Result | Changes (right) | True delta / day | Failed checks |
| --- | --- | --- | --- | --- |
| 201 | PASS | 0 | 0 | - |
| 202 | **FAIL** | 9 (3) | -GBP 49 | no_harm, direction |
| 203 | PASS | 0 | 0 | - |

Every safety check passed in all three worlds. The 202 failure: its experiment drew all five churn
responses near zero (api -0.68, data -0.78; pooled ~0.1 +- 1.0 vs truth ~1.5), so every model of the
robust class agreed churn barely responds and api / data raises were taken (true -83..+24 / day).
Remaining weakness: absence of evidence of a churn effect is treated as evidence of absence; the
fix needs an informative prior on the bill response from outside one experiment (owner decision).
Raw: `phase-9-heldout-seed{201,202,203}-shadow.json`, `phase-9-heldout-summary.json`.

Earlier history:

**FAIL: stopped for the owner (twice).** Second held-out run after the owner-approved
amendment (ADR 0016, `min_bill_share`): **seed 45 FAILED `direction` only** (4 / 7 changes
improved; no harm +GBP 13.9 / day; every safety check passed). Details in "Amendment and seed
45" below. The first run is described next.

First run: The four project_plan Phase 9 gate items pass. The owner's
"PHASE 9 MUST FIX" acceptance, the pre-registered held-out shadow re-evaluation on seed 44,
**FAILED** two gated checks (`no_harm`, `direction`): the only 5 changes (gpu_minutes +4.9%)
all lost value in truth. The cause was named in ADR 0016 before the run as a known
misspecification. Seed 44 is spent; nothing was re-run or tuned.

| project_plan Phase 9 gate | Result |
| --- | --- |
| CLV calculation validated on controlled examples | PASS: hand-calculated fixtures (halving survival = 87.5% of margin over 3 days; closed form = explicit sum to 1e-12; a year of daily discounting = 1 / (1 + r); no churn no discount = m x H; negative margin; invalid inputs), sensitivity grid over 4 discount rates x 5 horizons (`tests/customer/test_clv.py`) |
| churn leakage checks pass | PASS: hand-built warehouse whose future is poisoned (x100 usage, failures, broken service, future tier changes, future price changes, future customers): every snapshot value exact; deleting every row on or after T changes no feature; an attempt resolved after T is unseen; `current_tier` never read; every backtest window fitted on windows that end before it (`tests/customer/test_warehouse.py`, `test_models.py`) |
| long-term guardrails demonstrably alter unsafe short-term decisions | PASS: (1) unit cases: a raise that improves immediate contribution is rejected by the CLV penalty, shrunk by the segment guardrail, blocked by the churn-risk ceiling and by the price-change frequency (`tests/pricing/test_long_term.py`); (2) **controlled example, seed 42**: the Phase 6 method made 17 changes, 13 lost value (-GBP 3,526 / day); the Phase 9 method on the same worlds altered all 17 (13 / 13 losing), 0 changes, every gated check passes (true delta 0) |
| segment monitoring exists | PASS: `python -m praxis.customer monitor` / `make customer-monitor`: per segment churn vs expectation, 90-day retention, CLV + sensitivity, price-change exposure, alerts as structured logs (seed 44 at the first cycle: 1 alert, enterprise observed / expected 1.66) |
| **owner: held-out shadow re-evaluation (seed 44)** | **FAIL**: no harm -GBP 81 / day (>= 0 required); direction 0 / 5 (>= 0.80); every safety check passes |

required_test.md s15: CLV fixtures, churn label definition (voluntary in [T, T+28d); involuntary
= competing risk; days at risk incl. the churn day), observation window (censoring at 7 vs 28
days), leakage (above), segment behaviour (segment values ordered; hazards by tier vs truth
below), sensitivity to discount / horizon (grid + monotonicity), and the decision test (above).

## Protocol (in order)

1. Pre-registered before any model ran: `configs/customer/value.toml` (pinned).
2. Development on seed 1 only (pricing world prefix, experiment world). Found and fixed on the
   way: unconverted prospects counted as customers; the snapshot builder's exactness (integer
   sums; regression test with 2^53 + 1 + 1).
3. Power analysis on a scratch dev world (84-day test): SE 0.86 -> 0.51 but still no confident
   dev raise; no powered experiment added (ADR 0016).
4. ADR 0016 written, policy amended (`[long_term]` added, nothing loosened, pinned by a test),
   seed 44 registered in `shadow_acceptance.toml` (comment only), all before seed 44 existed.
5. Seed 44 built through every gate in order: experiment world (Phase 5 validity + Bayesian
   gates PASS, artifact `elasticity-hier-d57dbc96dd9b`), churn response, forecast world (Phase 4
   backtest acceptance 11 / 11 PASS, vWAPE ratio 0.786), pricing world, customer model on the
   196-day prefix (`churn-table-dffb7210a62d`). Shadow evaluation run once: **FAIL**.
6. After seed 44: the seed-42 controlled example (old Phase-6-era marts rebuilt with dbt from
   the same raw events; forecast lineage check passed) and `pricing-compare`.

## Results: held-out (seed 44, run once)

40 decisions: 5 change (gpu_minutes x5, `max_step`), 7 frozen (P(improve) < 0.90), 28 hold.

| Check (gated) | Result | Threshold |
| --- | --- | --- |
| constraint compliance (incl. the Phase 9 guardrails, re-derived from inputs) | 0 violations | 0 |
| shadow executions | 0 | 0 |
| complete records (incl. customer / churn-response lineage) | 40 / 40 | all |
| forecast lineage | prefix = artifact | required |
| **no harm** | **-GBP 81.4 per day** | >= 0 |
| **direction** | **0 / 5** | >= 0.80 |
| prediction calibration (contribution) | 0.97 | [0.5, 2] |
| churn guardrail in truth | max 0.08 pp per exposed customer | <= 0.5 pp |
| stress cases (6, incl. customer model unavailable) | 6 / 6 | all |

Reported: change share 0.125; captured share -0.05; best feasible candidates +GBP 1,667 per
day in truth (missed); naive unconstrained baseline -GBP 287 k per day. Churn prediction
(+max-step candidates, all products) predicted / true extra churners 0.91. Binding at +max
step: extrapolation limit 8, price-change frequency 5 (first cycle). Bill response pooled
1.83 +- 0.82 (247 voluntary events, heterogeneity p 0.46).

### Why it failed (diagnosed on the held-out truth after the run; nothing re-run)

| gpu +4.9% (per day, 5 cycles) | Predicted | True |
| --- | --- | --- |
| contribution change | +GBP 30-33 | +GBP 31-33 |
| value of extra churners | ~GBP 17 | ~GBP 46-50 |
| objective | +GBP 13-14 | -GBP 15 to -17 |

Contribution was right. The churn cost of gpu was understated ~2.7x. The bill-shock model
applies the response to the product's share of each customer's BILL; in the simulator churn
weights each product by a latent usage-mix share, and gpu is ~2% of bills but a larger share
of the mix. ADR 0016 recorded this before the run ("gpu's response is understated by ~2x on
development truth ... reported, not corrected"); on dev gpu froze at P(improve) ~0.7, on seed
44 the pooled draw and gpu's own (8.2 +- 7.4) estimate pushed it over 0.90. All other products
held, and overall churn prediction was honest (0.91). The loss is small in money (GBP 81 per
day against GBP 1,667 missed), but the gate is no harm and direction, and it failed.

### Customer model vs truth (seed 44, reported)
Champion: tier rate table (GLM backtest deviance 741.8 vs table 731.0). Calibrated hazards vs
the TRUE latent hazards of the same customers: log correlation 0.78-0.83 every cycle; tier
ratios starter 0.85-1.40, growth 0.74-1.20, enterprise 1.31-2.18 (over-predicts enterprise:
conservative); calibration factor 0.97 -> 1.80 as service degrades.

## Amendment and seed 45 (owner chose option 1, 2026-10-10)

`evidence.min_bill_share = 0.05`: a product under 5% of the experiment customers' bills (gpu,
2.1-2.2% in every world; all others >= 14.7%) has no identified churn response and is FROZEN
(`churn_response_unidentified`). Dev (seed 1): all checks pass, 32 hold / 8 frozen. Seed 45
pre-registered (ADR 0016, `shadow_acceptance.toml`) before it was built; experiment world
(Phase 5 gates PASS, `elasticity-hier-c6660c249c43`), forecast world (Phase 4 acceptance 11 / 11,
vWAPE ratio 0.766), pricing world, customer model on the prefix; one shadow run.

| Check (gated) | Seed 45 | Threshold |
| --- | --- | --- |
| constraint compliance / no execution / records / lineage / churn in truth / stress (6) | all PASS | |
| no harm | +GBP 13.9 per day | >= 0 |
| **direction** | **4 / 7 = 0.57** | >= 0.80 |
| prediction calibration (contribution) | 1.05 | [0.5, 2] |

40 decisions: 7 change (data transfer x4: true +2 .. +40 / day; cpu x3: true -31 .. -34 / day),
17 frozen (8 gpu `churn_response_unidentified`, 9 low confidence), 16 hold. Captured share 0.02
of +GBP 654 / day available. Predicted / true extra churners 0.79. The churn-risk ceiling bound
16 times.

**Why direction failed (truth, after the run):** the pooled bill response on seed 45 is a low draw
(1.26 +- 0.86; dev 2.8, seed 44 1.83). cpu's own test read 5.2 +- 3.1, but with SEs this wide the
heterogeneity test cannot separate it, so empirical Bayes pulled cpu to the low common value and
its raise cleared P(improve) 0.90 while losing ~GBP 32 / day in truth. With the response measured
to about +-70% of its size, the 0.90 rule plus pooling does not protect against an unlucky draw.
Seeds 42-45 are all spent for pricing.

## Results: controlled example (seed 42, after seed 44)

| | Phase 6 method (2026-10-06) | Phase 9 method |
| --- | --- | --- |
| changes | 17 (13 lost value) | 0 |
| status | 17 change, 15 frozen, 8 hold | 8 frozen, 32 hold |
| sum of true deltas of the changes | -GBP 3,526 / day | 0 |
| gated checks | FAIL (no harm, direction) | PASS (all 9) |

The 13 losing raises (api x8, premium x4, data transfer x1) became 11 hold and 2 frozen. The
seed-42 churn evidence is a low draw again (predicted / true extra churners 0.50); what blocked
the raises was the churn-risk ceiling (binding on 23 of 40 +max-step candidates) and the
segment guardrails, as designed for exactly this case: safe under evidence that is wrong.

## Results: development (seed 1)
All gated checks pass: 39 hold, 1 frozen. Predicted / true extra churners 1.22. Hazards vs
truth: tier ratios mostly 0.9-1.1, log correlation 0.68-0.87. Missed +GBP 683 / day.

## Tests executed
Final (2026-10-11, third amendment): `make check` exit 0 (1526 passed, 2 skipped, 13 m 06 s,
coverage floors OK for 157 files); `make nightly-science` exit 0 (seed 1 decided with the
leave-one-out archive: PASS, 32 hold / 8 frozen). New: `tests/pricing/test_churn_prior.py`
(brute-force check of the predictive prior, conjugate and conflict cases, archive integrity,
self-inclusion refusal, policy pin, CLI) and the archive-sources guard in `test_config.py`.

`make check` exit 0 (2026-10-10, final code incl. the amendment: 1490 passed, 2 skipped, 13 m 56 s, floors OK for 155 files): lint, mypy (strict), import
contracts (5 kept; `praxis.customer` in the model layer, `praxis.glm` in the foundation, both in
the simulator / web contracts), full suite incl. Postgres + emulator, coverage floors
(`praxis.customer/*` and `praxis.glm` >= 95%), chaos smoke, schemas, secrets, audit, frontend,
Terraform, workflow lint. New tests: `tests/customer/` (CLV, warehouse / leakage, models /
champion / artifact, values / calibration / monitor / CLI, config pin), `tests/unit/test_glm.py`,
`tests/elasticity/test_churn.py` (+ the churn-response step in the elasticity end-to-end),
`tests/pricing/test_long_term.py`, new Hypothesis properties for the three new constraints,
compliance re-checks of doctored records, truth split per tier, `pricing-compare`.

## Performance
Customer panel (5 snapshots, 1,000 customers) 0.4-0.6 s; training < 1 s; churn response on
8,000 customers < 2 s; shadow evaluation incl. customer values and true hazards ~16 s.

## Known limitations
* **Bill share is not the churn weight for every product** (gpu understated ~2-3x). This is the
  cause of the held-out failure.
* With 1,000 customers and ~140 voluntary churners in the prefix the GLM never beat the tier
  table; the recent calibration carries the hazard level.
* Phase 5 cell-level recovery failed again on seed 44 (cell coverage, pooling); tier-level
  checks passed. The optimiser uses tiers only.
* Missed value is now large in truth (+GBP 1,667 / day on seed 44), mostly blocked by the
  extrapolation limit (|log ratio| <= 0.18 from the tested price) and by first-cycle frequency.

* Measured with the defective Phase 4 artifact loader (see "Disclosure added 2026-10-25");
  needs a fresh pre-registered evaluation before live use.

## Cloud cost incurred
GBP 0.

## Options for the owner after seed 45
* Option 1 (bill-share freeze) was implemented and fixed the gpu failure; the remaining failure
  is sampling noise in the churn response, which only more evidence (a powered churn experiment,
  84 days: SE 0.86 -> 0.51 on dev) or a more conservative decision rule can address.
* Or accept Phase 9 as a SAFE shadow-mode system (no material harm on 3 of 4 held-out worlds
  with this method family; losses bounded by the guardrails) and proceed to Phase 10.
