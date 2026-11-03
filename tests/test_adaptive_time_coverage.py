"""Genuine temperature evidence, temporal coverage and diagnostic-only sparse data."""

from datetime import timedelta
from types import SimpleNamespace

from test_adaptive_braking import START, train
from test_brake_provenance import _sensor, _cycle
from custom_components.pumpsteer.thermal_model import ThermalModel


def test_same_temperature_new_ha_report_and_battery_are_not_measurements(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _cycle(sensor, monkeypatch, 15)
    states["sensor.indoor"].attributes["battery"] = 99
    _cycle(sensor, monkeypatch, 16)
    assert not sensor._adaptive_new_report
    states["sensor.indoor"].state = "20.4"
    _cycle(sensor, monkeypatch, 17)
    assert sensor._adaptive_new_report


def test_sixty_minute_observations_can_fit_without_licensing_short_action():
    model = ThermalModel()
    for hour in range(25):
        indoor = 23 - 0.25 * hour
        model.record_temp(START + timedelta(hours=hour), indoor, adaptive_reports=True)
        model.collect_braking_sample(indoor, 5)
    assert model.pending_samples >= 20
    assert model.fit()
    for hour in (26, 28, 30):
        at = START + timedelta(hours=hour)
        model.start_validation_session(at, 21, 5, adaptive_observations=True)
        predicted = model.predict_drop(21, 5, 60)
        assert model.update_validation_session(
            at + timedelta(minutes=60), 21 - predicted, 5
        )
        model.end_validation_session(at + timedelta(minutes=60), 21 - predicted)
    assert (
        model.adaptive_reason(START + timedelta(hours=32), 16)
        == "validated_horizon_too_long"
    )


def test_real_thirty_minute_endpoint_does_not_require_invented_midpoint():
    model = train(ThermalModel())
    for hour in (2, 3, 4):
        at = START + timedelta(hours=hour)
        model.start_validation_session(at, 21, 5, adaptive_observations=True)
        prediction = model.predict_drop(21, 5, 30)
        assert model.update_validation_session(
            at + timedelta(minutes=30), 21 - prediction, 5
        )
        model.end_validation_session(at + timedelta(minutes=30), 21 - prediction)
    assert model.adaptive_reason(START + timedelta(hours=5), 16) == "qualified"


def test_prestart_source_clock_and_unchanged_republish_never_refresh_evidence(
    monkeypatch,
):
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        adaptive_braking_enabled=True, control_signals_verified=True
    )
    sensor._temperature_reported_after = START + timedelta(minutes=10)
    states["sensor.indoor"].attributes["measurement_time"] = (
        START + timedelta(minutes=9)
    ).isoformat()
    _cycle(sensor, monkeypatch, 11)
    assert not sensor._adaptive_new_report
    states["sensor.indoor"].state = "20.4"
    _cycle(sensor, monkeypatch, 12)
    assert not sensor._adaptive_new_report
    states["sensor.indoor"].attributes["measurement_time"] = (
        START + timedelta(minutes=13)
    ).isoformat()
    _cycle(sensor, monkeypatch, 13)
    assert sensor._adaptive_new_report
    observed = sensor._adaptive_report
    _cycle(sensor, monkeypatch, 14)
    assert not sensor._adaptive_new_report
    assert sensor._adaptive_report == observed


def test_generic_last_seen_never_confirms_unchanged_temperature(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        adaptive_braking_enabled=True,
        control_signals_verified=True,
        control_measurement_time_attribute="last_seen",
    )
    for minute in (15, 16, 17):
        states["sensor.indoor"].attributes["last_seen"] = (
            START + timedelta(minutes=minute)
        ).isoformat()
        _cycle(sensor, monkeypatch, minute)
        assert not sensor._adaptive_new_report


def test_good_prospective_horizon_not_replaced_for_unrelated_long_session_mae():
    from test_adaptive_braking import qualified, validate

    model = qualified()
    model.restore_diagnostics(
        sample_count=model.sample_count,
        fit_rmse=model.fit_rmse,
        validated_sessions=3,
        prediction_mae=0.5,
    )
    for minute in range(61):
        temperature = 21 - 0.01 * minute
        model.record_temp(
            START + timedelta(hours=6, minutes=minute),
            temperature,
            adaptive_reports=True,
        )
        model.collect_braking_sample(temperature, 5)
    assert model.pending_samples >= 20
    assert not model.should_refit()
    for hour in (8, 9):
        validate(model, START + timedelta(hours=hour), error=0.25)
    assert model.should_refit()  # mixed bad rows are not three good short forecasts
    validate(model, START + timedelta(hours=10), error=0.25)
    assert model.should_refit()


def test_off_poll_samples_are_not_inherited_by_reenabled_adaptive_learning(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _cycle(sensor, monkeypatch, 15)
    sensor._config_entry.options["adaptive_braking_enabled"] = False
    for minute in range(16, 50):
        states["sensor.indoor"].state = str(21 - 0.01 * (minute - 16))
        _cycle(sensor, monkeypatch, minute)
    assert sensor._thermal_model.pending_samples > 0
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _cycle(sensor, monkeypatch, 50)
    assert sensor._thermal_model.pending_samples == 0
    assert not sensor._adaptive_new_report


def test_legacy_valid_coefficient_refits_from_genuine_points_not_poll_count():
    from test_adaptive_braking import validate

    model = ThermalModel()
    model.restore_k(0.04)
    model.restore_diagnostics(sample_count=300, fit_rmse=0.05)
    assert not model.adaptive_snapshot()["genuine_fit"]
    for minute in range(0, 151, 5):
        temperature = 21 - 0.01 * minute
        model.record_temp(
            START + timedelta(minutes=minute), temperature, adaptive_reports=True
        )
        model.collect_braking_sample(temperature, 5)
    assert model.pending_samples >= 20
    assert model.should_refit()
    assert model.fit()
    assert model.adaptive_snapshot()["genuine_fit"]
    for hour in (3, 4, 5):
        validate(model, START + timedelta(hours=hour))
    assert model.adaptive_reason(START + timedelta(hours=6), 16) == "qualified"


def test_actual_sixty_minute_chain_learns_but_cannot_license_short_control(monkeypatch):
    from test_adaptive_report_intervals import exercise, cold_step
    from custom_components.pumpsteer.control_replay import replay_control_step
    from dataclasses import replace

    sensor, states, accepted, count = exercise(monkeypatch, 60, episodes=10)
    model = sensor._thermal_model
    assert model.is_valid and model.sample_count >= 20
    assert (
        model.adaptive_reason(START + timedelta(minutes=7200), 16)
        == "validated_horizon_too_long"
    )
    assert len(model.adaptive_snapshot()["validations"]) == 3
    assert all(row[3] >= 60 for row in model.adaptive_snapshot()["validations"])
    before, data, actual = cold_step(sensor, states, monkeypatch, 7201)
    _, a = replay_control_step(before, data)
    _, b = replay_control_step(
        before, replace(data, cfg={**data.cfg, "adaptive_braking_enabled": False})
    )
    assert a.fake_temp == b.fake_temp
    assert a.attributes["adaptive_braking"]["multiplier"] == 1
    assert actual == round(a.fake_temp, 1)


def test_event_only_and_repeated_heartbeat_publishers_produce_same_temperature_evidence(
    monkeypatch,
):
    from test_adaptive_report_intervals import exercise

    periodic, _, observed_a, count_a = exercise(monkeypatch, 5)
    events, _, observed_b, count_b = exercise(monkeypatch, 5, event_driven=True)
    assert count_a["sensor.indoor"] > count_b["sensor.indoor"]
    assert observed_a == observed_b
    assert (
        periodic._thermal_model.adaptive_snapshot()
        == events._thermal_model.adaptive_snapshot()
    )


def test_flat_cached_series_never_creates_learning_or_safe_fault(monkeypatch):
    from test_adaptive_report_intervals import exercise

    sensor, states, observed, count = exercise(
        monkeypatch, 5, episodes=1, temperature_profile=lambda minute, ordinary: 23.0
    )
    assert not observed
    assert sensor._thermal_model.pending_samples == 0
    assert not sensor._thermal_model.is_valid
    assert sensor.extra_state_attributes["mode"] != "safe_mode"
    assert sensor._adaptive_braking["reason"] == "temperature_observation_missing"


def test_slowest_genuine_weighted_observation_controls_live_and_replay_freshness(
    monkeypatch,
):
    from dataclasses import replace
    from custom_components.pumpsteer.control_replay import (
        ControlReplayState,
        replay_control_step,
    )
    from test_control_replay_parity import _input
    from test_adaptive_braking import qualified

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        adaptive_braking_enabled=True,
        indoor_temp_entity_2="sensor.two",
        indoor_temp_entity_3="sensor.slow",
    )
    for entity in ("sensor.two", "sensor.slow"):
        states[entity] = SimpleNamespace(
            state="20.5", attributes={}, last_reported=START + timedelta(minutes=300)
        )

    async def prices(cfg, at):
        return [10.0] * 96, ["expensive"] * 96, 15, at.hour * 4 + at.minute // 15

    monkeypatch.setattr(sensor, "_get_prices", prices)
    _cycle(sensor, monkeypatch, 300)
    sensor._thermal_model = qualified()
    for minute in range(301, 333):
        for entity in ("sensor.two", "sensor.slow"):
            states[entity].last_reported = START + timedelta(minutes=minute)
        if minute == 301:
            for entity in ("sensor.indoor", "sensor.two", "sensor.slow"):
                states[entity].state = "20.4"
        if minute == 315:
            states["sensor.indoor"].state = "20.3"
        if minute == 320:
            states["sensor.two"].state = "20.3"
        if minute == 332:
            before = ControlReplayState.from_sensor(sensor)
        _cycle(sensor, monkeypatch, minute)
    assert len(sensor._temperature_coverage["indoor_active"]) == 3
    assert not sensor._adaptive_inputs_fresh
    coverage = min(
        row["observed_at"] for row in sensor._adaptive_temperature_evidence.values()
    )
    data = replace(
        _input(
            sensor,
            START + timedelta(minutes=332),
            indoor=sensor._temperature_value(sensor._cfg(), "indoor_temp_entity"),
        ),
        temperature_report_time=coverage,
    )
    _, replayed = replay_control_step(before, data)
    assert replayed.attributes["adaptive_braking"]["multiplier"] == 1
    assert sensor.state == round(replayed.fake_temp, 1)


def test_same_cached_value_after_outage_waits_for_new_observation_without_erasing_model(
    monkeypatch,
):
    from test_adaptive_braking import qualified

    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True

    async def prices(cfg, at):
        return [10.0] * 96, ["expensive"] * 96, 15, at.hour * 4 + at.minute // 15

    monkeypatch.setattr(sensor, "_get_prices", prices)
    _cycle(sensor, monkeypatch, 300)
    model = sensor._thermal_model = qualified()
    states["sensor.indoor"].state = "19.9"
    _cycle(sensor, monkeypatch, 301)
    assert sensor._adaptive_braking["state"] == "adapting"
    accepted = sensor._adaptive_report
    states["sensor.indoor"].state = "unavailable"
    _cycle(sensor, monkeypatch, 302)
    states["sensor.indoor"].state = "19.9"
    _cycle(sensor, monkeypatch, 303)
    assert sensor._thermal_model is model
    assert not sensor._adaptive_new_report
    assert not sensor._adaptive_inputs_fresh
    assert sensor._adaptive_report == accepted
    assert sensor._adaptive_braking["multiplier"] == 1
    states["sensor.indoor"].state = "19.8"
    _cycle(sensor, monkeypatch, 304)
    assert sensor._adaptive_new_report
    assert sensor._adaptive_braking["state"] == "adapting"
    assert all(
        at >= START + timedelta(minutes=304) for at, value in model._temp_history
    )


def test_first_subresolution_endpoint_is_not_replaced_by_later_larger_drop():
    model = train(ThermalModel())
    for hour in (2, 3, 4):
        at = START + timedelta(hours=hour)
        model.start_validation_session(at, 21, 5, adaptive_observations=True)
        model.update_validation_session(at + timedelta(minutes=30), 20.9, 5)
        # A later more favorable/resolved endpoint cannot replace the issued one.
        model.update_validation_session(at + timedelta(minutes=45), 20.7, 5)
        model.end_validation_session(at + timedelta(minutes=45), 20.7)
    assert model.adaptive_reason(START + timedelta(hours=5), 16) != "qualified"
    assert all(row[3] == 30 and row[5] < 0.2 for row in model._adaptive_validations)
