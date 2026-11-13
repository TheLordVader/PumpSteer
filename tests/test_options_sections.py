"""Saving a single options page preserves the other sections."""

import asyncio
from types import SimpleNamespace

import pytest
from options_form_helpers import submit_group

from custom_components.pumpsteer.options_flow import PumpSteerOptionsFlowHandler


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
        submit_group(flow, "thermal", {"thermal_intelligence_enabled": True})
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
    result = asyncio.run(submit_group(flow, "energy", {}))
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
        submit_group(flow, "thermal", {"thermal_intelligence_enabled": False})
    )
    assert result["data"]["thermal_intelligence_enabled"] is False


def test_energy_meter_selection_is_saved_and_can_be_cleared():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data={}, options={})
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    saved = asyncio.run(
        submit_group(
            flow, "energy", {"thermal_energy_entities": ["sensor.a", "sensor.b"]}
        )
    )
    assert saved["data"]["thermal_energy_entities"] == ["sensor.a", "sensor.b"]
    flow.config_entry.options = saved["data"]
    cleared = asyncio.run(submit_group(flow, "energy", {}))
    assert cleared["data"]["thermal_energy_entities"] == []


def test_basic_options_save_report_age_without_changing_other_sections():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={}, options={"thermal_intelligence_enabled": True}
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    result = asyncio.run(
        submit_group(flow, "report_age", {"temperature_report_max_age_minutes": 90})
    )
    assert result["data"]["temperature_report_max_age_minutes"] == 90
    assert result["data"]["thermal_intelligence_enabled"] is True


def test_basic_options_save_and_clear_optional_temperature_sources():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data={}, options={})
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    saved = asyncio.run(
        submit_group(
            flow,
            "temperature_sources",
            {
                "indoor_temp_entity_2": "sensor.room2",
                "indoor_temp_weight_1": 5,
                "indoor_temp_weight_2": 3,
                "outdoor_temp_entity_2": "sensor.backup",
            },
        )
    )["data"]
    assert saved["indoor_temp_weight_2"] == 3
    flow.config_entry.options = saved
    cleared = asyncio.run(
        submit_group(flow, "temperature_sources", {"indoor_temp_weight_1": 5})
    )
    assert cleared["data"]["indoor_temp_entity_2"] == ""
    assert cleared["data"]["outdoor_temp_entity_2"] == ""
    assert cleared["data"]["indoor_temp_weight_2"] == 3


def test_zone_page_saves_configuration_and_preserves_existing_options():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={}, options={"thermal_intelligence_enabled": True}
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    result = asyncio.run(
        submit_group(
            flow,
            "zone_1",
            {
                "zone_1_name": "Garage",
                "zone_1_entity": "sensor.garage",
                "zone_1_threshold": 5,
                "zone_1_enabled": True,
            },
        )
    )
    assert result["data"]["zone_1_entity"] == "sensor.garage"
    assert result["data"]["thermal_intelligence_enabled"] is True


def test_zone_validation_requires_sensor_and_valid_notify_service():
    flow = PumpSteerOptionsFlowHandler()
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity: SimpleNamespace(state="10"))
    )
    errors = flow._validate_entities(
        {"zone_1_enabled": True, "zone_1_notify_service": "light.turn_on"}
    )
    assert errors["zone_1_entity"] == "required"
    assert errors["zone_1_notify_service"] == "invalid_notification_setting"


def test_each_zone_page_preserves_other_zones_and_shared_recipient():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={},
        options={
            "zone_1_entity": "sensor.a",
            "zone_2_entity": "sensor.b",
            "zone_notify_service": "notify.family",
        },
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    result = asyncio.run(submit_group(flow, "zone_1", {"zone_1_name": "Garage"}))
    assert result["data"]["zone_1_entity"] == ""
    assert result["data"]["zone_2_entity"] == "sensor.b"
    assert result["data"]["zone_notify_service"] == "notify.family"


def test_all_zone_pages_preserve_other_zones_and_recipient():
    for index in range(1, 5):
        original = {f"zone_{i}_entity": f"sensor.room{i}" for i in range(1, 5)}
        original["zone_notify_service"] = "notify.shared"
        flow = PumpSteerOptionsFlowHandler()
        flow.config_entry = SimpleNamespace(data={}, options=original)
        flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
        flow._validate_entities = lambda data: {}
        flow.async_create_entry = lambda **kwargs: kwargs
        saved = asyncio.run(
            submit_group(
                flow,
                f"zone_{index}",
                {
                    f"zone_{index}_entity": f"sensor.new{index}",
                    f"zone_{index}_profile": "comfort",
                    f"zone_{index}_notify_service": "",
                },
            )
        )["data"]
        assert saved[f"zone_{index}_entity"] == f"sensor.new{index}"
        assert saved[f"zone_{index}_profile"] == "comfort"
        assert saved["zone_notify_service"] == "notify.shared"
        for other in set(range(1, 5)) - {index}:
            assert saved[f"zone_{other}_entity"] == f"sensor.room{other}"


def test_section_translations_cover_all_fields_and_zone_help():
    import json
    from pathlib import Path

    from custom_components.pumpsteer.options_flow import _BASIC_FIELDS, _FORM_SECTIONS

    for path in [
        Path("custom_components/pumpsteer/strings.json"),
        *[
            Path(f"custom_components/pumpsteer/translations/{lang}.json")
            for lang in ("en", "sv", "se")
        ],
    ]:
        steps = json.loads(path.read_text())["options"]["step"]
        assert set(steps) == {"init"}
        form = steps["init"]
        assert set(form["data"]) == _BASIC_FIELDS
        assert set(form["sections"]) == set(_FORM_SECTIONS)
        for name, keys in _FORM_SECTIONS.items():
            section = form["sections"][name]
            assert set(section["data"]) == keys
            assert section["name"] and section["description"]
        for i in range(1, 5):
            assert form["sections"][f"zone_{i}"]["data_description"][
                f"zone_{i}_notify_service"
            ]


def test_main_sensor_page_rejects_duplicate_with_existing_secondary(monkeypatch):

    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={},
        options={
            "indoor_temp_entity": "sensor.a",
            "indoor_temp_entity_2": "sensor.b",
            "real_outdoor_entity": "sensor.out",
            "electricity_price_entity": "sensor.price",
            "price_tomorrow_entity": "sensor.price",
        },
    )
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [], get=lambda entity: SimpleNamespace(state="20")
        )
    )
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    result = asyncio.run(
        submit_group(flow, "basic_settings", {"indoor_temp_entity": "sensor.b"})
    )
    assert result["type"] == "form"
    assert result["errors"]["indoor_temp_entity"] == "duplicate_temperature_sensor"


def test_additional_heat_sensor_cannot_alias_verified_heat_signal(monkeypatch):

    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={},
        options={
            "thermal_detection_method": "verified",
            "thermal_heat_activity_entity": "binary_sensor.heat",
        },
    )
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [], get=lambda entity: SimpleNamespace(state="on")
        )
    )
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    result = asyncio.run(
        submit_group(
            flow, "heat_details", {"thermal_compressor_entity": "binary_sensor.heat"}
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["heat_pump_data"] == "heat_signal_alias"


def test_supply_sensor_requires_heating_mode_on_same_page(monkeypatch):

    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data={}, options={})
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [], get=lambda entity: SimpleNamespace(state="20")
        )
    )
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    result = asyncio.run(
        submit_group(
            flow,
            "heat_sensors",
            {
                "thermal_detection_method": "manual",
                "thermal_supply_temp_entity": "sensor.supply",
            },
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["heat_pump_data"] == "entity_not_found"


def test_zone_recipient_omitted_by_ui_is_cleared_to_inherit_shared():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={},
        options={
            "zone_1_entity": "sensor.a",
            "zone_1_notify_service": "notify.old",
            "zone_notify_service": "notify.shared",
        },
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    saved = asyncio.run(submit_group(flow, "zone_1", {"zone_1_entity": "sensor.a"}))[
        "data"
    ]
    assert saved["zone_1_notify_service"] == ""
    assert saved["zone_notify_service"] == "notify.shared"


def test_legacy_data_heat_signal_alias_is_rejected(monkeypatch):

    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={
            "thermal_detection_method": "verified",
            "thermal_heat_activity_entity": "binary_sensor.heat",
        },
        options={},
    )
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [], get=lambda entity: SimpleNamespace(state="on")
        )
    )
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    result = asyncio.run(
        submit_group(
            flow, "heat_details", {"thermal_compressor_entity": "binary_sensor.heat"}
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["heat_pump_data"] == "heat_signal_alias"


def test_optional_heat_pump_telemetry_preserves_existing_options_and_can_clear():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={"thermal_balance_entity": "sensor.old"},
        options={
            "thermal_compressor_entity": "binary_sensor.compressor",
            "notify_service": "notify.family",
        },
    )
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    result = asyncio.run(
        submit_group(
            flow,
            "heat_pump_telemetry",
            {
                "thermal_balance_kind": "integral",
                "thermal_supply_target_entity": "sensor.target",
                "thermal_alarm_entity": "binary_sensor.alarm",
            },
        )
    )
    assert result["data"]["thermal_balance_entity"] == ""
    assert result["data"]["thermal_supply_target_entity"] == "sensor.target"
    assert result["data"]["thermal_alarm_entity"] == "binary_sensor.alarm"
    assert result["data"]["thermal_balance_kind"] == "integral"
    assert result["data"]["thermal_compressor_entity"] == "binary_sensor.compressor"
    assert result["data"]["notify_service"] == "notify.family"


def test_heat_pump_telemetry_menu_and_invalid_entity_validation():
    flow = PumpSteerOptionsFlowHandler()
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity: SimpleNamespace(state="10"))
    )
    errors = flow._validate_entities(
        {
            "thermal_balance_entity": "switch.nope",
            "thermal_alarm_entity": "climate.nope",
            "thermal_balance_kind": "bogus",
        }
    )
    assert errors["thermal_balance_entity"] == "entity_not_found"
    assert errors["thermal_alarm_entity"] == "entity_not_found"
    assert errors["thermal_balance_kind"] == "invalid_balance_kind"


def test_automatic_supply_sensor_can_be_saved_without_radiator_signal():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(
        data={}, options={"notify_service": "notify.me"}
    )
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [], get=lambda entity: SimpleNamespace(state="20")
        )
    )
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    saved = asyncio.run(
        submit_group(
            flow,
            "heat_sensors",
            {
                "thermal_detection_method": "automatic",
                "thermal_supply_temp_entity": "sensor.supply",
            },
        )
    )
    assert saved["type"] == "create_entry"
    assert saved["data"]["thermal_supply_temp_entity"] == "sensor.supply"
    assert saved["data"]["notify_service"] == "notify.me"


def test_manual_method_requires_supply_sensor():
    flow = PumpSteerOptionsFlowHandler()
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity: SimpleNamespace(state="on"))
    )
    errors = flow._validate_entities(
        {
            "thermal_detection_method": "manual",
            "thermal_heating_mode_entity": "binary_sensor.radiator",
        }
    )
    assert errors["thermal_supply_temp_entity"] == "required"


def _control_options_flow():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data={}, options={})
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [],
            get=lambda entity: SimpleNamespace(state="on"),
        )
    )
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    return flow


def test_control_pages_preserve_other_options_and_legacy_sensors():
    flow = _control_options_flow()
    flow.config_entry.data = {
        "thermal_aux_heat_entity": "binary_sensor.aux",
        "thermal_compressor_entity": "binary_sensor.compressor",
        "thermal_heating_mode_entity": "binary_sensor.heating",
        "thermal_hot_water_mode_entity": "binary_sensor.water",
    }
    flow.config_entry.options = {
        "zone_1_entity": "sensor.room",
        "notify_service": "notify.family",
    }
    result = asyncio.run(
        submit_group(
            flow,
            "extended_control",
            {
                "control_aux_preheat_limit": True,
                "control_signals_verified": True,
                "control_measurement_time_attribute": "measured_at",
            },
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"]["control_aux_preheat_limit"] is True
    assert result["data"]["zone_1_entity"] == "sensor.room"
    assert "thermal_aux_heat_entity" not in result["data"]  # retained in entry.data
    flow.config_entry.options = result["data"]
    result = asyncio.run(
        submit_group(flow, "advanced_control", {"control_balance_brake_limit": False})
    )
    assert result["data"]["control_aux_preheat_limit"] is True
    assert result["data"]["notify_service"] == "notify.family"


def test_optional_sensors_do_not_enable_control():
    flow = _control_options_flow()
    result = asyncio.run(
        submit_group(
            flow,
            "heat_pump_telemetry",
            {
                "thermal_aux_heat_entity": "binary_sensor.aux",
                "thermal_balance_entity": "sensor.balance",
            },
        )
    )
    assert "control_aux_preheat_limit" not in result["data"]
    assert "control_balance_brake_limit" not in result["data"]


def test_control_activation_requires_verified_signals_and_valid_normalization():
    flow = _control_options_flow()
    base = {
        "control_aux_preheat_limit": True,
        "control_balance_brake_limit": True,
        "thermal_aux_heat_entity": "binary_sensor.aux",
        "thermal_balance_entity": "sensor.balance",
        "thermal_compressor_entity": "binary_sensor.compressor",
        "thermal_heating_mode_entity": "binary_sensor.heating",
        "thermal_hot_water_mode_entity": "binary_sensor.water",
        "control_measurement_time_attribute": "measurement_time",
    }
    errors = flow._validate_entities(base)
    assert errors["control_aux_preheat_limit"] == "control_signals_required"
    assert errors["control_balance_brake_limit"] == "control_balance_required"
    base.update(
        control_signals_verified=True,
        control_balance_verified=True,
        control_balance_scale=-1,
        control_balance_offset=0,
        control_balance_deficit_threshold=100,
        control_balance_deficit_full=300,
    )
    errors = flow._validate_entities(base)
    assert "control_aux_preheat_limit" not in errors
    assert "control_balance_brake_limit" not in errors
    for change in (
        {"control_balance_scale": 0},
        {"control_balance_scale": float("nan")},
        {"control_balance_offset": None},
        {"control_balance_deficit_full": 100},
        {"control_balance_deficit_threshold": -1},
    ):
        errors = flow._validate_entities({**base, **change})
        assert errors["control_balance_brake_limit"] == "control_balance_required"
    errors = flow._validate_entities({**base, "control_measurement_time_attribute": ""})
    assert errors["control_aux_preheat_limit"] == "control_signals_required"


def test_balance_can_be_enabled_with_mode_and_balance_but_no_compressor():
    flow = _control_options_flow()
    # Legacy sensors in data must combine with newly saved options.
    flow.config_entry.data = {
        "thermal_heating_mode_entity": "binary_sensor.heating",
        "thermal_hot_water_mode_entity": "binary_sensor.water",
        "thermal_balance_entity": "sensor.balance",
    }
    flow.config_entry.options = {
        "control_signals_verified": True,
        "control_measurement_time_attribute": "measured_at",
    }
    merged = {
        **flow.config_entry.data,
        **flow.config_entry.options,
        "control_balance_brake_limit": True,
        "control_balance_verified": True,
        "control_balance_scale": -1,
        "control_balance_offset": 0,
        "control_balance_deficit_threshold": 100,
        "control_balance_deficit_full": 300,
    }
    assert "control_balance_brake_limit" not in flow._validate_entities(merged)
    result = asyncio.run(
        submit_group(
            flow,
            "advanced_control",
            {
                "control_balance_brake_limit": True,
                "control_balance_verified": True,
                "control_balance_scale": -1,
                "control_balance_offset": 0,
                "control_balance_deficit_threshold": 100,
                "control_balance_deficit_full": 300,
            },
        )
    )
    assert result["type"] == "create_entry", result
    assert result["data"]["control_balance_brake_limit"] is True


@pytest.mark.parametrize(
    "missing", ["thermal_heating_mode_entity", "thermal_hot_water_mode_entity"]
)
def test_balance_still_requires_verified_heating_and_hot_water_mode(missing):
    flow = _control_options_flow()
    cfg = {
        "control_balance_brake_limit": True,
        "control_balance_verified": True,
        "control_signals_verified": True,
        "control_balance_scale": -1,
        "control_balance_offset": 0,
        "control_balance_deficit_threshold": 100,
        "control_balance_deficit_full": 300,
        "thermal_heating_mode_entity": "binary_sensor.heating",
        "thermal_hot_water_mode_entity": "binary_sensor.water",
        "thermal_balance_entity": "sensor.balance",
    }
    cfg.pop(missing)
    assert (
        flow._validate_entities(cfg)["control_balance_brake_limit"]
        == "control_signals_required"
    )


@pytest.mark.parametrize(
    "missing", ["thermal_compressor_entity", "thermal_aux_heat_entity"]
)
def test_auxiliary_control_requires_its_own_drift_sensors(missing):
    flow = _control_options_flow()
    cfg = {
        "control_aux_preheat_limit": True,
        "control_signals_verified": True,
        "thermal_compressor_entity": "binary_sensor.compressor",
        "thermal_heating_mode_entity": "binary_sensor.heating",
        "thermal_hot_water_mode_entity": "binary_sensor.water",
        "thermal_aux_heat_entity": "binary_sensor.aux",
    }
    cfg.pop(missing)
    assert (
        flow._validate_entities(cfg)["control_aux_preheat_limit"]
        == "control_signals_required"
    )


@pytest.mark.parametrize("inverted,valid", [(True, True), (False, False)])
def test_shared_valve_requires_verified_complementary_polarity(inverted, valid):
    flow = _control_options_flow()
    cfg = {
        "control_balance_brake_limit": True,
        "control_aux_preheat_limit": True,
        "control_balance_verified": True,
        "control_signals_verified": True,
        "control_balance_scale": -1,
        "control_balance_offset": 0,
        "control_balance_deficit_threshold": 100,
        "control_balance_deficit_full": 300,
        "thermal_compressor_entity": "binary_sensor.compressor",
        "thermal_heating_mode_entity": "binary_sensor.valve",
        "thermal_hot_water_mode_entity": "binary_sensor.valve",
        "thermal_heating_mode_inverted": inverted,
        "thermal_aux_heat_entity": "binary_sensor.aux",
        "thermal_balance_entity": "sensor.balance",
    }
    errors = flow._validate_entities(cfg)
    assert ("control_balance_brake_limit" not in errors) is valid
    assert ("control_aux_preheat_limit" not in errors) is valid


@pytest.mark.parametrize(
    "alias", ["thermal_aux_heat_entity", "thermal_brine_pump_entity"]
)
def test_auxiliary_control_rejects_aliased_compressor_evidence(alias):
    flow = _control_options_flow()
    cfg = {
        "control_aux_preheat_limit": True,
        "control_signals_verified": True,
        "thermal_compressor_entity": "binary_sensor.compressor",
        "thermal_heating_mode_entity": "binary_sensor.heating",
        "thermal_hot_water_mode_entity": "binary_sensor.water",
        "thermal_aux_heat_entity": "binary_sensor.aux",
    }
    cfg[alias] = cfg["thermal_compressor_entity"]
    assert (
        flow._validate_entities(cfg)["control_aux_preheat_limit"]
        == "control_signals_required"
    )


def test_cross_page_switch_to_shared_valve_revokes_previous_verification():
    flow = _control_options_flow()
    flow.config_entry.data = {
        "thermal_heating_mode_entity": "binary_sensor.radiator",
        "thermal_hot_water_mode_entity": "binary_sensor.valve",
    }
    flow.config_entry.options = {
        "control_signals_verified": True,
        "control_balance_verified": True,
        "zone_1_entity": "sensor.room",
    }
    result = asyncio.run(
        submit_group(
            flow,
            "heat_sensors",
            {
                "thermal_heating_mode_entity": "binary_sensor.valve",
                "thermal_heating_mode_inverted": True,
            },
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"]["control_signals_verified"] is False
    assert result["data"]["control_balance_verified"] is True
    assert result["data"]["zone_1_entity"] == "sensor.room"


def test_disabling_control_clears_optional_legacy_calibration_only_on_own_page():
    flow = _control_options_flow()
    flow.config_entry.data = {"control_balance_scale": -1, "control_balance_offset": 0}
    flow.config_entry.options = {
        "control_aux_preheat_limit": True,
        "control_balance_brake_limit": True,
    }
    result = asyncio.run(
        submit_group(flow, "advanced_control", {"control_balance_brake_limit": False})
    )
    assert result["type"] == "create_entry"
    assert result["data"]["control_balance_scale"] == ""
    assert result["data"]["control_balance_offset"] == ""
    assert result["data"]["control_balance_brake_limit"] is False
    assert result["data"]["control_aux_preheat_limit"] is True


def test_control_section_has_translated_safety_explanations():
    import json
    from pathlib import Path

    for filename in (
        "strings.json",
        "translations/en.json",
        "translations/sv.json",
        "translations/se.json",
    ):
        translations = json.loads(
            (Path("custom_components/pumpsteer") / filename).read_text()
        )
        section = translations["options"]["step"]["init"]["sections"][
            "advanced_control"
        ]
        assert section["description"]
        assert set(section["data"]) == set(section["data_description"])
        assert "50" in section["data_description"]["control_balance_deficit_full"]


def test_source_change_revokes_verification_across_pages():
    flow = _control_options_flow()
    flow.config_entry.options = {
        "control_signals_verified": True,
        "control_balance_verified": True,
        "thermal_compressor_entity": "binary_sensor.old",
        "thermal_balance_entity": "sensor.old_balance",
        "control_balance_scale": -1,
        "control_balance_offset": 0,
    }
    result = asyncio.run(
        submit_group(
            flow, "heat_details", {"thermal_compressor_entity": "binary_sensor.new"}
        )
    )
    assert result["data"]["control_signals_verified"] is False
    assert result["data"]["control_balance_verified"] is True
    flow.config_entry.options = result["data"]
    result = asyncio.run(
        submit_group(
            flow,
            "heat_pump_telemetry",
            {"thermal_balance_entity": "sensor.new_balance"},
        )
    )
    assert result["data"]["control_balance_verified"] is False
    assert result["data"]["control_balance_scale"] == -1


def test_calibration_and_measurement_attribute_changes_need_new_confirmation():
    flow = _control_options_flow()
    flow.config_entry.options = {
        "control_balance_verified": True,
        "control_balance_scale": -1,
        "control_balance_offset": 0,
        "control_signals_verified": True,
        "control_measurement_time_attribute": "measured_at",
    }
    result = asyncio.run(
        submit_group(
            flow,
            "advanced_control",
            {
                "control_balance_scale": 1,
                "control_balance_offset": 0,
                "control_balance_verified": True,
            },
        )
    )
    assert result["data"]["control_balance_verified"] is False
    flow.config_entry.options = result["data"]
    result = asyncio.run(
        submit_group(
            flow,
            "extended_control",
            {
                "control_signals_verified": True,
                "control_measurement_time_attribute": "new_time",
            },
        )
    )
    assert result["data"]["control_signals_verified"] is False
    flow.config_entry.options = result["data"]
    result = asyncio.run(
        submit_group(
            flow,
            "extended_control",
            {
                "control_signals_verified": True,
                "control_measurement_time_attribute": "new_time",
            },
        )
    )
    assert result["data"]["control_signals_verified"] is True
