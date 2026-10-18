"""Diagnostic HA report qualification, brake validation and persistence."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_brake_provenance import _sensor
from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer import sensor as sensor_module

NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)


def report(value, at=NOW, **attrs):
    return SimpleNamespace(
        state=str(value),
        last_reported=at,
        attributes={"unit_of_measurement": "°C", **attrs},
    )


def observer():
    sensor, _ = _sensor()
    return sensor._thermal_observer


def test_restore_does_not_validate_boolean_or_nan():
    model = ThermalModel()
    model.restore_k(True)
    assert not model.is_valid
    model.restore_k(float("nan"))
    assert not model.is_valid


def test_restore_preserves_fit_count_without_measurements():
    model = ThermalModel()
    model.restore_k(0.037123456, sample_count=23)
    assert model.k == 0.037123456
    assert model.sample_count == 23
    assert model.pending_samples == 0
    assert not model._temp_history


@pytest.mark.parametrize(
    "kind", ["missing_time", "old", "future", "restored", "unit", "nan", "bool"]
)
def test_unqualified_reports_never_train(kind):
    obs = observer()
    indoor = report(21)
    if kind == "missing_time":
        del indoor.last_reported
    elif kind == "old":
        indoor.last_reported = NOW - timedelta(minutes=16)
    elif kind == "future":
        indoor.last_reported = NOW + timedelta(seconds=1)
    elif kind == "restored":
        indoor.attributes["restored"] = True
    elif kind == "unit":
        indoor.attributes["unit_of_measurement"] = "°F"
    elif kind == "nan":
        indoor.state = "nan"
    else:
        indoor.state = True
    obs.observe(NOW, indoor, report(5), braking=True)
    assert not obs.model._temp_history
    assert obs.model.pending_samples == 0
    assert obs.attributes()["status"] == "insufficient_data"


def test_distinct_reports_learn_existing_model_during_braking_only():
    obs = observer()
    for minute in range(30):
        now = NOW + timedelta(minutes=minute)
        obs.observe(now, report(21 - minute * 0.01, now), report(5, now), braking=True)
    assert obs.model.pending_samples >= 20
    obs.model.fit()
    assert obs.model.is_valid
    assert obs.model.k == pytest.approx(0.6 / 15.85, abs=0.001)
    before = obs.model.pending_samples
    now += timedelta(minutes=1)
    obs.observe(now, report(20.7, now), report(5, now), braking=False)
    assert obs.model.pending_samples == before


def test_polling_same_report_does_not_add_training_samples():
    obs = observer()
    inside, outside = report(21), report(5)
    for second in range(0, 300, 30):
        obs.observe(NOW + timedelta(seconds=second), inside, outside, braking=True)
    assert len(obs.model._temp_history) == 1
    assert obs.model.pending_samples == 0


def test_prediction_and_actual_have_frozen_k_and_descriptive_quality():
    obs = observer()
    obs.model.restore_k(0.04, sample_count=25)
    obs.observe(NOW, report(21), report(5), braking=True)
    prediction = obs.diagnostics.pending_predictions[0]
    for minute in range(1, 31):
        now = NOW + timedelta(minutes=minute)
        obs.observe(now, report(21 - minute * 0.01, now), report(5, now), braking=True)
    result = obs.attributes()
    comparison = result["last_comparison"]["30"]
    assert comparison["predicted_change_c"] == pytest.approx(
        prediction.temperature_c - 21, abs=0.0001
    )
    assert comparison["actual_change_c"] == pytest.approx(-0.3)
    assert comparison["error_c"] == pytest.approx(
        20.7 - prediction.temperature_c, abs=0.0001
    )
    assert result["quality"]["30"]["count"] == 1
    assert result["reliability"] == "conditional_observed_error"


@pytest.mark.parametrize("interruption", ["end", "invalid", "gap", "clock", "restart"])
def test_interrupted_brakes_never_receive_invented_outcomes(interruption):
    obs = observer()
    obs.model.restore_k(0.04)
    obs.observe(NOW, report(21), report(5), braking=True)
    if interruption == "restart":
        obs.start(NOW + timedelta(minutes=1))
    else:
        at = NOW + timedelta(minutes=1 if interruption != "gap" else 20)
        if interruption == "clock":
            at = NOW - timedelta(seconds=1)
        obs.observe(
            at,
            report("unavailable" if interruption == "invalid" else 20.9, at),
            report(5, at),
            braking=interruption != "end",
        )
    now = NOW + timedelta(minutes=30)
    obs.observe(now, report(20, now), report(5, now), braking=True)
    assert obs.diagnostics.quality()[30].count == 0
    assert obs.model.k == 0.04


def test_start_boundary_blocks_restored_or_pre_reload_reports():
    obs = observer()
    obs.start(NOW)
    obs.observe(NOW, report(21), report(5), braking=True)
    assert not obs.model._temp_history
    later = NOW + timedelta(seconds=1)
    obs.observe(later, report(21, later), report(5, later), braking=True)
    assert len(obs.model._temp_history) == 1


def test_fallback_is_not_restored_as_a_learned_parameter(monkeypatch):
    sensor, _ = _sensor()
    monkeypatch.setattr(
        sensor,
        "async_get_last_state",
        AsyncMock(
            return_value=SimpleNamespace(
                state="5", attributes={"thermal_k": 0.05, "thermal_k_valid": False}
            )
        ),
    )
    monkeypatch.setattr(
        sensor, "async_get_last_extra_data", AsyncMock(return_value=None), raising=False
    )
    asyncio.run(sensor.async_added_to_hass())
    assert not sensor._thermal_model.is_valid


@pytest.mark.parametrize("count", [0, 19, None, True, "20", 20, 300])
def test_legacy_valid_flag_requires_evidence_of_a_fit(monkeypatch, count):
    sensor, _ = _sensor()
    monkeypatch.setattr(
        sensor,
        "async_get_last_state",
        AsyncMock(
            return_value=SimpleNamespace(
                state="5",
                attributes={
                    "thermal_k": 0.05,
                    "thermal_k_valid": True,
                    "thermal_k_samples": count,
                },
            )
        ),
    )
    monkeypatch.setattr(
        sensor, "async_get_last_extra_data", AsyncMock(return_value=None), raising=False
    )
    asyncio.run(sensor.async_added_to_hass())
    proven = type(count) is int and count >= 20
    assert sensor._thermal_model.is_valid is proven
    assert sensor._thermal_model.sample_count == (count if proven else 0)


def test_unfitted_model_does_not_publish_fake_predictions():
    obs = observer()
    obs.observe(NOW, report(21), report(5), braking=True)
    attrs = obs.attributes()
    assert attrs["predicted_change_c"] == {}
    assert attrs["reliability"] == "insufficient_evaluations"
    assert attrs["reason"] == "model_not_fitted"


def test_training_storage_is_bounded():
    model = ThermalModel()
    for minute in range(2000):
        now = NOW + timedelta(minutes=minute)
        model.record_temp(now, 21 - minute * 0.001)
        model.collect_braking_sample(21 - minute * 0.001, 0)
    assert model.pending_samples <= 240


def test_restored_forecast_attributes_are_not_current_diagnostics(monkeypatch):
    sensor, _ = _sensor()
    monkeypatch.setattr(
        sensor,
        "async_get_last_state",
        AsyncMock(
            return_value=SimpleNamespace(
                state="5",
                attributes={
                    "thermal_k": 0.04,
                    "thermal_k_valid": True,
                    "thermal_diagnostics": {
                        "predicted_change_c": {"30": -2},
                        "reliability": "old",
                    },
                },
            )
        ),
    )
    monkeypatch.setattr(
        sensor, "async_get_last_extra_data", AsyncMock(return_value=None), raising=False
    )
    asyncio.run(sensor.async_added_to_hass())
    assert (
        sensor.extra_state_attributes["thermal_diagnostics"]["predicted_change_c"] == {}
    )
    assert (
        sensor.extra_state_attributes["thermal_diagnostics"]["reliability"]
        == "insufficient_evaluations"
    )


def test_explicit_extra_invalidity_does_not_fall_back_to_stale_attributes(monkeypatch):
    sensor, _ = _sensor()
    monkeypatch.setattr(
        sensor,
        "async_get_last_state",
        AsyncMock(
            return_value=SimpleNamespace(
                state="5", attributes={"thermal_k": 0.04, "thermal_k_valid": True}
            )
        ),
    )
    extra = SimpleNamespace(
        as_dict=lambda: {"thermal_model": {"k": 0.05, "valid": False}}
    )
    monkeypatch.setattr(
        sensor,
        "async_get_last_extra_data",
        AsyncMock(return_value=extra),
        raising=False,
    )
    asyncio.run(sensor.async_added_to_hass())
    assert not sensor._thermal_model.is_valid


def test_legacy_fit_count_larger_than_new_buffer_is_preserved():
    model = ThermalModel()
    model.restore_k(0.04, sample_count=300)
    assert model.is_valid and model.sample_count == 300


@pytest.mark.parametrize("source", ["indoor_temp_entity", "real_outdoor_entity"])
def test_changed_temperature_entity_starts_new_observation_segment(monkeypatch, source):
    sensor, states = _sensor()
    sensor._thermal_model.restore_k(0.04, 27)
    sensor._thermal_observer.observe(NOW, report(21), report(5), braking=True)
    assert sensor._thermal_observer.diagnostics.pending_count == 3
    now = NOW + timedelta(minutes=30)
    monkeypatch.setattr(sensor_module.dt_util, "now", lambda: now)
    sensor._attributes = {"mode": "braking", "brake_factor": 1}
    monkeypatch.setattr(sensor, "_do_update", AsyncMock())
    sensor._config_entry.options[source] = "sensor.replacement"
    states.update(
        {
            "sensor.indoor": report(21, now),
            "sensor.outdoor": report(5, now),
            "sensor.replacement": report(
                20 if source == "indoor_temp_entity" else 6, now
            ),
        }
    )
    asyncio.run(sensor.async_update())
    assert sensor._thermal_observer.diagnostics.quality()[30].count == 0
    assert sensor._thermal_model.pending_samples == 0
    assert sensor._thermal_observer.diagnostics.pending_count == 0
    assert (sensor._thermal_model.k, sensor._thermal_model.sample_count) == (0.04, 27)


def test_diagnostic_failure_cannot_change_control_or_ohmigo_dispatch(monkeypatch):
    sensor, states = _sensor()
    now = NOW
    monkeypatch.setattr(sensor_module.dt_util, "now", lambda: now)
    output = []

    async def send(hass, entry, fake, last):
        output.append(fake)
        return last

    monkeypatch.setattr(sensor_module, "async_push_ohmigo", send)
    asyncio.run(sensor.async_update())
    expected = (sensor.state, sensor.extra_state_attributes["mode"], output[-1])

    def fail(*args, **kwargs):
        raise ValueError("synthetic diagnostic failure")

    monkeypatch.setattr(sensor._thermal_observer, "observe", fail)
    # Reset control so both runs have identical PI/ramp origins.
    other, other_states = _sensor()
    monkeypatch.setattr(other._thermal_observer, "observe", fail)
    asyncio.run(other.async_update())
    assert (other.state, other.extra_state_attributes["mode"], output[-1]) == expected


@pytest.mark.parametrize("event_time", ["valid", None, "naive", "future"])
def test_start_qualifies_against_actual_event_with_safe_fallback(
    monkeypatch, event_time
):
    sensor, states = _sensor()
    sensor._attributes = {"mode": "braking", "brake_factor": 1}
    monkeypatch.setattr(sensor, "_do_update", AsyncMock())
    monkeypatch.setattr(
        sensor_module.dt_util, "now", lambda: NOW + timedelta(seconds=2)
    )
    states.update(
        {
            "sensor.indoor": report(21, NOW + timedelta(seconds=1)),
            "sensor.outdoor": report(5, NOW + timedelta(seconds=1)),
        }
    )
    fired = {
        "valid": NOW,
        "naive": NOW.replace(tzinfo=None),
        "future": NOW + timedelta(days=1),
        None: None,
    }[event_time]
    sensor.async_write_ha_state = lambda: None
    asyncio.run(sensor._handle_ha_started(SimpleNamespace(time_fired=fired)))
    assert len(sensor._thermal_model._temp_history) == (
        1 if event_time == "valid" else 0
    )


def test_ten_minute_reports_learn_without_counting_repeated_polls():
    obs = observer()
    for minute in range(211):
        at = NOW + timedelta(minutes=minute // 10 * 10)
        obs.observe(
            NOW + timedelta(minutes=minute),
            report(21 - (minute // 10) * 0.1, at),
            report(0, at),
            braking=True,
        )
    assert obs.model.pending_samples == 21
    obs.model.fit()
    assert obs.model.is_valid


def test_unchanged_distinct_ten_minute_reports_score_real_outcome():
    obs = observer()
    obs.model.restore_k(0.04, sample_count=25)
    for minute in range(31):
        at = NOW + timedelta(minutes=minute // 10 * 10)
        obs.observe(
            NOW + timedelta(minutes=minute), report(21, at), report(5, at), braking=True
        )
    result = obs.attributes()
    assert obs.model.pending_samples == 0  # flat temperature is not cooling evidence
    assert len(obs.model._temp_history) == 4
    assert result["last_comparison"]["30"]["actual_change_c"] == 0
    assert result["quality"]["30"]["count"] == 1


@pytest.mark.parametrize("heat", [True, None])
def test_external_heat_breaks_learning_and_forecasts_until_new_reports(heat):
    obs = observer()
    obs.model.restore_k(0.04, sample_count=25)
    for minute in range(5):
        now = NOW + timedelta(minutes=minute)
        obs.observe(now, report(21 - minute * 0.01, now), report(5, now), braking=True)
    assert obs.model.pending_samples > 0
    now += timedelta(minutes=1)
    inside, outside = report(20.9, now), report(5, now)
    obs.observe(now, inside, outside, braking=True, external_heat=heat)
    assert obs.model.pending_samples == 0
    assert not obs.model._temp_history
    assert obs.diagnostics.pending_count == 0
    assert obs.attributes()["reason"] == (
        "external_heat_active" if heat else "external_heat_unknown"
    )
    obs.observe(
        now + timedelta(seconds=1), inside, outside, braking=True, external_heat=False
    )
    assert not obs.model._temp_history
    now += timedelta(minutes=1)
    obs.observe(
        now, report(20.8, now), report(5, now), braking=True, external_heat=False
    )
    assert len(obs.model._temp_history) == 1
    assert obs.model.k == 0.04


@pytest.mark.parametrize(
    "heat_state", ["on", "unknown", "unavailable", None, "restored"]
)
def test_configured_external_heat_pauses_before_control_can_fit(
    monkeypatch, heat_state
):
    sensor, states = _sensor()
    sensor._config_entry.options["external_heat_entity"] = "input_boolean.fire"
    if heat_state is not None:
        states["input_boolean.fire"] = SimpleNamespace(
            state="off" if heat_state == "restored" else heat_state,
            attributes={"restored": heat_state == "restored"},
        )
    for minute in range(25):
        now = NOW + timedelta(minutes=minute)
        sensor._thermal_observer.observe(
            now, report(21 - minute * 0.01, now), report(5, now), braking=True
        )
    assert sensor._thermal_model.pending_samples >= 20
    # Match source configuration so the test exercises heat, not source reset.
    sensor._thermal_sources = sensor._thermal_source_ids()
    monkeypatch.setattr(sensor_module.dt_util, "now", lambda: now)

    async def finish_brake():
        sensor._thermal_model.fit()

    monkeypatch.setattr(sensor, "_do_update", finish_brake)
    asyncio.run(sensor.async_update())
    assert not sensor._thermal_model.is_valid
    assert sensor._thermal_model.pending_samples == 0


def test_report_during_heat_pause_cannot_start_recovery_series():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    obs.observe(NOW, report(21), report(5), braking=True, external_heat=True)
    # Report arrived after last on-poll, but before the first observed off state.
    at = NOW + timedelta(seconds=30)
    obs.observe(
        NOW + timedelta(minutes=1),
        report(20.9, at),
        report(5, at),
        braking=True,
        external_heat=False,
    )
    assert not obs.model._temp_history
    assert obs.diagnostics.pending_count == 0
    now = NOW + timedelta(minutes=2)
    obs.observe(
        now, report(20.8, now), report(5, now), braking=True, external_heat=False
    )
    assert len(obs.model._temp_history) == 1


@pytest.mark.parametrize("heat", ["off", "on", "unknown", "missing"])
def test_external_heat_setting_does_not_change_controller_or_output(monkeypatch, heat):
    monkeypatch.setattr(sensor_module.dt_util, "now", lambda: NOW)
    outputs = []

    async def send(hass, entry, fake, last):
        outputs.append(fake)
        return last

    monkeypatch.setattr(sensor_module, "async_push_ohmigo", send)
    baseline, _ = _sensor()
    candidate, states = _sensor()
    candidate._config_entry.options["external_heat_entity"] = "input_boolean.fire"
    if heat != "missing":
        states["input_boolean.fire"] = SimpleNamespace(state=heat, attributes={})
    asyncio.run(baseline.async_update())
    asyncio.run(candidate.async_update())
    assert outputs[0] == outputs[1]
    for key in ("mode", "brake_factor", "fake_outdoor_temperature", "price_category"):
        assert baseline.extra_state_attributes.get(
            key
        ) == candidate.extra_state_attributes.get(key)
    assert baseline._pi._integral == candidate._pi._integral


def test_unload_cancels_thermal_startup_and_template_listeners(monkeypatch):
    sensor, _ = _sensor()
    removed = []
    sensor.hass.bus = SimpleNamespace(
        async_listen=lambda *args: lambda: removed.append("template"),
        async_listen_once=lambda *args: lambda: removed.append("startup"),
    )
    monkeypatch.setattr(
        sensor, "async_get_last_extra_data", AsyncMock(return_value=None), raising=False
    )
    asyncio.run(sensor.async_added_to_hass())
    asyncio.run(sensor.async_will_remove_from_hass())
    assert sorted(removed) == ["startup", "template"]
