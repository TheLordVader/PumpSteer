"""Regression tests for diagnostic Thermal Learning on the current main branch."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.sensor import PumpSteerSensor
from custom_components.pumpsteer.thermal_model import ThermalModel


_START = datetime(2026, 10, 12, 11, 0, tzinfo=timezone.utc)


def _feed_cooling(model: ThermalModel, minutes: int) -> None:
    """Create valid, consecutive one-minute cooling observations."""
    for minute in range(minutes):
        indoor = 21.0 - 0.01 * minute
        model.record_temp(_START + timedelta(minutes=minute), indoor)
        model.collect_braking_sample(indoor, 5.0)


def _sensor():
    return PumpSteerSensor(
        SimpleNamespace(states=SimpleNamespace(get=lambda _: None)),
        SimpleNamespace(entry_id="thermal-confidence-test", data={}, options={}),
    )


def _run_session(model: ThermalModel, minutes: int = 60, drop: float = 0.8) -> None:
    model.start_validation_session(_START, 21.0, 5.0)
    for minute in range(1, minutes + 1):
        indoor = 21.0 - drop * minute / minutes
        assert model.update_validation_session(
            _START + timedelta(minutes=minute), indoor, 5.0
        )
    model.end_validation_session(_START + timedelta(minutes=minutes), 21.0 - drop)


def test_fitting_requires_20_accepted_samples_not_100():
    model = ThermalModel()
    _feed_cooling(model, 22)
    assert model.pending_samples == 19
    assert model.fit() is False
    enough_samples = ThermalModel()
    _feed_cooling(enough_samples, 23)
    assert enough_samples.pending_samples == 20
    assert enough_samples.fit() is True
    assert enough_samples.is_valid


def test_94_accepted_samples_yield_nonzero_confidence():
    model = ThermalModel()
    _feed_cooling(model, 97)

    assert model.pending_samples == 94
    assert model.fit() is True
    assert model.is_valid is True
    assert model.sample_count == 94
    assert 0.005 < model.k < 0.5
    assert model.fit_rmse is not None
    assert model.confidence >= 0.35
    assert model.learning_sessions == 0
    assert model.validated_sessions == 0


def test_legacy_fitted_k_restores_confidence_with_sample_history_summary():
    model = ThermalModel()
    model.restore_k(0.0162)
    model.restore_diagnostics(sample_count=94)

    assert model.is_valid
    assert model.sample_count == 94
    assert model.confidence == pytest.approx(0.35)


def test_quality_and_sessions_survive_restore():
    model = ThermalModel()
    model.restore_k(0.0162)
    model.restore_diagnostics(
        sample_count=94,
        fit_rmse=0.12,
        learning_sessions=3,
        validated_sessions=2,
        prediction_mae=0.2,
    )
    before = model.confidence
    reloaded = ThermalModel()
    reloaded.restore_k(model.k)
    reloaded.restore_diagnostics(
        sample_count=model.sample_count,
        fit_rmse=model.fit_rmse,
        learning_sessions=model.learning_sessions,
        validated_sessions=model.validated_sessions,
        prediction_mae=model.prediction_mae,
    )

    assert reloaded.confidence == pytest.approx(before)
    assert reloaded.validated_sessions == 2
    assert reloaded.prediction_mae == pytest.approx(0.2)


def test_refit_resets_old_model_accuracy_and_keeps_learning_history():
    model = ThermalModel()
    model.restore_k(0.05)
    _run_session(model, minutes=60, drop=0.8)

    assert model.validated_sessions == 1
    assert model.prediction_mae == pytest.approx(0.0, abs=1e-6)
    assert model.last_prediction_error == pytest.approx(0.0, abs=1e-6)
    assert model.learning_sessions == 1

    _feed_cooling(model, 23)
    assert model.fit() is True
    assert model.k != pytest.approx(0.05)
    assert model.learning_sessions == 1
    assert model.validated_sessions == 0
    assert model.prediction_mae is None
    assert model.last_prediction_error is None
    assert model.last_session_duration_minutes is None
    assert model.last_session_actual_drop is None
    assert model.last_session_predicted_drop is None

    # Only a subsequent session may validate the newly fitted k.
    _run_session(model, minutes=60, drop=0.6)
    assert model.validated_sessions == 1
    assert model.prediction_mae is not None
    assert model.learning_sessions == 2


def test_failed_refit_does_not_discard_valid_current_model_accuracy():
    model = ThermalModel()
    model.restore_k(0.05)
    _run_session(model)

    old_mae = model.prediction_mae
    old_error = model.last_prediction_error
    _feed_cooling(model, 22)
    assert model.pending_samples == 19
    assert model.fit() is False

    assert model.k == pytest.approx(0.05)
    assert model.validated_sessions == 1
    assert model.prediction_mae == old_mae
    assert model.last_prediction_error == old_error
    assert model.learning_sessions == 1


def test_refit_invalidates_restored_accuracy_and_active_old_k_session():
    model = ThermalModel()
    model.restore_k(0.05)
    model.restore_diagnostics(
        sample_count=94,
        fit_rmse=0.1,
        learning_sessions=4,
        validated_sessions=3,
        prediction_mae=0.2,
    )
    model.start_validation_session(_START, 21.0, 5.0)
    _feed_cooling(model, 23)

    assert model.validation_session_active
    assert model.fit() is True

    assert model.learning_sessions == 4
    assert model.validated_sessions == 0
    assert model.prediction_mae is None
    assert model.last_prediction_error is None
    assert not model.validation_session_active


def test_one_real_brake_session_is_counted_and_validated():
    model = ThermalModel()
    model.restore_k(0.05)
    _run_session(model)

    assert model.learning_sessions == 1
    assert model.validated_sessions == 1
    assert model.last_session_duration_minutes == pytest.approx(60)
    assert model.last_session_actual_drop == pytest.approx(0.8)
    assert model.last_session_predicted_drop == pytest.approx(0.8)
    assert model.prediction_mae == pytest.approx(0.0, abs=1e-6)


def test_validation_does_not_use_future_outdoor_observations():
    model = ThermalModel()
    model.restore_k(0.05)
    model.start_validation_session(_START, 21.0, 5.0)
    for minute in range(1, 61):
        assert model.update_validation_session(
            _START + timedelta(minutes=minute), 21.0 - 0.01 * minute, -5.0
        )
    assert model.end_validation_session(_START + timedelta(minutes=60), 20.4)
    # The forecast at the start cannot know the later outdoor observations.
    assert model.last_session_predicted_drop == pytest.approx(0.8)
    assert model.prediction_mae == pytest.approx(0.2)


def test_old_noncausal_validation_is_not_restored_as_current_accuracy():
    model = ThermalModel()
    model.restore_k(0.05)
    model.restore_diagnostics(
        sample_count=50,
        learning_sessions=4,
        validated_sessions=3,
        prediction_mae=0.1,
        validation_basis=None,
    )
    assert model.sample_count == 50
    assert model.learning_sessions == 4
    assert model.validated_sessions == 0
    assert model.prediction_mae is None


def test_short_session_does_not_count_toward_confidence():
    model = ThermalModel()
    model.restore_k(0.05)
    _run_session(model, minutes=5)

    assert model.learning_sessions == 0
    assert model.validated_sessions == 0
    assert model.prediction_mae is None


def test_stale_gap_invalidates_session_instead_of_inventing_cooling():
    model = ThermalModel()
    model.restore_k(0.05)
    model.start_validation_session(_START, 21.0, 5.0)
    assert model.update_validation_session(_START + timedelta(minutes=1), 20.98, 5.0)
    assert not model.update_validation_session(
        _START + timedelta(minutes=12), 20.5, 5.0
    )

    assert not model.validation_session_active
    assert model.learning_sessions == 0
    assert model.validated_sessions == 0


def test_flat_session_counts_observation_but_not_validation():
    model = ThermalModel()
    model.restore_k(0.05)
    _run_session(model, minutes=20, drop=0.0)

    assert model.learning_sessions == 1
    assert model.validated_sessions == 0
    assert model.prediction_mae is None


def test_sensor_diagnostics_include_confidence_and_learning_reason():
    sensor = _sensor()
    sensor._thermal_model.restore_k(0.0162)
    sensor._thermal_model.restore_diagnostics(sample_count=94)
    attrs = sensor._base_attrs(21.0, 21.0, 5.0, "normal", 3)

    assert attrs["thermal_confidence"] == pytest.approx(0.35)
    assert attrs["thermal_confidence_percent"] == pytest.approx(35.0)
    assert attrs["thermal_learning_sessions"] == 0
    assert attrs["thermal_validated_sessions"] == 0
    assert attrs["thermal_learning_reason"] == "not_evaluated"


def test_sensor_closes_session_when_braking_stops():
    sensor = _sensor()
    sensor._thermal_model.restore_k(0.05)
    for minute in range(20):
        now = _START + timedelta(minutes=minute)
        indoor = 21.0 - 0.02 * minute
        sensor._thermal_model.record_temp(now, indoor)
        sensor._update_thermal_learning(
            active=True, reason="active", now=now, indoor=indoor, outdoor=5.0
        )

    sensor._update_thermal_learning(
        active=False,
        reason="price_not_expensive",
        now=_START + timedelta(minutes=20),
        indoor=20.6,
        outdoor=5.0,
    )

    assert not sensor._thermal_model.validation_session_active
    assert not sensor._thermal_learning_active
    assert sensor._thermal_model.learning_sessions == 1
    assert sensor._thermal_model.validated_sessions == 1
    assert sensor._thermal_learning_reason == "price_not_expensive"


@pytest.mark.parametrize("outdoor", [10.0, 0.0, -15.0])
@pytest.mark.parametrize("minutes", [30, 60, 120])
def test_bounded_response_fixed_horizon(outdoor, minutes):
    import math

    model = ThermalModel()
    model.restore_k(0.03)
    expected = outdoor + (21.0 - outdoor) * math.exp(-0.03 * minutes / 60)
    predicted = 21.0 - model.predict_bounded_drop(21.0, outdoor, minutes)
    assert predicted == pytest.approx(expected)
    linear = 21.0 - model.predict_drop(21.0, outdoor, minutes)
    assert abs(predicted - expected) < abs(linear - expected)
    assert abs(predicted - expected) < abs(21.0 - expected)


def test_bounded_response_rejects_invalid_and_negative_horizon():
    model = ThermalModel()
    assert model.predict_bounded_drop(21, 5, -1) is None
    assert model.predict_bounded_drop(float("nan"), 5, 60) is None


def test_learned_response_on_separated_synthetic_braking_periods():
    """Learn only observed temperatures; score fixed horizons on later episodes."""
    import math

    results = []
    for outdoor in (10.0, 0.0, -15.0):
        model = ThermalModel()
        # Training is a separate stable-braking episode, without a heat input.
        for minute in range(100):
            indoor = outdoor + (21.0 - outdoor) * math.exp(-0.03 * minute / 60)
            model.record_temp(_START + timedelta(minutes=minute), indoor)
            model.collect_braking_sample(indoor, outdoor)
        assert model.fit()
        for horizon in (30, 60, 120):
            start = 20.5
            actual = outdoor + (start - outdoor) * math.exp(-0.03 * horizon / 60)
            bounded = start - model.predict_bounded_drop(start, outdoor, horizon)
            linear = start - model.predict_drop(start, outdoor, horizon)
            results.append(
                (abs(actual - bounded), abs(actual - linear), abs(actual - start))
            )
    means = [sum(row[i] for row in results) / len(results) for i in range(3)]
    print("heldout_MAE bounded/linear/persistence:", means)
    assert means[0] < means[1] < means[2]


def test_ha_conditional_projection_is_not_validated_by_old_session_metrics():
    sensor = _sensor()
    sensor._thermal_model.restore_k(0.03)
    sensor._thermal_model.restore_diagnostics(validated_sessions=2, prediction_mae=0.01)
    sensor._thermal_learning_active = True
    attrs = sensor._base_attrs(21.0, 21.0, 5.0, "expensive", 3)
    assert attrs["thermal_conditional_drop_60m_c"] > 0
    assert attrs["thermal_conditional_forecast_status"] == "experimental_unvalidated"
    sensor._thermal_learning_active = False
    assert (
        sensor._base_attrs(21.0, 21.0, 5.0, "normal", 3)[
            "thermal_conditional_drop_60m_c"
        ]
        is None
    )
