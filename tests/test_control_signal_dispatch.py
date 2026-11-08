"""Active modifiers use the same guarded output path as basic control."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_temperature_input_safety import control, _report  # noqa: F401
from test_safe_mode_output import setup_sensor  # noqa: F401
from custom_components.pumpsteer import relay_guard
from custom_components.pumpsteer.control_intelligence import SIGNALS, evaluate_limits


@pytest.mark.parametrize("interruption", ["missing", "old_measurement", "changed"])
def test_optional_signal_change_blocks_second_output(control, interruption):
    sensor, states, clock = control
    _report(states, clock)
    cfg = sensor._config_entry.options
    cfg.update(control_aux_preheat_limit=True, control_signals_verified=True)
    for role, value in [
        ("compressor", "on"),
        ("heating", "on"),
        ("hot_water", "off"),
        ("auxiliary", "on"),
    ]:
        entity = "binary_sensor." + role
        cfg[SIGNALS[role]] = entity
        states[entity] = SimpleNamespace(
            state=value,
            last_reported=clock[0],
            attributes={"measurement_time": clock[0]},
        )
    cfg = sensor._cfg()
    sensor._control_intelligence = evaluate_limits(clock[0], cfg, states)
    sensor._control_applied_kind = "preheat_multiplier"
    sensor._control_signal_snapshot = {
        cfg[key]: (
            states[cfg[key]].state,
            states[cfg[key]].last_reported,
            states[cfg[key]].attributes["measurement_time"],
        )
        for key in SIGNALS.values()
        if cfg.get(key)
    }
    sensor._state = 7.0
    sensor._pi._integral = 2.5
    sensor._brake_ramp = 0.7
    calls = []

    async def dispatch(domain, service, data, blocking=False):
        calls.append((domain, service))
        source = states["binary_sensor.auxiliary"]
        if interruption == "missing":
            states.pop("binary_sensor.auxiliary")
        elif interruption == "old_measurement":
            source.attributes["measurement_time"] -= timedelta(minutes=10)
        else:
            source.state = "off"

    sensor.hass.services.async_call = dispatch
    asyncio.run(
        sensor._push_validated_temperature(
            0, cfg, {"indoor_temp_entity": 21.0, "real_outdoor_entity": -3.3}
        )
    )
    assert calls == [("test_output", "set_value")]
    assert sensor._ohmigo_last_push is None
    assert sensor.state == 7.0
    assert sensor._pi._integral == 2.5
    assert sensor._brake_ramp == 0.7
    assert sensor._attributes["control_signal_status"] == "interrupted"


@pytest.mark.parametrize("phase", ["gos", "number", "guard", "post"])
@pytest.mark.parametrize("change", ["refresh", "lost", "transient"])
def test_real_controller_output_and_guard_optional_events(
    control, monkeypatch, phase, change
):
    """A harmless report continues; loss invalidates only the optional decision."""
    sensor, states, clock = control
    _report(states, clock)
    cfg = sensor._config_entry.options
    cfg.update(
        aggressiveness=3,
        preheat_boost_enabled=True,
        control_aux_preheat_limit=True,
        control_signals_verified=True,
        ohmigo_relay_entity="switch.relay",
    )
    for role, value in [
        ("compressor", "on"),
        ("heating", "on"),
        ("hot_water", "off"),
        ("auxiliary", "on"),
    ]:
        entity = "binary_sensor." + role
        cfg[SIGNALS[role]] = entity
        states[entity] = SimpleNamespace(
            state=value,
            last_reported=clock[0],
            attributes={"measurement_time": clock[0]},
        )
    for entity, value in [
        ("switch.push", "on"),
        ("switch.guard", "on"),
        ("switch.relay", "off"),
    ]:
        states[entity] = SimpleNamespace(state=value, attributes={})
    states["sensor.indoor"].state = "20"
    sensor._pi._integral = 2.5
    sensor._preheat_ramp = 0.7
    sensor._prev_aggressiveness = 3

    async def prices(cfg, now):
        return [1.0] * 24, ["normal", "normal", "expensive"] + ["normal"] * 21, 60, 0

    async def forecast():
        return [-5.0] * 6

    monkeypatch.setattr(sensor, "_get_prices", prices)
    monkeypatch.setattr(sensor, "_forecast_temps", forecast)
    callbacks = []

    def track(_hass, ids, callback):
        entry = (set(ids), callback)
        callbacks.append(entry)
        return lambda: callbacks.remove(entry)

    monkeypatch.setattr(relay_guard, "async_track_state_change_event", track)
    import homeassistant.helpers.event as ha_event

    monkeypatch.setattr(ha_event, "async_track_state_change_event", track)
    monkeypatch.setattr(relay_guard, "_STABILIZE_SECONDS", 0)
    monkeypatch.setattr(relay_guard, "_VERIFY_SECONDS", 0)
    registry = SimpleNamespace(
        async_get_entity_id=lambda platform, domain, unique: (
            "switch.push"
            if unique.endswith("_ohmigo_enabled")
            else "switch.guard"
            if unique.endswith("_ohmigo_relay_guard")
            else None
        )
    )
    monkeypatch.setattr(relay_guard.er, "async_get", lambda _hass: registry)
    sensor.hass.async_create_task = asyncio.create_task
    guard = relay_guard.OhmigoRelayGuard(
        sensor.hass, sensor._config_entry, "switch.relay"
    )
    guard._started = True
    sensor.hass.data = {
        "pumpsteer": {sensor._config_entry.entry_id: {"relay_guard": guard}}
    }
    calls = []
    cause_event = [True]

    def publish(value, *, refresh=False):
        entity = "binary_sensor.auxiliary"
        old = states.get(entity)
        report = clock[0] + timedelta(seconds=1) if refresh else clock[0]
        new = SimpleNamespace(
            state=value, last_reported=report, attributes={"measurement_time": report}
        )
        states[entity] = new
        event = SimpleNamespace(
            data={"entity_id": entity, "old_state": old, "new_state": new}
        )
        for ids, callback in list(callbacks):
            if entity in ids:
                callback(event)

    async def dispatch(domain, service, data, blocking=False):
        calls.append((domain, service))
        selected = (
            (phase == "gos" and domain == "test_output")
            or (phase == "number" and domain == "number")
            or (phase == "guard" and domain == "switch" and service == "turn_on")
        )
        if selected and cause_event[0]:
            if change == "refresh":
                clock[0] += timedelta(seconds=1)
                publish("on", refresh=False)
            else:
                publish("unavailable")
                if change == "transient":
                    publish("on")
        if domain == "switch" and service == "turn_on":
            states["switch.relay"].state = "on"

    sensor.hass.services.async_call = dispatch
    asyncio.run(sensor.async_update())
    assert sensor.state is not None
    assert sensor.extra_state_attributes["mode"] == "preheating"
    assert sensor._pi._integral == pytest.approx(2.535)
    assert sensor._preheat_ramp >= 0.7
    assert callbacks == []
    if phase == "post":
        # Once dispatch completes, there is no permanent extra-source watcher;
        # the next normal control cycle observes current evidence afresh.
        assert ("number", "set_value") in calls
        assert ("switch", "turn_on") in calls
        if change == "refresh":
            publish("on", refresh=True)
        else:
            publish("unavailable")
            if change == "transient":
                publish("on")
        assert (
            sensor.extra_state_attributes.get("control_signal_status") != "interrupted"
        )
        clock[0] += timedelta(minutes=1)
        _report(states, clock)
        asyncio.run(sensor.async_update())
        assert sensor._pi._integral == pytest.approx(2.57)
        assert sensor._control_intelligence["preheat_multiplier"] == (
            1.0 if change == "lost" else 0.0
        )
        assert callbacks == []
        return
    if change == "refresh":
        assert ("number", "set_value") in calls
        assert ("switch", "turn_on") in calls
        assert (
            sensor.extra_state_attributes.get("control_signal_status") != "interrupted"
        )
    else:
        assert sensor.extra_state_attributes["control_signal_status"] == "interrupted"
        if phase != "guard":
            assert ("switch", "turn_on") not in calls
        else:
            assert guard.status == "awaiting_temperature_output"
        if phase == "gos":
            assert ("number", "set_value") not in calls
        # No immediate retry: the next normal update integrates exactly one
        # additional minute and re-evaluates the current observation.
        cause_event[0] = False
        clock[0] += timedelta(minutes=1)
        _report(states, clock)
        asyncio.run(sensor.async_update())
        assert sensor._pi._integral == pytest.approx(2.57)
        assert sensor.extra_state_attributes["mode"] == "preheating"
        assert (
            sensor.extra_state_attributes.get("control_signal_status") != "interrupted"
        )
        assert sensor._control_intelligence["preheat_multiplier"] == (
            1.0 if change == "lost" else 0.0
        )


def test_invalid_optional_input_before_service_preserves_control_state(control):
    sensor, states, clock = control
    _report(states, clock)
    cfg = sensor._config_entry.options
    cfg.update(control_aux_preheat_limit=True, control_signals_verified=True)
    for role, value in [
        ("compressor", "on"),
        ("heating", "on"),
        ("hot_water", "off"),
        ("auxiliary", "on"),
    ]:
        entity = "binary_sensor." + role
        cfg[SIGNALS[role]] = entity
        states[entity] = SimpleNamespace(
            state=value,
            last_reported=clock[0],
            attributes={"measurement_time": clock[0]},
        )
    cfg = sensor._cfg()
    sensor._control_intelligence = evaluate_limits(clock[0], cfg, states)
    sensor._control_applied_kind = "preheat_multiplier"
    sensor._control_signal_snapshot = {cfg[SIGNALS["auxiliary"]]: ()}
    states["binary_sensor.auxiliary"].state = "unavailable"
    sensor._state = 7
    sensor._pi._integral = 2.5
    asyncio.run(
        sensor._push_validated_temperature(
            0, cfg, {"indoor_temp_entity": 21.0, "real_outdoor_entity": -3.3}
        )
    )
    assert sensor.hass.services.calls == []
    assert sensor.state == 7
    assert sensor._pi._integral == 2.5
    assert sensor._attributes["control_signal_status"] == "interrupted"
