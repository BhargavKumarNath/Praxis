# ADR 0015: Dunning states, recovery models and scheduled retries

Status: Accepted (Phase 8). Written 2026-10-08, before the held-out recovery world (seed 42)
was built. Pre-registered and hash-pinned (`tests/recovery/test_config.py`): the recovery world
`configs/simulator/scenarios/recovery_eval.toml`, the dunning policy
`configs/recovery/policy.toml`, the model hyper-parameters `configs/recovery/model.toml` and
the acceptance `configs/recovery/acceptance.toml` (amended once on the development world, see
"Amendment").

## Context
Phase 7 delivers provider-neutral payment events (`payment.failed` is never `final`: ending
collection is a policy decision). Phase 8 must replace the fixed retry schedule with
evidence-based recovery decisions (project_plan Phase 8, `project.md` s6.4), schedule retries
through a Cloud Tasks abstraction, and prove it against the simulator's ground truth. The
simulator's Phase 1 retry process (success probability independent of timing and reason) gave
nothing to learn about *when* to retry, so the world itself had to become informative first.

## Decision

### Ground truth (simulator, opt-in, hash-neutral)
`billing.recovery` (absent = the Phase 1 process, every existing `config_hash` and golden
checksum unchanged). After an invoice's first failed attempt the payment becomes collectible
after a latent time C, or never: `P(cure) = sigmoid(cure_logit[reason] + 4.0 (rel - 0.9))`,
`C ~ Weibull(shape[reason], scale[reason] exp(-2.0 (rel - 0.9)))`, with `rel` the customer's
LATENT payment reliability. A retry at elapsed time e succeeds iff `e >= C` (collectibility is
absorbing) and the failure reason persists. Reason profiles differ on purpose (transient
processor errors; late-curing insufficient funds with an increasing hazard; card declines that
rarely cure and do so early). Draws use a new random stream, so no other stream moves.

**Identification:** the logging policy is a randomised retry-timing experiment: each retry gap
is drawn uniformly from {1, 2, 3, 4, 5, 7, 10} days by a hash of (salt, invoice, attempt).
Retry times are therefore independent of C, and P(C <= t | x) is identified for t in [1, 20]
days (first retries cover 1..10 densely, third attempts reach 20 sparsely). Nothing beyond
day 20 is identified; the policy never retries later (`horizon_days = 20`).

### States (two machines, `praxis.domain.dunning`)
* **Dunning case** (one per invoice whose collection failed): `past_due` (opened, undecided),
  `grace` (full access, retry planned), `restricted` (limited access), `suspended` (no
  access), `recovered`, `closed` (subscription cancelled). Access only tightens until payment
  (no restricted -> grace, no suspended -> restricted); a suspended customer who pays is
  recovered. Entry on `payment.failed`, or directly `recovered` when a recovery arrives
  before any failure (out-of-order delivery).
* **Retry job** (one scheduled charge): `scheduled -> executing -> succeeded | failed`, or
  `scheduled -> cancelled | superseded | expired`. An executing job cannot be cancelled: the
  provider may already hold the charge, so its outcome must be recorded.

"Retry scheduled" is a job state, not a case stage: access and retry status change for
different reasons, and folding them together would multiply states without a new guarantee.
Both machines are enforced in Python and by the Postgres state-guard trigger (migration
0004 seeds the reachability closure; a test keeps them equal).

### Policies (`praxis.recovery`)
* **Baseline** (the plan text): attempt 1 fails -> grace, retry 3 days later; attempt 2 fails
  -> restricted, retry 7 days later; attempt 3 fails -> suspended. It is also the fallback.
* **Model policy:** after every failure, choose the remaining retries (at most
  `max_attempts - attempts made`, whole days in (now, 20]) maximising expected net value from
  the champion's curve g(t) = P(C <= t | x), conditioned on every attempt so far having
  failed: recovered amount x (1 - 0.004 x day) - 30 per retry executed - 150 per failed retry;
  stop when no schedule is worth more than nothing. Only the next retry is acted on; the
  service re-plans after each failure (with absorbing cures, open and closed loop agree;
  property-tested). Stage: grace before day 7, restricted after, suspended when stopping.
* **Fallback** to the baseline, with the reason recorded on the decision: no / corrupt
  artifact, artifact older than 45 days, features unavailable, unknown category, invalid
  model output. Dunning never depends on the model being present.

### Models (truth-free, warehouse marts only; never import the simulator)
* `recovery_features.v1`: reason, tier as billed, payment method, existing customer, tenure,
  amount, prior invoices / first-attempt failures / recoveries known strictly BEFORE the
  failure (property-tested for leakage). One pure function serves training (marts) and
  serving (Postgres event log); a parity test shows identical features on a real world.
* **Calibrated binary model:** LightGBM, monotone non-decreasing in elapsed time, on each
  episode's FIRST retry (later retries are conditional on earlier failures), isotonic
  calibration on the latest 25% of training rows (time-ordered).
* **Survival model:** mixture-cure Weibull, `q(x) F(t | x)`, cure logit and log scale linear in
  the features, one shape per reason, interval-censored maximum likelihood over ALL retries
  (C in (last failure, success] or right-censored at the last failure / the training cutoff),
  analytic gradient, weak ridge (a N(0, 1/0.001) prior on the summed likelihood).
* **Baseline model:** the empirical first-retry success rate per (reason, gap), shrunk to the
  reason's rate. Every model must beat it.
* **Protocol:** selection (fit on data before day 100, score first retries resolved in
  [100, 120), champion = lower log loss), artifact (refit on data before day 120), evaluation
  (episodes failing in [120, 160)). The artifact carries model / data / feature / policy
  versions, code revision, parameters, selection metrics, checksums and creation time.

### Scheduling (`praxis.dunning`, Cloud Tasks semantics checked 2026-10-08)
* `TaskQueue` protocol: named tasks (create of an existing or recently deleted / executed
  name = `ALREADY_EXISTS`, reserved up to 24 h), delete while scheduled or dispatched, no
  update (a reschedule is delete + create under a NEW name), at-least-once delivery.
  `LocalTaskQueue` (explicit clock; tests and local runs) and `CloudTasksQueue` (REST, OIDC
  HTTP task to `POST /v1/tasks/payment-retry`, plus a shared `X-Praxis-Task-Token`).
  Terraform declares the queue (bounded dispatch rate and retries) and is never applied.
* **Transactional outbox:** a decision, its stage move and its job row commit in the event's
  transaction; the Cloud Task is created after the commit (`enqueued_at`), and
  `flush_outbox` re-creates missing tasks under the same name.
* **No duplicate side effects:** one idempotency key per (invoice, attempt) for the provider
  charge; partial unique indexes allow one live job per invoice and one possibly-charged job
  per (invoice, attempt); the executor commits `executing` before calling the provider, and a
  re-dispatch repeats the call with the same key.
* **No avoidable retry on stale state:** a recovery event cancels scheduled jobs; before
  charging, the executor re-checks the case and re-fetches the provider invoice (paid, closed,
  attempt already made -> cancel); a dispatch later than 24 h expires the job and re-plans.
* **Events:** a new Pub/Sub subscription `dunning` (stateful events) drives `DunningService`,
  idempotent via `processed_events` (consumer `dunning`) and tolerant of reordering
  (attempt numbers only move forward; a late first failure only corrects time zero).

## Consequences
* With Stripe, Praxis must be the only retrier: in the sandbox Dashboard, Billing > Revenue
  recovery > Retries, Smart Retries must be disabled and the custom schedule left without
  automatic retries (checked against Stripe's docs 2026-10-08), or both would charge. This is
  a documented manual setting, not automated. Stripe also refuses to retry hard declines
  (e.g. `authentication_required`, `lost_card`) without a new payment method; a Praxis retry of
  such an invoice simply fails again, which the replay values as a failed retry.
* Suspension is a Praxis access stage. Praxis does not yet mark the provider invoice
  uncollectible or cancel the provider subscription on suspension (no write for that in the
  gateway contract yet).
* The science replay does not feed retries back into the simulated customer (failed retries
  raise churn through `burden` in the simulator); disruption is costed in the valuation and
  reported, and long-horizon churn effects belong to Phase 9.
* The survival family matches the generating family per customer, but `rel` is latent, so the
  observable distribution is a mixture: the model is mildly misspecified by design.

## Amendment (2026-10-08, development world only, owner-approved, before seed 42)
On seed 1 two registered gates proved statistically unsound at this world size (~500
evaluation rows), not the models: (1) a perfectly calibrated model with the champion's own
predictions exceeds ECE 0.03 in 87% of simulated draws (median 0.041), so the ECE gate became
"consistent with perfect calibration" (ECE <= the 95th percentile of ECE when outcomes are
Bernoulli(own predictions), 2,000 draws, seed 0); raw ECE and the 0.03 reference are still
reported. (2) The truth gate is applied to reasons with at least 100 evaluation episodes;
smaller reasons (23-49 episodes on seed 1) are reported. No tolerance or threshold changed.

The development world also showed a genuine limitation that the amendment does NOT hide:
risky payers churn out over time, so later failures cure more often (true P(C <= 10) for
insufficient funds 0.57 in the training window, 0.67 in the evaluation window). A model
trained on the earlier window under-predicts (0.57): the truth gate failed on seed 1 with
error 0.13 and may fail on seed 42. The policy still beat the baseline there.

Also fixed on the development world (code, not acceptance): the survival ridge was applied to
the per-observation mean likelihood (an accidental prior n x 0.001 strong); the Weibull
derivatives overflowed for extreme parameters.
