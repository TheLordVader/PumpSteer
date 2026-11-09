"""Actual controller boundaries feed one passive, stored forecast audit."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_safe_mode_output import setup_sensor  # noqa: F401
from test_temperature_input_safety import _report, control  # noqa: F401

from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit
from custom_components.pumpsteer.sensor import MODE_SAFE


def _start(fixture):
    sensor, states, clock = fixture
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    # Existing GOS configuration makes the empirical Ohmigo command unknown.
    _report(states, clock, unit_of_measurement="°C")
    asyncio.run(sensor._do_update())
    pending = sensor._plan_audit.fixed_pending
    assert pending is not None
    assert pending["kind"] == "shared_command_response"
    assert pending["curves"] == [{"name": "persistence", "rate": 0.0}]
    assert sensor._thermal_intelligence.current_command["coordinate"] is None
    assert sensor._thermal_intelligence.model.parameters() is None
    return sensor, states, clock


def test_controller_issues_before_fit_and_stores_same_actual_outcome(request):
    sensor, states, clock = _start(request.getfixturevalue("control"))
    issued = clock[0]
    saved = []
    sensor._pilot_store = SimpleNamespace(async_delay_save=lambda callback, delay: saved.append(callback()))
    for minute in range(5, 31, 5):
        clock[0] = issued + timedelta(minutes=minute)
        if minute == 30:
            states["sensor.indoor"].state = "20.8"
        _report(states, clock, unit_of_measurement="°C")
        asyncio.run(sensor._do_update())
    rows = sensor._plan_audit.fixed_records
    assert len(rows) == 1
    row = rows[0]
    assert row["horizon"] == 30
    assert row["issued"] == issued.isoformat()
    assert row["observed"] == clock[0].isoformat()
    assert row["actual"] == 20.8
    assert row["predictions"] == [21]
    assert row["physical_measurement_time"] == "unknown"
    # A score created after this cycle's checkpoint waits for the next bounded
    # whole-file checkpoint; it must not force a second write at the same time.
    clock[0] += timedelta(minutes=5)
    asyncio.run(sensor._save_pilot_store())
    assert saved[-1]["plan_audit"]["fixed_v2"] == rows
    assert saved[-1]["plan_audit"]["fixed_v1"] == []
    restored = ForecastAudit()
    restored.restore_plan(saved[-1]["plan_audit"], clock[0])
    assert restored.fixed_records == rows
    assert restored.fixed_pending is None


@pytest.mark.parametrize("interruption", ["source", "safe", "mode", "output"])
def test_controller_discards_pending_on_context_interruption(request, interruption):
    sensor, states, clock = _start(request.getfixturevalue("control"))
    issued = clock[0]
    old_context = sensor._plan_audit.fixed_pending["context_id"]
    clock[0] += timedelta(minutes=1)
    _report(states, clock, unit_of_measurement="°C")
    if interruption == "source":
        states["sensor.other"] = SimpleNamespace(state="21", last_reported=clock[0],
                                                  attributes={"unit_of_measurement": "°C"})
        sensor._config_entry.options["indoor_temp_entity"] = "sensor.other"
        asyncio.run(sensor._do_update())
    elif interruption == "output":
        sensor._config_entry.options["ohmigo_entity"] = "number.other_receiver"
        asyncio.run(sensor._do_update())
    elif interruption == "safe":
        states["sensor.indoor"].state = "unavailable"
        asyncio.run(sensor._do_update())
        assert sensor.extra_state_attributes["mode"] == MODE_SAFE
    else:
        # Exercise the real source snapshot/runtime adapter with a mode change.
        cfg = sensor._cfg()
        cycle = sensor._thermal_cycle(clock[0], 21, -3.3, -3.3, "braking", {"brake_factor": 1}, cfg)
        sensor._thermal_intelligence.begin_cycle(cycle, use_ohmigo_input=False)
        sensor._update_shared_thermal_audit(clock[0], cfg)
    pending = sensor._plan_audit.fixed_pending
    assert pending is None or pending["context_id"] != old_context
    assert not sensor._plan_audit.fixed_records
    clock[0] = issued + timedelta(minutes=30)
    states["sensor.indoor"].state = "20.8"
    _report(states, clock, unit_of_measurement="°C")
    asyncio.run(sensor._do_update())
    assert not sensor._plan_audit.fixed_records


def test_missing_cycles_cannot_bridge_a_two_hour_forecast(request):
    sensor, states, clock = _start(request.getfixturevalue('control'))
    clock[0] += timedelta(minutes=120)
    states['sensor.indoor'].state = '20.8'
    _report(states, clock, unit_of_measurement='°C')
    asyncio.run(sensor._do_update())
    assert not sensor._plan_audit.fixed_records
