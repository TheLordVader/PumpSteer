"""Conditional physics envelopes; uncertainty is not probability or safety proof."""

from dataclasses import replace, FrozenInstanceError
from datetime import datetime, timedelta, timezone
import math
import pytest
from custom_components.pumpsteer.thermal_intelligence import (
    TemperatureMeasurement,
    ThermalDiagnostics,
    ThermalComparison,
)
from custom_components.pumpsteer.thermal_episodes import EpisodeTracker
from custom_components.pumpsteer.thermal_reliability import (
    ForecastAssumptions,
    make_episode_forecast,
    assess_forecast,
)

START = datetime(2026, 10, 16, tzinfo=timezone.utc)


def setup(mode="normal", heating_kind="estimated", tin=21, tout=5):
    episode = EpisodeTracker("run-a").observe(START, mode, heating_kind=heating_kind)
    refs = ThermalDiagnostics().observe(
        now=START,
        indoor=TemperatureMeasurement(tin, START),
        outdoor=TemperatureMeasurement(tout, START),
        k_per_hour=0.05,
        model_fitted=True,
        operating_mode=mode,
    )
    assumptions = ForecastAssumptions(
        k_bounds=(0.05, 0.05),
        outdoor_bounds=(tout, tout),
        heat_bounds=(0.0, 0.0),
        sensor_error_c=0.0,
        valid_from=START,
        valid_until=START + timedelta(minutes=120),
        source="explicit synthetic scenario",
    )
    return refs, episode, assumptions


@pytest.mark.parametrize(
    "horizon,expected",
    [(30, 20.60495859245332), (60, 20.219670792011424), (120, 19.47739868857535)],
)
def test_exact_passive_case_is_recovered_without_new_control_parameters(
    horizon, expected
):
    refs, episode, assumptions = setup()
    p = next(p for p in refs if p.horizon_minutes == horizon)
    forecast, reason = make_episode_forecast(p, episode, assumptions)
    assert reason == ""
    assert forecast.point_c == pytest.approx(expected)
    assert forecast.lower_c == pytest.approx(expected) == forecast.upper_c
    assert forecast.reference is p and forecast.episode is episode
    assert forecast.uncertainty_kind == "conditional_assumption_bounds"
    with pytest.raises(FrozenInstanceError):
        forecast.point_c = 0


def test_known_effective_heat_is_an_explicit_input_not_inferred_from_outdoor():
    refs, e, a = setup(heating_kind="known")
    f, _ = make_episode_forecast(refs[2], e, replace(a, heat_bounds=(0.5, 0.5)))
    assert f.point_c == pytest.approx(20.429024508215756)
    assert f.point_c > refs[2].temperature_c


@pytest.mark.parametrize(
    "mode,kind",
    [("normal", "unknown"), ("safe_mode", "known"), ("sensor_interruption", "known")],
)
def test_unreliable_context_rejects_prediction_instead_of_zero_filling(mode, kind):
    refs, e, a = setup(mode, kind)
    f, reason = make_episode_forecast(refs[0], e, a)
    assert f is None and reason


@pytest.mark.parametrize(
    "change",
    [
        dict(k_bounds=(0.06, 0.1)),
        dict(k_bounds=(0.1, 0.01)),
        dict(outdoor_bounds=(6.0, 8.0)),
        dict(heat_bounds=(-1.0, 0.0)),
        dict(sensor_error_c=float("nan")),
        dict(k_bounds=[0.05, 0.05]),
        dict(valid_until=START + timedelta(minutes=29)),
        dict(source=""),
    ],
)
def test_invalid_or_insufficient_assumptions_reject_without_mutating_reference(change):
    refs, e, a = setup()
    f, reason = make_episode_forecast(refs[0], e, replace(a, **change))
    assert f is None and reason
    assert refs[0].k_per_hour == 0.05


def test_stale_episode_or_mode_mismatch_cannot_certify_an_issuance():
    refs, e, a = setup()
    assert (
        make_episode_forecast(
            refs[0], replace(e, observed_until=START - timedelta(seconds=1)), a
        )[0]
        is None
    )
    assert (
        make_episode_forecast(refs[0], replace(e, source_mode="braking"), a)[0] is None
    )


def test_warm_outdoors_and_residual_heat_crossing_outdoor_temperature_are_bounded():
    refs, e, a = setup(tin=20.9, tout=21)
    a = replace(
        a,
        k_bounds=(0.02, 0.1),
        outdoor_bounds=(20.0, 22.0),
        heat_bounds=(0.2, 1.0),
        sensor_error_c=0.02,
    )
    f, _ = make_episode_forecast(refs[2], e, a)
    actual = 31 + (20.9 - 31) * math.exp(
        -0.05 * 2
    )  # independently known constant-input solution
    assert f.lower_c < actual < f.upper_c
    assert f.lower_c <= f.point_c <= f.upper_c


def test_time_varying_heating_and_weather_inside_assumed_bounds_remain_contained():
    refs, e, a = setup()
    a = replace(
        a,
        k_bounds=(0.03, 0.08),
        outdoor_bounds=(0.0, 10.0),
        heat_bounds=(0.0, 1.5),
        sensor_error_c=0.02,
    )
    f, _ = make_episode_forecast(refs[2], e, a)
    tin = 21
    for second in range(7200):
        tout = 5 + 4 * math.sin(second / 900)
        k = 0.05 + 0.02 * math.sin(second / 1200)
        heat = 0.6 + 0.4 * math.sin(second / 700)
        tin += (-k * (tin - tout) + heat) / 3600
        if second == 7199:
            assert f.lower_c < tin < f.upper_c


def test_assessment_reports_cooling_underestimation_lower_breach_and_exposure():
    refs, e, a = setup()
    f, _ = make_episode_forecast(refs[0], e, a)
    actual = f.point_c - 0.5
    c = ThermalComparison(
        refs[0], refs[0].due_at, actual, -0.5, 0.0, refs[0].due_at, "evaluated"
    )
    target = replace(e, observed_until=refs[0].due_at)
    result = assess_forecast(f, c, target)
    assert result.error_c == pytest.approx(-0.5)
    assert result.cooling_underestimation_c == pytest.approx(0.5)
    assert result.lower_breach_c == pytest.approx(0.5)
    assert result.covered is False and result.exposure == "observed_same_episode"
    changed = replace(target, number=2, mode="braking", source_mode="braking")
    assert assess_forecast(f, c, changed).exposure == "transitioned"
    assert assess_forecast(f, c, None).exposure == "unknown"


def test_missing_outcomes_and_other_forecast_identity_do_not_create_fake_metrics():
    refs, e, a = setup()
    f, _ = make_episode_forecast(refs[0], e, a)
    missed = ThermalComparison(
        refs[0], None, None, None, None, refs[0].due_at, "missed"
    )
    result = assess_forecast(f, missed, None)
    assert result.error_c is None and result.covered is None
    with pytest.raises(ValueError):
        assess_forecast(f, replace(missed, prediction=refs[1]), None)


def test_uncertain_heat_range_never_invents_midpoint_delivered_power():
    refs, e, a = setup()
    f, _ = make_episode_forecast(refs[2], e, replace(a, heat_bounds=(0.0, 1.8)))
    assert f.point_c == pytest.approx(refs[2].temperature_c)
    assert f.upper_c > f.point_c


def test_late_actual_keeps_point_error_but_cannot_score_due_time_interval():
    refs, e, a = setup()
    p = refs[0]
    f, _ = make_episode_forecast(p, e, replace(a, valid_until=p.due_at))
    actual_at = p.due_at + timedelta(minutes=2)
    actual = 5 + 16 * math.exp(-0.05 * 32 / 60)
    c = ThermalComparison(
        p, actual_at, actual, actual - p.temperature_c, 120.0, actual_at, "evaluated"
    )
    result = assess_forecast(f, c, replace(e, observed_until=actual_at))
    assert result.error_c < 0 and result.cooling_underestimation_c > 0
    assert result.covered is None and result.lower_breach_c is None
    assert result.interval_status == "insufficient_time_coverage"


def test_structurally_identical_foreign_reference_cannot_cross_run_boundaries():
    refs, e, a = setup()
    p = refs[0]
    f, _ = make_episode_forecast(p, e, a)
    foreign = replace(p)
    assert foreign == p and foreign is not p
    c = ThermalComparison(
        foreign, p.due_at, 20.0, 20.0 - p.temperature_c, 0.0, p.due_at, "evaluated"
    )
    with pytest.raises(ValueError):
        assess_forecast(f, c, replace(e, observed_until=p.due_at))


@pytest.mark.parametrize(
    "offset,status,kind",
    [
        (-1, "outside_match_tolerance", None),
        (0, "evaluated", "exact_target"),
        (1, "evaluated", "time_adjusted"),
        (59, "evaluated", "time_adjusted"),
        (120, "evaluated", "time_adjusted"),
        (121, "outside_match_tolerance", None),
    ],
)
def test_interval_time_contract_preserves_nominal_point_and_checks_tolerance(
    offset, status, kind
):
    refs, e, a = setup()
    p = refs[0]
    f, _ = make_episode_forecast(p, e, a)
    before = replace(f)
    actual_at = p.due_at + timedelta(seconds=offset)
    actual = 5 + 16 * math.exp(-0.05 * (1800 + offset) / 3600)
    c = ThermalComparison(
        p, actual_at, actual, actual - p.temperature_c, offset, actual_at, "evaluated"
    )
    result = assess_forecast(f, c)
    assert result.interval_status == status
    assert result.interval_kind == kind
    assert result.error_c == pytest.approx(actual - f.point_c)
    assert c.error_c == actual - p.temperature_c
    if kind is None:
        assert result.covered is None and result.interval_lower_c is None
        assert result.interval_at is None
    else:
        assert result.interval_at == actual_at
        assert result.interval_lower_c == pytest.approx(actual)
        assert result.interval_upper_c == pytest.approx(actual)
        assert result.lower_breach_c == pytest.approx(0, abs=1e-12)
    assert f == before and f.reference is p


@pytest.mark.parametrize("coverage_seconds", [58, 59])
def test_one_second_issue_offset_uses_frozen_inputs_despite_revision_and_missing_outdoor(
    coverage_seconds,
):
    engine = ThermalDiagnostics()
    issued_at = START + timedelta(seconds=1)
    tracker = EpisodeTracker("run-a")
    tracker.observe(START, "normal", heating_kind="estimated")
    episode = tracker.observe(issued_at, "normal", heating_kind="estimated")
    p = engine.observe(
        now=issued_at,
        indoor=TemperatureMeasurement(21, START),
        outdoor=TemperatureMeasurement(5, START),
        k_per_hour=0.05,
        operating_mode="normal",
    )[0]
    _, _, a = setup()
    a = replace(a, valid_until=p.due_at + timedelta(seconds=coverage_seconds))
    f, _ = make_episode_forecast(p, episode, a)
    original = replace(f)
    actual_at = START + timedelta(minutes=31)
    engine.observe(
        now=actual_at,
        indoor=TemperatureMeasurement(20.1, actual_at),
        outdoor=None,
        k_per_hour=0.09,
        model_fitted=True,
        operating_mode="normal",
    )
    c = engine.last_comparisons[0]
    assert c.prediction is p and c.lateness_seconds == 59
    assert engine.model_version != p.model_version
    assert engine.reason == "missing_outdoor"
    result = assess_forecast(f, c)
    other = assess_forecast(f, replace(c, actual_temperature_c=22))
    assert result.interval_status == (
        "evaluated" if coverage_seconds == 59 else "insufficient_time_coverage"
    )
    assert result.error_c == pytest.approx(20.1 - p.temperature_c)
    assert result.interval_lower_c == other.interval_lower_c
    assert result.interval_upper_c == other.interval_upper_c
    if coverage_seconds == 59:
        assert result.interval_lower_c == pytest.approx(20.591958153837354)
        assert result.interval_kind == "time_adjusted"
        assert result.covered is False and other.covered is False
    else:
        assert result.covered is None and result.interval_kind is None
    assert f == original and f.reference is p and f.assumptions is original.assumptions


def test_interval_is_independent_of_whether_later_outcome_is_contained():
    refs, e, a = setup()
    p = refs[0]
    f, _ = make_episode_forecast(p, e, replace(a, heat_bounds=(0.0, 1.0)))
    actual_at = p.due_at + timedelta(seconds=59)
    c = ThermalComparison(p, actual_at, 20.8, None, 59, actual_at, "evaluated")
    inside = assess_forecast(f, c)
    outside = assess_forecast(f, replace(c, actual_temperature_c=30))
    assert inside.covered is True and outside.covered is False
    assert inside.interval_lower_c == outside.interval_lower_c
    assert inside.interval_upper_c == outside.interval_upper_c
    assert inside.interval_at == outside.interval_at == actual_at


def test_four_hours_of_minute_samples_and_one_second_issue_lag_have_17_intervals():
    engine = ThermalDiagnostics()
    tracker = EpisodeTracker("minute-samples")
    forecasts = {}
    assessments = []
    for minute in range(241):
        measured_at = START + timedelta(minutes=minute)
        now = measured_at + timedelta(seconds=1)
        tracker.observe(measured_at, "normal", heating_kind="estimated")
        episode = tracker.observe(now, "normal", heating_kind="estimated")
        refs = engine.observe(
            now=now,
            indoor=TemperatureMeasurement(21, measured_at),
            outdoor=TemperatureMeasurement(5, measured_at),
            k_per_hour=0.05,
            operating_mode="normal",
        )
        for p in refs:
            _, _, a = setup()
            f, reason = make_episode_forecast(
                p,
                episode,
                replace(
                    a, valid_from=measured_at, valid_until=now + timedelta(minutes=122)
                ),
            )
            assert reason == ""
            forecasts[id(p)] = f
        for c in engine.last_comparisons:
            assessments.append(assess_forecast(forecasts.pop(id(c.prediction)), c))
    assert len(assessments) == 17
    assert all(a.interval_kind == "time_adjusted" for a in assessments)
    assert all(a.interval_status == "evaluated" for a in assessments)
    assert all(a.observation.lateness_seconds == 59 for a in assessments)
