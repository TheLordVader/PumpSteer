"""Compact passive diagnostic follows the controller without polling."""

import asyncio
from types import SimpleNamespace
from test_brake_provenance import _sensor
from custom_components.pumpsteer import sensor as module


def test_status_and_no_duplicate_writes():
    control, _ = _sensor()
    cls = getattr(module, "PumpSteerThermalStatusSensor", None)
    assert cls is not None
    ent = cls(control)
    ent.hass = control.hass
    calls = []
    ent.async_write_ha_state = lambda: calls.append(
        (ent.native_value, ent.extra_state_attributes)
    )
    asyncio.run(ent.async_added_to_hass())
    assert ent.native_value == "disabled"
    control._config_entry.options["thermal_intelligence_enabled"] = True
    control._pilot_status = "heat_signal_not_configured"
    ent._async_update_from_controller()
    assert ent.native_value == "waiting"
    assert ent.extra_state_attributes["passive"] is True
    ent._async_update_from_controller()
    assert len(calls) == 2
    control._pilot_plan = {
        "planning_status": "hypothetical_unvalidated",
        "reason": "test",
    }
    ent._async_update_from_controller()
    assert ent.native_value == "hypothetical_plan"
    assert ent.extra_state_attributes["planning_status"] == "hypothetical_unvalidated"


def test_previous_intervals_do_not_hide_current_sensor_loss():
    attrs = {
        "thermal_intelligence_status": "heat_signal_unavailable",
        "thermal_intelligence_reason": "heat_signal_unavailable",
        "thermal_intelligence_learning_intervals": 7,
        "thermal_intelligence_plan_detail": {"planning_status": "unavailable"},
    }
    controller = SimpleNamespace(
        unique_id="test",
        _attr_device_info={},
        _temperature_unloaded=False,
        _cfg=lambda: {"thermal_intelligence_enabled": True},
        extra_state_attributes=attrs,
    )
    ent = module.PumpSteerThermalStatusSensor(controller)
    ent.async_write_ha_state = lambda: None
    ent._async_update_from_controller()
    assert ent.native_value == "waiting"
    attrs["thermal_intelligence_status"] = "control_context_unqualified"
    ent._async_update_from_controller()
    assert ent.native_value == "waiting"
    attrs["thermal_intelligence_status"] = "learning"
    ent._async_update_from_controller()
    assert ent.native_value == "learning"
