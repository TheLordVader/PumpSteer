"""Replayed/restored reports cannot become new physical observations."""

from datetime import timedelta
from types import SimpleNamespace
import asyncio

from custom_components.pumpsteer.control_intelligence import evaluate_limits
from test_control_intelligence import NOW, config, states
from test_temperature_input_safety import control, _report  # noqa: F401
from test_safe_mode_output import setup_sensor  # noqa: F401


def test_restored_sample_is_not_fresh_evidence_after_restart():
    cfg, obs = config(), states()
    startup = NOW + timedelta(seconds=30)
    # A restored template can get a new HA report time without a restored flag.
    for state in obs.values():
        state.last_reported = startup + timedelta(seconds=1)
    result = evaluate_limits(
        startup + timedelta(seconds=2), cfg, obs, measured_after=startup
    )
    assert result["preheat_multiplier"] == result["brake_multiplier"] == 1
    # Each source must actually provide a newly acquired message after startup.
    for state in obs.values():
        state.attributes["measurement_time"] = (
            startup + timedelta(seconds=1)
        ).isoformat()
    result = evaluate_limits(
        startup + timedelta(seconds=2), cfg, obs, measured_after=startup
    )
    assert result["preheat_multiplier"] == 0
    assert result["brake_multiplier"] == 0.5


def test_live_controller_requires_new_samples_after_ha_start(control):
    sensor, all_states, clock = control
    sensor._config_entry.options.update(config())
    sensor._temperature_reported_after = clock[0]
    for entity, source in states(clock[0]).items():
        all_states[entity] = SimpleNamespace(
            state=source.state,
            attributes=source.attributes.copy(),
            last_reported=clock[0] + timedelta(seconds=1),
        )
    clock[0] += timedelta(seconds=1)
    _report(all_states, clock)
    asyncio.run(sensor.async_update())
    assert sensor._control_intelligence["active"] is False
    clock[0] += timedelta(seconds=1)
    _report(all_states, clock)
    for entity in states(clock[0]):
        all_states[entity].last_reported = clock[0]
        all_states[entity].attributes["measurement_time"] = clock[0].isoformat()
    asyncio.run(sensor.async_update())
    assert sensor._control_intelligence["active"] is True


def test_live_balance_brake_does_not_depend_on_compressor_or_brine(
    control, monkeypatch
):
    sensor, all_states, clock = control
    options = config()
    options["control_aux_preheat_limit"] = False
    options["thermal_brine_pump_entity"] = "binary_sensor.absent_brine"
    options["aggressiveness"] = 3
    sensor._config_entry.options.update(options)
    all_states.update(states(clock[0]))
    all_states.pop("binary_sensor.compressor")
    _report(all_states, clock)
    sensor._brake_ramp = 1
    sensor._prev_aggressiveness = 3

    async def prices(cfg, now):
        return [2.0] * 96, ["expensive"] * 96, 15, 32

    monkeypatch.setattr(sensor, "_get_prices", prices)
    asyncio.run(sensor.async_update())
    assert sensor._control_intelligence["balance_reason"] == "deficit_limit"
    assert sensor.extra_state_attributes["brake_factor"] < 1
    assert set(sensor._control_signal_snapshot) == {
        "binary_sensor.heating",
        "binary_sensor.water",
        "sensor.balance",
    }
