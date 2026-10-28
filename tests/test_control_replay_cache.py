"""Actual HA control cycles invalidate stale passive replay contexts."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer import forecast
from custom_components.pumpsteer.pilot_vendor import response_optimizer
from homeassistant.helpers import entity_registry
from test_brake_provenance import _cycle, _sensor


START = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


def _ready_sensor(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        weather_entity="weather.local",
        pid_ki=0,
    )
    sensor._config_entry.options.pop("house_inertia")
    ids = {
        "preheat_enabled": "switch.preheat",
        "holiday_mode": "switch.holiday",
        "holiday_start": "datetime.holiday_start",
        "holiday_end": "datetime.holiday_end",
        "house_inertia": "number.house_inertia",
    }

    def entity_id(domain, platform, unique_id):
        for suffix, result in ids.items():
            if unique_id.endswith("_" + suffix):
                return result
        return None

    monkeypatch.setattr(
        entity_registry,
        "async_get",
        lambda hass: SimpleNamespace(async_get_entity_id=entity_id),
    )
    for name, value in (
        ("switch.preheat", "off"),
        ("switch.holiday", "off"),
        ("datetime.holiday_start", "2030-01-01T00:00:00+00:00"),
        ("datetime.holiday_end", "2030-01-02T00:00:00+00:00"),
        ("number.house_inertia", "2"),
    ):
        states[name] = SimpleNamespace(
            state=value, last_reported=START, last_changed=START, attributes={}
        )
    for name in ("sensor.indoor", "sensor.outdoor"):
        states[name].attributes["unit_of_measurement"] = "°C"
        states[name].last_changed = START
    states["sensor.price"].last_reported = START
    states["sensor.price"].attributes.update(
        today=[1.0] * 96,
        unit_of_measurement="SEK/kWh",
        raw_today=[
            {
                "start": (
                    START.replace(hour=0) + timedelta(minutes=15 * i)
                ).isoformat(),
                "end": (
                    START.replace(hour=0) + timedelta(minutes=15 * (i + 1))
                ).isoformat(),
                "value": 1.0,
            }
            for i in range(96)
        ],
    )
    states["weather.local"] = SimpleNamespace(
        state="cloudy", last_reported=START, attributes={"temperature_unit": "°C"}
    )
    rows = [
        {"datetime": (START + timedelta(hours=i)).isoformat(), "temperature": 5.0}
        for i in range(28)
    ]

    async def forecast_stub(*args, **kwargs):
        callback = kwargs.get("raw_weather_callback")
        if callback:
            callback(rows)
        return []

    monkeypatch.setattr(forecast, "async_build_forecast", forecast_stub)
    calls = []
    original = response_optimizer.optimize

    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(response_optimizer, "optimize", count)
    return sensor, states, calls


@pytest.mark.parametrize(
    ("entity", "value"),
    [
        ("switch.preheat", "on"),
        ("datetime.holiday_start", "2030-01-01T01:00:00+00:00"),
        ("number.house_inertia", "3"),
    ],
)
def test_real_cycle_invalidates_cached_reference_on_control_context_change(
    monkeypatch, entity, value
):
    sensor, states, calls = _ready_sensor(monkeypatch)
    reference, _ = _sensor()
    reference._config_entry.options.update(weather_entity="weather.local", pid_ki=0)
    reference._config_entry.options.pop("house_inertia")
    reference.hass.states = SimpleNamespace(get=states.get)
    pushed, reference_pushed = [], []

    async def record_push(value, *args):
        pushed.append(value)

    async def record_reference_push(value, *args):
        reference_pushed.append(value)

    monkeypatch.setattr(sensor, "_push_validated_temperature", record_push)
    monkeypatch.setattr(reference, "_push_validated_temperature", record_reference_push)

    def cycle(minute):
        _cycle(reference, monkeypatch, minute)
        result = _cycle(sensor, monkeypatch, minute)
        assert sensor.state == reference.state
        assert pushed == reference_pushed
        return result

    initial = cycle(0)
    initial_output = sensor.state
    assert initial["thermal_intelligence_plan_detail"]["comparison"]["baseline"] == (
        "shared_control_passive"
    )
    assert len(calls) == 1

    unchanged = cycle(1)
    assert (
        unchanged["thermal_intelligence_planning_status"] == "hypothetical_unvalidated"
    )
    assert sensor.state == initial_output
    assert len(calls) == 1

    states[entity].state = value
    states[entity].last_changed = START + timedelta(minutes=2)
    states[entity].last_reported = START + timedelta(minutes=2)
    changed = cycle(2)
    assert sensor.state == initial_output
    assert changed["thermal_intelligence_planning_status"] == "unavailable"
    assert changed["thermal_intelligence_plan_detail"]["reason"] == (
        "context_changed_waiting_for_replan"
    )
    assert len(calls) == 1

    recalculated = cycle(30)
    assert sensor.state == initial_output
    assert (
        recalculated["thermal_intelligence_planning_status"]
        == "hypothetical_unvalidated"
    )
    assert recalculated["thermal_intelligence_plan_detail"]["comparison"][
        "baseline"
    ] == ("shared_control_passive")
    assert len(calls) == 2


def test_saturated_pi_integral_change_invalidates_cached_reference(monkeypatch):
    sensor, _, calls = _ready_sensor(monkeypatch)
    sensor._config_entry.options.update(pid_ki=0.1, pid_output_clamp=0.2)
    initial = _cycle(sensor, monkeypatch, 0)
    assert initial["thermal_intelligence_plan_detail"]["comparison"]["baseline"] == (
        "shared_control_passive"
    )
    output = sensor.state
    integral = sensor._pi._integral
    assert len(calls) == 1

    next_cycle = _cycle(sensor, monkeypatch, 1)
    assert (
        sensor.state == output
    )  # The saturated PI output did not reveal its changed state.
    assert sensor._pi._integral != integral
    comparison = next_cycle["thermal_intelligence_plan_detail"]["comparison"]
    assert comparison["baseline"] == "unavailable"
    assert comparison["baseline_reason"] == ("control_state_changed_waiting_for_replan")
    assert len(calls) == 1  # Invalidation must not trigger another optimizer run.


def test_changed_brake_ramp_invalidates_reference_even_when_output_matches(monkeypatch):
    sensor, _, calls = _ready_sensor(monkeypatch)
    sensor._config_entry.options.update(target_temperature=20.0, pid_kp=20, pid_ki=0)
    initial = _cycle(sensor, monkeypatch, 0)
    assert initial["thermal_intelligence_plan_detail"]["comparison"]["baseline"] == (
        "shared_control_passive"
    )
    output = sensor.state
    # The PI output and brake target both happen to be 15 °C. A ramp change is
    # therefore hidden by the output but matters to subsequent replay steps.
    sensor._brake_ramp = 0.5
    sensor._brake_last_t = START
    next_cycle = _cycle(sensor, monkeypatch, 1)
    assert sensor.state == output == 15.0
    assert sensor._brake_ramp > 0
    comparison = next_cycle["thermal_intelligence_plan_detail"]["comparison"]
    assert comparison["baseline"] == "unavailable"
    assert comparison["baseline_reason"] == "control_state_changed_waiting_for_replan"
    assert len(calls) == 1
