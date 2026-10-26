"""The ordinary options flow persists an explicit, validated yes/no preference."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer import options_flow


VALID_ENTITIES = {
    "indoor_temp_entity": "sensor.indoor",
    "real_outdoor_entity": "sensor.outdoor",
    "electricity_price_entity": "sensor.price",
    "price_tomorrow_entity": "sensor.tomorrow",
}


def _flow(monkeypatch, *, data=None, options=None):
    defaults = {}

    def marker(key, **kwargs):
        if "default" in kwargs:
            defaults[key] = kwargs["default"]
        return key

    # Existing offline stubs omit HA schema defaults and description metadata.
    # Capture that boundary while exercising the real flow and validation.
    monkeypatch.setattr(options_flow.vol, "Required", marker)
    monkeypatch.setattr(options_flow.vol, "Optional", marker)
    flow = options_flow.PumpSteerOptionsFlowHandler()
    flow.config_entry = SimpleNamespace(data=data or {}, options=options or {})
    flow.hass = SimpleNamespace(
        states=SimpleNamespace(
            async_all=lambda domain: [],
            get=lambda entity: (
                SimpleNamespace(state="21")
                if entity in VALID_ENTITIES.values()
                else None
            ),
        )
    )
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}
    return flow, defaults


@pytest.mark.parametrize(
    ("data", "options", "expected"),
    [
        ({}, {}, False),
        ({"external_heat_sources": True}, {}, True),
        ({}, {"external_heat_sources": True}, True),
        ({"external_heat_sources": True}, {"external_heat_sources": False}, False),
    ],
)
def test_form_defaults_to_no_and_honors_existing_preference(
    monkeypatch, data, options, expected
):
    flow, defaults = _flow(monkeypatch, data=data, options=options)
    result = asyncio.run(flow.async_step_init())
    assert result["data_schema"]["external_heat_sources"] == {"boolean": {}}
    assert defaults["external_heat_sources"] is expected


@pytest.mark.parametrize("choice", [True, False])
def test_saves_yes_and_no_as_boolean(monkeypatch, choice):
    flow, _ = _flow(monkeypatch, options={"external_heat_sources": not choice})
    result = asyncio.run(
        flow.async_step_init({**VALID_ENTITIES, "external_heat_sources": choice})
    )
    assert result["type"] == "create_entry"
    assert result["data"]["external_heat_sources"] is choice


@pytest.mark.parametrize("existing", [{}, {"external_heat_sources": True}])
def test_missing_preference_saves_default_or_preserves_existing(monkeypatch, existing):
    flow, _ = _flow(monkeypatch, options=existing)
    result = asyncio.run(flow.async_step_init(dict(VALID_ENTITIES)))
    assert result["type"] == "create_entry"
    assert result["data"]["external_heat_sources"] is (True if existing else False)


@pytest.mark.parametrize("invalid", ["true", "false", "on", 0, 1, None, [], {}])
def test_rejects_non_boolean_preference(monkeypatch, invalid):
    flow, _ = _flow(monkeypatch)
    result = asyncio.run(
        flow.async_step_init({**VALID_ENTITIES, "external_heat_sources": invalid})
    )
    assert result["type"] == "form"
    assert result["errors"] == {
        "external_heat_sources": "invalid_external_heat_sources"
    }


@pytest.mark.parametrize(
    "relative", ["strings.json", "translations/en.json", "translations/sv.json"]
)
def test_option_has_localized_label_description_and_validation_error(relative):
    component = Path(__file__).resolve().parents[1] / "custom_components/pumpsteer"
    translation = json.loads((component / relative).read_text())
    form = translation["options"]["step"]["init"]
    assert form["data"]["external_heat_sources"]
    assert form["data_description"]["external_heat_sources"]
    assert translation["options"]["error"]["invalid_external_heat_sources"]
