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
        "zones",
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
    result = asyncio.run(flow.async_step_energy({}))
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


def test_energy_meter_selection_is_saved_and_can_be_cleared():
    flow = PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data={}, options={})
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    saved = asyncio.run(
        flow.async_step_energy({"thermal_energy_entities": ["sensor.a", "sensor.b"]})
    )
    assert saved["data"]["thermal_energy_entities"] == ["sensor.a", "sensor.b"]
    flow.config_entry.options = saved["data"]
    cleared = asyncio.run(flow.async_step_energy({}))
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
        flow.async_step_report_age({"temperature_report_max_age_minutes": 90})
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
        flow.async_step_temperature_sources(
            {
                "indoor_temp_entity_2": "sensor.room2",
                "indoor_temp_weight_1": 5,
                "indoor_temp_weight_2": 3,
                "outdoor_temp_entity_2": "sensor.backup",
            }
        )
    )["data"]
    assert saved["indoor_temp_weight_2"] == 3
    flow.config_entry.options = saved
    cleared = asyncio.run(
        flow.async_step_temperature_sources({"indoor_temp_weight_1": 5})
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
        flow.async_step_zone_1(
            {
                "zone_1_name": "Garage",
                "zone_1_entity": "sensor.garage",
                "zone_1_threshold": 5,
                "zone_1_enabled": True,
            }
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
    result = asyncio.run(flow.async_step_zone_1({"zone_1_name": "Garage"}))
    assert result["data"]["zone_1_entity"] == ""
    assert result["data"]["zone_2_entity"] == "sensor.b"
    assert result["data"]["zone_notify_service"] == "notify.family"


def test_zone_menu_offers_four_independent_pages():
    flow = PumpSteerOptionsFlowHandler()
    flow.async_show_menu = lambda **kwargs: kwargs
    result = asyncio.run(flow.async_step_zones())
    assert result["menu_options"] == [
        "zone_1",
        "zone_2",
        "zone_3",
        "zone_4",
        "zone_notifications",
    ]


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
            getattr(flow, f"async_step_zone_{index}")(
                {
                    f"zone_{index}_entity": f"sensor.new{index}",
                    f"zone_{index}_profile": "comfort",
                    f"zone_{index}_notify_service": "",
                }
            )
        )["data"]
        assert saved[f"zone_{index}_entity"] == f"sensor.new{index}"
        assert saved[f"zone_{index}_profile"] == "comfort"
        assert saved["zone_notify_service"] == "notify.shared"
        for other in set(range(1, 5)) - {index}:
            assert saved[f"zone_{other}_entity"] == f"sensor.room{other}"


def test_menu_translations_cover_all_pages_and_zone_help():
    import json
    from pathlib import Path

    for path in [
        Path("custom_components/pumpsteer/strings.json"),
        *[
            Path(f"custom_components/pumpsteer/translations/{lang}.json")
            for lang in ("en", "sv", "se")
        ],
    ]:
        steps = json.loads(path.read_text())["options"]["step"]
        for menu in ("init", "basic", "telemetry", "zones", "ohmigo"):
            assert all(key in steps for key in steps[menu]["menu_options"])
        for i in range(1, 5):
            page = steps[f"zone_{i}"]
            assert (
                page["description"]
                and page["data_description"][f"zone_{i}_notify_service"]
            )


def test_main_sensor_page_rejects_duplicate_with_existing_secondary(monkeypatch):
    from custom_components.pumpsteer import options_flow

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
        flow.async_step_basic_settings({"indoor_temp_entity": "sensor.b"})
    )
    assert result["type"] == "form"
    assert result["errors"]["indoor_temp_entity"] == "duplicate_temperature_sensor"


def test_additional_heat_sensor_cannot_alias_verified_heat_signal(monkeypatch):
    from custom_components.pumpsteer import options_flow

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
        flow.async_step_heat_details(
            {"thermal_compressor_entity": "binary_sensor.heat"}
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["thermal_compressor_entity"] == "heat_signal_alias"


def test_supply_sensor_requires_heating_mode_on_same_page(monkeypatch):
    from custom_components.pumpsteer import options_flow

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
        flow.async_step_heat_sensors(
            {
                "thermal_detection_method": "manual",
                "thermal_supply_temp_entity": "sensor.supply",
            }
        )
    )
    assert result["type"] == "form"
    assert result["errors"]["thermal_heating_mode_entity"] == "entity_not_found"


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
    saved = asyncio.run(flow.async_step_zone_1({"zone_1_entity": "sensor.a"}))["data"]
    assert saved["zone_1_notify_service"] == ""
    assert saved["zone_notify_service"] == "notify.shared"
