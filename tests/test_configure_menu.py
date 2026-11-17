"""Native page boundaries must never change another page's saved settings."""

import asyncio
import copy

import pytest
from test_configure_form import _frontend_values
from test_configure_form import flow as configure_flow

PAGES = ("basic", "thermal", "diagnostics", "control")


@pytest.fixture(name="flow")
def menu_flow(monkeypatch):
    return configure_flow.__wrapped__(monkeypatch)


def visit(instance, page, values=None):
    return asyncio.run(getattr(instance, f"async_step_{page}")(values))


def test_configure_menu_has_four_direct_pages_and_does_not_save(flow):
    before = copy.deepcopy(flow.config_entry.options)
    menu = asyncio.run(flow.async_step_init())
    assert menu["type"] == "menu"
    assert list(menu["menu_options"]) == list(PAGES)
    assert flow.config_entry.options == before


def test_every_existing_field_is_visible_on_exactly_one_page(flow):
    expected = {key.schema for key in flow._fields({})}
    found = []
    for page, count in zip(PAGES, (22, 2, 60, 10), strict=True):
        form = visit(flow, page)
        keys = []
        for key, validator in form["data_schema"].items():
            if hasattr(validator, "options"):
                keys.extend(str(k) for k in validator.schema)
            else:
                keys.append(str(key))
        assert len(keys) == count
        found.extend(keys)
    assert len(found) == len(set(found)) == 94
    assert set(found) == expected
    assert set(visit(flow, "thermal")["data_schema"]["thermal"].schema) == {
        "thermal_intelligence_enabled",
        "external_heat_sources",
    }
    assert (
        "adaptive_braking_enabled"
        in visit(flow, "control")["data_schema"]["advanced_control"].schema
    )


@pytest.mark.parametrize("page", PAGES)
def test_opening_reopening_and_unchanged_page_saves_preserve_all_options(flow, page):
    flow.config_entry.options.update(
        zone_notify_service="notify.zones", notification_cooldown_minutes=60
    )
    before = copy.deepcopy(flow.config_entry.options)
    for other in PAGES:
        visit(flow, other)  # Opening/cancelling a page has no persistence effect.
    assert flow.config_entry.options == before
    form = visit(flow, page)
    saved = visit(flow, page, _frontend_values(form["data_schema"]))
    assert saved["type"] == "create_entry"
    assert saved["data"] == before
    assert flow.config_entry.options == before


def test_price_notifications_do_not_clear_hidden_zone_recipient(flow):
    flow.config_entry.options["zone_notify_service"] = "notify.zones"
    form = visit(flow, "basic")
    values = _frontend_values(form["data_schema"])
    assert "zone_notify_service" not in values["notifications"]
    values["notifications"]["notify_service"] = "notify.prices"
    saved = visit(flow, "basic", values)
    assert saved["data"] == {
        **flow.config_entry.options,
        "notify_service": "notify.prices",
    }


def test_sequential_page_saves_preserve_previous_page_changes(flow):
    original = copy.deepcopy(flow.config_entry.options)
    for page, section, key, value in (
        ("thermal", "thermal", "thermal_intelligence_enabled", True),
        ("diagnostics", "zone_notifications", "zone_notify_service", "notify.zones"),
        ("basic", "notifications", "notify_service", "notify.prices"),
        ("control", "advanced_control", "adaptive_braking_enabled", True),
    ):
        form = visit(flow, page)
        values = _frontend_values(form["data_schema"])
        values[section][key] = value
        saved = visit(flow, page, values)
        assert saved["type"] == "create_entry"
        original[key] = value
        assert saved["data"] == original
        flow.config_entry.options = saved["data"]


def test_hidden_or_stale_foreign_page_payload_cannot_change_control(flow):
    saved = visit(
        flow,
        "thermal",
        {
            "thermal": {
                "thermal_intelligence_enabled": True,
                "adaptive_braking_enabled": True,
            },
            "advanced_control": {"control_aux_preheat_limit": True},
            "indoor_temp_entity": "sensor.foreign",
        },
    )
    assert saved["data"] == {
        **flow.config_entry.options,
        "thermal_intelligence_enabled": True,
    }


def test_ti_without_internal_telemetry_does_not_activate_adaptive_control(flow):
    saved = visit(flow, "thermal", {"thermal": {"thermal_intelligence_enabled": True}})
    assert saved["data"]["thermal_intelligence_enabled"] is True
    assert saved["data"]["adaptive_braking_enabled"] is False
    assert not any(
        key.startswith("thermal_") and key.endswith("_entity") for key in saved["data"]
    )


def test_cross_page_error_is_visible_and_does_not_write_partial_values(flow):
    flow.config_entry.options["thermal_compressor_entity"] = "binary_sensor.old"
    original = copy.deepcopy(flow.config_entry.options)
    flow._validate_entities = lambda cfg: (
        {"control_aux_preheat_limit": "control_signals_required"}
        if cfg.get("thermal_compressor_entity") == "binary_sensor.new"
        else {}
    )
    result = visit(
        flow,
        "diagnostics",
        {"heat_pump_data": {"thermal_compressor_entity": "binary_sensor.new"}},
    )
    assert result["type"] == "form"
    assert result["errors"] == {"base": "control_signals_required"}
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
    form = visit(flow, "diagnostics")
    values = _frontend_values(form["data_schema"])
    values["heat_pump_data"]["thermal_compressor_entity"] = "binary_sensor.new"
    saved = visit(flow, "diagnostics", values)
    assert saved["data"] == {
        **original,
        "thermal_compressor_entity": "binary_sensor.new",
        "control_signals_verified": False,
    }


def test_zone_recipient_can_be_intentionally_cleared_without_losing_prices(flow):
    flow.config_entry.options.update(
        zone_notify_service="notify.zones", notify_service="notify.prices"
    )
    form = visit(flow, "diagnostics")
    values = _frontend_values(form["data_schema"])
    del values["zone_notifications"]["zone_notify_service"]
    saved = visit(flow, "diagnostics", values)
    assert saved["data"] == {**flow.config_entry.options, "zone_notify_service": ""}
