"""Active modifiers use the same guarded output path as basic control."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_temperature_input_safety import control, _report  # noqa: F401
from test_safe_mode_output import setup_sensor  # noqa: F401
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
    assert sensor.state is None
