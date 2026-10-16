"""Whole-episode holdout evidence and honest conditional coverage accounting."""

import json
import pytest
from thermal_shadow_validation import benchmark, summarize, case, truth, run_case


def test_metrics_penalize_cooling_underestimation_and_keep_missing_targets():
    rows = [
        dict(
            dataset="holdout",
            method="bounded",
            horizon=30,
            issued=True,
            status="evaluated",
            error=-0.5,
            lower_breach=0.3,
            covered=False,
            width=0.4,
            interval_kind="exact_target",
            assessed_width=0.4,
        ),
        dict(
            dataset="holdout",
            method="bounded",
            horizon=30,
            issued=True,
            status="evaluated",
            error=0.2,
            lower_breach=0.0,
            covered=True,
            width=0.6,
            interval_kind="exact_target",
            assessed_width=0.6,
        ),
        dict(
            dataset="holdout",
            method="bounded",
            horizon=30,
            issued=True,
            status="missed",
            error=None,
            lower_breach=None,
            covered=None,
            width=0.5,
            interval_status="no_observation",
        ),
        dict(
            dataset="holdout",
            method="bounded",
            horizon=30,
            issued=False,
            status="rejected",
            error=None,
            lower_breach=None,
            covered=None,
            width=None,
        ),
    ]
    metric = summarize(rows)[0]
    assert (metric["attempted"], metric["issued"], metric["evaluated"]) == (4, 3, 2)
    assert metric["admission"] == 0.75 and metric[
        "evaluation_coverage"
    ] == pytest.approx(2 / 3)
    assert metric["mae_c"] == pytest.approx(0.35) and metric["bias_c"] == pytest.approx(
        -0.15
    )
    assert metric["worst_absolute_error_c"] == 0.5
    assert metric["mean_cooling_underestimation_c"] == 0.25
    assert metric["worst_cooling_underestimation_c"] == 0.5
    assert metric["intervals"]["exact_target"]["coverage"] == 0.5
    assert metric["intervals"]["exact_target"]["worst_lower_breach_c"] == 0.3
    assert metric["mean_nominal_interval_width_c"] == 0.5


def test_empty_and_point_only_metrics_do_not_invent_uncertainty():
    assert summarize([]) == []
    rows = [
        dict(
            dataset="holdout",
            method="previous_reference",
            horizon=120,
            issued=True,
            status="evaluated",
            error=-0.5,
            lower_breach=None,
            covered=None,
            width=None,
        )
    ]
    metric = summarize(rows)[0]
    assert metric["intervals"]["exact_target"]["coverage"] is None
    assert metric["worst_cooling_underestimation_c"] == 0.5


@pytest.fixture(scope="module")
def validation():
    return benchmark()


def test_chronological_split_keeps_training_and_holdout_episodes_disjoint(validation):
    assert len(validation["training_ids"]) == 42
    assert len(validation["holdout_ids"]) == 44
    assert len(validation["shift_ids"]) == 21
    assert max(validation["training_ids"]) < min(validation["holdout_ids"])
    assert set(validation["training_ids"]).isdisjoint(
        validation["holdout_ids"] + validation["shift_ids"]
    )
    assert validation["calibration_frozen_before_holdout"] is True


def test_validation_exercises_all_horizons_modes_and_adverse_conditions(validation):
    rows = validation["records"]
    assert {r["horizon"] for r in rows} == {30, 60, 120}
    assert {r["season"] for r in rows} == {"winter", "shoulder"}
    assert {r["building"] for r in rows} == {"slow", "medium", "fast"}
    assert {r["variant"] for r in rows} >= {
        "regular",
        "irregular",
        "sensor_failure",
        "revision",
        "transitions",
        "restart",
        "stale_origin",
        "safe_mode",
        "sensor_interruption",
    }
    assert {r["initial_mode"] for r in rows} >= {
        "normal",
        "preheating",
        "pre_braking",
        "braking",
        "brake_hold",
        "recovery",
        "precool",
        "safe_mode",
        "sensor_interruption",
    }
    assert all(r["model_k"] == 0.05 for r in rows if r["issued"])


def test_model_revisions_and_sparse_reports_preserve_two_hour_predictions(validation):
    rows = [
        r
        for r in validation["records"]
        if r["dataset"] == "holdout"
        and r["variant"] == "revision"
        and r["horizon"] == 120
        and r["method"] == "previous_reference"
    ]
    assert rows and all(r["status"] == "evaluated" for r in rows)
    assert all(r["latest_model_version"] > r["model_version"] for r in rows)


def test_restarts_and_sensor_failures_are_censored_not_fabricated_accurate(validation):
    rows = validation["records"]
    censored = [
        r
        for r in rows
        if r["variant"] == "restart" and r["horizon"] in (60, 120) and r["issued"]
    ]
    assert censored and all(
        r["status"] == "restart_censored" and r["error"] is None for r in censored
    )
    missed = [
        r
        for r in rows
        if r["variant"] == "sensor_failure" and r["horizon"] == 30 and r["issued"]
    ]
    assert missed and all(r["error"] is None for r in missed)
    transitioned = [
        r for r in rows if r["variant"] == "transitions" and r["status"] == "evaluated"
    ]
    assert transitioned and all(r["exposure"] == "transitioned" for r in transitioned)


def test_distribution_shift_failures_and_abstentions_remain_visible(validation):
    rows = validation["records"]
    assert any(r["method"] == "bounded_heat_balance" and not r["issued"] for r in rows)
    assert any(
        r["dataset"] == "shift"
        and r["method"] == "bounded_heat_balance"
        and r["covered"] is False
        for r in rows
    )
    assert any(
        r["dataset"] == "shift"
        and r["method"] == "empirical_training_band"
        and r["covered"] is False
        for r in rows
    )
    print(json.dumps(validation, sort_keys=True))


def test_source_refusals_and_baseline_exposure_survive_candidate_abstention(validation):
    rows = validation["records"]
    stale = [
        r
        for r in rows
        if r["variant"] == "stale_origin" and r["method"] == "bounded_heat_balance"
    ]
    assert stale and all(r["rejection"] == "stale_indoor" for r in stale)
    baseline = [
        r
        for r in rows
        if r["dataset"] == "holdout"
        and r["heating_kind"] == "unknown"
        and r["variant"] == "regular"
        and r["method"] == "previous_reference"
        and r["status"] == "evaluated"
    ]
    assert baseline and all(r["exposure"] == "observed_same_episode" for r in baseline)


def test_irregular_target_times_do_not_claim_due_time_uncertainty_coverage(validation):
    rows = [
        r
        for r in validation["records"]
        if r["variant"] == "irregular"
        and r["method"] == "bounded_heat_balance"
        and r["status"] == "evaluated"
    ]
    assert rows and all(r["error"] is not None for r in rows)
    covered = [r for r in rows if r["horizon"] in (30, 60)]
    assert all(r["interval_kind"] == "time_adjusted" for r in covered)
    assert all(r["covered"] is not None for r in covered)
    uncovered = [r for r in rows if r["horizon"] == 120]
    assert all(r["interval_status"] == "insufficient_time_coverage" for r in uncovered)
    assert all(r["covered"] is None for r in uncovered)


@pytest.mark.parametrize("disturbance", ["heat_loss", "heat_gain", "cold_front"])
def test_surprise_pairs_are_identical_until_40_minutes_and_freeze_same_forecasts(
    disturbance,
):
    reference = case(108, "surprise", 7)
    reference.update(variant="regular", heating_kind="known")
    disturbed = dict(reference, disturbance=disturbance)
    baseline, changed = truth(reference), truth(disturbed)
    assert all(baseline[m] == changed[m] for m in range(-10, 41))
    assert baseline[60] != changed[60]
    issued_reference, issued_changed = run_case(reference), run_case(disturbed)
    for name in ("original", "bounds", "nominal", "origin_episode", "rejections"):
        assert issued_reference[name] == issued_changed[name]


def test_common_cohort_includes_all_five_methods_on_identical_outcomes(validation):
    methods = {
        "previous_reference",
        "linear",
        "recent_trend",
        "empirical_training_band",
        "bounded_heat_balance",
    }
    common = validation["common_records"]
    groups = {}
    for r in common:
        key = (r["dataset"], r["id"], r["scenario"], r["horizon"])
        groups.setdefault(key, []).append(r)
    assert groups
    for rows in groups.values():
        assert {r["method"] for r in rows} == methods
        assert (
            len(
                {
                    (
                        r["origin_at"],
                        r["issued_at"],
                        r["due_at"],
                        r["actual_at"],
                        r["actual_temperature_c"],
                    )
                    for r in rows
                }
            )
            == 1
        )
        assert all(r["issued"] and r["error"] is not None for r in rows)
    assert {r["scenario"] for r in common if r["dataset"] == "surprise"} == {
        "reference",
        "heat_loss",
        "heat_gain",
        "cold_front",
    }


def test_exact_and_adjusted_interval_metrics_and_dropouts_are_separate(validation):
    metric = next(
        r
        for r in validation["summary"]
        if (r["dataset"], r["method"], r["horizon"])
        == ("holdout", "bounded_heat_balance", 30)
    )
    assert metric["attempted"] == 44 and metric["issued"] == 33
    assert metric["evaluated"] == 28
    assert metric["intervals"]["exact_target"]["count"] == 22
    assert metric["intervals"]["time_adjusted"]["count"] == 6
    assert sum(metric["rejections"].values()) == 11
    assert sum(metric["missing_outcomes"].values()) == 5
    assert "uncertainty_coverage" not in metric
