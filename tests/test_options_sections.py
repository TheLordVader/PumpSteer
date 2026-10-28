"""Saving a single options page preserves the other sections."""

import asyncio
from types import SimpleNamespace
from custom_components.pumpsteer.options_flow import PumpSteerOptionsFlowHandler


def test_menu_includes_separate_ohmigo_safety_page():
    flow = PumpSteerOptionsFlowHandler()
    flow.async_show_menu = lambda **kwargs: kwargs
    result = asyncio.run(flow.async_step_init())
    assert result["menu_options"] == [
        "basic",
        "thermal",
        "telemetry",
        "notifications",
        "ohmigo",
    ]


def test_thermal_page_preserves_other_options():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={},
        options={
            "indoor_temp_entity": "sensor.room",
            "real_outdoor_entity": "sensor.outside",
            "electricity_price_entity": "sensor.price",
            "price_tomorrow_entity": "sensor.price",
            "notification_level": "braking_only",
            "ohmigo_entity": "number.output",
        },
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    result = asyncio.run(
        flow.async_step_thermal({"thermal_intelligence_enabled": True})
    )
    assert result["data"]["thermal_intelligence_enabled"] is True
    assert result["data"]["notification_level"] == "braking_only"
    assert result["data"]["ohmigo_entity"] == "number.output"


def test_telemetry_page_can_clear_selected_sensor_and_preserves_other_pages():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={"thermal_power_entity": "sensor.old"},
        options={"notification_level": "all"},
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    result = asyncio.run(flow.async_step_telemetry({}))
    assert result["data"]["thermal_power_entity"] == ""
    assert result["data"]["notification_level"] == "all"


def test_disabling_thermal_does_not_depend_on_unavailable_indoor_sensor():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={}, options={"thermal_intelligence_enabled": True}
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {"indoor_temp_entity": "entity_unavailable"}
    flow.async_create_entry = lambda **kwargs: kwargs
    result = asyncio.run(
        flow.async_step_thermal({"thermal_intelligence_enabled": False})
    )
    assert result["data"]["thermal_intelligence_enabled"] is False
