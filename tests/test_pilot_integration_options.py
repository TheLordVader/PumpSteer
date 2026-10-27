"""Experimental options are persistent, off by default and never alter PI."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from custom_components.pumpsteer import options_flow
from test_brake_provenance import _sensor, _cycle


def test_options_flow_defaults_and_retains_explicit_values(monkeypatch):
    flow = options_flow.PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data={}, options={})
    flow.hass = SimpleNamespace(states=SimpleNamespace(async_all=lambda domain: []))
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
    flow.async_show_form = lambda **kwargs: kwargs
    schema = asyncio.run(flow.async_step_init())["data_schema"]
    assert ("thermal_intelligence_enabled", False) in schema
    assert ("external_heat_sources", False) in schema
    flow.config_entry.options = {
        "thermal_intelligence_enabled": True,
        "external_heat_sources": True,
    }
    schema = asyncio.run(flow.async_step_init())["data_schema"]
    assert ("thermal_intelligence_enabled", True) in schema
    assert ("external_heat_sources", True) in schema
    flow._validate_entities = lambda data: {}
    flow.async_create_entry = lambda **kwargs: kwargs
    saved = asyncio.run(
        flow.async_step_init(
            {"thermal_intelligence_enabled": True, "external_heat_sources": True}
        )
    )
    assert saved["data"] == {
        "thermal_intelligence_enabled": True,
        "external_heat_sources": True,
    }


def test_disabled_is_state_identical_and_enabled_reports_honest_fallback(monkeypatch):
    base, _ = _sensor()
    candidate, _ = _sensor()
    candidate._config_entry.options["thermal_intelligence_enabled"] = False
    normal = _cycle(base, monkeypatch, 15)
    disabled = _cycle(candidate, monkeypatch, 15)
    assert normal == disabled
    candidate._config_entry.options["thermal_intelligence_enabled"] = True
    enabled = _cycle(candidate, monkeypatch, 16)
    _cycle(base, monkeypatch, 16)
    assert enabled["thermal_intelligence_status"] == "heat_signal_not_configured"
    assert candidate.state == base.state


def test_safe_and_recovery_never_turn_on_assistance(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    _cycle(sensor, monkeypatch, 15)
    states["sensor.indoor"].state = "unavailable"
    assert (
        _cycle(sensor, monkeypatch, 16)["thermal_intelligence_status"]
        == "suspended_safe"
    )
    states["sensor.indoor"].state = "20.5"
    assert (
        _cycle(sensor, monkeypatch, 17)["thermal_intelligence_status"]
        == "heat_signal_not_configured"
    )


def test_both_settings_have_english_and_swedish_text():
    root = Path(__file__).parents[1] / "custom_components/pumpsteer"
    for name in ("strings.json", "translations/en.json", "translations/sv.json"):
        section = json.loads((root / name).read_text())["options"]["step"]["init"]
        for key in ("thermal_intelligence_enabled", "external_heat_sources"):
            assert section["data"][key] and section["data_description"][key]


def test_disabled_does_not_expose_restored_diagnostic():
    sensor, _ = _sensor()
    sensor._attributes.update(
        thermal_intelligence_status="not_available",
        thermal_intelligence_reason="integration_incomplete",
    )
    assert "thermal_intelligence_status" not in sensor.extra_state_attributes
    assert "thermal_intelligence_reason" not in sensor.extra_state_attributes


def test_enabled_reads_real_heat_and_feeds_pilot_without_changing_pi(monkeypatch):
    from datetime import datetime, timedelta, timezone

    base, _ = _sensor()
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_heat_activity_entity="binary_sensor.house_heat",
    )
    states["sensor.indoor"].attributes["unit_of_measurement"] = "°C"
    states["sensor.outdoor"].attributes["unit_of_measurement"] = "°C"
    states["sensor.outdoor"].last_changed = datetime(
        2026, 10, 11, 8, tzinfo=timezone.utc
    )
    for minute in range(0, 66, 5):
        at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc) + timedelta(minutes=minute)
        states["binary_sensor.house_heat"] = SimpleNamespace(
            state="on",
            last_reported=at,
            last_changed=datetime(2026, 10, 11, 8, tzinfo=timezone.utc),
            attributes={},
        )
        _cycle(base, monkeypatch, minute)
        attrs = _cycle(sensor, monkeypatch, minute)
        assert sensor.state == base.state
    assert attrs["thermal_intelligence_learning_intervals"] == 1
    assert attrs["thermal_intelligence_plan"] == "not_available"


def test_external_heat_setting_prevents_unqualified_learning(monkeypatch):
    sensor, _ = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        external_heat_sources=True,
        thermal_heat_activity_entity="binary_sensor.house_heat",
    )
    attrs = _cycle(sensor, monkeypatch, 15)
    assert attrs["thermal_intelligence_status"] == "heat_signal_unavailable"
    assert attrs["thermal_intelligence_external_heat"] == "background_unverified"
    assert attrs["thermal_intelligence_learning_intervals"] == 0
    assert attrs["thermal_intelligence_plan"] == "not_available"


def test_update_error_breaks_pending_pilot_interval(monkeypatch):
    from custom_components.pumpsteer.pilot_passive import PilotPassive

    sensor, _ = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    sensor._pilot = PilotPassive()
    sensor._pilot.manager.house.start = 123
    sensor.hass.is_running = True

    async def broken():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(sensor, "_do_update", broken)
    asyncio.run(sensor.async_update())
    assert sensor._pilot.manager.house.start is None


def test_safe_does_not_expose_previous_forecast_or_price_as_current(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    _cycle(sensor, monkeypatch, 15)
    sensor._current_price = 2.0
    sensor._current_price_unit = "SEK/kWh"
    sensor._last_outlook = SimpleNamespace(night_min_temp=0, day_max_temp=None)
    states["sensor.indoor"].state = "unavailable"
    attrs = _cycle(sensor, monkeypatch, 16)
    assert attrs["thermal_intelligence_price"] == "unavailable"
    assert attrs["thermal_intelligence_forecast"] == "unavailable"


def test_disable_then_enable_does_not_continue_old_learning_interval(monkeypatch):
    from custom_components.pumpsteer.pilot_passive import PilotPassive

    sensor, _ = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    sensor._pilot = PilotPassive()
    sensor._pilot.manager.house.start = 123
    sensor._config_entry.options["thermal_intelligence_enabled"] = False
    _cycle(sensor, monkeypatch, 15)
    assert sensor._pilot is None
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    _cycle(sensor, monkeypatch, 16)
    assert sensor._pilot is None


def test_options_reload_does_not_show_previous_source_diagnostics(monkeypatch):
    sensor, _ = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    sensor._pilot_price_available = True
    sensor._pilot_forecast_available = True
    sensor._current_price = 2.0
    sensor._current_price_unit = "SEK/kWh"
    sensor._last_outlook = SimpleNamespace(night_min_temp=0, day_max_temp=None)

    async def no_update():
        pass

    monkeypatch.setattr(sensor, "async_update", no_update)
    asyncio.run(sensor.async_options_update_listener(sensor.hass, sensor._config_entry))
    attrs = sensor.extra_state_attributes
    assert attrs["thermal_intelligence_price"] == "unavailable"
    assert attrs["thermal_intelligence_forecast"] == "unavailable"


def test_enabled_diagnoses_original_quarter_hour_price_coverage(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    states["sensor.price"].attributes["unit_of_measurement"] = "SEK/kWh"
    states["sensor.price"].last_reported = at
    states["sensor.price"].attributes["raw_today"] = [
        {
            "start": (at + timedelta(minutes=15 * i)).isoformat(),
            "end": (at + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": 1.0 + i / 10,
        }
        for i in range(8)
    ]
    sensor._observe_pilot(sensor._cfg(), at, "normal")
    assert (
        sensor.extra_state_attributes["thermal_intelligence_price_forecast"]
        == "coverage_complete_issue_time_unknown"
    )
    assert sensor.extra_state_attributes["thermal_intelligence_plan"] == "not_available"


def test_diagnostic_failure_never_changes_primary_control(monkeypatch):
    from custom_components.pumpsteer import pilot_forecast

    base, _ = _sensor()
    sensor, _ = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    monkeypatch.setattr(
        pilot_forecast,
        "price_window",
        lambda *args: (_ for _ in ()).throw(
            RuntimeError("synthetic diagnostic failure")
        ),
    )
    _cycle(base, monkeypatch, 15)
    attrs = _cycle(sensor, monkeypatch, 15)
    assert sensor.state == base.state
    assert attrs["thermal_intelligence_status"] == "learning_error"
    assert attrs["thermal_intelligence_plan"] == "not_available"


def test_extra_heat_setting_allows_background_aware_unvalidated_learning(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        external_heat_sources=True,
        thermal_heat_activity_entity="binary_sensor.house_heat",
    )
    states["sensor.indoor"].attributes["unit_of_measurement"] = "°C"
    states["sensor.outdoor"].attributes["unit_of_measurement"] = "°C"
    states["sensor.outdoor"].last_changed = datetime(
        2026, 10, 11, 8, tzinfo=timezone.utc
    )
    for minute in range(0, 66, 5):
        at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc) + timedelta(minutes=minute)
        states["binary_sensor.house_heat"] = SimpleNamespace(
            state="on",
            last_reported=at,
            last_changed=datetime(2026, 10, 11, 8, tzinfo=timezone.utc),
            attributes={},
        )
        attrs = _cycle(sensor, monkeypatch, minute)
    assert attrs["thermal_intelligence_learning_intervals"] == 1
    assert attrs["thermal_intelligence_model"] == "unvalidated"
    assert attrs["thermal_intelligence_external_heat"] == "background_unverified"
    assert attrs["thermal_intelligence_plan"] == "not_available"


def test_unavailable_current_price_is_not_advertised_as_available():
    from datetime import datetime, timezone

    sensor, states = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    states["sensor.price"].state = "unavailable"
    states["sensor.price"].last_reported = at
    states["sensor.price"].attributes["unit_of_measurement"] = "SEK/kWh"
    sensor._current_price = 2.0
    sensor._observe_pilot(sensor._cfg(), at, "normal")
    assert sensor.extra_state_attributes["thermal_intelligence_price"] == "unavailable"


def test_plan_refusal_identifies_absent_heat_and_safe_mode(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    normal = _cycle(sensor, monkeypatch, 15)
    assert normal["thermal_intelligence_reason"] == "heat_signal_not_configured"
    states["sensor.indoor"].state = "unavailable"
    safe = _cycle(sensor, monkeypatch, 16)
    assert safe["thermal_intelligence_reason"] == "safe_mode"
