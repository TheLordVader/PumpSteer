"""Experimental options are persistent, off by default and never alter PI."""

import asyncio
import json
import pytest
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


def test_enabled_without_weather_does_not_query_an_invalid_ha_entity(monkeypatch):
    sensor, _ = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    get_state = sensor.hass.states.get

    def strict_get(entity_id):
        if entity_id is None:
            raise AttributeError("Home Assistant requires an entity ID")
        return get_state(entity_id)

    sensor.hass.states.get = strict_get
    attrs = _cycle(sensor, monkeypatch, 15)
    assert attrs["mode"] != "safe_mode"
    assert attrs["thermal_intelligence_plan_detail"]["reason"] != "planning_error"


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


def test_hot_water_transition_during_learning_discards_fit(monkeypatch):
    from datetime import datetime, timezone
    from custom_components.pumpsteer import sensor as sensor_module

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_hot_water_mode_entity="binary_sensor.hot_water",
    )
    at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    states["binary_sensor.hot_water"] = SimpleNamespace(
        state="off", last_reported=at, last_changed=at, attributes={}
    )

    class Pump:
        def add_interval(self, sample):
            states["binary_sensor.hot_water"].state = "on"

    class Pilot:
        def __init__(self):
            self.manager = SimpleNamespace(pump=Pump())
            self.reset = False

        def reset_interval(self):
            self.reset = True

    pilot = Pilot()
    sensor._pilot = pilot
    monkeypatch.setattr(sensor, "_observe_pilot", lambda *args: object())
    monkeypatch.setattr(sensor_module.dt_util, "now", lambda: at)
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].last_reported = at
    asyncio.run(sensor._do_update())
    assert pilot.reset
    assert sensor._pilot_plan["reason"] == "inputs_changed_during_learning"


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


def test_response_observer_uses_reported_ohmigo_entity(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_heat_activity_entity="binary_sensor.house_heat",
        ohmigo_entity="number.reported_command",
        ohmigo_relay_entity="switch.active",
    )
    from custom_components.pumpsteer import ohmigo
    from custom_components.pumpsteer.pilot_vendor.pump_response import PumpResponseModel
    import threading

    thread_ids = []
    original = PumpResponseModel.add_interval

    def record_thread(self, interval):
        thread_ids.append(threading.get_ident())
        return original(self, interval)

    monkeypatch.setattr(PumpResponseModel, "add_interval", record_thread)
    main_thread = threading.get_ident()

    monkeypatch.setattr(
        ohmigo, "_switch_entity_id", lambda hass, entry_id: "switch.push"
    )
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].attributes["unit_of_measurement"] = "°C"
        states[key].last_changed = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)

    async def no_push(*args, **kwargs):
        pass

    monkeypatch.setattr(sensor, "_push_validated_temperature", no_push)
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    for minute in range(16):
        at = start + timedelta(minutes=minute)
        states["binary_sensor.house_heat"] = SimpleNamespace(
            state="on", last_reported=at, last_changed=start, attributes={}
        )
        states["number.reported_command"] = SimpleNamespace(
            state="4",
            last_reported=at,
            last_changed=start,
            attributes={"unit_of_measurement": "°C"},
        )
        states["switch.push"] = SimpleNamespace(
            state="on", last_reported=at, last_changed=start, attributes={}
        )
        states["switch.active"] = SimpleNamespace(
            state="on", last_reported=at, last_changed=start, attributes={}
        )
        _cycle(sensor, monkeypatch, minute)
    assert len(sensor._pilot.manager.pump.history) == 1
    assert thread_ids and thread_ids[0] != main_thread
    states["switch.active"].state = "off"
    _cycle(sensor, monkeypatch, 16)
    assert sensor._pilot.manager.pump.reason == "response_input_unavailable"

    states["switch.active"].state = "on"
    for minute in range(17, 33):
        at = start + timedelta(minutes=minute)
        for key in (
            "binary_sensor.house_heat",
            "number.reported_command",
            "switch.push",
            "switch.active",
        ):
            states[key].last_reported = at
        if minute == 32:
            import pytest

            async def cancelled(*args, **kwargs):
                raise asyncio.CancelledError

            monkeypatch.setattr(asyncio, "to_thread", cancelled)
            sensor._pilot.manager.pump.ready = True
            with pytest.raises(asyncio.CancelledError):
                _cycle(sensor, monkeypatch, minute)
            assert not sensor._pilot.manager.pump.ready
            assert sensor._pilot.manager.pump.reason == "interval_interrupted"
        else:
            _cycle(sensor, monkeypatch, minute)


def test_supply_and_verified_heating_mode_feed_observer(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_supply_temp_entity="sensor.supply",
        thermal_heating_mode_entity="binary_sensor.radiator_mode",
        thermal_supply_threshold=30,
    )
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].attributes["unit_of_measurement"] = "°C"
        states[key].last_changed = start
    for minute in range(0, 66, 5):
        at = start + timedelta(minutes=minute)
        states["sensor.supply"] = SimpleNamespace(
            state="32",
            last_reported=at,
            last_changed=start,
            attributes={"unit_of_measurement": "°C"},
        )
        states["binary_sensor.radiator_mode"] = SimpleNamespace(
            state="on", last_reported=at, last_changed=start, attributes={}
        )
        attrs = _cycle(sensor, monkeypatch, minute)
    assert attrs["thermal_intelligence_learning_intervals"] == 1
    assert attrs["thermal_intelligence_heat_source"] == "supply_temperature_indicator"
    states["binary_sensor.radiator_mode"].state = "off"
    attrs = _cycle(sensor, monkeypatch, 66)
    assert attrs["thermal_intelligence_status"] == "heat_signal_unavailable"


def test_supply_accepts_explicit_inverted_radiator_mode(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_supply_temp_entity="sensor.supply",
        thermal_heating_mode_entity="binary_sensor.valve",
        thermal_heating_mode_inverted=True,
        thermal_hot_water_mode_entity="binary_sensor.valve",
    )
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].attributes["unit_of_measurement"] = "°C"
        states[key].last_changed = start
    for minute in range(0, 66, 5):
        at = start + timedelta(minutes=minute)
        states["sensor.supply"] = SimpleNamespace(
            state="32",
            last_reported=at,
            last_changed=start,
            attributes={"unit_of_measurement": "°C"},
        )
        states["binary_sensor.valve"] = SimpleNamespace(
            state="off", last_reported=at, last_changed=start, attributes={}
        )
        attrs = _cycle(sensor, monkeypatch, minute)
    assert attrs["thermal_intelligence_learning_intervals"] == 1
    states["binary_sensor.valve"].state = "on"
    attrs = _cycle(sensor, monkeypatch, 66)
    assert attrs["thermal_intelligence_status"] == "hot_water_active"
    states["binary_sensor.valve"].state = "unknown"
    attrs = _cycle(sensor, monkeypatch, 67)
    assert attrs["thermal_intelligence_status"] == "hot_water_mode_unavailable"
    states["binary_sensor.valve"].state = "off"
    attrs = _cycle(sensor, monkeypatch, 72)
    assert attrs["thermal_intelligence_status"] == "hot_water_mode_unavailable"


def test_options_offer_supply_mode_and_threshold(monkeypatch):
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
    assert any(
        key[0] == "thermal_supply_temp_entity"
        for key in schema
        if isinstance(key, tuple)
    )
    assert any(
        key[0] == "thermal_heating_mode_entity"
        for key in schema
        if isinstance(key, tuple)
    )
    assert ("thermal_supply_threshold", 30) in schema


def test_new_paired_bad_forecast_invalidates_cached_plan(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_heat_activity_entity="binary_sensor.house_heat",
    )
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].attributes["unit_of_measurement"] = "°C"
        states[key].last_changed = start
    states["binary_sensor.house_heat"] = SimpleNamespace(
        state="on", last_reported=start, last_changed=start, attributes={}
    )
    _cycle(sensor, monkeypatch, 0)
    audit = sensor._pilot.audit
    audit.records = [
        {
            "revision": "r",
            "error": 0.2,
            "persistence_error": 0,
            "horizon_seconds": 900,
            "observed": start.isoformat(),
        }
        for _ in range(4)
    ]
    assert audit.issue(
        start, 20.5, 5, 1, 0.02, 0.4, 0.1, "r", source_identity=(None, None)
    )
    sensor._pilot_plan = {"planning_status": "validated_passive"}
    sensor._pilot_plan_next = start + timedelta(hours=1)
    for minute in (5, 10):
        at = start + timedelta(minutes=minute)
        states["binary_sensor.house_heat"].last_reported = at
        attrs = _cycle(sensor, monkeypatch, minute)
    assert attrs["thermal_intelligence_planning_status"] != "validated_passive"
    assert attrs["thermal_intelligence_forecast_quality"]["status"] == "degraded"


def test_degraded_model_keeps_monitoring_actual_outcomes_without_exposing_plan(
    monkeypatch,
):
    from datetime import datetime, timedelta, timezone
    import hashlib
    from custom_components.pumpsteer import forecast
    from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit
    from custom_components.pumpsteer.pilot_vendor.pump_response import (
        ResponseParameters,
    )

    sensor, states = _sensor()
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True, weather_entity="weather.local"
    )
    price = states["sensor.price"]
    price.last_reported = start
    price.attributes.update(
        unit_of_measurement="SEK/kWh",
        raw_today=[
            {
                "start": (
                    start.replace(hour=0) + timedelta(minutes=15 * i)
                ).isoformat(),
                "end": (
                    start.replace(hour=0) + timedelta(minutes=15 * (i + 1))
                ).isoformat(),
                "value": 1 + i / 100,
            }
            for i in range(96)
        ],
    )
    states["weather.local"] = SimpleNamespace(
        state="cloudy", last_reported=start, attributes={"temperature_unit": "°C"}
    )

    async def forecast_stub(*args, **kwargs):
        callback = kwargs.get("raw_weather_callback")
        if callback:
            callback(
                [
                    {
                        "datetime": (start + timedelta(hours=i)).isoformat(),
                        "temperature": 5 - i,
                    }
                    for i in range(7)
                ]
            )
        return []

    monkeypatch.setattr(forecast, "async_build_forecast", forecast_stub)
    house = {
        "loss": 0.02,
        "gain": 0.4,
        "background": 0.1,
        "mae": 0.02,
        "persistence_mae": 0.2,
        "samples": 8,
        "outdoor_min": -5,
        "outdoor_max": 10,
        "indoor_min": 19,
        "indoor_max": 22,
        "heat_min": 0,
        "heat_max": 1,
    }
    raw = "pilot-house-v1:" + ":".join(
        format(house[key], ".17g") for key in ("loss", "gain", "background")
    )
    revision = hashlib.sha256(raw.encode()).hexdigest()[:16]
    audit = ForecastAudit()
    audit.records = [
        {
            "revision": revision,
            "error": 0.2,
            "persistence_error": 0,
            "horizon_seconds": 900,
            "observed": start.isoformat(),
        }
        for _ in range(5)
    ]
    pump = SimpleNamespace(
        ready=True,
        reason="ready",
        parameters=ResponseParameters(),
        history=[(i, i % 2, i % 2, 5) for i in range(112)],
    )
    sensor._pilot = SimpleNamespace(
        house_intervals=8,
        audit=audit,
        last_indoor_report=start,
        source_identity=(None, None),
        manager=SimpleNamespace(
            pump=pump,
            recent=[(start.timestamp(), 0.5, 1.0)],
            response_state=lambda ts: (0.4, ()),
        ),
        validated_house=lambda now: house,
    )
    sensor._pilot_sources = (
        "sensor.indoor",
        "sensor.outdoor",
        "binary_sensor.heat",
        False,
    )
    monkeypatch.setattr(sensor, "_observe_pilot", lambda *args: None)
    attrs = _cycle(sensor, monkeypatch, 0)
    assert attrs["thermal_intelligence_planning_status"] == "unavailable"
    assert (
        attrs["thermal_intelligence_plan_detail"]["reason"]
        == "forecast_quality_degraded"
    )
    assert audit.pending is not None
    assert audit.pending["revision"] == revision
    for minute in (5, 10, 15):
        at = start + timedelta(minutes=minute)
        indoor = SimpleNamespace(
            state="20.5",
            last_reported=at if minute == 15 else start,
            last_changed=start,
            attributes={"unit_of_measurement": "°C"},
        )
        outdoor = SimpleNamespace(
            state="5",
            last_reported=at,
            last_changed=start,
            attributes={"unit_of_measurement": "°C"},
        )
        heat = SimpleNamespace(
            state="on", last_reported=at, last_changed=start, attributes={}
        )
        audit.observe(at, indoor, outdoor, heat)
    assert len(audit.records) == 6
    assert audit.records[-1]["horizon_seconds"] == 900
    assert audit.summary(revision)["status"] == "degraded"
    # Subsequent predictions are issued at real cycle times and matched only
    # with later reports. A few good outcomes must not immediately erase the
    # earlier failures; the existing revision-specific rolling window decides.
    indoor_value = 20.5
    for index in range(1, 8):
        issued = start + timedelta(minutes=30 * index)
        states["sensor.indoor"].state = str(indoor_value)
        sensor._pilot.last_indoor_report = issued
        sensor._pilot.manager.recent = [(issued.timestamp(), 0.5, 1.0)]
        sensor._pilot_plan_next = None
        before = audit.summary(revision)["status"]
        attrs = _cycle(sensor, monkeypatch, 30 * index)
        if before == "degraded":
            assert attrs["thermal_intelligence_planning_status"] == "unavailable"
        assert audit.pending is not None
        predicted = audit.pending["forecast"]
        for minute in (5, 10, 15):
            observed = issued + timedelta(minutes=minute)
            indoor = SimpleNamespace(
                state=str(predicted if minute == 15 else indoor_value),
                last_reported=observed if minute == 15 else issued,
                last_changed=issued,
                attributes={"unit_of_measurement": "°C"},
            )
            outdoor = SimpleNamespace(
                state="5",
                last_reported=observed,
                last_changed=issued,
                attributes={"unit_of_measurement": "°C"},
            )
            heat = SimpleNamespace(
                state="on", last_reported=observed, last_changed=issued, attributes={}
            )
            audit.observe(observed, indoor, outdoor, heat)
        indoor_value = predicted
    assert audit.summary(revision)["status"] == "monitoring"
    issued = start + timedelta(minutes=240)
    states["sensor.indoor"].state = str(indoor_value)
    sensor._pilot.last_indoor_report = issued
    sensor._pilot.manager.recent = [(issued.timestamp(), 0.5, 1.0)]
    sensor._pilot_plan_next = None
    attrs = _cycle(sensor, monkeypatch, 240)
    assert attrs["thermal_intelligence_planning_status"] == "validated_passive"


def test_power_telemetry_uses_bounded_event_subscription_and_no_new_entity(monkeypatch):
    import sys
    from datetime import datetime, timedelta, timezone
    from custom_components.pumpsteer import sensor as sensor_module

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_power_entity="sensor.power",
        thermal_heating_mode_entity="binary_sensor.radiator",
    )
    now = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(
        sensor_module.dt_util, "now", lambda: now - timedelta(seconds=1)
    )
    callbacks = []
    unsubscribed = []
    event_module = sys.modules["homeassistant.helpers.event"]

    def track(hass, entity_ids, callback):
        callbacks.append((entity_ids, callback))
        return lambda: unsubscribed.append(True)

    monkeypatch.setattr(event_module, "async_track_state_change_event", track)
    sensor._setup_power_telemetry()
    ids, callback = callbacks[0]
    assert set(ids) == {"sensor.power", "binary_sensor.radiator", "sensor.price"}
    states["sensor.price"].attributes.update(
        unit_of_measurement="SEK/kWh",
        raw_today=[
            {
                "start": now.isoformat(),
                "end": (now + timedelta(hours=1)).isoformat(),
                "value": 1,
            }
        ],
    )
    states["sensor.price"].last_reported = now
    for minute in (0, 5):
        at = now + timedelta(minutes=minute)
        states["sensor.power"] = SimpleNamespace(
            state="1000",
            last_reported=at,
            last_changed=now,
            attributes={"unit_of_measurement": "W"},
        )
        states["binary_sensor.radiator"] = SimpleNamespace(
            state="on", last_reported=at, last_changed=now, attributes={}
        )
        callback(
            SimpleNamespace(
                time_fired=at + timedelta(microseconds=500),
                data={"entity_id": "sensor.power"},
            )
        )
    assert sensor.extra_state_attributes["thermal_intelligence_electrical"][
        "energy_kwh_estimate"
    ] == pytest.approx(1 / 12, abs=0.0001)
    prior_ledger = sensor._power_ledger
    sensor._config_entry.options["thermal_heating_mode_inverted"] = True
    sensor._setup_power_telemetry()
    assert sensor._power_ledger is not prior_ledger
    assert prior_ledger.anchor is None
    assert set(callbacks[-1][0]) == {
        "sensor.power",
        "binary_sensor.radiator",
        "sensor.price",
    }
    sensor._stop_power_telemetry()
    assert unsubscribed == [True, True]


@pytest.mark.parametrize("with_water", [False, True])
def test_inverted_valve_classifies_power_without_changing_report_times(with_water):
    from datetime import datetime, timedelta, timezone
    from custom_components.pumpsteer.pilot_power_ledger import PowerLedger

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_power_entity="sensor.power",
        thermal_heating_mode_entity="binary_sensor.valve",
        thermal_heating_mode_inverted=True,
    )
    if with_water:
        sensor._config_entry.options["thermal_hot_water_mode_entity"] = (
            "binary_sensor.valve"
        )
    sensor._power_ledger = PowerLedger()
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    for minute in (0, 1):
        at = start + timedelta(minutes=minute)
        states["sensor.power"] = SimpleNamespace(
            state="1000",
            last_reported=at,
            last_changed=start,
            attributes={"unit_of_measurement": "W"},
        )
        states["binary_sensor.valve"] = SimpleNamespace(
            state="off", last_reported=at, last_changed=start, attributes={}
        )
        sensor._record_power_snapshot(at)
    assert sensor._power_ledger.summary()[
        "radiator_mode_kwh_estimate"
    ] == pytest.approx(1 / 60, abs=0.0001)
    assert states["binary_sensor.valve"].state == "off"
    assert states["binary_sensor.valve"].last_reported == start + timedelta(minutes=1)
    if with_water:
        for minute in (10, 11):
            at = start + timedelta(minutes=minute)
            states["sensor.power"] = SimpleNamespace(
                state="1000",
                last_reported=at,
                last_changed=at,
                attributes={"unit_of_measurement": "W"},
            )
            states["binary_sensor.valve"] = SimpleNamespace(
                state="on",
                last_reported=at,
                last_changed=start + timedelta(minutes=10),
                attributes={},
            )
            sensor._record_power_snapshot(at)
        assert sensor._power_ledger.summary()[
            "hot_water_mode_kwh_estimate"
        ] == pytest.approx(1 / 60, abs=0.0001)


def test_existing_control_cycle_recovers_unchanged_ha_power_reports(monkeypatch):
    import sys
    from datetime import datetime, timedelta, timezone
    from custom_components.pumpsteer import sensor as sensor_module

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True, thermal_power_entity="sensor.power"
    )
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(
        sensor_module.dt_util, "now", lambda: start - timedelta(seconds=1)
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.event"],
        "async_track_state_change_event",
        lambda hass, ids, callback: lambda: None,
    )
    sensor._setup_power_telemetry()
    states["sensor.price"].attributes.update(
        unit_of_measurement="SEK/kWh",
        forecast=[
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "value": 1,
            }
        ],
    )
    states["sensor.price"].last_reported = start
    for minute in range(0, 6):
        at = start + timedelta(minutes=minute)
        states["sensor.power"] = SimpleNamespace(
            state="1000",
            last_reported=at,
            last_changed=start,
            attributes={"unit_of_measurement": "W"},
        )
        _cycle(sensor, monkeypatch, minute)
    assert sensor._power_ledger.summary()["energy_kwh_estimate"] == pytest.approx(
        1 / 12, abs=0.0001
    )
    assert sensor._power_ledger.summary()["qualified_seconds"] == 300


def test_other_ha_event_never_fabricates_a_power_interval(monkeypatch):
    import sys
    from datetime import datetime, timedelta, timezone
    from custom_components.pumpsteer import sensor as sensor_module

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_power_entity="sensor.power",
        thermal_heating_mode_entity="binary_sensor.radiator",
    )
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(
        sensor_module.dt_util, "now", lambda: start - timedelta(seconds=1)
    )
    callbacks = []
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.event"],
        "async_track_state_change_event",
        lambda hass, ids, cb: callbacks.append(cb) or (lambda: None),
    )
    sensor._setup_power_telemetry()
    callback = callbacks[0]
    states["sensor.power"] = SimpleNamespace(
        state="1000",
        last_reported=start,
        last_changed=start,
        attributes={"unit_of_measurement": "W"},
    )
    states["binary_sensor.radiator"] = SimpleNamespace(
        state="on", last_reported=start, last_changed=start, attributes={}
    )
    callback(SimpleNamespace(time_fired=start, data={"entity_id": "sensor.power"}))
    change = start + timedelta(minutes=4)
    states["binary_sensor.radiator"] = SimpleNamespace(
        state="off", last_reported=change, last_changed=change, attributes={}
    )
    callback(
        SimpleNamespace(time_fired=change, data={"entity_id": "binary_sensor.radiator"})
    )
    assert sensor._power_ledger.summary()["energy_kwh_estimate"] == 0
    assert sensor._power_ledger.summary()["qualified_seconds"] == 0
    for minute in (5, 10):
        at = start + timedelta(minutes=minute)
        states["sensor.power"].last_reported = at
        callback(SimpleNamespace(time_fired=at, data={"entity_id": "sensor.power"}))
    assert sensor._power_ledger.summary()["energy_kwh_estimate"] == pytest.approx(
        1 / 12, abs=0.0001
    )


def test_power_subscription_is_not_created_when_disabled_and_survives_unrelated_options(
    monkeypatch,
):
    import sys

    sensor, _ = _sensor()
    sensor._config_entry.options["thermal_power_entity"] = "sensor.power"
    calls = []
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.event"],
        "async_track_state_change_event",
        lambda hass, ids, cb: calls.append(tuple(ids)) or (lambda: None),
    )
    sensor._setup_power_telemetry()
    assert sensor._power_ledger is None and not calls
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    sensor._setup_power_telemetry()
    previous = sensor._power_ledger
    sensor._config_entry.options["target_temperature"] = 22
    sensor._setup_power_telemetry()
    assert sensor._power_ledger is previous
    assert len(calls) == 1


@pytest.mark.parametrize("heat_kind", ["supply", "binary"])
def test_hot_water_mode_blocks_ambiguous_learning_then_recovers(monkeypatch, heat_kind):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_supply_temp_entity="sensor.supply",
        thermal_heating_mode_entity="binary_sensor.radiator",
        thermal_hot_water_mode_entity="binary_sensor.hot_water",
        thermal_supply_threshold=30,
    )
    if heat_kind == "binary":
        sensor._config_entry.options["thermal_heat_activity_entity"] = (
            "binary_sensor.heat"
        )
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].attributes["unit_of_measurement"] = "°C"
        states[key].last_changed = start
    states["sensor.supply"] = SimpleNamespace(
        state="32",
        last_reported=start,
        last_changed=start,
        attributes={"unit_of_measurement": "°C"},
    )
    states["binary_sensor.radiator"] = SimpleNamespace(
        state="on", last_reported=start, last_changed=start, attributes={}
    )
    states["binary_sensor.heat"] = SimpleNamespace(
        state="on", last_reported=start, last_changed=start, attributes={}
    )
    states["binary_sensor.hot_water"] = SimpleNamespace(
        state="on", last_reported=start, last_changed=start, attributes={}
    )
    attrs = _cycle(sensor, monkeypatch, 0)
    assert attrs["thermal_intelligence_status"] == "hot_water_active"
    assert attrs["thermal_intelligence_reason"] == "hot_water_active"
    assert sensor._pilot.manager.house.start is None
    at = start + timedelta(minutes=1)
    states["binary_sensor.hot_water"].state = "unavailable"
    states["binary_sensor.hot_water"].last_reported = at
    states["binary_sensor.hot_water"].last_changed = at
    attrs = _cycle(sensor, monkeypatch, 1)
    assert attrs["thermal_intelligence_status"] == "hot_water_mode_unavailable"
    assert attrs["thermal_intelligence_reason"] == "hot_water_mode_unavailable"
    assert sensor._pilot.manager.house.start is None
    states["binary_sensor.hot_water"].state = "off"
    states["binary_sensor.hot_water"].last_changed = start + timedelta(minutes=2)
    for minute in range(2, 8):
        at = start + timedelta(minutes=minute)
        for key in (
            "sensor.supply",
            "binary_sensor.heat",
            "binary_sensor.radiator",
            "binary_sensor.hot_water",
        ):
            states[key].last_reported = at
        attrs = _cycle(sensor, monkeypatch, minute)
    assert attrs["thermal_intelligence_status"] not in (
        "hot_water_active",
        "hot_water_mode_unavailable",
        "learning_error",
    )
    assert sensor._pilot.manager.house.start is not None


def test_hot_water_roundtrip_between_cycles_breaks_learning_interval(monkeypatch):
    from datetime import datetime, timedelta, timezone

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_heat_activity_entity="binary_sensor.heat",
        thermal_hot_water_mode_entity="binary_sensor.hot_water",
    )
    start = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].attributes["unit_of_measurement"] = "°C"
        states[key].last_changed = start
    states["binary_sensor.heat"] = SimpleNamespace(
        state="on", last_reported=start, last_changed=start, attributes={}
    )
    states["binary_sensor.hot_water"] = SimpleNamespace(
        state="off", last_reported=start, last_changed=start, attributes={}
    )
    _cycle(sensor, monkeypatch, 0)
    pilot = sensor._pilot
    assert pilot.manager.house.start is not None
    pilot.validation_after = start.timestamp() - 100
    changed = start + timedelta(seconds=45)
    states["binary_sensor.hot_water"].last_changed = changed
    states["binary_sensor.hot_water"].last_reported = changed
    states["binary_sensor.heat"].last_reported = start + timedelta(minutes=1)
    _cycle(sensor, monkeypatch, 1)
    assert pilot.validation_after == (start + timedelta(minutes=1)).timestamp()
    assert pilot.manager.house.start == (start + timedelta(minutes=1)).timestamp()
