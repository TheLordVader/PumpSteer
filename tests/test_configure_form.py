"""One Configure submission must preserve options and their provenance."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer import options_flow


@pytest.fixture
def flow(monkeypatch):
    instance = options_flow.PumpSteerOptionsFlowHandler()
    instance.config_entry = SimpleNamespace(
        data={
            "indoor_temp_entity": "sensor.room",
            "real_outdoor_entity": "sensor.outdoor",
            "electricity_price_entity": "sensor.today",
            "price_tomorrow_entity": "sensor.tomorrow",
            "weather_entity": "weather.home",
        },
        options={
            "thermal_intelligence_enabled": False,
            "adaptive_braking_enabled": False,
            "indoor_temp_entity_2": "sensor.second",
            "indoor_temp_weight_1": 70,
            "indoor_temp_weight_2": 20,
            "indoor_temp_weight_3": 10,
            "ohmigo_entity": "number.output",
            "modbus_payload_template": "value: '{{ fake_temp }}'",
            "private_future_key": {"keep": [1, 2]},
        },
    )
    instance.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [],
            get=lambda entity: SimpleNamespace(state="20"),
        )
    )
    instance.async_show_menu = lambda **kw: {"type": "menu", **kw}
    instance.async_show_form = lambda **kw: {"type": "form", **kw}
    instance.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return instance


def test_configure_opens_one_form_with_five_basic_fields(flow):
    result = asyncio.run(flow.async_step_init())
    assert result["type"] == "form"
    schema = result["data_schema"]
    assert list(schema)[:5] == [
        "indoor_temp_entity",
        "real_outdoor_entity",
        "electricity_price_entity",
        "price_tomorrow_entity",
        "weather_entity",
    ]
    assert "temperature_sources" in schema
    assert "outputs" in schema
    assert "thermal" in schema


def test_omitted_sections_keep_all_values_and_data_precedence(flow):
    original = dict(flow.config_entry.options)
    result = asyncio.run(flow.async_step_init(dict(flow.config_entry.data)))
    assert result["type"] == "create_entry"
    assert result["data"] == original
    assert "indoor_temp_entity" not in result["data"]


def test_one_thermal_edit_keeps_sources_outputs_and_unknown_options(flow):
    original = dict(flow.config_entry.options)
    result = asyncio.run(
        flow.async_step_init(
            {
                "thermal": {
                    "thermal_intelligence_enabled": True,
                    "adaptive_braking_enabled": False,
                    "external_heat_sources": False,
                }
            }
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"] == {**original, "thermal_intelligence_enabled": True}


def test_explicit_clear_overrides_data_without_clearing_omitted_sections(flow):
    flow.config_entry.data["thermal_power_entity"] = "sensor.old_power"
    result = asyncio.run(
        flow.async_step_init(
            {
                "heat_pump_data": {
                    "thermal_power_entity": "",
                }
            }
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"]["thermal_power_entity"] == ""
    assert result["data"]["indoor_temp_entity_2"] == "sensor.second"
    assert result["data"]["ohmigo_entity"] == "number.output"


def test_error_retains_edits_in_other_sections_and_expands_error_section(flow):
    result = asyncio.run(
        flow.async_step_init(
            {
                "temperature_sources": {"indoor_temp_entity_2": "sensor.room"},
                "notifications": {"notify_service": "notify.new"},
            }
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["temperature_sources"] == "duplicate_temperature_sensor"
    section = result["data_schema"]["temperature_sources"]
    assert section.options["collapsed"] is False
    # A corrected retry must not require re-entering another section's edit.
    corrected = asyncio.run(
        flow.async_step_init(
            {
                "temperature_sources": {"indoor_temp_entity_2": "sensor.other"},
                "notifications": {"notify_service": "notify.new"},
            }
        )
    )
    assert corrected["data"]["notify_service"] == "notify.new"
    assert flow.config_entry.options["indoor_temp_entity_2"] == "sensor.second"


def _frontend_values(schema):
    """HA initializes suggested values in every section, including collapsed ones."""
    result = {}
    for marker, validator in schema.items():
        if hasattr(validator, "options"):
            result[str(marker)] = _frontend_values(validator.schema)
        else:
            value = (marker.description or {}).get("suggested_value")
            if value is not None:
                result[str(marker)] = value
    return result


def test_unchanged_full_frontend_submission_does_not_materialize_defaults(flow):
    original_data = dict(flow.config_entry.data)
    original_options = dict(flow.config_entry.options)
    flow.config_entry.options["notification_cooldown_minutes"] = 60
    original_options["notification_cooldown_minutes"] = 60
    opened = asyncio.run(flow.async_step_init())
    submitted = _frontend_values(opened["data_schema"])
    saved = asyncio.run(flow.async_step_init(submitted))
    assert saved["type"] == "create_entry"
    assert saved["data"] == original_options
    assert flow.config_entry.data == original_data
    assert "zone_1_enabled" not in saved["data"]
    assert "external_heat_sources" not in saved["data"]


def test_clearing_optional_selector_in_full_form_preserves_its_neighbours(flow):
    opened = asyncio.run(flow.async_step_init())
    submitted = _frontend_values(opened["data_schema"])
    del submitted["temperature_sources"]["indoor_temp_entity_2"]
    saved = asyncio.run(flow.async_step_init(submitted))
    assert saved["type"] == "create_entry"
    assert saved["data"]["indoor_temp_entity_2"] == ""
    assert saved["data"]["indoor_temp_weight_1"] == 70
    assert saved["data"]["indoor_temp_weight_2"] == 20
    assert saved["data"]["ohmigo_entity"] == "number.output"


def test_validation_retry_displays_all_edits_including_cleared_values(flow):
    result = asyncio.run(
        flow.async_step_init(
            {
                "temperature_sources": {"indoor_temp_entity_2": "sensor.room"},
                "notifications": {"notify_service": "notify.new"},
                "outputs": {"ohmigo_entity": ""},
            }
        )
    )
    assert result["type"] == "form"
    retry = _frontend_values(result["data_schema"])
    assert retry["notifications"]["notify_service"] == "notify.new"
    assert "ohmigo_entity" not in retry["outputs"]
    retry["temperature_sources"]["indoor_temp_entity_2"] = "sensor.corrected"
    saved = asyncio.run(flow.async_step_init(retry))
    assert saved["type"] == "create_entry"
    assert saved["data"]["notify_service"] == "notify.new"
    assert saved["data"]["ohmigo_entity"] == ""


@pytest.mark.parametrize("value", ["", None])
def test_explicit_empty_optional_entity_is_saved_as_disabled(flow, value):
    result = asyncio.run(flow.async_step_init({"outputs": {"ohmigo_entity": value}}))
    assert result["type"] == "create_entry"
    assert result["data"]["ohmigo_entity"] == ""
    assert result["data"]["modbus_payload_template"] == ""


def test_unknown_input_cannot_write_arbitrary_options(flow):
    saved = asyncio.run(
        flow.async_step_init(
            {
                "not_a_section": {"indoor_temp_entity": "sensor.fake"},
                "thermal": {"unknown_setting": True},
            }
        )
    )
    assert saved["data"] == flow.config_entry.options


def test_select_suggests_string_but_preserves_stored_integer_on_noop(flow):
    # HA's select validator requires a string even for legacy numeric storage.
    flow.config_entry.options["notification_cooldown_minutes"] = 60
    form = asyncio.run(flow.async_step_init())
    submitted = _frontend_values(form["data_schema"])
    assert submitted["notifications"]["notification_cooldown_minutes"] == "60"
    saved = asyncio.run(flow.async_step_init(submitted))
    assert saved["data"]["notification_cooldown_minutes"] == 60


def test_first_heat_signal_cannot_turn_displayed_automatic_into_verified(flow):
    form = asyncio.run(flow.async_step_init())
    submitted = _frontend_values(form["data_schema"])
    assert submitted["heat_pump_data"]["thermal_detection_method"] == "automatic"
    submitted["heat_pump_data"].update(
        thermal_heat_activity_entity="binary_sensor.shared",
        thermal_compressor_entity="binary_sensor.shared",
    )
    result = asyncio.run(flow.async_step_init(submitted))
    assert result["type"] == "form"
    assert result["errors"]["heat_pump_data"] == "heat_signal_alias"


def test_first_supply_signal_keeps_the_detection_method_shown_in_the_form(flow):
    form = asyncio.run(flow.async_step_init())
    submitted = _frontend_values(form["data_schema"])
    submitted["heat_pump_data"].update(
        thermal_supply_temp_entity="sensor.supply",
        thermal_heating_mode_entity="binary_sensor.heating",
    )
    result = asyncio.run(flow.async_step_init(submitted))
    assert result["type"] == "create_entry"
    assert result["data"]["thermal_detection_method"] == "automatic"


def test_errors_target_visible_native_form_elements(flow):
    result = asyncio.run(
        flow.async_step_init(
            {
                "temperature_sources": {"indoor_temp_entity_2": "sensor.room"},
            }
        )
    )
    # Native ha-form renders errors on top-level schema names. It does not
    # traverse invented section/field paths into expandable sections.
    assert set(result["errors"]) <= set(result["data_schema"]) | {"base"}
    assert result["errors"]["temperature_sources"] == "duplicate_temperature_sensor"
