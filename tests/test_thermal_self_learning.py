"""Independent cooling windows and explainable, fallible heat evidence."""

from datetime import timedelta
import math

import pytest
from test_thermal_observer import NOW, observer, report

from custom_components.pumpsteer.thermal_model import ThermalModel


def feed(obs, minute, indoor, *, outdoor=5):
    at = NOW + timedelta(minutes=minute)
    obs.observe(
        at,
        report(indoor, at),
        report(outdoor, at),
        braking=True,
    )


def cooling(minute, *, k=0.04, extra=0):
    # Exact constant-input solution; extra is equivalent C/hour, not watts.
    decay = math.exp(-k * minute / 60)
    return 5 + (21 - 5) * decay + extra / k * (1 - decay)


def test_initial_fit_rejects_minority_slow_cooling_contamination():
    model = ThermalModel()
    for index in range(32):
        k = 0.04 if index < 24 else 0.015
        at = NOW + timedelta(minutes=index * 11)
        model.clear_history()
        model.record_temp(at, 21)
        model.record_temp(at + timedelta(minutes=10), 21 - k * 16 / 6)
        model.collect_braking_sample(21, 5)
    model.fit()
    assert model.is_valid
    assert model.k == pytest.approx(0.04)
    assert model.sample_count == 24


def test_default_candidate_learns_from_nonoverlapping_clean_windows():
    obs = observer()
    for minute in range(201):
        feed(obs, minute, cooling(minute))
        assert obs.model.pending_samples <= minute // 10
    assert obs.model.pending_samples == 20
    obs.model.fit()
    assert obs.model.is_valid
    assert obs.model.k == pytest.approx(0.04, abs=0.0001)


def test_heat_like_residuals_are_rejected_and_recover_without_manual_status():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(31):
        feed(obs, minute, cooling(minute))
    initial_samples = obs.model.pending_samples
    start = cooling(30)
    for minute in range(31, 51):
        decay = math.exp(-0.04 * (minute - 30) / 60)
        tin = 5 + (start - 5) * decay + 1.2 / 0.04 * (1 - decay)
        feed(obs, minute, tin)
    evidence = obs.attributes()["heat_evidence"]
    assert evidence["status"] == "suspected_extra_heat"
    assert evidence["confidence"] == "moderate"
    assert evidence["verified"] is False
    assert obs.model.pending_samples == initial_samples
    # Stop the hidden heat; no entity, type or activity flag is supplied.
    start = tin
    for minute in range(51, 71):
        tin = 5 + (start - 5) * math.exp(-0.04 * (minute - 50) / 60)
        feed(obs, minute, tin)
    assert obs.attributes()["heat_evidence"]["status"] == "consistent_with_model"
    assert obs.model.pending_samples == initial_samples + 2


def test_anomalous_actual_outcome_remains_in_forecast_quality():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(61):
        # After a confirmed baseline, heat begins 10 min before the next target.
        tin = cooling(minute) + (max(0, minute - 50) * 1.2 / 60)
        feed(obs, minute, tin)
    attrs = obs.attributes()
    assert attrs["quality"]["30"]["count"] == 2
    assert attrs["last_comparison"]["30"]["error_c"] == pytest.approx(0.2, abs=0.001)
    assert attrs["heat_evidence"]["status"] == "suspected_extra_heat"
    assert obs.model.pending_samples == 5


@pytest.mark.parametrize("interruption", ["restart", "invalid", "gap", "clock", "mode"])
def test_unfinished_window_cannot_bridge_lifecycle_or_quality_boundary(interruption):
    obs = observer()
    for minute in range(6):
        feed(obs, minute, cooling(minute))
    at = NOW + timedelta(minutes=6)
    if interruption == "restart":
        obs.start(at)
    elif interruption == "invalid":
        obs.observe(at, report("unavailable", at), report(5, at), braking=True)
    elif interruption == "mode":
        obs.observe(at, report(cooling(6), at), report(5, at), braking=False)
    elif interruption == "clock":
        obs.observe(NOW - timedelta(seconds=1), report(21), report(5), braking=True)
    for minute in range(12 if interruption == "gap" else 7, 16):
        feed(obs, minute, cooling(minute))
    assert obs.model.pending_samples == 0
    assert obs.attributes()["heat_evidence"]["confidence"] == "none"


def test_heat_suspicion_does_not_censor_real_forecast_errors():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(31):
        feed(obs, minute, cooling(minute, extra=1.2))
    attrs = obs.attributes()
    assert attrs["quality"]["30"]["count"] == 1
    assert attrs["quality"]["30"]["missed_count"] == 0
    assert attrs["last_comparison"]["30"]["error_c"] > 0.5
    assert obs.model.pending_samples == 0


@pytest.mark.parametrize("possible", [False, True])
@pytest.mark.parametrize("assist", [False, True])
def test_yes_no_and_experimental_assistance_preserve_actual_control(
    monkeypatch, possible, assist
):
    import asyncio
    from types import SimpleNamespace
    from test_brake_provenance import _sensor
    from custom_components.pumpsteer import sensor as module

    baseline, _ = _sensor()
    candidate, states = _sensor()
    candidate._config_entry.options.update(
        external_heat_sources=possible,
        thermal_assist_enabled=assist,
        external_heat_entity="input_boolean.old_marker",
    )
    # The obsolete marker must neither be needed nor pause this candidate.
    states["input_boolean.old_marker"] = SimpleNamespace(
        state="unavailable", attributes={}
    )
    candidate._thermal_model.restore_k(0.04, 25)
    outputs = []

    async def send(hass, entry, fake, last):
        outputs.append(fake)
        return last

    monkeypatch.setattr(module, "async_push_ohmigo", send)
    for minute in range(60):
        at = NOW + timedelta(minutes=minute)
        monkeypatch.setattr(module.dt_util, "now", lambda: at)
        for sensor in (baseline, candidate):
            for name, value in (
                ("sensor.indoor", cooling(minute)),
                ("sensor.outdoor", 5),
            ):
                sensor.hass.states.get(name).__dict__.update(report(value, at).__dict__)
            asyncio.run(sensor.async_update())
        assert outputs[-2] == outputs[-1]
        for key in (
            "mode",
            "brake_factor",
            "preheat_boost_c",
            "pi_i_term",
            "fake_outdoor_temperature",
        ):
            assert baseline.extra_state_attributes.get(
                key
            ) == candidate.extra_state_attributes.get(key)
    assessment = candidate.extra_state_attributes["thermal_assist"]
    assert assessment["requested"] is True  # passive planning is now always assessed
    assert assessment["effective"] is False
    assert assessment["eligible"] is False  # no identified generalized response
    assert assessment["delta_fake_c"] == 0
    assert "heat_evidence" in candidate.extra_state_attributes["thermal_diagnostics"]


def test_robust_learning_can_recover_from_an_initial_slow_cooling_model():
    obs = observer()
    # A legacy fit can have underestimated cooling after unknown heating.
    obs.model.restore_k(0.015, 25)
    for minute in range(201):
        feed(obs, minute, cooling(minute, k=0.08))
    obs.model.fit()
    assert obs.model.k == pytest.approx(0.08, abs=0.0001)


def test_sensor_drift_can_indicate_heat_but_is_never_verified_as_a_source():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(51):
        # No extra heat: a changing sensor bias has the same observed signature.
        feed(obs, minute, cooling(minute) + max(0, minute - 30) * 1.2 / 60)
    evidence = obs.attributes()["heat_evidence"]
    assert evidence["status"] == "suspected_extra_heat"
    assert evidence["verified"] is False
    assert obs.attributes()["heat_source_identifiable"] is False


def test_weak_hidden_heat_can_be_missed_without_claiming_its_absence():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(31):
        feed(obs, minute, cooling(minute, extra=0.15))
    evidence = obs.attributes()["heat_evidence"]
    assert evidence["status"] == "consistent_with_model"
    assert evidence["confidence"] == "none"
    assert evidence["verified"] is False
    assert obs.model.pending_samples == 3


def test_outdoor_excursion_rejects_window_even_when_endpoints_match():
    obs = observer()
    for minute in range(11):
        feed(obs, minute, cooling(minute), outdoor=7 if minute == 5 else 5)
    assert obs.model.pending_samples == 0
    assert obs.attributes()["heat_evidence"]["status"] == "context_changed"


def test_detection_confidence_and_unfinished_windows_do_not_survive_restart():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    for minute in range(51):
        feed(obs, minute, cooling(minute) + max(0, minute - 30) * 1.2 / 60)
    assert obs.attributes()["heat_evidence"]["confidence"] == "moderate"
    obs.start(NOW + timedelta(minutes=51))
    assert obs.model.k == 0.04
    assert obs.attributes()["heat_evidence"]["confidence"] == "none"
    assert obs.model.pending_samples == 0


@pytest.mark.parametrize("k", [0.025, 0.055])
def test_sensor_quantization_does_not_select_only_fast_cooling_windows(k):
    obs = observer()
    for minute in range(241):
        feed(obs, minute, round(cooling(minute, k=k), 1))
    obs.model.fit()
    assert obs.model.is_valid
    assert obs.model.k == pytest.approx(k, abs=0.003)


def test_bounded_training_buffer_still_reports_new_admitted_windows():
    obs = observer()
    for minute in range(2501):
        feed(obs, minute, cooling(minute, k=0.025))
    assert obs.model.pending_samples == 240
    assert obs.attributes()["learning_windows"]["accepted"] == 250
    assert (
        obs.attributes()["heat_evidence"]["last_window"]["qualified_for_learning"]
        is True
    )


def test_overestimated_restored_model_cannot_permanently_block_clean_relearning():
    obs = observer()
    obs.model.restore_k(0.08, 25)
    for minute in range(201):
        feed(obs, minute, cooling(minute, k=0.025))
    obs.model.fit()
    assert obs.model.k == pytest.approx(0.025, abs=0.0001)


def test_unconfirmed_restored_model_cannot_label_mismatch_as_probable_heat():
    obs = observer()
    obs.model.restore_k(0.08, 25)
    for minute in range(21):
        feed(obs, minute, cooling(minute, k=0.025))
    assert obs.attributes()["heat_evidence"]["status"] == "unconfirmed_model"
    assert obs.attributes()["heat_evidence"]["confidence"] == "none"


def test_weak_weather_matches_cannot_lock_learning_in_colder_weather():
    obs = observer()
    obs.model.restore_k(0.08, 25)
    for minute in range(31):
        feed(obs, minute, 17 + 4 * math.exp(-0.025 * minute / 60), outdoor=17)
    for minute in range(31, 56):
        at = NOW + timedelta(minutes=minute)
        outside = max(5, 17 - (minute - 31) * 0.5)
        obs.observe(at, report(21, at), report(outside, at), braking=False)
    for minute in range(56, 257):
        feed(obs, minute, cooling(minute - 56, k=0.025))
    obs.model.fit()
    assert obs.model.k == pytest.approx(0.025, abs=0.0001)


@pytest.mark.parametrize("count", [0, True, "20"])
def test_restored_extra_data_cannot_turn_unproven_fallback_into_a_learned_model(
    monkeypatch, count
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from test_brake_provenance import _sensor

    sensor, _ = _sensor()
    extra = SimpleNamespace(
        as_dict=lambda: {
            "thermal_model": {"k": 0.05, "valid": True, "sample_count": count}
        }
    )
    monkeypatch.setattr(
        sensor,
        "async_get_last_extra_data",
        AsyncMock(return_value=extra),
        raising=False,
    )
    asyncio.run(sensor.async_added_to_hass())
    assert not sensor._thermal_model.is_valid
