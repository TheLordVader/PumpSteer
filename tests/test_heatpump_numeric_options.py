"""The numeric fault interpretation must be explicit and source-bound."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.options_flow import PumpSteerOptionsFlowHandler
from custom_components.pumpsteer import options_flow


@pytest.fixture(autouse=True)
def selectors(monkeypatch):
    monkeypatch.setattr(
        options_flow.vol,
        "Optional",
        lambda name, default=None, **kwargs: (name, default),
    )
    monkeypatch.setattr(
        options_flow.vol,
        "Required",
        lambda name, default=None, **kwargs: (name, default),
    )


def _flow(options=None):
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={
            "indoor_temp_entity": "sensor.inside",
            "real_outdoor_entity": "sensor.outside",
            "electricity_price_entity": "sensor.price",
            "price_tomorrow_entity": "sensor.price",
        },
        options=options or {},
    )
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [], get=lambda entity: SimpleNamespace(state="0")
        )
    )
    flow.async_create_entry = lambda **kwargs: kwargs
    flow.async_show_form = lambda **kwargs: kwargs
    return flow


def test_numeric_and_binary_fault_entities_are_selectable_without_implicit_activation():
    flow = _flow()
    for entity in ("sensor.fault", "binary_sensor.fault"):
        errors = flow._validate_entities({"thermal_alarm_entity": entity})
        assert "thermal_alarm_entity" not in errors


@pytest.mark.parametrize("normal", [None, "", "nan", "inf", True])
def test_verified_numeric_alarm_needs_a_valid_normal_value(normal):
    flow = _flow()
    errors = flow._validate_entities(
        {
            "thermal_alarm_entity": "sensor.fault",
            "thermal_alarm_numeric_verified": True,
            "thermal_alarm_numeric_normal": normal,
        }
    )
    assert errors.get("thermal_alarm_numeric_normal") == "invalid_numeric_alarm"


def test_numeric_alarm_settings_are_saved_without_changing_other_options():
    flow = _flow(
        {"thermal_alarm_entity": "sensor.fault", "zone_1_entity": "sensor.room"}
    )
    result = asyncio.run(
        flow.async_step_heat_pump_telemetry(
            {
                "thermal_alarm_entity": "sensor.fault",
                "thermal_alarm_numeric_verified": True,
                "thermal_alarm_numeric_normal": 0,
            }
        )
    )
    assert result["data"]["thermal_alarm_numeric_normal"] == 0
    assert result["data"]["thermal_alarm_numeric_verified"] is True
    assert result["data"]["zone_1_entity"] == "sensor.room"


@pytest.mark.parametrize(
    "changed", ["thermal_alarm_entity", "thermal_alarm_numeric_normal"]
)
def test_changed_numeric_source_or_normal_value_revokes_previous_verification(changed):
    flow = _flow(
        {
            "thermal_alarm_entity": "sensor.old",
            "thermal_alarm_numeric_verified": True,
            "thermal_alarm_numeric_normal": 0,
        }
    )
    values = {
        "thermal_alarm_entity": "sensor.old",
        "thermal_alarm_numeric_verified": True,
        "thermal_alarm_numeric_normal": 0,
    }
    values[changed] = "sensor.new" if changed == "thermal_alarm_entity" else 1
    result = asyncio.run(flow.async_step_heat_pump_telemetry(values))
    assert result["data"]["thermal_alarm_numeric_verified"] is False
