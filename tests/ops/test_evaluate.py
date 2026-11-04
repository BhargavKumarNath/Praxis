"""SLO and alert evaluators: boundaries, no-data handling, one alert per category."""

from __future__ import annotations

import pytest

from praxis.ops.config import OpsConfig, load_ops_config
from praxis.ops.evaluate import evaluate_alerts, evaluate_slo, evaluate_slos
from praxis.telemetry import MetricsView, Telemetry


@pytest.fixture(scope="module")
def cfg() -> OpsConfig:
    return load_ops_config()


def _alerts(cfg: OpsConfig, tel: Telemetry) -> dict[str, object]:
    view = tel.snapshot()
    return {a.rule_id: a for a in evaluate_alerts(cfg.rules, evaluate_slos(cfg.slos, view), view)}


@pytest.mark.parametrize(
    ("ok", "errors", "state"), [(995, 5, "ok"), (994, 6, "breach"), (5, 5, "no_data")]
)
def test_availability_boundary(
    cfg: OpsConfig, tel: Telemetry, ok: int, errors: int, state: str
) -> None:
    tel.inc("http_requests_total", ok, route="/x", method="GET", status=200)
    tel.inc("http_requests_total", errors, route="/x", method="GET", status=500)
    tel.inc("http_server_errors_total", errors, route="/x")
    status = evaluate_slo(cfg.slo("api_availability"), tel.snapshot())
    assert status.state == state
    if state != "no_data":
        assert status.budget_consumed == pytest.approx((errors / (ok + errors)) / 0.005)


def test_webhook_success_counts_only_accepted_outcomes(cfg: OpsConfig, tel: Telemetry) -> None:
    for outcome, n in (
        ("accepted", 80),
        ("duplicate", 10),
        ("ignored", 5),
        ("invalid_signature", 5),
    ):
        tel.inc("webhook_events_total", n, outcome=outcome)
    s = evaluate_slo(cfg.slo("webhook_processing_success"), tel.snapshot())
    assert s.state == "breach" and s.observed == pytest.approx(0.95)


def test_latency_slo_uses_conservative_bucket_bounds(cfg: OpsConfig, tel: Telemetry) -> None:
    slo = cfg.slo("pricing_decision_latency_p95")
    assert evaluate_slo(slo, tel.snapshot()).state == "no_data"
    for _ in range(10):
        tel.observe("pricing_decision_duration_ms", 2000.0)  # bucket bound 2500: at the limit
    assert evaluate_slo(slo, tel.snapshot()).state == "ok"
    for _ in range(10):
        tel.observe("pricing_decision_duration_ms", 3000.0)  # bucket bound 5000: over
    assert evaluate_slo(slo, tel.snapshot()).state == "breach"


def test_gauge_slo_judges_the_worst_series(cfg: OpsConfig, tel: Telemetry) -> None:
    slo = cfg.slo("dlq_backlog")
    assert evaluate_slo(slo, tel.snapshot()).state == "no_data"
    tel.set("dlq_backlog", 10, subscription="a")
    tel.set("dlq_backlog", 3, subscription="b")
    assert evaluate_slo(slo, tel.snapshot()).state == "ok"
    tel.set("dlq_backlog", 11, subscription="b")
    assert evaluate_slo(slo, tel.snapshot()).state == "breach"


def test_a_healthy_system_fires_nothing(cfg: OpsConfig, tel: Telemetry) -> None:
    tel.set("queue_backlog", 5, subscription="s")
    tel.set("data_source_age_s", 3600, source="fred")
    tel.set("model_drift_alarm", 0, model="m")
    tel.set("model_calibration_80", 0.8, model="m")
    tel.set("business_kpi_deviation", -0.1, kpi="revenue")
    tel.set("price_change_fraction", 0.2)
    assert _alerts(cfg, tel) == {}


def test_each_major_category_alerts_with_context_and_runbook(
    cfg: OpsConfig, tel: Telemetry
) -> None:
    tel.set("queue_backlog", 1001, subscription="operational")  # service
    tel.set("data_source_age_s", 604_801, source="open_meteo")  # data
    tel.set("model_calibration_80", 0.69, model="demand_forecast")  # model
    tel.set("business_kpi_deviation", -0.31, kpi="revenue")  # business (abs>)
    fired = _alerts(cfg, tel)
    assert set(fired) == {
        "queue_backlog_high",
        "source_stale",
        "calibration_regression",
        "business_kpi_shock",
    }
    for alert in fired.values():
        ctx = alert.context()  # type: ignore[attr-defined]
        assert ctx["runbook"].startswith("docs/runbooks/") and ctx["series"] is not None
    assert {a.category for a in fired.values()} == {"service", "data", "model", "business"}  # type: ignore[attr-defined]
    assert fired["source_stale"].series == {"source": "open_meteo"}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("metric", "kwargs", "value", "fires"),
    [
        ("queue_backlog", {"subscription": "s"}, 1000, False),  # strict >
        ("queue_backlog", {"subscription": "s"}, 1001, True),
        ("model_drift_alarm", {"model": "m"}, 1, True),  # >=
        ("model_calibration_80", {"model": "m"}, 0.70, False),  # strict <
        ("business_kpi_deviation", {"kpi": "k"}, 0.30, False),
        ("business_kpi_deviation", {"kpi": "k"}, 0.31, True),
        ("price_change_fraction", {}, 0.5, False),
        ("price_change_fraction", {}, 0.51, True),
    ],
)
def test_threshold_boundaries(
    cfg: OpsConfig, tel: Telemetry, metric: str, kwargs: dict[str, str], value: float, fires: bool
) -> None:
    tel.set(metric, value, **kwargs)
    assert bool(_alerts(cfg, tel)) is fires


def test_ratio_rule_needs_enough_denominator_and_unknown_sources_are_not_judged(
    cfg: OpsConfig, tel: Telemetry
) -> None:
    tel.inc("model_inference_total", 5, model="m", outcome="unavailable")
    assert "inference_failures" not in _alerts(cfg, tel)  # 5 < min_denominator 20
    tel.inc("model_inference_total", 15, model="m", outcome="served")
    assert "inference_failures" in _alerts(cfg, tel)  # 5 / 20 = 25% > 5%
    tel.set("data_source_age_s", 10**9, source="not_configured")
    assert "source_stale" not in _alerts(cfg, tel)


def test_slo_rules_fire_on_breach_only(cfg: OpsConfig, tel: Telemetry) -> None:
    tel.set("forecast_age_hours", 49, model="m")
    tel.set("dlq_backlog", 11, subscription="s")
    assert {"forecast_stale", "dlq_backlog_high"} <= set(_alerts(cfg, tel))
    empty = MetricsView()
    assert evaluate_alerts(cfg.rules, evaluate_slos(cfg.slos, empty), empty) == []


# --- two-window rule (Phase 15, ADR 0023) -------------------------------------------------
def _requests(tel: Telemetry, ok: int, bad: int) -> MetricsView:
    for _ in range(ok):
        tel.inc("http_requests_total", route="/x", method="GET", status="200")
    for _ in range(bad):
        tel.inc("http_requests_total", route="/x", method="GET", status="503")
        tel.inc("http_server_errors_total", route="/x")
    return tel.snapshot()


def test_slo_alert_needs_the_recent_window_to_breach_too(cfg: OpsConfig, tel: Telemetry) -> None:
    outage = _requests(tel, ok=0, bad=25)
    first = evaluate_slos(cfg.slos, outage, recent=outage.increase(MetricsView()))
    avail = next(s for s in first if s.id == "api_availability")
    assert (avail.state, avail.recent_state, avail.alerting) == ("breach", "breach", True)
    assert "api_availability_slo_breach" in {
        a.rule_id for a in evaluate_alerts(cfg.rules, first, outage)
    }

    healed = _requests(tel, ok=25, bad=0)  # cumulative still 50% bad, recent window clean
    statuses = evaluate_slos(cfg.slos, healed, recent=healed.increase(outage))
    avail = next(s for s in statuses if s.id == "api_availability")
    assert (avail.state, avail.recent_state, avail.alerting) == ("breach", "ok", False)
    assert avail.as_dict()["recent_state"] == "ok"
    assert evaluate_alerts(cfg.rules, statuses, healed) == []

    idle = evaluate_slos(cfg.slos, healed, recent=healed.increase(healed))  # no traffic
    assert next(s for s in idle if s.id == "api_availability").recent_state == "no_data"
    # Without a recent view (one-off health read) the cumulative breach alone alerts.
    assert next(s for s in evaluate_slos(cfg.slos, healed) if s.id == "api_availability").alerting


def test_latency_slo_uses_the_recent_window(cfg: OpsConfig, tel: Telemetry) -> None:
    for _ in range(25):
        tel.observe("event_lag_ms", 9000.0, subscription="s")
    slow = tel.snapshot()
    for _ in range(1000):
        tel.observe("event_lag_ms", 10.0, subscription="s")
    fast = tel.snapshot()
    lag = next(s for s in evaluate_slos(cfg.slos, fast, recent=fast.increase(slow))
               if s.id == "event_consumer_lag_p95")  # fmt: skip
    assert lag.state == "ok"  # p95 of everything is already fine
    for _ in range(4000):
        tel.observe("event_lag_ms", 9000.0, subscription="s")
    again = tel.snapshot()
    lag = next(s for s in evaluate_slos(cfg.slos, again, recent=again.increase(fast))
               if s.id == "event_consumer_lag_p95")  # fmt: skip
    assert (lag.state, lag.recent_state) == ("breach", "breach")


def test_ratio_rule_needs_the_recent_window(cfg: OpsConfig, tel: Telemetry) -> None:
    for _ in range(20):
        tel.inc("model_inference_total", model="m", outcome="unavailable")
    down = tel.snapshot()
    rules = tuple(r for r in cfg.rules if r.id == "inference_failures")
    assert [a.rule_id for a in evaluate_alerts(rules, [], down, down.increase(MetricsView()))] == [
        "inference_failures"
    ]
    for _ in range(5):
        tel.inc("model_inference_total", model="m", outcome="served")
    served = tel.snapshot()
    assert evaluate_alerts(rules, [], served, served.increase(down)) == []
    assert evaluate_alerts(rules, [], served, served.increase(served)) == []  # nothing recent
    assert [a.rule_id for a in evaluate_alerts(rules, [], served)] == ["inference_failures"]


def test_threshold_rules_on_gauges_ignore_the_recent_window(cfg: OpsConfig, tel: Telemetry) -> None:
    rules = tuple(r for r in cfg.rules if r.kind == "threshold")
    tel.set("model_serving_refused", 1, model="demand_forecast", stage="champion")
    view = tel.snapshot()
    alerts = evaluate_alerts(rules, [], view, view.increase(view))
    assert [a.rule_id for a in alerts] == ["model_unservable"]
    tel.set("model_serving_refused", 1, model="demand_forecast", stage="shadow")
    tel.set("model_serving_refused", 0, model="demand_forecast", stage="champion")
    assert evaluate_alerts(rules, [], tel.snapshot()) == []
