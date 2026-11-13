"""Optional native-HA smoke probe; all states and configuration are synthetic.

Run from the repository root in an isolated environment with HA installed.
Does not start HA, use a real config entry, call services or contact production.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import voluptuous_serialize
from homeassistant.core import State
from homeassistant.helpers import config_validation as cv

from custom_components.pumpsteer.options_flow import PumpSteerOptionsFlowHandler

DATA = {
    "indoor_temp_entity": "sensor.example_indoor",
    "real_outdoor_entity": "sensor.example_outdoor",
    "electricity_price_entity": "sensor.example_price_today",
    "price_tomorrow_entity": "sensor.example_price_tomorrow",
    "weather_entity": "weather.example",
}
OPTIONS = {
    "indoor_temp_entity_2": "sensor.example_indoor_2",
    "indoor_temp_entity_3": "sensor.example_indoor_3",
    "indoor_temp_weight_1": 70,
    "indoor_temp_weight_2": 20,
    "indoor_temp_weight_3": 10,
    "ohmigo_entity": "number.example_output",
    "modbus_payload_template": 'value: "{{ fake_temp }}"',
    "thermal_intelligence_enabled": False,
    "adaptive_braking_enabled": False,
    "notification_cooldown_minutes": 60,
    "unknown_preserved_option": {"keep": [1, 2]},
}


def initial_data(schema):
    result = {}
    for field in schema:
        if field.get("type") == "expandable":
            result[field["name"]] = initial_data(field["schema"])
        elif "suggested_value" in field.get("description", {}):
            value = field["description"]["suggested_value"]
            if value is not None:
                result[field["name"]] = value
    return result


async def main():
    entry = SimpleNamespace(data=DATA, options=OPTIONS, domain="pumpsteer")
    states = {
        key: State(key, "20", {"friendly_name": key.split(".")[1].replace("_", " ")})
        for key in [
            *DATA.values(),
            OPTIONS["indoor_temp_entity_2"],
            OPTIONS["indoor_temp_entity_3"],
            OPTIONS["ohmigo_entity"],
        ]
    }
    flow = PumpSteerOptionsFlowHandler()
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            get=states.get,
            async_all=lambda domain: [s for s in states.values() if s.domain == domain],
        ),
        config_entries=SimpleNamespace(async_get_known_entry=lambda _: entry),
    )
    flow.handler = "synthetic-ui-test"
    flow.flow_id = "synthetic-ui-test"
    form = await flow.async_step_init()
    schema = voluptuous_serialize.convert(
        form["data_schema"], custom_serializer=cv.custom_serializer
    )
    payload = initial_data(schema)
    validated = form["data_schema"](payload)
    saved = await flow.async_step_init(validated)
    assert saved["data"] == OPTIONS, saved
    # The native selectors and section validators process a cleared entity.
    del payload["temperature_sources"]["indoor_temp_entity_2"]
    saved = await flow.async_step_init(form["data_schema"](payload))
    assert saved["data"]["indoor_temp_entity_2"] == ""
    assert saved["data"]["indoor_temp_entity_3"] == "sensor.example_indoor_3"
    # HA must serialize both the nested error path and expanded section.
    payload["temperature_sources"]["indoor_temp_entity_2"] = DATA["indoor_temp_entity"]
    bad = await flow.async_step_init(form["data_schema"](payload))
    assert bad["errors"]["temperature_sources"] == "duplicate_temperature_sensor"
    bad_schema = voluptuous_serialize.convert(
        bad["data_schema"], custom_serializer=cv.custom_serializer
    )
    assert next(x for x in bad_schema if x["name"] == "temperature_sources")["expanded"]
    # Adding a first source must not change the displayed automatic method
    # into legacy manual/verified inference by omitting an implicit default.
    payload = initial_data(schema)
    shared = "binary_sensor.example_shared"
    states[shared] = State(shared, "on")
    payload["heat_pump_data"].update(
        thermal_heat_activity_entity=shared, thermal_compressor_entity=shared
    )
    aliased = await flow.async_step_init(form["data_schema"](payload))
    assert aliased["errors"]["heat_pump_data"] == "heat_signal_alias"
    print(
        "Native HA smoke: unchanged save, selector clearing, validation and section serialization passed"
    )
    print(
        "Fields:",
        sum(len(x["schema"]) if x.get("type") == "expandable" else 1 for x in schema),
        "Sections:",
        sum(x.get("type") == "expandable" for x in schema),
    )


asyncio.run(main())
