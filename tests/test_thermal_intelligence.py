"""Offline contract for conditional thermal references and delayed validation."""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pumpsteer.thermal_intelligence import (
    TemperatureMeasurement,
    ThermalDiagnostics,
)

START = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)


def _observe(engine, minute, *, indoor=21.0, outdoor=5.0, k=0.05, fitted=True):
    now = START + timedelta(minutes=minute)
    return engine.observe(
        now=now,
        indoor=TemperatureMeasurement(indoor, now),
        outdoor=TemperatureMeasurement(outdoor, now),
        k_per_hour=k,
        model_fitted=fitted,
    )


@pytest.mark.parametrize(
    "horizon, expected",
    [(30, 20.60495859245332), (60, 20.219670792011424), (120, 19.47739868857535)],
)
def test_exact_passive_reference_and_physical_target_time(horizon, expected):
    engine = ThermalDiagnostics()
    predictions = _observe(engine, 0)
    prediction = next(p for p in predictions if p.horizon_minutes == horizon)
    assert prediction.temperature_c == pytest.approx(expected)
    assert prediction.issued_at == START
    assert prediction.due_at == START + timedelta(minutes=horizon)
    assert prediction.k_per_hour == 0.05
    assert engine.status == "conditional"
    assert set(prediction.warnings) == {
        "heating_input_unknown",
        "residual_heat_unmodelled",
        "constant_outdoor_assumption",
    }


def test_warmer_outdoors_produces_a_bounded_warming_reference():
    predictions = _observe(ThermalDiagnostics(), 0, outdoor=27)
    values = [p.temperature_c for p in predictions]
    assert 21 < values[0] < values[1] < values[2] < 27


def test_unvalidated_coefficient_is_flagged_without_inventing_confidence():
    engine = ThermalDiagnostics()
    predictions = _observe(engine, 0, fitted=False)
    assert all("coefficient_not_validated" in p.warnings for p in predictions)
    assert all(q.count == 0 and q.mae_c is None for q in engine.quality().values())


@pytest.mark.parametrize("sensor", ["indoor", "outdoor"])
@pytest.mark.parametrize(
    "value", [None, float("nan"), float("inf"), -float("inf"), True, "20", -101, 101]
)
def test_invalid_measurements_do_not_become_temperatures(sensor, value):
    engine = ThermalDiagnostics()
    assert _observe(engine, 0, **{sensor: value}) == ()
    assert engine.status == "insufficient_data"
    assert engine.predictions == ()
    assert engine.pending_count == 0


@pytest.mark.parametrize("missing", ["indoor", "outdoor"])
def test_absent_measurement_is_unavailable(missing):
    data = dict(
        indoor=TemperatureMeasurement(21, START),
        outdoor=TemperatureMeasurement(5, START),
    )
    data[missing] = None
    engine = ThermalDiagnostics()
    assert engine.observe(now=START, k_per_hour=0.05, **data) == ()
    assert engine.status == "insufficient_data"


@pytest.mark.parametrize(
    "k", [None, 0, -1, 0.005, 0.5, float("nan"), float("inf"), True, "0.05"]
)
def test_invalid_model_does_not_fall_back_to_an_invented_coefficient(k):
    engine = ThermalDiagnostics()
    assert _observe(engine, 0, k=k) == ()
    assert engine.reason == "invalid_model"


@pytest.mark.parametrize("indoor,outdoor", [(0, -5), (-10, -15)])
def test_real_zero_and_negative_temperatures_remain_valid(indoor, outdoor):
    predictions = _observe(ThermalDiagnostics(), 0, indoor=indoor, outdoor=outdoor)
    assert len(predictions) == 3
    assert all(outdoor < p.temperature_c < indoor for p in predictions)


@pytest.mark.parametrize("field", ["now", "indoor", "outdoor"])
def test_naive_timestamps_are_rejected(field):
    engine = ThermalDiagnostics()
    data = dict(
        now=START,
        indoor=TemperatureMeasurement(21, START),
        outdoor=TemperatureMeasurement(5, START),
        k_per_hour=0.05,
    )
    if field == "now":
        data[field] = START.replace(tzinfo=None)
    else:
        data[field] = TemperatureMeasurement(
            21 if field == "indoor" else 5, START.replace(tzinfo=None)
        )
    assert engine.observe(**data) == ()
    assert engine.status == "insufficient_data"


@pytest.mark.parametrize("field", ["indoor", "outdoor"])
@pytest.mark.parametrize("age_seconds", [-1, 301])
def test_future_and_stale_measurements_are_rejected(field, age_seconds):
    engine = ThermalDiagnostics()
    data = dict(
        indoor=TemperatureMeasurement(21, START),
        outdoor=TemperatureMeasurement(5, START),
    )
    data[field] = TemperatureMeasurement(
        21 if field == "indoor" else 5, START - timedelta(seconds=age_seconds)
    )
    assert engine.observe(now=START, k_per_hour=0.05, **data) == ()
    assert engine.status == "insufficient_data"


def test_old_sample_is_extrapolated_without_backdating_forecast_issuance():
    engine = ThermalDiagnostics()
    predictions = engine.observe(
        now=START + timedelta(minutes=5),
        indoor=TemperatureMeasurement(21, START),
        outdoor=TemperatureMeasurement(5, START),
        k_per_hour=0.05,
    )
    assert predictions[0].origin_at == START
    assert predictions[0].issued_at == START + timedelta(minutes=5)
    assert predictions[0].due_at == START + timedelta(minutes=35)
    assert 20.219670792011424 < predictions[0].temperature_c < 20.60495859245332
    assert "measurement_age_extrapolated" in predictions[0].warnings


def test_exact_future_measurements_score_each_horizon_separately():
    engine = ThermalDiagnostics()
    for minute in range(0, 121, 5):
        actual = 5 + 16 * math.exp(-0.05 * minute / 60)
        _observe(engine, minute, indoor=actual)
    quality = engine.quality()
    assert [quality[h].count for h in (30, 60, 120)] == [4, 3, 1]
    for q in quality.values():
        assert q.mae_c == pytest.approx(0.0, abs=1e-10)
        assert q.bias_c == pytest.approx(0.0, abs=1e-10)
        assert q.max_absolute_error_c == pytest.approx(0.0, abs=1e-10)


@pytest.mark.parametrize("actual_k, sign", [(0.0, 1), (0.1, -1)])
def test_unmeasured_heat_or_faster_cooling_appears_as_signed_error_not_retraining(
    actual_k, sign
):
    engine = ThermalDiagnostics()
    for minute in range(0, 31, 5):
        _observe(engine, minute, indoor=5 + 16 * math.exp(-actual_k * minute / 60))
    q = engine.quality()[30]
    assert q.count == 1
    assert q.bias_c * sign > 0
    assert q.mae_c == pytest.approx(abs(q.bias_c))
    assert all(p.k_per_hour == 0.05 for p in engine.predictions)
    assert all("heating_input_unknown" in p.warnings for p in engine.predictions)


def test_duplicate_measurements_cannot_issue_or_score_forecasts():
    engine = ThermalDiagnostics()
    _observe(engine, 0)
    for minute in (0, 1, 2):
        assert (
            engine.observe(
                now=START + timedelta(minutes=minute),
                indoor=TemperatureMeasurement(21, START),
                outdoor=TemperatureMeasurement(5, START),
                k_per_hour=0.05,
                model_fitted=True,
            )
            == ()
        )
    assert engine.pending_count == 3
    assert all(q.count == 0 for q in engine.quality().values())


def test_gap_preserves_pending_until_an_authentic_target_measurement():
    engine = ThermalDiagnostics()
    for minute in (0, 5, 10):
        _observe(engine, minute)
    assert _observe(engine, 16) == ()
    assert engine.pending_count == 3
    assert all(q.count == 0 and q.missed_count == 0 for q in engine.quality().values())
    _observe(engine, 30)
    assert engine.quality()[30].count == 1


def test_invalid_reading_preserves_completed_quality_and_unrelated_pending_work():
    engine = ThermalDiagnostics()
    for minute in range(0, 31, 5):
        _observe(engine, minute)
    before = engine.quality()[30]
    pending = engine.pending_count
    assert _observe(engine, 35, indoor=None) == ()
    assert engine.pending_count == pending
    assert engine.predictions == ()
    assert engine.quality()[30].count == before.count
    assert engine.quality()[30].mae_c == before.mae_c


@pytest.mark.parametrize("reverse_wall_clock", [True, False])
def test_reversed_clock_cancels_but_reversed_sample_only_blocks_issuance(
    reverse_wall_clock,
):
    engine = ThermalDiagnostics()
    _observe(engine, 0)
    _observe(engine, 1)
    now = START + timedelta(minutes=0.5 if reverse_wall_clock else 2)
    sample_at = START + timedelta(minutes=0.5)
    assert (
        engine.observe(
            now=now,
            indoor=TemperatureMeasurement(21, sample_at),
            outdoor=TemperatureMeasurement(5, sample_at),
            k_per_hour=0.05,
            model_fitted=True,
        )
        == ()
    )
    assert engine.pending_count == (0 if reverse_wall_clock else 3)
    assert engine.status == "insufficient_data"
    assert len(_observe(engine, 3)) == (3 if reverse_wall_clock else 0)


@pytest.mark.parametrize("change", ["k", "fitted"])
def test_quality_cohorts_keep_old_records_and_do_not_bypass_cadence(change):
    engine = ThermalDiagnostics()
    for minute in range(0, 31, 5):
        _observe(engine, minute, fitted=False)
    previous_version = engine.model_version
    previous_pending = engine.pending_count
    assert engine.quality()[30].count == 1
    new = _observe(
        engine, 31, k=0.1 if change == "k" else 0.05, fitted=change == "fitted"
    )
    assert new == ()
    assert all(q.count == 0 and q.mae_c is None for q in engine.quality().values())
    assert engine.quality(previous_version)[30].count == 1
    assert engine.pending_count == previous_pending


def test_restart_has_no_old_forecasts_or_fabricated_validation():
    previous = ThermalDiagnostics()
    _observe(previous, 0)
    restarted = ThermalDiagnostics()
    assert restarted.pending_count == 0
    assert all(q.count == 0 for q in restarted.quality().values())
    _observe(restarted, 120)
    assert restarted.last_comparisons == ()
    assert restarted.pending_count == 3


@pytest.mark.parametrize("late_seconds, expected_count", [(0, 1), (120, 1), (121, 0)])
def test_comparison_tolerance_is_explicit_and_late_targets_are_missed(
    late_seconds, expected_count
):
    engine = ThermalDiagnostics()
    for minute in (*range(0, 26, 5), 28):
        _observe(engine, minute)
    _observe(engine, 30 + late_seconds / 60)
    q = engine.quality()[30]
    assert q.count == expected_count
    assert q.missed_count == 1 - expected_count
    if expected_count:
        assert engine.last_comparisons[0].lateness_seconds == late_seconds


def test_receipt_time_does_not_make_an_early_actual_measurement_a_future_sample():
    engine = ThermalDiagnostics()
    for minute in range(0, 29, 4):
        _observe(engine, minute)
    engine.observe(
        now=START + timedelta(minutes=32),
        indoor=TemperatureMeasurement(21, START + timedelta(minutes=29)),
        outdoor=TemperatureMeasurement(5, START + timedelta(minutes=29)),
        k_per_hour=0.05,
        model_fitted=True,
    )
    assert engine.quality()[30].count == 0
    engine.observe(
        now=START + timedelta(minutes=33),
        indoor=TemperatureMeasurement(21, START + timedelta(minutes=30)),
        outdoor=TemperatureMeasurement(5, START + timedelta(minutes=30)),
        k_per_hour=0.05,
        model_fitted=True,
    )
    assert engine.quality()[30].count == 1


def test_cadence_and_all_histories_stay_bounded_during_long_running_observation():
    engine = ThermalDiagnostics()
    issued = 0
    for minute in range(0, 2401, 5):
        issued += len(_observe(engine, minute))
        assert engine.pending_count <= 24
        assert len(engine.predictions) <= 3
        assert len(engine.last_comparisons) <= 24
    assert issued == 81 * 3
    assert all(q.count == 64 for q in engine.quality().values())


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
    ],
)
def test_dst_horizons_and_validation_use_physical_utc_time(start):
    engine = ThermalDiagnostics()
    zone = ZoneInfo("Europe/Stockholm")
    first = None
    for minute in range(0, 61, 5):
        instant = start + timedelta(minutes=minute)
        local = instant.astimezone(zone)
        result = engine.observe(
            now=local,
            indoor=TemperatureMeasurement(21, local),
            outdoor=TemperatureMeasurement(5, local),
            k_per_hour=0.05,
            model_fitted=True,
        )
        if first is None:
            first = result
    for p in first:
        assert p.due_at.timestamp() - p.issued_at.timestamp() == p.horizon_minutes * 60
    assert engine.quality()[60].count == 1


@pytest.mark.parametrize("sensor", ["indoor", "outdoor"])
def test_sensor_reversal_blocks_issuance_but_only_valid_indoor_scores(sensor):
    engine = ThermalDiagnostics()
    for minute in (0, 5, 10, 15, 20, 25, 29):
        _observe(engine, minute)
    now = START + timedelta(minutes=30)
    data = dict(
        indoor=TemperatureMeasurement(21, now), outdoor=TemperatureMeasurement(5, now)
    )
    data[sensor] = TemperatureMeasurement(
        21 if sensor == "indoor" else 5, START + timedelta(minutes=28)
    )
    assert engine.observe(now=now, k_per_hour=0.05, model_fitted=True, **data) == ()
    assert engine.status == "insufficient_data"
    assert engine.pending_count == (3 if sensor == "indoor" else 2)
    assert engine.quality()[30].count == (0 if sensor == "indoor" else 1)


@pytest.mark.parametrize("sensor", ["indoor", "outdoor"])
def test_sensor_gap_does_not_cancel_unrelated_pending_targets(sensor):
    engine = ThermalDiagnostics()
    _observe(engine, 0)
    now = START + timedelta(minutes=5)
    data = dict(
        indoor=TemperatureMeasurement(21, now), outdoor=TemperatureMeasurement(5, now)
    )
    data[sensor] = TemperatureMeasurement(
        21 if sensor == "indoor" else 5, START + timedelta(minutes=4)
    )
    engine.observe(now=now, k_per_hour=0.05, model_fitted=True, **data)
    assert _observe(engine, 10) == ()
    assert engine.pending_count == 3
    assert all(q.count == 0 and q.missed_count == 0 for q in engine.quality().values())


def test_duplicate_indoor_sample_does_not_hide_untrustworthy_outdoor_issuance():
    engine = ThermalDiagnostics()
    _observe(engine, 0)
    _observe(engine, 2)
    assert (
        engine.observe(
            now=START + timedelta(minutes=3),
            indoor=TemperatureMeasurement(21, START + timedelta(minutes=2)),
            outdoor=TemperatureMeasurement(5, START + timedelta(minutes=1)),
            k_per_hour=0.05,
            model_fitted=True,
        )
        == ()
    )
    assert engine.status == "insufficient_data"
    assert engine.pending_count == 3


def test_repeated_fresh_outdoor_sample_does_not_prevent_indoor_validation():
    engine = ThermalDiagnostics()
    for minute in (0, 5, 10, 15, 20, 25, 29):
        _observe(engine, minute)
    engine.observe(
        now=START + timedelta(minutes=30),
        indoor=TemperatureMeasurement(21, START + timedelta(minutes=30)),
        outdoor=TemperatureMeasurement(5, START + timedelta(minutes=29)),
        k_per_hour=0.05,
        model_fitted=True,
    )
    assert engine.quality()[30].count == 1
    assert engine.quality()[30].missed_count == 0


def test_outdoor_refresh_is_remembered_when_indoor_sample_is_duplicated():
    engine = ThermalDiagnostics()
    _observe(engine, 0)
    engine.observe(
        now=START + timedelta(minutes=4),
        indoor=TemperatureMeasurement(21, START),
        outdoor=TemperatureMeasurement(5, START + timedelta(minutes=4)),
        k_per_hour=0.05,
        model_fitted=True,
    )
    engine.observe(
        now=START + timedelta(minutes=6),
        indoor=TemperatureMeasurement(21, START + timedelta(minutes=5)),
        outdoor=TemperatureMeasurement(5, START + timedelta(minutes=6)),
        k_per_hour=0.05,
        model_fitted=True,
    )
    assert engine.pending_count == 3
    assert engine.predictions[0].issued_at == START
    assert all(q.missed_count == 0 for q in engine.quality().values())
