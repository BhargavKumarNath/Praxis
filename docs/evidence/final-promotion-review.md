# Review of promotion request `req_a680c42f35b5a82a` (2026-11-08, read-only)

**SYNTHETIC live world** (seed 9001). Data was read through the console proxy, with GET only:
`/api/console/operator` and `/api/console/mlops` on `praxis-dev-console`, revision
`praxis-dev-console-00008-2t6`.

**The request.**
- Promote `demand-hybrid-84d53ff98b99` (stage `shadow`; trained on data to 2026-09-01) over
  champion `demand-hybrid-aa245daf9e08`.
- Filed by `praxis-agent` on 2026-11-04 at 18:40 UTC.
- It cites shadow report `shadow-2ca81516600d1147fd25`, with "no_issue found by deterministic
  triage".

**Shadow evidence.** The lifecycle policy is `mlops-a7576b6cc4da`, with pre-registered limits.
There are 49 daily shadow reports, from 2026-09-21 to 2026-11-08, and **all 49 passed**. Origins
grew from 14 to 62, and every report was scored on the same observations as the champion.

| Check | Limit | Range over the 49 reports | Cited report (2026-09-27, 20 origins) |
| --- | --- | --- | --- |
| VWAPE vs champion | <= 1.02 | 0.9978 to 0.9995 | 0.9995 (0.10636 vs 0.10641) |
| Pinball vs champion | <= 1.02 | 0.9992 to 1.0037 | 0.9999 |
| Worst slice vs champion | <= 1.1 | <= 1.0016 | 1.0016 (`region=us_west`) |
| Coverage 80 | in [0.7, 0.9] | in band | 0.785 |
| Coverage 50 | in [0.4, 0.6] | in band | 0.485 |

**Context.**
- The champion has an open `model_drift` incident (`inc_0805e9a7d86a66eb`, 7 occurrences).
- The champion's live 80 % calibration is 0.750.

**Assessment: good to promote under the pre-registered policy.** The candidate is consistently
non-inferior, and very slightly better on VWAPE (about 0.2 %). The gain is marginal, and the
pinball ratio has drifted from 0.9999 to 1.0037, which is still far inside 1.02. Promotion is
reversible through the registry rollback (ADR 0017).

**Decision.**
- The owner approved it conditionally on 2026-11-08: "If the promotion request is good then I
  have approved it".
- The approval itself is a console write by the named approver. The automated session's attempt
  to send it was blocked by the session's permission policy.
- Status at the time of the review: PENDING.
- **Update, 2026-11-08: APPROVED and executed** by the owner on 2026-11-08 at 17:30:45 UTC (console POST, approver `owner`). Registry decision `promote-af5ee1e9c5b242b7ade67cbe26f29859`: `demand-hybrid-84d53ff98b99` -> champion, `demand-hybrid-aa245daf9e08` -> retired.
- This review is not evidence for any pre-registered gate (CLAUDE.md s24, live loop).

# Console token rotation: why it was NOT done on 2026-11-08

`scripts/console_token.py rotate --legacy-days 7` writes `owner=<new>@<date>,owner=<old>@<date>`
(checked on a dummy value). The **deployed** console (image `console-web@sha256:3ed55674…`)
runs the parser from before ADR 0021 amendment 1. That parser rejects a duplicate approver name
and would read `@date` as part of the token.

Adding that secret version now would therefore stop the deployed console from starting at its
next cold start. **Rotation and the new console image must go out together**: rotate, then
`make cloud-images cloud-plan`, review the plan, then `cloud-apply` (owner actions, ADR 0026).
