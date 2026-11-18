"""Configure sections preserve settings and validation across edits."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from test_configure_form import _frontend_values
from test_configure_form import flow as configure_flow

@pytest.fixture(name="flow")
def single_form_flow(monkeypatch):
    return configure_flow.__wrapped__(monkeypatch)


def submit(instance, values=None):
    return asyncio.run(instance.async_step_init(values))


def test_form_covers_every_field_and_separates_passive_and_active_controls(flow):
    expected = {key.schema for key in flow._fields({})}
    schema = submit(flow)["data_schema"]
    found = {
        str(child)
        for key, validator in schema.items()
        for child in (validator.schema if hasattr(validator, "options") else (key,))
    }
    assert found == expected
    assert set(schema["thermal"].schema) == {
        "thermal_intelligence_enabled",
        "external_heat_sources",
    }
    assert "adaptive_braking_enabled" in schema["advanced_control"].schema


def test_price_notifications_do_not_clear_hidden_zone_recipient(flow):
    flow.config_entry.options["zone_notify_service"] = "notify.zones"
    form = submit(flow)
    values = _frontend_values(form["data_schema"])
    assert "zone_notify_service" not in values["notifications"]
    values["notifications"]["notify_service"] = "notify.prices"
    saved = submit(flow, values)
    assert saved["data"] == {
        **flow.config_entry.options,
        "notify_service": "notify.prices",
    }


def test_sequential_section_edits_preserve_previous_saves(flow):
    original = copy.deepcopy(flow.config_entry.options)
    for section, key, value in (
        ("thermal", "thermal_intelligence_enabled", True),
        ("zone_notifications", "zone_notify_service", "notify.zones"),
        ("notifications", "notify_service", "notify.prices"),
        ("advanced_control", "adaptive_braking_enabled", True),
    ):
        form = submit(flow)
        values = _frontend_values(form["data_schema"])
        values[section][key] = value
        saved = submit(flow, values)
        assert saved["type"] == "create_entry"
        original[key] = value
        assert saved["data"] == original
        flow.config_entry.options = saved["data"]


def test_adaptive_flag_in_wrong_section_cannot_activate_control(flow):
    saved = submit(
        flow,
        {
            "thermal": {
                "thermal_intelligence_enabled": True,
                "adaptive_braking_enabled": True,
            },
        },
    )
    assert saved["data"] == {
        **flow.config_entry.options,
        "thermal_intelligence_enabled": True,
    }


def test_ti_without_internal_telemetry_does_not_activate_adaptive_control(flow):
    saved = submit(flow, {"thermal": {"thermal_intelligence_enabled": True}})
    assert saved["data"]["thermal_intelligence_enabled"] is True
    assert saved["data"]["adaptive_braking_enabled"] is False
    assert not any(
        key.startswith("thermal_") and key.endswith("_entity") for key in saved["data"]
    )


def test_error_in_another_section_opens_it_without_saving_partial_values(flow):
    flow.config_entry.options["thermal_compressor_entity"] = "binary_sensor.old"
    original = copy.deepcopy(flow.config_entry.options)
    flow._validate_entities = lambda cfg: (
        {"control_aux_preheat_limit": "control_signals_required"}
        if cfg.get("thermal_compressor_entity") == "binary_sensor.new"
        else {}
    )
    result = submit(
        flow,
        {"heat_pump_data": {"thermal_compressor_entity": "binary_sensor.new"}},
    )
    assert result["type"] == "form"
    assert result["errors"] == {"advanced_control": "control_signals_required"}
    assert result["data_schema"]["advanced_control"].options["collapsed"] is False
    assert set(result["errors"]) <= set(result["data_schema"]) | {"base"}
    assert (
        _frontend_values(result["data_schema"])["heat_pump_data"][
            "thermal_compressor_entity"
        ]
        == "binary_sensor.new"
    )
    assert flow.config_entry.options == original


def test_diagnostic_source_change_revokes_foreign_control_verification(flow):
    flow.config_entry.options.update(
        control_signals_verified=True, thermal_compressor_entity="binary_sensor.old"
    )
    original = copy.deepcopy(flow.config_entry.options)
    form = submit(flow)
    values = _frontend_values(form["data_schema"])
    values["heat_pump_data"]["thermal_compressor_entity"] = "binary_sensor.new"
    saved = submit(flow, values)
    assert saved["data"] == {
        **original,
        "thermal_compressor_entity": "binary_sensor.new",
        "control_signals_verified": False,
    }


def test_zone_recipient_can_be_intentionally_cleared_without_losing_prices(flow):
    flow.config_entry.options.update(
        zone_notify_service="notify.zones", notify_service="notify.prices"
    )
    form = submit(flow)
    values = _frontend_values(form["data_schema"])
    del values["zone_notifications"]["zone_notify_service"]
    saved = submit(flow, values)
    assert saved["data"] == {**flow.config_entry.options, "zone_notify_service": ""}


def test_old_control_dialog_cannot_restore_verification_after_other_flow_source_change(
    flow,
):
    flow.config_entry.options.update(
        control_signals_verified=True,
        thermal_compressor_entity="binary_sensor.old",
    )
    other = copy.copy(flow)
    control = submit(flow)
    stale = _frontend_values(control["data_schema"])
    diagnostics = submit(other)
    changed = _frontend_values(diagnostics["data_schema"])
    changed["heat_pump_data"]["thermal_compressor_entity"] = "binary_sensor.new"
    flow.config_entry.options = submit(other, changed)["data"]
    assert flow.config_entry.options["control_signals_verified"] is False

    rejected = submit(flow, stale)
    assert rejected["type"] == "form"
    assert rejected["errors"] == {"base": "configuration_changed"}
    refreshed = _frontend_values(rejected["data_schema"])
    assert refreshed["advanced_control"]["control_signals_verified"] is False
    unchanged = submit(flow, refreshed)
    assert unchanged["data"] == flow.config_entry.options
    assert unchanged["data"]["control_signals_verified"] is False


@pytest.mark.parametrize(
    "filename,word",
    [
        ("strings.json", "adaptive"),
        ("translations/en.json", "adaptive"),
        ("translations/sv.json", "adaptiv"),
        ("translations/se.json", "adaptiv"),
    ],
)
def test_external_heat_help_explains_its_existing_adaptive_effect(filename, word):
    texts = json.loads((Path("custom_components/pumpsteer") / filename).read_text())
    description = texts["options"]["step"]["init"]["sections"]["thermal"][
        "data_description"
    ]["external_heat_sources"]
    assert word in description.lower()
