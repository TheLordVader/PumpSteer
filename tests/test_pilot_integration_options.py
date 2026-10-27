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
    assert enabled["thermal_intelligence_status"] == "not_available"
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
        == "not_available"
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
