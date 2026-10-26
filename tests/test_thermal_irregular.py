"""Real report intervals, no inferred physical sampling or synthetic freshness."""

from datetime import timedelta
import math

import pytest

from test_thermal_observer import NOW, observer, report


def feed_sparse(obs, minute, cadence=30, *, unavailable_at=None, source=None):
    at = NOW + timedelta(minutes=minute)
    reported = NOW + timedelta(minutes=minute // cadence * cadence)
    elapsed = (reported - NOW).total_seconds() / 3600
    attrs = {} if source is None else {"selected_source": source}
    inside = report(5 + 16 * math.exp(-0.04 * elapsed), reported, **attrs)
    if minute == unavailable_at:
        inside = report("unavailable", at)
    obs.observe(at, inside, report(5, at), braking=True)


@pytest.mark.parametrize("cadence", [20, 30, 60])
def test_sparse_reports_learn_using_real_interval(cadence):
    obs = observer()
    for minute in range(cadence * 21 + 1):
        feed_sparse(obs, minute, cadence)
    assert obs.model.pending_samples == 21
    window = obs.attributes()["heat_evidence"]["last_window"]
    assert window["minutes"] == cadence
    obs.model.fit()
    assert obs.model.is_valid
    assert obs.model.k == pytest.approx(0.04, abs=0.001)


def test_silence_does_not_cancel_pending_or_train_or_issue_again():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(30):
        feed_sparse(obs, minute)
    assert obs.model.pending_samples == 0
    assert obs.diagnostics.pending_count == 3
    assert obs.attributes()["predicted_change_c"] == {}  # stale current display
    feed_sparse(obs, 30)
    assert obs.model.pending_samples == 1
    assert obs.diagnostics.quality()[30].count == 1


def test_invalid_then_cached_old_value_cannot_reanchor():
    obs = observer()
    for minute in range(30):
        feed_sparse(obs, minute, unavailable_at=20)
    feed_sparse(obs, 30)
    assert obs.model.pending_samples == 0
    for minute in range(31, 61):
        feed_sparse(obs, minute)
    assert obs.model.pending_samples == 1


def test_metadata_source_change_splits_interval_then_recovers():
    obs = observer()
    for minute in range(61):
        feed_sparse(obs, minute, source="a" if minute < 15 else "b")
    assert obs.model.pending_samples == 1  # 30–60 only, never 0–30


def test_unknown_or_safe_context_splits_interval():
    obs = observer()
    for minute in range(61):
        if minute == 15:
            at = NOW + timedelta(minutes=minute)
            obs.observe(at, report(20.8, at), report(5, at), braking=False)
        else:
            feed_sparse(obs, minute)
    assert obs.model.pending_samples == 1


def test_observer_outage_is_not_normal_source_silence():
    obs = observer()
    feed_sparse(obs, 0)
    feed_sparse(obs, 30)
    assert obs.model.pending_samples == 0
    for minute in range(31, 61):
        feed_sparse(obs, minute)
    assert obs.model.pending_samples == 1


def test_unqualified_model_cannot_collect_long_endpoints_directly():
    obs = observer()
    obs.model.record_temp(NOW, 21)
    obs.model.record_temp(NOW + timedelta(minutes=60), 20.4)
    assert not obs.model.collect_braking_sample(20.7, 5)


def test_asynchronous_return_does_not_keep_moving_recovery_boundary():
    obs = observer()
    inside, outside = report(21), report(5)
    for minute in range(65):
        at = NOW + timedelta(minutes=minute)
        if minute == 20:
            inside = report("unavailable", at)
        elif minute in (30, 60):
            inside = report(21 - minute * 0.01, at)
        if minute in (34, 64):
            outside = report(5, at)
        obs.observe(at, inside, outside, braking=True)
    assert obs.model.pending_samples == 1  # genuine 30–60 endpoints


def test_overlong_interval_is_not_training():
    obs = observer()
    for minute in range(182):
        at = NOW + timedelta(minutes=minute)
        reported = NOW if minute < 181 else at
        obs.observe(
            at,
            report(21 if minute < 181 else 20, reported),
            report(5, at),
            braking=True,
        )
    assert obs.model.pending_samples == 0


def test_weather_excursion_between_endpoints_is_not_ignored():
    obs = observer()
    for minute in range(31):
        at = NOW + timedelta(minutes=minute)
        reported = NOW if minute < 30 else at
        outside = 8 if 15 <= minute < 20 else 5
        obs.observe(
            at,
            report(21 if minute < 30 else 20.7, reported),
            report(outside, at),
            braking=True,
        )
    assert obs.model.pending_samples == 0
    assert obs.attributes()["heat_evidence"]["last_window"]["outdoor_range_c"] == 3


def test_collected_interval_retains_duration_and_unknown_sensor_properties():
    obs = observer()
    for minute in range(31):
        feed_sparse(obs, minute)
    sample = obs.model._samples[0]
    assert sample.interval_hours == 0.5
    w = obs.attributes()["heat_evidence"]["last_window"]
    assert w["resolution_c"] is None
    assert w["reporting_threshold_c"] is None
    assert obs.attributes()["measurement_basis"] == "ha_report; physical_age_unverified"


def test_even_qualified_history_cannot_exceed_bounded_interval():
    obs = observer()
    obs.model.record_temp(NOW, 21)
    obs.model.record_temp(NOW + timedelta(minutes=121), 20, qualified_interval=True)
    assert not obs.model.collect_braking_sample(20.5, 5)


@pytest.mark.parametrize("boundary", ["safe", "restored", "unavailable", "nan"])
def test_new_boundary_while_awaiting_other_source_still_invalidates_anchor(boundary):
    obs = observer()
    inside, outside = report(21), report(5)
    for minute in range(91):
        at = NOW + timedelta(minutes=minute)
        if minute == 20:
            outside = report("unavailable", at)
        if minute in (30, 60, 90):
            inside = report(21 - minute * 0.01, at)
        if minute == 35 and boundary != "safe":
            inside = (
                report(20.7, at, restored=True)
                if boundary == "restored"
                else report(boundary, at)
            )
        if minute == 36 and boundary != "safe":
            inside = report(20.7, NOW + timedelta(minutes=30))  # old retained state
        if minute == 40:
            outside = report(5, at)
        obs.observe(
            at, inside, outside, braking=not (boundary == "safe" and minute == 35)
        )
        if minute == 60:
            assert obs.model.pending_samples == 0  # never 30–60 across boundary35
    assert obs.model.pending_samples == 1  # recovery: 60–90
