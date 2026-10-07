"""Evidence loading: gated artifact + its own report + registry, or a clear refusal."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from praxis.elasticity.config import load_registry
from praxis.pricing.evidence import (
    EvidenceError,
    churn_rel_slope,
    load_evidence,
    pool_slopes,
    resolve_artifact_dir,
)
from tests.pricing.fakes import write_artifact, write_report


def test_churn_effect_is_a_relative_rate_ratio_per_log_price() -> None:
    g = {
        "control": {"units": 1000, "churn_rate": 0.03},
        "treatment": {"units": 1000, "churn_rate": 0.05},
    }
    slope, se, base = churn_rel_slope(g, math.log(1.2))
    q_c, q_t = 30.5 / 1001, 50.5 / 1001  # +0.5 continuity correction
    assert slope == pytest.approx(math.log(q_t / q_c) / math.log(1.2))
    expected_se = math.sqrt((1 - q_t) / (1000 * q_t) + (1 - q_c) / (1000 * q_c)) / math.log(1.2)
    assert se == pytest.approx(expected_se) and base == 0.03
    # a price CUT that raised churn gives a negative slope (noise is not hidden)
    assert churn_rel_slope(g, math.log(1 / 1.2))[0] < 0
    # an arm without churners stays finite
    none = {
        "control": {"units": 500, "churn_rate": 0.0},
        "treatment": {"units": 500, "churn_rate": 0.0},
    }
    assert churn_rel_slope(none, 0.18)[0] == pytest.approx(0.0)


@pytest.mark.parametrize(
    "guardrails",
    [
        {"control": {"units": 0, "churn_rate": 0.0}, "treatment": {"units": 5, "churn_rate": 0.1}},
        {
            "control": {"units": 5, "churn_rate": math.nan},
            "treatment": {"units": 5, "churn_rate": 0.1},
        },
    ],
)
def test_empty_guardrails_are_refused(guardrails: dict[str, dict[str, float]]) -> None:
    with pytest.raises(EvidenceError):
        churn_rel_slope(guardrails, 0.18)


def test_load_evidence_joins_artifact_report_and_registry(tmp_path: Path) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    ev = load_evidence(art, report, registry)
    assert ev.tiers["growth"].mean == -1.2 and ev.tiers["growth"].sd == 0.03
    assert set(ev.products) == {e.product for e in registry.experiments}
    api = ev.products["api_requests"]
    assert api.assignment_date.isoformat() == "2026-02-02" and api.window_days == 28
    expected = math.log((0.040 * 2000 + 0.5) / (0.035 * 2000 + 0.5)) / math.log(1.2)
    assert api.raw_churn_rel_slope == pytest.approx(expected)
    assert api.control_churn_rate == 0.035
    # raises and cuts with the same churn ratio: slopes +-0.73 are pooled toward the mean
    assert abs(api.churn_rel_slope) < abs(api.raw_churn_rel_slope)
    assert api.churn_rel_se < api.raw_churn_rel_se
    assert api.churn_rel_upper(1.6449) > api.churn_rel_slope
    # the artifact root also works: the report picks its own artifact
    assert load_evidence(tmp_path / "models", report, registry).model_version == ev.model_version


def test_root_resolution_needs_a_matching_artifact(tmp_path: Path) -> None:
    write_artifact(tmp_path / "models")
    report = write_report(tmp_path / "report.json", data_version="units-other")
    with pytest.raises(EvidenceError, match="no elasticity artifact"):
        resolve_artifact_dir(tmp_path / "models", report)
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(EvidenceError, match="report unusable"):
        resolve_artifact_dir(tmp_path / "models", tmp_path / "bad.json")


@pytest.mark.parametrize(
    ("report_kwargs", "message"),
    [
        ({"data_version": "units-elsewhere"}, "does not belong"),
        ({"passed": False}, "validity gate"),
    ],
)
def test_inconsistent_or_ungated_evidence_is_refused(
    tmp_path: Path, report_kwargs: dict[str, object], message: str
) -> None:
    art = write_artifact(tmp_path / "models")
    report = write_report(tmp_path / "report.json", **report_kwargs)  # type: ignore[arg-type]
    with pytest.raises(EvidenceError, match=message):
        load_evidence(art, report, load_registry())


def test_registry_mismatch_and_missing_results_are_refused(tmp_path: Path) -> None:
    registry = load_registry()
    art = write_artifact(tmp_path / "models", registry)
    report = write_report(tmp_path / "report.json", registry)
    other = registry.model_copy(update={"experiments": registry.experiments[:2]})
    with pytest.raises(EvidenceError, match="registry"):
        load_evidence(art, report, other)
    data = json.loads(report.read_text())
    data["experiments"] = data["experiments"][:1]
    report.write_text(json.dumps(data))
    with pytest.raises(EvidenceError, match="no results"):
        load_evidence(art, report, registry)


def test_corrupt_artifacts_and_reports_are_refused(tmp_path: Path) -> None:
    art = write_artifact(tmp_path / "models")
    report = write_report(tmp_path / "report.json")
    (art / "estimates.json").write_text("{}")
    with pytest.raises(EvidenceError, match="artifact unusable"):
        load_evidence(art, report, load_registry())
    good = write_artifact(tmp_path / "models2")
    with pytest.raises(EvidenceError, match="report unusable"):
        load_evidence(good, tmp_path / "missing.json", load_registry())


def test_pooling_keeps_agreeing_estimates_and_shrinks_noise() -> None:
    means, sds, tau = pool_slopes([0.02, 0.02, 0.02], [0.01, 0.02, 0.03])
    assert tau == 0.0 and means == pytest.approx([0.02] * 3)
    common_se = (1 / (1 / 0.01**2 + 1 / 0.02**2 + 1 / 0.03**2)) ** 0.5
    assert sds == pytest.approx([common_se] * 3)  # complete pooling: the common mean's SE
    # the seed-1 development slopes: noisy, slightly over-dispersed -> strong partial pooling
    raw = [0.057, 0.006, -0.053, -0.012, 0.034]
    means, _, tau = pool_slopes(raw, [0.033, 0.031, 0.053, 0.031, 0.045])
    assert 0 < tau < 0.02
    assert max(means) - min(means) < 0.5 * (max(raw) - min(raw))
    # (with unequal SEs, shrinkage differs per estimate, so ranks need not be preserved)
    # genuinely heterogeneous estimates keep their ordering, shrunk toward the mean
    means, sds, tau = pool_slopes([0.0, 0.5, 1.0], [0.01, 0.01, 0.01])
    assert tau > 0.3 and means[0] < means[1] < means[2]
    assert all(sd < 0.02 for sd in sds)
    assert pool_slopes([0.3], [0.1]) == ([0.3], [0.1], 0.0)
    with pytest.raises(EvidenceError):
        pool_slopes([0.1, 0.2], [0.0, 0.1])
