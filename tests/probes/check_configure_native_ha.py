"""Native HA 2026.11.1 OptionsFlow/schema probe, entirely synthetic.

No HA start, services, integration reload or Store writes. Only entry persistence
and the integration-loader boundary are faked; routing/validation are actual HA.
PUMPSTEER_NATIVE_REPO can run these checks against an earlier checkout.
"""

import asyncio
import copy
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import homeassistant  # Initialize HA's schema compatibility before imports.
import probatio
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

ROOT = Path(
    os.environ.get("PUMPSTEER_NATIVE_REPO", Path(__file__).resolve().parents[2])
)
sys.path.insert(0, str(ROOT))
from custom_components.pumpsteer.config_flow import PumpSteerConfigFlow

DATA = {
    "indoor_temp_entity": "sensor.example_indoor",
    "real_outdoor_entity": "sensor.example_outdoor",
    "electricity_price_entity": "sensor.example_today",
    "price_tomorrow_entity": "sensor.example_tomorrow",
    "weather_entity": "weather.example",
}
OPTIONS = {
    "indoor_temp_entity_2": "sensor.example_indoor_2",
    "indoor_temp_entity_3": "sensor.example_indoor_3",
    "indoor_temp_weight_1": 70,
    "indoor_temp_weight_2": 20,
    "indoor_temp_weight_3": 10,
    "ohmigo_entity": "number.example_output",
    "ohmigo_relay_entity": "switch.example_relay",
    "modbus_service": "mqtt.publish",
    "modbus_payload_template": 'topic: synthetic/value\npayload: "{{ fake_temp }}"',
    "thermal_intelligence_enabled": False,
    "adaptive_braking_enabled": False,
    "notification_cooldown_minutes": 60,
    "zone_notify_service": "notify.example_zones",
    "unknown_preserved_option": {"keep": [1, 2]},
}


def serialized(form):
    return probatio.to_field_list(
        form["data_schema"], custom_serializer=cv.custom_serializer
    )


def initial_data(fields):
    """Match ha-form suggestions, including children in collapsed sections."""
    result = {}
    for field in fields:
        if field.get("type") == "expandable":
            result[field["name"]] = initial_data(field["schema"])
        elif "suggested_value" in field.get("description", {}):
            value = field["description"]["suggested_value"]
            if value is not None:
                result[field["name"]] = value
    return result


async def main():
    with TemporaryDirectory(prefix="pumpsteer-native-configure-") as directory:
        hass = HomeAssistant(directory)
        entry = SimpleNamespace(
            entry_id="synthetic-entry",
            domain="pumpsteer",
            data=copy.deepcopy(DATA),
            options=copy.deepcopy(OPTIONS),
        )
        updates = []

        def lookup(key):
            assert key == entry.entry_id
            return entry

        def update(selected, *, options):
            assert selected is entry
            if options == entry.options:
                return False
            entry.options = copy.deepcopy(options)
            updates.append(copy.deepcopy(options))
            return True

        def reload_forbidden(_):
            raise AssertionError("reload forbidden")

        hass.config_entries = SimpleNamespace(
            async_get_known_entry=lookup,
            async_update_entry=update,
            async_schedule_reload=reload_forbidden,
        )
        manager = config_entries.OptionsFlowManager(hass)
        calls = AsyncMock(side_effect=AssertionError("HA service forbidden"))

        async def handler(*_):
            return PumpSteerConfigFlow

        for entity in (
            *DATA.values(),
            OPTIONS["indoor_temp_entity_2"],
            OPTIONS["indoor_temp_entity_3"],
            OPTIONS["ohmigo_entity"],
            OPTIONS["ohmigo_relay_entity"],
        ):
            hass.states.async_set(entity, "20")

        async def open_form():
            form = await manager.async_init(entry.entry_id)
            assert form["type"] == "form" and form["step_id"] == "init"
            return form

        async def save(form, payload):
            return await manager.async_configure(form["flow_id"], payload)

        with (
            patch.object(config_entries, "_async_get_flow_handler", handler),
            patch.object(type(hass.services), "async_call", calls),
        ):
            form = await open_form()
            schema = serialized(form)
            assert [field["name"] for field in schema[:5]] == list(DATA)
            assert all(
                field["type"] == "expandable" and field["expanded"] is False
                for field in schema[5:]
            )
            fields = [field["name"] for field in schema[:5]] + [
                child["name"] for section in schema[5:] for child in section["schema"]
            ]
            assert len(fields) == len(set(fields)) == 94
            manager.async_abort(form["flow_id"])
            assert entry.options == OPTIONS and not updates
            print("PASS: actual OptionsFlowManager opens one form with all 94 fields")

            form = await open_form()
            assert (await save(form, initial_data(serialized(form))))[
                "type"
            ] == "create_entry"
            assert entry.options == OPTIONS and not updates and entry.data == DATA
            print("PASS: unchanged save preserves values, types and implicit defaults")

            expected = copy.deepcopy(OPTIONS)
            for section, key, value in (
                ("notifications", "notify_service", "notify.example_prices"),
                ("thermal", "thermal_intelligence_enabled", True),
                (

                    "zone_notifications",
                    "zone_notify_service",
                    "notify.other_zones",
                ),
            ):
                form = await open_form()
                payload = initial_data(serialized(form))
                payload[section][key] = value
                assert (await save(form, payload))["type"] == "create_entry"
                expected[key] = value
                assert entry.options == expected
            assert entry.options["adaptive_braking_enabled"] is False
            assert not any(
                k.startswith("thermal_") and k.endswith("_entity")
                for k in entry.options
            )
            print(
                "PASS: sequential saves retain previous sections; passive TI needs no internal telemetry"
            )

            for section, key in (
                ("outputs", "ohmigo_entity"),
                ("outputs", "ohmigo_relay_entity"),
                ("outputs", "modbus_service"),
                ("outputs", "modbus_payload_template"),
                ("zone_notifications", "zone_notify_service"),
            ):
                entry.options = copy.deepcopy(expected)
                form = await open_form()
                payload = initial_data(serialized(form))
                del payload[section][key]
                assert (await save(form, payload))["type"] == "create_entry"
                assert entry.options == {**expected, key: ""}
            entry.options = {
                **expected,
                "thermal_energy_entities": ["sensor.example_energy"],
            }
            hass.states.async_set("sensor.example_energy", "100")
            form = await open_form()
            payload = initial_data(serialized(form))
            payload["heat_pump_data"]["thermal_energy_entities"] = []
            assert (await save(form, payload))["type"] == "create_entry"
            assert entry.options == {**expected, "thermal_energy_entities": []}
            print(
                "PASS: independent Ohmigo/GOS/zone clearing and empty energy list preserve other sections"
            )

            entry.options = copy.deepcopy(expected)
            form = await open_form()
            payload = initial_data(serialized(form))
            payload["temperature_sources"]["indoor_temp_entity_2"] = DATA[
                "indoor_temp_entity"
            ]
            payload["notifications"]["notify_service"] = "notify.corrected"
            bad = await save(form, payload)
            assert bad["errors"] == {
                "temperature_sources": "duplicate_temperature_sensor"
            }
            assert entry.options == expected
            retry = initial_data(serialized(bad))
            assert retry["notifications"]["notify_service"] == "notify.corrected"
            retry["temperature_sources"]["indoor_temp_entity_2"] = (
                "sensor.example_indoor_2"
            )
            assert (await save(bad, retry))["type"] == "create_entry"
            assert entry.options == {**expected, "notify_service": "notify.corrected"}
            print(
                "PASS: native validation error retains draft and never saves partial options"
            )

            trusted = {
                **expected,
                "control_signals_verified": True,
                "control_aux_preheat_limit": True,
                "control_balance_brake_limit": True,
                "control_balance_verified": True,
                "control_measurement_time_attribute": "measurement_time",
                "thermal_detection_method": "automatic",
                "thermal_compressor_entity": "binary_sensor.example_compressor",
                "thermal_heating_mode_entity": "binary_sensor.example_heating",
                "thermal_hot_water_mode_entity": "binary_sensor.example_dhw",
                "thermal_aux_heat_entity": "binary_sensor.example_aux",
                "thermal_brine_pump_entity": "binary_sensor.example_brine",
                "thermal_balance_entity": "sensor.example_balance",
                "thermal_balance_kind": "degree_minutes",
                "control_balance_scale": -1,
                "control_balance_offset": 0,
                "control_balance_deficit_threshold": 30,
                "control_balance_deficit_full": 50,
                "thermal_alarm_entity": "sensor.example_fault",
                "thermal_alarm_numeric_normal": 0,
                "thermal_alarm_numeric_verified": True,
            }
            for key, value in trusted.items():
                if key.endswith("_entity") and isinstance(value, str):
                    hass.states.async_set(value, "20")
            hass.states.async_set("binary_sensor.example_compressor2", "on")
            for section, key, value, flag in (
                (

                    "heat_pump_data",
                    "thermal_compressor_entity",
                    "binary_sensor.example_compressor2",
                    "control_signals_verified",
                ),
                (

                    "advanced_control",
                    "control_balance_scale",
                    -2,
                    "control_balance_verified",
                ),
                (

                    "heat_pump_data",
                    "thermal_alarm_numeric_normal",
                    1,
                    "thermal_alarm_numeric_verified",
                ),
            ):
                entry.options = copy.deepcopy(trusted)
                form = await open_form()
                assert (await save(form, initial_data(serialized(form))))[
                    "type"
                ] == "create_entry"
                assert entry.options == trusted
                form = await open_form()
                payload = initial_data(serialized(form))
                payload[section][key] = value
                assert (await save(form, payload))["type"] == "create_entry"
                assert entry.options == {**trusted, key: value, flag: False}
            entry.options = copy.deepcopy(trusted)
            form = await open_form()
            payload = initial_data(serialized(form))
            del payload["heat_pump_data"]["thermal_heating_mode_entity"]
            rejected = await save(form, payload)
            assert (
                rejected["type"] == "form"
                and rejected["errors"]["advanced_control"] == "control_signals_required"
            )
            assert entry.options == trusted
            manager.async_abort(rejected["flow_id"])
            print(
                "PASS: three verification families preserved/revoked correctly; cross-section errors visible"
            )

            entry.options = copy.deepcopy(trusted)
            control = await open_form()
            stale = initial_data(serialized(control))
            diagnostics = await open_form()
            changed = initial_data(serialized(diagnostics))
            changed["heat_pump_data"]["thermal_compressor_entity"] = (
                "binary_sensor.example_compressor2"
            )
            assert (await save(diagnostics, changed))["type"] == "create_entry"
            assert entry.options["control_signals_verified"] is False
            before = copy.deepcopy(entry.options)
            rejected = await save(control, stale)
            assert rejected["type"] == "form"
            assert rejected["errors"] == {"base": "configuration_changed"}
            assert entry.options == before
            fresh = initial_data(serialized(rejected))
            assert fresh["advanced_control"]["control_signals_verified"] is False
            assert (await save(rejected, fresh))["type"] == "create_entry"
            assert entry.options == before
            print(
                "PASS: two concurrent native flows cannot restore revoked source verification"
            )
        assert calls.await_count == 0
        assert not list(Path(directory).rglob(".storage/*"))
        print(
            f"HA Core {homeassistant.const.__version__}: PASS; no HA start, services, reload or Store writes"
        )


if __name__ == "__main__":
    asyncio.run(main())
