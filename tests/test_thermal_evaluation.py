"""Regression evidence for independent issuance and delayed evaluation."""

import math
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pumpsteer.thermal_intelligence import (
    TemperatureMeasurement,
    ThermalDiagnostics,
)

START = datetime(2026, 10, 15, tzinfo=timezone.utc)


def observe(engine, minute, *, outdoor_minute=None, k=0.05, **extra):
    at = START + timedelta(minutes=minute)
    outside_at = START + timedelta(
        minutes=minute if outdoor_minute is None else outdoor_minute
    )
    return engine.observe(
        now=at,
        indoor=TemperatureMeasurement(5 + 16 * math.exp(-0.05 * minute / 60), at),
        outdoor=TemperatureMeasurement(5, outside_at),
        k_per_hour=k,
        model_fitted=True,
        **extra,
    )


def test_frequent_model_revisions_still_evaluate_long_horizons():
    engine = ThermalDiagnostics()
    comparisons = []
    for minute in range(121):
        observe(engine, minute, k=0.05 + 0.00001 * minute)
        comparisons.extend(engine.last_comparisons)
    assert [
        sum(c.prediction.horizon_minutes == h for c in comparisons)
        for h in (30, 60, 120)
    ] == [4, 3, 1]
    first_long = next(c for c in comparisons if c.prediction.horizon_minutes == 120)
    assert first_long.prediction.k_per_hour == 0.05
    assert first_long.error_c == pytest.approx(0, abs=1e-10)


@pytest.mark.parametrize("repeat_last_outdoor", [True, False])
def test_six_minute_outdoor_reporting_does_not_destroy_future_indoor_validation(
    repeat_last_outdoor,
):
    engine = ThermalDiagnostics()
    comparisons = []
    for minute in range(121):
        at = START + timedelta(minutes=minute)
        outside = TemperatureMeasurement(5, START + timedelta(minutes=minute // 6 * 6))
        engine.observe(
            now=at,
            indoor=TemperatureMeasurement(21, at),
            outdoor=outside if repeat_last_outdoor or minute % 6 == 0 else None,
            k_per_hour=0.05,
            model_fitted=True,
        )
        comparisons.extend(engine.last_comparisons)
    assert [
        sum(c.prediction.horizon_minutes == h for c in comparisons)
        for h in (30, 60, 120)
    ] == [4, 3, 1]


def test_revision_statistics_remain_separate_and_old_evidence_is_immutable():
    engine = ThermalDiagnostics()
    initial = observe(engine, 0)
    first_version = initial[0].model_version
    for minute in range(1, 121):
        observe(engine, minute, k=0.1 if minute >= 10 else 0.05)
    assert initial[0].k_per_hour == 0.05
    assert initial[0].model_version == first_version
    assert engine.model_version != first_version
    assert engine.quality(first_version)[120].count == 1
    assert engine.quality()[120].count == 0
    assert engine.quality()[30].bias_c > 0
    assert engine.quality(first_version)[30].bias_c == pytest.approx(0, abs=1e-10)
    assert set(engine.quality_history()) == {first_version, engine.model_version}
    with pytest.raises(FrozenInstanceError):
        initial[0].k_per_hour = 0.2


def test_evidence_retains_actual_values_timestamps_model_and_operating_context():
    engine = ThermalDiagnostics()
    predictions = engine.observe(
        now=START + timedelta(minutes=2),
        indoor=TemperatureMeasurement(21, START),
        outdoor=TemperatureMeasurement(4, START + timedelta(minutes=1)),
        k_per_hour=0.07,
        model_fitted=False,
        operating_mode="braking",
    )
    p = predictions[0]
    assert (p.indoor_temperature_c, p.outdoor_temperature_c) == (21, 4)
    assert p.origin_at == START
    assert p.outdoor_at == START + timedelta(minutes=1)
    assert p.issued_at == START + timedelta(minutes=2)
    assert p.due_at == START + timedelta(minutes=32)
    assert p.k_per_hour == 0.07 and p.model_fitted is False
    assert p.conditional is True and p.operating_mode == "braking"
    assert p in engine.pending_predictions


def test_indoor_gap_before_target_does_not_invalidate_authentic_target_measurement():
    engine = ThermalDiagnostics()
    observe(engine, 0)
    observe(engine, 5)
    observe(engine, 30)
    assert engine.quality()[30].count == 1
    assert engine.quality()[30].missed_count == 0
    assert engine.quality()[30].coverage == 1


@pytest.mark.parametrize(
    "bad_outdoor", [None, "stale", "reversed", "future", "invalid"]
)
def test_outdoor_unavailable_for_issuance_does_not_block_indoor_evaluation(bad_outdoor):
    engine = ThermalDiagnostics()
    for minute in (0, 5, 10, 15, 20, 25, 29):
        observe(engine, minute)
    now = START + timedelta(minutes=30)
    values = {
        None: None,
        "stale": TemperatureMeasurement(5, START),
        "reversed": TemperatureMeasurement(5, START + timedelta(minutes=28)),
        "future": TemperatureMeasurement(5, now + timedelta(seconds=1)),
        "invalid": TemperatureMeasurement(float("nan"), now),
    }
    assert (
        engine.observe(
            now=now,
            indoor=TemperatureMeasurement(21, now),
            outdoor=values[bad_outdoor],
            k_per_hour=0.05,
            model_fitted=True,
        )
        == ()
    )
    assert engine.quality()[30].count == 1
    assert engine.pending_count == 2


def test_invalid_current_coefficient_does_not_block_old_model_evaluation():
    engine = ThermalDiagnostics()
    observe(engine, 0)
    for minute in range(1, 31):
        observe(engine, minute, k=None)
    assert engine.quality()[30].count == 1
    assert engine.pending_count == 2


def test_missing_and_stale_indoor_samples_expire_only_due_forecasts():
    engine = ThermalDiagnostics()
    observe(engine, 0)
    engine.observe(
        now=START + timedelta(minutes=48),
        indoor=TemperatureMeasurement(21, START + timedelta(minutes=30)),
        outdoor=None,
        k_per_hour=None,
    )
    assert engine.quality()[30].count == 0
    assert engine.quality()[30].missed_count == 1
    assert engine.quality()[30].coverage == 0
    assert engine.pending_count == 2
    record = engine.evaluation_history[0]
    assert record.status == "missed" and record.error_c is None
    assert record.actual_at is None


def test_delayed_authentic_sample_within_freshness_limit_can_score_original_target():
    engine = ThermalDiagnostics()
    observe(engine, 0)
    engine.observe(
        now=START + timedelta(minutes=35),
        indoor=TemperatureMeasurement(21, START + timedelta(minutes=30)),
        outdoor=None,
        k_per_hour=None,
    )
    result = engine.last_comparisons[0]
    assert result.actual_at == START + timedelta(minutes=30)
    assert result.resolved_at == START + timedelta(minutes=35)
    assert result.lateness_seconds == 0
    assert result.status == "evaluated"


def test_reversed_indoor_sample_is_skipped_without_destroying_pending_forecasts():
    engine = ThermalDiagnostics()
    for minute in (0, 5, 10, 15, 20, 25, 29):
        observe(engine, minute)
    engine.observe(
        now=START + timedelta(minutes=30),
        indoor=TemperatureMeasurement(21, START + timedelta(minutes=28)),
        outdoor=None,
        k_per_hour=0.05,
        model_fitted=True,
    )
    assert engine.last_comparisons == () and engine.pending_count == 3
    observe(engine, 31)
    assert engine.quality()[30].count == 1


def test_model_updates_do_not_bypass_issue_cadence_or_reuse_indoor_origins():
    engine = ThermalDiagnostics()
    issued = []
    for minute in range(31):
        issued.extend(observe(engine, minute, k=0.05 + minute * 0.001))
    assert len(issued) == 6
    now = START + timedelta(minutes=31)
    assert (
        engine.observe(
            now=now,
            indoor=TemperatureMeasurement(21, START + timedelta(minutes=30)),
            outdoor=TemperatureMeasurement(5, now),
            k_per_hour=0.09,
        )
        == ()
    )


def test_histories_and_model_groups_remain_bounded_under_continuous_revisions():
    engine = ThermalDiagnostics()
    for minute in range(0, 2401, 5):
        observe(engine, minute, k=0.04 + minute * 0.00001)
        assert engine.pending_count <= 24
        assert len(engine.evaluation_history) <= 192
    history = engine.quality_history()
    assert len(history) <= 192
    for h in (30, 60, 120):
        assert sum(group[h].eligible_count for group in history.values()) == 64


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
    ],
)
def test_sparse_irregular_polling_across_dst_keeps_physical_horizons(start):
    engine = ThermalDiagnostics()
    first = None
    for minute in (0, 6, 17, 30, 47, 60, 87, 120):
        at = (start + timedelta(minutes=minute)).astimezone(
            ZoneInfo("Europe/Stockholm")
        )
        issued = engine.observe(
            now=at,
            indoor=TemperatureMeasurement(21, at),
            outdoor=TemperatureMeasurement(5, at),
            k_per_hour=0.05,
        )
        first = first or issued
    assert engine.quality()[120].count == 1
    assert first[2].due_at.timestamp() - first[2].issued_at.timestamp() == 7200


def test_restart_has_no_cross_instance_predictions_or_quality():
    previous = ThermalDiagnostics()
    observe(previous, 0)
    restarted = ThermalDiagnostics()
    observe(restarted, 120)
    assert restarted.evaluation_history == ()
    assert restarted.quality()[120].eligible_count == 0
