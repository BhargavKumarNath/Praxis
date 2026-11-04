# Evidence: the live loop (ADR 0027)

Everything here is SYNTHETIC: the live world is the simulator (seed 9001, 1,000 customers,
`configs/simulator/scenarios/live.toml`), run as a clock of one simulated day per real UTC
day. Nothing below is a real-world business result, and nothing measured on the live world
is evidence for a pre-registered phase gate (it is an operating environment, not a held-out
evaluation).

## 1. Local trial (2026-11-04, before any cloud tick)

Same code path as the cloud tick, against a throwaway Postgres container. The models went to
a scratch directory and the warehouse covered the live world through 2026-11-03 (212 days).

| Step | Wall time | Peak RSS |
| --- | --- | --- |
| simulate 212 days | 17 s | 146 MB |
| raw load | 3 s | - |
| dbt marts (26 models) | 5 s | - |
| backtest (bootstrap acceptance) | 130 s | 337 MB |
| train | 47 s | 342 MB |
| `operate` 2026-08-31 .. 2026-11-04 (66 days) | 1,945 s | 485 MB |

Bootstrap champion: `demand-hybrid-185dfc13d7dc` (passed backtest acceptance, validated,
promoted by a named approver under the registry's bootstrap rule).

What the lifecycle did on its own over the 66 operated days (`configs/mlops/demand.toml`,
unchanged):

* 2026-09-01: drift (feature, prediction) -> retraining -> candidate
  `demand-hybrid-e47ecc4c9f7f` validated -> **shadow**.
* 2026-09-21 onward: the shadow had 14+ matured origins, was evaluated each day, and
  **passed** (45 / 45 evaluations). On 2026-11-04, over 58 origins and 29,232 paired rows:
  VWAPE ratio 0.998 (<= 1.02), pinball ratio 1.002 (<= 1.02), worst slice
  (`region=us_west`) 1.001 (<= 1.1), coverage_80 0.765 ([0.7, 0.9]), coverage_50 0.478
  ([0.4, 0.6]).
* 2026-10-13, 10-20, 10-27, 11-03: scheduled + drift retrains. Each new candidate validated
  and became a **challenger**; it did not start shadowing, because only one shadow runs at a
  time.
* No rollback: the champion was not on probation, since nothing was promoted inside the
  window.
* The challenger is close to the champion (ratio ~1.0): it passes "no worse than" and is not
  shown to be better. The promotion decision is the human's.

Operator review (`praxis.agent investigate --kind review_candidate`, the deterministic
triage; no LLM): outcome `completed`, grounded, recommended action `request_promotion`. One
**PENDING** row was written to `agent_action_requests`: kind promotion, target
`demand-hybrid-e47ecc4c9f7f`, evidence = the shadow report. Nothing was promoted.

Defects found by the trial and fixed before any cloud tick:
1. `save_artifact` published by renaming a directory, which Cloud Storage FUSE cannot do
   atomically. It now writes each file by file rename and the manifest last (the publish
   point); a directory without a manifest is an interrupted write and is rewritten
   (`tests/forecasting/test_artifact.py::test_an_interrupted_write_is_rewritten`).
2. The tick summary read a `requests` key the agent CLI never emits. It now records
   `recommended_action` and `promotion_requested`, both read from the agent's real output
   shape.
3. The writer's Cloud Storage mount is root-owned by default. The live job mounts with
   `uid=10001,gid=10001` (`mount_options`, which needs the google-beta provider for jobs in
   6.x).

## 2. Cloud deployment (2026-11-04, `praxis-dev-510522`)

Terraform (saved, reviewed plans only):
1. `-target=module.apis`: 1 add (Cloud Scheduler API).
2. Full plan: 13 add, 5 in-place change, **0 destroy, 0 replace**. A first combined plan
   showed 5 replacements of Pub/Sub DLQ IAM members. These came from the project number
   being unknown until the new API was enabled, so the plan was not applied and was split
   into two stages; the second stage shows none of them.
3. Image update after fix 2: 0 add, 8 in-place change, 0 destroy.

Cloud bootstrap (`gcloud run jobs execute praxis-dev-live --args=praxis.live,bootstrap,
--approver,BhargavKumarNath`, execution `praxis-dev-live-ngd4c`, 1,001 s wall):

| Step | Elapsed (s) |
| --- | --- |
| simulate | 16.9 |
| load | 6.3 |
| dbt | 12.2 |
| backtest | 605.0 |
| train | 188.5 |
| register | 10.1 |
| validate | 97.3 |
| promote | 3.1 |

Champion: `demand-hybrid-aa245daf9e08`. The approver name is the owner's (the session ran on
the owner's instruction to take the system live); every later promotion waits for the owner
in the console.

## 3. Catch-up ticks (2026-11-04): 2026-08-31 .. 2026-11-04 operated in the cloud

Each tick operated up to 7 days (`max_catchup_days`). Durations are wall time of
`gcloud run jobs execute --wait`, including the world rebuild (~40 s).

| Tick | Execution | Days | Wall (s) | vCPU | Lifecycle events |
| --- | --- | --- | --- | --- | --- |
| 1 | lsnf4 | 08-31 .. 09-06 | 1,100 | 2 | drift (feature, prediction) -> retrain 09-01 -> `demand-hybrid-84d53ff98b99` validated -> **shadow** |
| 2 | kvwzw | 09-07 .. 09-13 | 167 | 2 | shadow maturing |
| 3 | l6mcj | 09-14 .. 09-20 | 201 | 2 | shadow maturing |
| 4 | l8cfw | 09-21 .. 09-27 | 208 | 2 | shadow evaluated, **passed** -> operator review -> **PENDING promotion request** |
| 5 | rmc79 | 09-28 .. 10-04 | 227 | 2 | shadow passed daily; review returns the same pending request (one per kind + target) |
| 6 | wmlvc | 10-05 .. 10-11 | 214 | 2 | same |
| 7 | f54vk | 10-12 .. 10-18 | 1,646 | 2 | **FAILED**: `operate` killed at the 1,500 s step timeout (amendment 1). The 10-13 retrain had already completed (`demand-hybrid-4d32941b86ed` -> challenger); state not advanced |
| 8 | thjzz | 10-12 .. 10-18 | 227 | 2 | retry of the same days: drift alarms, the completed retrain found and not repeated |
| 9 | trl6c | 10-19 .. 10-25 | 1,794 | 2 | scheduled + drift retrain 10-20 -> challenger |
| 10 | sw4hk | 10-26 .. 11-01 | 2,228 | 2 | scheduled + drift retrain 10-27 -> challenger |
| 11 | lfmtc | 11-02 .. 11-04 | 2,113 | 2 | scheduled + drift retrain 11-03 -> challenger; **behind_days 0** |
| 12 | fsd2s | - | - | 4 | second tick the same day: `up_to_date` (idempotent) |

The trial and the cloud agree on the same days for retraining (09-01, 10-13, 10-20, 10-27,
11-03), shadow start (09-01), the first passing shadow evaluation (09-21) and the review
outcome. Model versions differ because a version hashes its lineage, which includes the
code revision.

What the cloud run shows, honestly stated:
* **Automatic:** detecting drift, retraining on schedule and on drift, validating, moving a
  validated candidate into shadow, evaluating the shadow daily against the champion on the
  same observations, and the operator filing a promotion request. None of these needed a
  person.
* **Human:** the request waits in the console. The registry champion is still the bootstrap
  model `demand-hybrid-aa245daf9e08` until the owner approves (or rejects) the request for
  `demand-hybrid-84d53ff98b99`.
* **Not exercised:** automatic rollback. It applies only to a champion on probation, that
  is after a promotion, so it has not run in the cloud yet. It was proven by the Phase 10
  drill (ADR 0017).
* **Failure (amendment 1):** tick 7 timed out and the next tick recovered with nothing lost
  or repeated.
* **Growth (amendment 2):** retrain days took ~1,600 s, ~2,000 s and ~2,000 s on 2 vCPU,
  because the required backtest grows with the history. The job now has 4 vCPU
  (= `num_threads`) and a 4 h budget. The 4 vCPU timing is not measured yet: the first
  4 vCPU retrain is the next scheduled one.

## 4. Live deployment smoke (`scripts/cloud_smoke.py`, bounded)

All runs are kept. The checks and their expectations were not changed between runs.

| Run | File | Result | Cause / fix |
| --- | --- | --- | --- |
| 1 | `live-deployment-smoke-run1.json` | 48 / 49 FAIL | forecast 500: the published warehouse had views pointing at a missing catalog. Fixed in code (one file name) and repaired with `republish` (ADR 0027 amendment 3) |
| 2 | `live-deployment-smoke-run2.json` | 47 / 49 FAIL | the API was right (registry champion, fresh forecast, feature date 2026-11-03), but the smoke's *expected* champion was `null`. `status` returned the newest report by file name, which was the `republish` report. It had also been returning the bootstrap report, which sorts after the dated ones. Fixed: `status` reads the newest dated tick report and repairs go to `reports/repairs/` (the existing repair report was moved there, not deleted) |
| 3 | `live-deployment-smoke.json` | **49 / 49 PASS** (2026-11-04) | API serves the registry champion `demand-hybrid-aa245daf9e08`; forecast fresh, feature date 2026-11-03; triggers enabled; live volume private |
