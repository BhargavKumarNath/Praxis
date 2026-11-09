"""Bounded post-deployment smoke test of the deployed environment (ADR 0026).

Synthetic only; never Stripe, never a load test. Every check states its expectation up
front and the report records what was observed (docs/evidence/deployment-smoke.json):

* exposure: no allUsers / allAuthenticatedUsers binding, anonymous callers get 403 at IAM,
  and the guard's own policy names only its push identity as invoker (project Owners still
  inherit run.invoker through roles/owner; that call is recorded, and it changes nothing);
* API: health, the registry's champion is what is served, a fresh forecast from the live
  warehouse, ops health, no schema;
* live loop (ADR 0027): the two daily triggers, today's tick recorded on the live volume,
  the volume private;
* console: UI, approver authorization (401 / 200 / 404) through the UI's proxy;
* events: a bounded cloud produce (2 x the same 100 customer-days: the second copy is pure
  duplicates), then each consumer drains its subscription; duplicates are absorbed, nothing
  is dead-lettered, and a second drain finds nothing (acks hold);
* ops: one monitor tick; the spend guard receives a synthetic BELOW-threshold notification
  through the real topic and push subscription (it must answer `hold`);
* secrets: every secret's readers are exactly the declared processes.

Usage: CLOUD_PROJECT=<id> CLOUD_REGION=us-central1 .venv/bin/python scripts/cloud_smoke.py
The approver token is read from Secret Manager into memory and never printed or written.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT = os.environ["CLOUD_PROJECT"]
REGION = os.environ.get("CLOUD_REGION", "us-central1")
ENV = "dev"
OUT = Path(os.environ.get("SMOKE_OUT", "docs/evidence/deployment-smoke.json"))
SECRET_READERS = {  # mirrors infra/terraform/modules/identity (pinned by tests/security)
    # console-public: the read-only console copy (ADR 0032)
    "database-url": {"api", "console", "console-public", "streaming", "ops", "agent", "live"},
    "stripe-webhook-secret": {"api"},
    "stripe-api-key": {"api"},
    "task-token": {"api"},
    "console-approvers": {"console"},
    "llm-api-key": {"agent"},
    "postgres-password": {"db"},
}
results: list[dict[str, Any]] = []


def gcloud(*args: str) -> str:
    out = subprocess.run(  # noqa: S603
        ["gcloud", *args, f"--project={PROJECT}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=900,
    )
    return out.stdout.strip()


def check(name: str, expected: Any, observed: Any, passed: bool) -> None:
    results.append({"check": name, "expected": expected, "observed": observed, "pass": passed})
    print(("PASS " if passed else "FAIL ") + name + f": {observed}", flush=True)  # noqa: T201


def http(
    url: str,
    *,
    token: str | None = None,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Serverless-Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def service_url(name: str) -> str:
    return gcloud(
        "run",
        "services",
        "describe",
        f"praxis-{ENV}-{name}",
        f"--region={REGION}",
        "--format=value(status.url)",
    )


def run_job(job: str, args: list[str], key: str) -> dict[str, Any]:
    """Execute once with overridden args; return the last report holding ``key``.

    Cloud Logging stores a single-line JSON print as ``jsonPayload`` and a pretty-printed
    report as one ``textPayload`` per line, so both are searched."""
    execution = gcloud(
        "run",
        "jobs",
        "execute",
        f"praxis-{ENV}-{job}",
        f"--region={REGION}",
        *([f"--args={','.join(args)}"] if args else []),  # none: the job's declared args
        "--wait",
        "--format=value(metadata.name)",
    )
    flt = (
        f'resource.type="cloud_run_job" AND '
        f'labels."run.googleapis.com/execution_name"="{execution}"'
    )
    for _ in range(24):  # log ingestion lags the execution by a few seconds
        entries = json.loads(
            gcloud(
                "logging",
                "read",
                flt,
                "--order=asc",
                "--limit=2000",
                "--freshness=1h",
                "--format=json",
            )
        )
        found: list[dict[str, Any]] = [
            e["jsonPayload"] for e in entries if key in e.get("jsonPayload", {})
        ]
        text = "\n".join(  # stdout only: Cloud Run appends its own "Container called exit"
            e["textPayload"] for e in entries if "textPayload" in e and _stdout(e)
        )
        if "\n}" in text or text.rstrip().endswith("}"):
            try:
                pretty = last_json(text)
            except json.JSONDecodeError:
                pretty = {}
            if key in pretty:
                found.append(pretty)
        if found:
            return found[-1]
        time.sleep(5)
    raise RuntimeError(f"no {key!r} report in the logs of {execution}")


def last_json(text: str) -> dict[str, Any]:
    """The report a CLI prints last (pretty JSON over several log lines)."""
    start = text.rfind("\n{") + 1 if "\n{" in text else text.find("{")
    report, _ = json.JSONDecoder().raw_decode(text[start:])  # ignore any trailing line
    if not isinstance(report, dict):
        raise json.JSONDecodeError("report is not an object", text, start)
    return report


def _stdout(entry: dict[str, Any]) -> bool:
    return str(entry.get("logName", "")).endswith("%2Fstdout")


def exposure() -> dict[str, str]:
    urls = {s: service_url(s) for s in ("api", "console", "spend-guard")}
    for name in urls:
        policy = gcloud(
            "run",
            "services",
            "get-iam-policy",
            f"praxis-{ENV}-{name}",
            f"--region={REGION}",
            "--format=json",
        )
        public = "allUsers" in policy or "allAuthenticatedUsers" in policy
        check(
            f"{name}: no public IAM binding",
            "no allUsers/allAuthenticatedUsers",
            "public" if public else "private",
            not public,
        )
        # Not /healthz: Cloud Run's frontend reserves paths ending in "z" (Google 404).
        for path in ("/", "/v1/ops/health", "/v1/budget"):
            status, _ = http(f"{urls[name]}{path}")
            check(f"{name}: anonymous {path} rejected by IAM", "403", status, status == 403)
    return urls


def api(url: str, token: str, champion: str | None) -> None:
    status, _ = http(f"{url}/healthz", token=token)
    check(
        "api /healthz is reserved by Cloud Run's frontend (use /v1/ops/health)",
        "404 from Google, never the container",
        status,
        status == 404,
    )
    status, body = http(f"{url}/v1/forecasts/demand/model", token=token)
    model = json.loads(body) if status == 200 else {}
    observed = {k: model.get(k) for k in ("model_version", "served_from", "stage")}
    check(
        "api serves the registry's champion (checksum-verified on load)",
        {"model_version": champion, "served_from": "registry", "stage": "champion"},
        observed,
        status == 200
        and champion is not None
        and observed == {"model_version": champion, "served_from": "registry", "stage": "champion"},
    )
    status, body = http(f"{url}/v1/forecasts/demand", token=token, method="POST", body={})
    fc = json.loads(body) if status == 200 else {}
    observed = {k: fc.get(k) for k in ("model_version", "freshness_status", "feature_date")}
    yesterday = datetime.fromordinal(datetime.now(UTC).toordinal() - 1).date().isoformat()
    check(
        "api forecast on the live warehouse (features from yesterday, UTC)",
        {"model_version": champion, "freshness_status": "fresh", "feature_date": yesterday},
        observed if status == 200 else f"{status} {body}",
        status == 200
        and observed
        == {"model_version": champion, "freshness_status": "fresh", "feature_date": yesterday},
    )
    status, _ = http(f"{url}/v1/ops/health", token=token)
    check("api /v1/ops/health", "200", status, status == 200)
    status, _ = http(f"{url}/openapi.json", token=token)
    check("api schema not published outside local", "404", status, status == 404)
    status, _ = http(f"{url}/v1/webhooks/stripe", token=token, method="POST", body={})
    check("api Stripe webhook unconfigured (no Stripe)", "503", status, status == 503)


def console(url: str, token: str) -> None:
    secret = f"--secret=praxis-{ENV}-console-approvers"
    approver = newest_approver_token(gcloud("secrets", "versions", "access", "latest", secret))
    status, body = http(f"{url}/", token=token)
    check("console UI", "200 (Next app)", status, status == 200 and "<html" in body.lower())
    status, _ = http(f"{url}/api/console/whoami", token=token)
    check("console admin: no approver token", "401", status, status == 401)
    auth = {"Authorization": f"Bearer {approver}"}
    status, body = http(f"{url}/api/console/whoami", token=token, headers=auth)
    check(
        "console admin: named approver",
        "200 owner",
        f"{status} {body}",
        status == 200 and "owner" in body,
    )
    wrong = {"Authorization": "Bearer " + "x" * 48}
    status, _ = http(
        f"{url}/api/console/requests/smoke/approve", token=token, method="POST", headers=wrong
    )
    check("console approve: wrong token", "401", status, status == 401)
    status, _ = http(
        f"{url}/api/console/requests/smoke/approve", token=token, method="POST", headers=auth
    )
    check("console approve: authorized, unknown request", "404", status, status == 404)
    status, _ = http(f"{url}/api/console/executive", token=token)
    check("console read model (Postgres over private VPC)", "200", status, status == 200)


def newest_approver_token(raw: str) -> str:
    """The first entry's token from ``name=token@YYYY-MM-DD,...`` (ADR 0021 amendment 1).
    ``console_token.py rotate`` writes the new token first and the old one after it."""
    _, _, rest = raw.split(",", 1)[0].strip().partition("=")
    return rest.rpartition("@")[0] if "@" in rest else rest


def registry_champion(console_url: str, token: str) -> str | None:
    """The champion in the model registry, read through the console's read model. A named
    approver may promote after the morning tick, so the tick's report can be stale."""
    status, body = http(f"{console_url}/api/console/mlops", token=token)
    panel = json.loads(body).get("panels", {}).get("versions", {}) if status == 200 else {}
    champion = (panel.get("data") or {}).get("champions", {}).get("demand_forecaster")
    return champion if isinstance(champion, str) else None


def live() -> str | None:
    """The live loop (ADR 0027): triggers, today's tick, the private volume. Returns the
    champion the last tick operated with (reported; the API is checked against the registry)."""
    triggers = json.loads(gcloud("scheduler", "jobs", "list", f"--location={REGION}",
                                 "--format=json"))  # fmt: skip
    observed = {t["name"].rsplit("/", 1)[-1]: [t.get("schedule"), t.get("state")]
                for t in triggers}  # fmt: skip
    expected = {f"praxis-{ENV}-live-tick": ["10 3 * * *", "ENABLED"],
                f"praxis-{ENV}-ops-tick": ["50 3 * * *", "ENABLED"]}  # fmt: skip
    check("live: daily triggers (live tick, then ops tick)", expected, observed,
          observed == expected)  # fmt: skip
    status = run_job("live", ["praxis.live", "status"], "last_as_of")
    today = datetime.now(UTC).date().isoformat()
    report = status.get("newest_report") or {}
    check("live: today's tick is recorded", today, status.get("last_as_of"),
          status.get("last_as_of") == today and report.get("is_synthetic") is True)  # fmt: skip
    bucket = json.loads(gcloud("storage", "buckets", "describe",
                               f"gs://praxis-{ENV}-live-{PROJECT}", "--format=json"))  # fmt: skip
    pap = bucket.get("public_access_prevention")
    check("live: volume is private", "enforced", pap, pap == "enforced")
    return report.get("champion")


def consume(role: str, job: str = "streaming") -> dict[str, Any]:
    args = [
        "praxis.streaming",
        "--environment",
        ENV,
        "consume",
        "--cloud",
        "--project",
        PROJECT,
        "--role",
        role,
        "--idle-timeout",
        "20",
        "--duckdb",
        "/tmp/stream.duckdb",  # noqa: S108 - the job container's own ephemeral disk
    ]
    return run_job(job, args, "loop")


def events() -> None:
    produce = [
        "praxis.streaming",
        "--environment",
        ENV,
        "produce",
        "--cloud",
        "--project",
        PROJECT,
        "--customers",
        "20",
        "--days",
        "5",
        "--seed",
        "7",
    ]
    first = run_job("streaming", produce, "published")["published"]
    second = run_job("streaming", produce, "published")["published"]
    check(
        "pubsub publish (bounded: 100 customer-days, twice)",
        "same count twice, > 0",
        [first, second],
        first == second and first > 0,
    )
    for role in ("operational", "warehouse", "monitoring"):
        report = consume(role)
        loop = report["loop"]
        subs: list[dict[str, Any]] = list(report["metrics"].values())
        counters: dict[str, int] = subs[0].get("counters", {}) if subs else {}
        ok = (
            loop["deliveries"] > 0
            and loop["acks"] == loop["deliveries"]
            and not loop["crashed"]
            and counters.get("dead_lettered", 0) == 0
        )
        check(
            f"{role}: drained and acked, 0 dead-lettered",
            "deliveries > 0, acks == deliveries, dead_lettered 0",
            {"loop": loop, "counters": counters},
            ok,
        )
        if role == "warehouse":
            rows = report.get("warehouse_rows")
            check(
                "warehouse: two copies of every event stored once (idempotent insert)",
                f"rows == {first} (published once), deliveries >= {2 * first}",
                {"rows": rows, "deliveries": loop["deliveries"]},
                rows == first and loop["deliveries"] >= 2 * first,
            )
        if role == "monitoring":
            unique = counters.get("unique_events_observed")
            dupes = counters.get("duplicate_deliveries", 0)
            check(
                "monitoring: duplicates detected, unique events == published",
                f"unique == {first}, duplicates >= {first}",
                {"unique": unique, "dupes": dupes},
                unique == first and dupes >= first,
            )
        again = consume(role)["loop"]
        check(
            f"{role}: second drain finds nothing (acks held)",
            "0 deliveries",
            again,
            again["deliveries"] == 0,
        )
    dlq = consume("dlq-inspect", job="ops")["loop"]
    check(
        "dlq-inspect: no dead letters (ops identity)", "0 deliveries", dlq, dlq["deliveries"] == 0
    )


def ops_and_guard() -> None:
    # Declared args (praxis.ops run --max-ticks 1). gcloud rejects a repeated --args value.
    summary = run_job("ops", [], "stale_processes")
    check(
        "ops monitor tick (alerts evaluated into incidents)",
        "status reported",
        summary,
        "status" in summary and summary["status"] != "tick_failed",
    )
    note = {
        "budgetDisplayName": f"praxis-{ENV}-monthly",
        "costAmount": 0.01,
        "budgetAmount": 10,
        "currencyCode": "GBP",
        "costIntervalStart": "smoke",
    }
    sent = datetime.now(UTC)
    gcloud(
        "pubsub",
        "topics",
        "publish",
        f"praxis-{ENV}-budget-alerts",
        f"--message={json.dumps(note)}",
    )
    flt = (
        f'resource.type="cloud_run_revision" AND resource.labels.service_name='
        f'"praxis-{ENV}-spend-guard" AND jsonPayload.message="budget notification" AND '
        f'timestamp>="{sent.isoformat()}"'
    )
    seen: list[dict[str, Any]] = []
    for _ in range(24):
        seen = json.loads(gcloud("logging", "read", flt, "--limit=10", "--format=json"))
        if any(e["jsonPayload"].get("cost_gbp") == "0.01" for e in seen):
            break
        time.sleep(5)
    hit = [e["jsonPayload"] for e in seen if e["jsonPayload"].get("cost_gbp") == "0.01"]
    check(
        "spend guard: notification via real topic + OIDC push, below threshold",
        "action hold (billing untouched)",
        hit[:1],
        bool(hit) and hit[0]["action"] == "hold",
    )
    billing = gcloud("billing", "projects", "describe", PROJECT, "--format=value(billingEnabled)")
    check("billing still enabled after the guard smoke", "True", billing, billing == "True")


def secrets() -> None:
    for secret, readers in SECRET_READERS.items():
        policy = json.loads(
            gcloud("secrets", "get-iam-policy", f"praxis-{ENV}-{secret}", "--format=json")
        )
        members = {m for b in policy.get("bindings", []) for m in b["members"]}
        got = {m.split(":")[1].split("@")[0].removeprefix(f"praxis-{ENV}-") for m in members}
        check(f"secret {secret}: readers", sorted(readers), sorted(got), got == readers)


def main() -> int:
    token = gcloud("auth", "print-identity-token")
    urls = exposure()
    policy = json.loads(
        gcloud(
            "run",
            "services",
            "get-iam-policy",
            f"praxis-{ENV}-spend-guard",
            f"--region={REGION}",
            "--format=json",
        )
    )
    invokers = sorted(
        m
        for b in policy.get("bindings", [])
        if b["role"] == "roles/run.invoker"
        for m in b["members"]
    )
    guard_sa = f"serviceAccount:praxis-{ENV}-guard@{PROJECT}.iam.gserviceaccount.com"
    check("spend-guard: service policy invokers", [guard_sa], invokers, invokers == [guard_sa])
    status, body = http(f"{urls['spend-guard']}/v1/budget", token=token, method="POST", body={})
    check(
        "spend-guard: project Owner (inherits run.invoker) posts an empty body",
        "200 ignore (unusable message acked; billing untouched)",
        f"{status} {body}",
        status == 200 and '"ignore"' in body,
    )
    tick_champion = live()
    champion = registry_champion(urls["console"], token)
    check(
        "registry champion readable (console read model)",
        "a model version",
        {"registry": champion, "last_tick": tick_champion},
        champion is not None,
    )
    api(urls["api"], token, champion)
    console(urls["console"], token)
    events()
    ops_and_guard()
    secrets()
    passed = sum(r["pass"] for r in results)
    report = {
        "project": PROJECT,
        "region": REGION,
        "at": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "passed": passed,
        "total": len(results),
        "checks": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(f"{passed}/{len(results)} checks passed -> {OUT}")  # noqa: T201
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
