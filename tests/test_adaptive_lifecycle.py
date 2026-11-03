"""Preserve experience across unavailable reports; requalify after real drift."""

from datetime import timedelta
from types import SimpleNamespace


from test_adaptive_braking import START, qualified, validate
from test_brake_provenance import _sensor, _cycle


def learned_sensor(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _cycle(sensor, monkeypatch, 300)
    sensor._thermal_model = qualified()
    return sensor, states


def test_temporary_missing_reports_keep_learned_model_and_safe_restore_summary(
    monkeypatch,
):
    sensor, states = learned_sensor(monkeypatch)
    model = sensor._thermal_model
    saved = model.adaptive_snapshot()
    states["sensor.indoor"].state = "unavailable"
    _cycle(sensor, monkeypatch, 301)
    assert sensor._thermal_model is model
    assert model.adaptive_snapshot() == saved
    assert not sensor._adaptive_inputs_fresh
    attrs = sensor.extra_state_attributes
    assert attrs["thermal_k_valid"]
    assert attrs["adaptive_model_state"]["evidence"] == saved
    states["sensor.indoor"].state = "20.5"
    _cycle(sensor, monkeypatch, 302)
    assert sensor._thermal_model is model
    assert model.adaptive_reason(START + timedelta(minutes=302), 16) == "qualified"


def test_partial_weighted_composition_is_not_a_new_house(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        adaptive_braking_enabled=True,
        indoor_temp_entity_2="sensor.room2",
        indoor_temp_weight_2=2,
    )
    states["sensor.room2"] = SimpleNamespace(
        state="20.5", attributes={}, last_reported=START + timedelta(minutes=300)
    )
    _cycle(sensor, monkeypatch, 300)
    model = sensor._thermal_model = qualified()
    states["sensor.room2"].state = "unavailable"
    _cycle(sensor, monkeypatch, 301)
    assert sensor._thermal_model is model
    assert not sensor._adaptive_inputs_fresh
    assert sensor._adaptive_braking["multiplier"] == 1
    states["sensor.room2"].state = "20.5"
    states["sensor.room2"].last_reported = START + timedelta(minutes=302)
    _cycle(sensor, monkeypatch, 302)
    assert sensor._thermal_model is model
    assert sensor._adaptive_inputs_fresh
    # A genuine changed weight is a new response context, even if currently missing.
    states["sensor.room2"].state = "unavailable"
    sensor._config_entry.options["indoor_temp_weight_2"] = 3
    _cycle(sensor, monkeypatch, 303)
    assert sensor._thermal_model is not model
    assert not sensor._thermal_model.is_valid


def test_repeated_quarter_degree_failures_trigger_refit_then_independent_validation():
    model = qualified()
    for hour in (6, 7, 8):
        validate(model, START + timedelta(hours=hour), error=0.25)
    # The old general .3 C refit trigger does not see these failed adaptive checks.
    for minute in range(61):
        indoor = 21 - 0.012 * minute
        model.record_temp(START + timedelta(hours=9, minutes=minute), indoor)
        model.collect_braking_sample(indoor, 5)
    assert model.adaptive_reason(START + timedelta(hours=10), 16) == "validation_error"
    assert model.should_refit()
    assert model.fit()
    assert (
        model.adaptive_reason(START + timedelta(hours=10), 16)
        == "insufficient_validation"
    )
    for hour in (11, 12, 13):
        validate(model, START + timedelta(hours=hour))
    assert model.adaptive_reason(START + timedelta(hours=14), 16) == "qualified"


def test_temporary_backup_weather_source_pauses_without_replacing_model(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        adaptive_braking_enabled=True, outdoor_temp_entity_2="sensor.backup"
    )
    states["sensor.backup"] = SimpleNamespace(
        state="5", attributes={}, last_reported=START + timedelta(minutes=300)
    )
    _cycle(sensor, monkeypatch, 300)
    model = sensor._thermal_model = qualified()
    states["sensor.outdoor"].state = "unavailable"
    _cycle(sensor, monkeypatch, 301)
    assert sensor._outdoor_active_entity == "sensor.backup"
    assert sensor._thermal_model is model
    assert not sensor._adaptive_inputs_fresh
    states["sensor.outdoor"].state = "5"
    _cycle(sensor, monkeypatch, 302)
    _cycle(sensor, monkeypatch, 303)
    assert sensor._outdoor_active_entity == "sensor.outdoor"
    assert sensor._thermal_model is model
    assert sensor._adaptive_inputs_fresh


def test_legacy_summary_without_config_signature_relearns_changed_complete_source(
    monkeypatch,
):
    sensor, states = learned_sensor(monkeypatch)
    old = sensor._thermal_model
    sensor._adaptive_configuration = None  # ec67760 did not persist this field
    sensor._config_entry.options["indoor_temp_entity"] = "sensor.other"
    states["sensor.other"] = SimpleNamespace(state="20.5", attributes={})
    _cycle(sensor, monkeypatch, 301)
    assert sensor._thermal_model is not old
    assert not sensor._thermal_model.is_valid


def test_legacy_model_with_primary_delayed_and_backup_first_is_retained(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        adaptive_braking_enabled=True, outdoor_temp_entity_2="sensor.backup"
    )
    states["sensor.backup"] = SimpleNamespace(
        state="5", attributes={}, last_reported=START + timedelta(minutes=300)
    )
    _cycle(sensor, monkeypatch, 300)
    model = sensor._thermal_model = qualified()
    sensor._adaptive_configuration = None
    states["sensor.outdoor"].state = "unavailable"
    _cycle(sensor, monkeypatch, 301)
    assert sensor._thermal_model is model
    assert not sensor._adaptive_inputs_fresh
    states["sensor.outdoor"].state = "5"
    _cycle(sensor, monkeypatch, 302)
    _cycle(sensor, monkeypatch, 303)
    assert sensor._thermal_model is model
    assert sensor._adaptive_inputs_fresh
