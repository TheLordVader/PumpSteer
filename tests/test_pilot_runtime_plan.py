"""Raw real forecast intervals, strictly passive optimizer diagnostics."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from custom_components.pumpsteer.pilot_runtime_plan import hypothetical_plan

T = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)


def price_state():
    rows = [
        {
            "start": (T + timedelta(minutes=15 * i)).isoformat(),
            "end": (T + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": 0.2 if i < 4 else 3,
        }
        for i in range(8)
    ]
    return SimpleNamespace(
        state="1",
        last_reported=T,
        attributes={"unit_of_measurement": "SEK/kWh", "raw_today": rows},
    )


def weather():
    return [
        {"datetime": (T + timedelta(hours=i)).isoformat(), "temperature": 5 - i}
        for i in range(3)
    ]


def test_real_forecasts_execute_pilot_response_optimizer_without_control(monkeypatch):
    from custom_components.pumpsteer.pilot_vendor import response_optimizer

    real = response_optimizer.optimize
    calls = []

    def spy(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(response_optimizer, "optimize", spy)
    result = hypothetical_plan(
        now=T,
        indoor=20.8,
        outdoor=5,
        command=5,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price_state()],
        weather_rows=weather(),
        weather_unit="°C",
    )
    assert calls and result["planning_status"] == "hypothetical_unvalidated"
    assert len(result["temperatures"]) == 9 and len(result["duties"]) == 8
    assert result["cost_kind"] == "proxy_not_currency"


def test_missing_forecast_abstains_without_running():
    result = hypothetical_plan(
        now=T,
        indoor=20.8,
        outdoor=5,
        command=5,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price_state()],
        weather_rows=[],
        weather_unit="°C",
    )
    assert result["planning_status"] == "unavailable"
    assert result["reason"] == "weather_forecast_unavailable"


def test_ha_cycle_runs_optimizer_with_no_heat_sensor_and_keeps_pi_unchanged(
    monkeypatch,
):
    from custom_components.pumpsteer import forecast
    from custom_components.pumpsteer.pilot_vendor import response_optimizer
    from test_brake_provenance import _sensor, _cycle

    base, base_states = _sensor()
    candidate, states = _sensor()
    now = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    candidate._config_entry.options.update(
        thermal_intelligence_enabled=True, weather_entity="weather.local"
    )
    p = states["sensor.price"]
    p.last_reported = now
    p.attributes["unit_of_measurement"] = "SEK/kWh"
    p.attributes["raw_today"] = [
        {
            "start": (now.replace(hour=0) + timedelta(minutes=15 * i)).isoformat(),
            "end": (now.replace(hour=0) + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": 1.0 + i / 100,
        }
        for i in range(96)
    ]
    base_states["sensor.price"].attributes = dict(p.attributes)
    base_states["sensor.price"].last_reported = now
    states["weather.local"] = SimpleNamespace(
        state="cloudy", last_reported=now, attributes={"temperature_unit": "°C"}
    )

    async def forecast_stub(*args, **kwargs):
        callback = kwargs.get("raw_weather_callback")
        if callback:
            callback(
                [
                    {
                        "datetime": (now + timedelta(hours=i)).isoformat(),
                        "temperature": 5 - i,
                    }
                    for i in range(4)
                ]
            )
        return []

    monkeypatch.setattr(forecast, "async_build_forecast", forecast_stub)
    calls = []
    real = response_optimizer.optimize

    def spy(*args, **kwargs):
        calls.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(response_optimizer, "optimize", spy)
    before = _cycle(base, monkeypatch, 0)
    attrs = _cycle(candidate, monkeypatch, 0)
    assert calls
    assert attrs["thermal_intelligence_planning_status"] == "hypothetical_unvalidated"
    assert (
        attrs["thermal_intelligence_plan_detail"]["cost_kind"] == "proxy_not_currency"
    )
    assert candidate.state == base.state
    assert attrs["mode"] == before["mode"]
    candidate._config_entry.options["target_temperature"] = 21.5
    changed = _cycle(candidate, monkeypatch, 1)
    assert changed["thermal_intelligence_planning_status"] == "unavailable"
    assert (
        changed["thermal_intelligence_plan_detail"]["reason"]
        == "context_changed_waiting_for_replan"
    )
    states["weather.local"].state = "unavailable"
    unavailable = _cycle(candidate, monkeypatch, 2)
    assert unavailable["thermal_intelligence_planning_status"] == "unavailable"
    assert (
        unavailable["thermal_intelligence_plan_detail"]["reason"]
        == "weather_forecast_unavailable"
    )


def test_store_roundtrip_keeps_historical_model_unvalidated():
    import asyncio
    import copy
    from custom_components.pumpsteer.pilot_passive import PilotPassive
    from custom_components.pumpsteer.pilot_vendor.learning_interval import (
        LearningInterval,
    )
    from test_brake_provenance import _sensor

    class MemoryStore:
        payload = None

        def async_delay_save(self, callback, delay):
            self.payload = copy.deepcopy(callback())

    sensor, _ = _sensor()
    sensor._pilot_store = MemoryStore()
    sensor._pilot = PilotPassive()
    sensor._pilot_sources = (
        "sensor.indoor",
        "sensor.outdoor",
        "binary_sensor.heat",
        False,
    )
    sensor._pilot.house_intervals = 1
    sensor._pilot.source_identity = (None, "sensor.outdoor")
    sample = LearningInterval(T.timestamp(), 1, 20, 20.1, 5, 0.3, None, 1)
    sensor._pilot.manager.model.add_interval(sample)
    asyncio.run(sensor._save_pilot_store())
    restored = PilotPassive()
    sensor._pilot_restore = sensor._pilot_store.payload
    sensor._restore_pilot_history(
        restored, sensor._pilot_sources, T + timedelta(minutes=1)
    )
    assert len(restored.manager.model.history) == 1
    assert not restored.manager.pump.ready
    assert restored.manager.house.start is None
    assert restored.status == "restored_unvalidated"


def test_corrupt_or_future_history_is_not_restored():
    from custom_components.pumpsteer.pilot_passive import PilotPassive
    from test_brake_provenance import _sensor

    sensor, _ = _sensor()
    sig = ("sensor.indoor", "sensor.outdoor", "binary_sensor.heat", False)
    sensor._pilot_restore = {
        "sources": list(sig),
        "house": {"observations": [{"end": (T + timedelta(days=1)).timestamp()}]},
        "pump": {"history": []},
    }
    pilot = PilotPassive()
    sensor._restore_pilot_history(pilot, sig, T)
    assert not pilot.manager.model.history
    assert not pilot.manager.pump.ready


def test_sensor_lifecycle_saves_and_restores_qualified_history(monkeypatch):
    import asyncio
    import copy
    import sys
    from types import ModuleType

    from custom_components.pumpsteer import sensor as sensor_module
    from test_brake_provenance import _cycle, _sensor

    persisted = {}

    class Store:
        def __init__(self, hass, version, key):
            self.key = key

        def async_delay_save(self, callback, delay):
            persisted[self.key] = copy.deepcopy(callback())

        async def async_load(self):
            return copy.deepcopy(persisted.get(self.key))

    module = ModuleType("homeassistant.helpers.storage")
    module.Store = Store
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", module)
    at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(sensor_module.dt_util, "now", lambda: at - timedelta(minutes=1))
    sensor, states = _sensor()

    async def no_extra_data():
        return None

    monkeypatch.setattr(
        sensor, "async_get_last_extra_data", no_extra_data, raising=False
    )
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_heat_activity_entity="binary_sensor.house_heat",
    )
    for key in ("sensor.indoor", "sensor.outdoor"):
        states[key].attributes["unit_of_measurement"] = "°C"
        states[key].last_changed = at - timedelta(minutes=1)
    asyncio.run(sensor.async_added_to_hass())
    for minute in range(0, 66, 5):
        when = at + timedelta(minutes=minute)
        states["binary_sensor.house_heat"] = SimpleNamespace(
            state="on",
            last_reported=when,
            last_changed=at - timedelta(minutes=1),
            attributes={},
        )
        _cycle(sensor, monkeypatch, minute)
    assert persisted
    assert len(next(iter(persisted.values()))["house"]["observations"]) == 1
    restored, other = _sensor()
    monkeypatch.setattr(
        restored, "async_get_last_extra_data", no_extra_data, raising=False
    )
    restored._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_heat_activity_entity="binary_sensor.house_heat",
    )
    for key in ("sensor.indoor", "sensor.outdoor"):
        other[key].attributes["unit_of_measurement"] = "°C"
        other[key].last_changed = at - timedelta(minutes=1)
    monkeypatch.setattr(
        sensor_module.dt_util, "now", lambda: at + timedelta(minutes=66)
    )
    asyncio.run(restored.async_added_to_hass())
    when = at + timedelta(minutes=70)
    other["binary_sensor.house_heat"] = SimpleNamespace(
        state="on",
        last_reported=when,
        last_changed=at - timedelta(minutes=1),
        attributes={},
    )
    _cycle(restored, monkeypatch, 70)
    assert restored._pilot.house_intervals == 1
    assert not restored._pilot.manager.pump.ready
    assert restored._pilot.manager.house.start == when.timestamp()


@__import__("pytest").mark.parametrize("bad", [float("nan"), "nan", "1e999"])
def test_nonfinite_restored_model_is_rejected(bad):
    from custom_components.pumpsteer.pilot_passive import PilotPassive
    from test_brake_provenance import _sensor

    sensor, _ = _sensor()
    signature = ("sensor.indoor", "sensor.outdoor", "binary_sensor.heat", False)
    model = PilotPassive().manager.model.export_state()
    model["base"]["state"][0] = bad
    sensor._pilot_restore = {
        "sources": list(signature),
        "house": model,
        "pump": {"history": []},
    }
    restored = PilotPassive()
    sensor._restore_pilot_history(restored, signature, T)
    assert not restored.manager.model.history
    assert not restored.manager.pump.ready
    assert restored.status == "waiting_for_heat_signal"


def test_passive_horizon_uses_four_real_forecast_hours():
    price = price_state()
    price.attributes["raw_today"] = [
        {
            "start": (T + timedelta(minutes=15 * i)).isoformat(),
            "end": (T + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": 1 + i / 20,
        }
        for i in range(16)
    ]
    weather_rows = [
        {"datetime": (T + timedelta(hours=i)).isoformat(), "temperature": 5 - i}
        for i in range(5)
    ]
    result = hypothetical_plan(
        now=T,
        indoor=20.8,
        outdoor=5,
        command=5,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=weather_rows,
        weather_unit="°C",
    )
    assert result["horizon_minutes"] == 240
    assert len(result["temperatures"]) == 17
    assert result["planning_status"] == "hypothetical_unvalidated"


def test_verified_response_used_without_calling_house_model_validated(monkeypatch):
    from custom_components.pumpsteer.pilot_vendor.pump_response import (
        ResponseParameters,
    )
    from custom_components.pumpsteer.pilot_vendor import response_optimizer

    seen = []
    original = response_optimizer.optimize

    def capture(
        controller,
        indoor,
        outdoor,
        prices,
        baseline,
        parameters,
        initial_state,
        actuator,
    ):
        seen.append((parameters, initial_state, actuator.learned_response))
        return original(
            controller,
            indoor,
            outdoor,
            prices,
            baseline,
            parameters,
            initial_state,
            actuator,
        )

    monkeypatch.setattr(response_optimizer, "optimize", capture)
    params = ResponseParameters(retention=0.5, slope=0.7)
    result = hypothetical_plan(
        now=T,
        indoor=20.8,
        outdoor=5,
        command=5,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price_state()],
        weather_rows=weather(),
        weather_unit="°C",
        response_context=(params, (0.4, ())),
    )
    assert seen == [(params, (0.4, ()), True)]
    assert result["planning_status"] == "hypothetical_unvalidated"
    assert result["response_status"] == "validated_response_only"


def test_validated_passive_plan_uses_response_and_qualified_house_parameters():
    from custom_components.pumpsteer.pilot_vendor.pump_response import (
        ResponseParameters,
    )

    context = {
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
    result = hypothetical_plan(
        now=T,
        indoor=20.8,
        outdoor=5,
        command=5,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price_state()],
        weather_rows=weather(),
        weather_unit="°C",
        response_context=(ResponseParameters(), (0.4, ())),
        house_context=context,
        response_scope={
            "request_min": 0,
            "request_max": 1,
            "heat_min": 0,
            "heat_max": 1,
        },
    )
    assert result["planning_status"] == "validated_passive"
    assert result["validation"]["mae"] == 0.02
    assert "assumed_model" not in result
    context["outdoor_min"] = 4.5
    result = hypothetical_plan(
        now=T,
        indoor=20.8,
        outdoor=5,
        command=5,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price_state()],
        weather_rows=weather(),
        weather_unit="°C",
        response_context=(ResponseParameters(), (0.4, ())),
        house_context=context,
        response_scope={
            "request_min": 0,
            "request_max": 1,
            "heat_min": 0,
            "heat_max": 1,
        },
    )
    assert result["planning_status"] == "hypothetical_unvalidated"


def test_ha_cycle_exposes_validated_passive_with_synthetic_qualified_fixture(
    monkeypatch,
):
    from custom_components.pumpsteer import forecast
    from custom_components.pumpsteer.pilot_vendor.pump_response import (
        ResponseParameters,
    )
    from test_brake_provenance import _sensor, _cycle

    sensor, states = _sensor()
    at = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True, weather_entity="weather.local"
    )
    price = states["sensor.price"]
    price.last_reported = at
    price.attributes.update(
        unit_of_measurement="SEK/kWh",
        raw_today=[
            {
                "start": (at.replace(hour=0) + timedelta(minutes=15 * i)).isoformat(),
                "end": (
                    at.replace(hour=0) + timedelta(minutes=15 * (i + 1))
                ).isoformat(),
                "value": 1 + i / 100,
            }
            for i in range(96)
        ],
    )
    states["weather.local"] = SimpleNamespace(
        state="cloudy", last_reported=at, attributes={"temperature_unit": "°C"}
    )

    async def forecast_stub(*args, **kwargs):
        if kwargs.get("raw_weather_callback"):
            kwargs["raw_weather_callback"](
                [
                    {
                        "datetime": (at + timedelta(hours=i)).isoformat(),
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
    pump = SimpleNamespace(
        ready=True,
        reason="ready",
        parameters=ResponseParameters(),
        history=[(i, i % 2, i % 2, 5) for i in range(112)],
        export_state=lambda: {"history": []},
    )
    from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit

    sensor._pilot = SimpleNamespace(
        house_intervals=8,
        audit=ForecastAudit(),
        last_indoor_report=at,
        source_identity=(None, None),
        manager=SimpleNamespace(
            pump=pump,
            model=SimpleNamespace(export_state=lambda: {"observations": []}),
            recent=[(at.timestamp(), 0.5, 1.0)],
            response_state=lambda ts: (0.4, ()),
        ),
        validated_house=lambda now: house,
    )
    saved = []

    class MemoryStore:
        def async_delay_save(self, callback, delay):
            saved.append(callback())

    sensor._pilot_store = MemoryStore()
    sensor._pilot_sources = (
        "sensor.indoor",
        "sensor.outdoor",
        "binary_sensor.heat",
        False,
    )
    monkeypatch.setattr(sensor, "_observe_pilot", lambda *args: None)
    attrs = _cycle(sensor, monkeypatch, 0)
    assert attrs["thermal_intelligence_planning_status"] == "validated_passive"
    assert attrs["thermal_intelligence_plan_detail"]["validation"]["samples"] == 8
    assert saved[-1]["forecast_audit"]["pending"]["issued"] == at.isoformat()
    assert (
        attrs["thermal_intelligence_plan_detail"]["monitoring_forecast"][
            "horizon_minutes"
        ]
        == 15
    )
    assert (
        attrs["thermal_intelligence_plan_detail"]["cost_kind"] == "proxy_not_currency"
    )
    sensor._config_entry.options["external_heat_sources"] = True
    sensor._pilot_plan_next = None
    attrs = _cycle(sensor, monkeypatch, 1)
    assert attrs["thermal_intelligence_planning_status"] == "hypothetical_unvalidated"
    from custom_components.pumpsteer.pilot_vendor import response_optimizer

    original = response_optimizer.optimize

    def change_weather(*args, **kwargs):
        states["weather.local"].state = "unavailable"
        return original(*args, **kwargs)

    monkeypatch.setattr(response_optimizer, "optimize", change_weather)
    sensor._pilot_plan_next = None
    attrs = _cycle(sensor, monkeypatch, 2)
    assert attrs["thermal_intelligence_planning_status"] == "unavailable"
    assert (
        attrs["thermal_intelligence_plan_detail"]["reason"]
        == "inputs_changed_during_planning"
    )


def test_extended_horizon_reports_terminal_state_without_claiming_savings():
    price = price_state()
    price.attributes["raw_today"] = [
        {
            "start": (T + timedelta(minutes=15 * i)).isoformat(),
            "end": (T + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": 1.0,
        }
        for i in range(32)
    ]
    forecasts = [
        {"datetime": (T + timedelta(hours=i)).isoformat(), "temperature": 5}
        for i in range(9)
    ]
    result = hypothetical_plan(
        now=T,
        indoor=20.8,
        outdoor=5,
        command=5,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=forecasts,
        weather_unit="°C",
    )
    assert result["horizon_minutes"] == 480
    assert result["comparison"]["baseline"] == "unavailable"
    assert result["comparison"]["heat_storage"] == "unknown"
    assert result["comparison"]["savings"] == "unverified"
    assert result["comparison"]["final_temperature"] == result["temperatures"][-1]
