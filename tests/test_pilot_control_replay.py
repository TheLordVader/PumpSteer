"""Causal, passive comparison using the shared controller and Pilot model."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.control_replay import ControlReplayState
from custom_components.pumpsteer.pilot_runtime_plan import (
    _shared_control_reference,
    hypothetical_plan,
)
from custom_components.pumpsteer.pilot_vendor.pump_response import (
    ResponseParameters,
    virtual_request,
)

NOW = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


class CountingParameters:
    def __init__(self):
        self.requests = []

    def advance(self, request, heat, queue, outdoor):
        self.requests.append(request)
        return request, queue


class CountingHouse:
    def __init__(self):
        self.calls = 0

    def _predict_temp(self, indoor, outdoor, heat):
        self.calls += 1
        return indoor + 0.01 * heat


def context():
    return {
        "state": ControlReplayState(),
        "cfg": {},
        "categories": ("normal",) * 96,
        "active_prices": (1.0,) * 96,
        "interval_minutes": 15,
        "current_slot": 32,
        "summer_threshold": 18.0,
        "aggressiveness": 3,
        "house_inertia": 2.0,
        "preheat_enabled": False,
        "calendar_tz": timezone.utc,
        "configured_target": 21.0,
        "inputs_qualified": True,
        "holiday_context_known": True,
        "holiday_start": None,
        "holiday_end": None,
        "holiday_boolean_on": False,
    }


def args():
    params = CountingParameters()
    house = CountingHouse()
    weather = [
        {"datetime": (NOW + timedelta(hours=i)).isoformat(), "temperature": 5.0}
        for i in range(28)
    ]
    intervals = [
        (NOW + timedelta(minutes=15 * i), NOW + timedelta(minutes=15 * (i + 1)), 1.0)
        for i in range(8)
    ]
    control_prices = [
        (NOW + timedelta(minutes=15 * i), NOW + timedelta(minutes=15 * (i + 1)), 1.0)
        for i in range(32)
    ]
    return dict(
        context=context(),
        now=NOW,
        indoor=20.5,
        actual_outdoor=5.0,
        command=4.0,
        target=21.0,
        floor=20.5,
        ceiling=21.5,
        outdoor_segments=[(a, b, 5.0) for a, b, _ in intervals],
        price_segments=intervals,
        control_price_segments=control_prices,
        prices=[1.0] * 8,
        weather_rows=weather,
        pilot_virtuals=[2.0] * 8,
        controller=house,
        parameters=params,
        initial=(0.0, ()),
        offset=12.0,
        house_scope=None,
        response_scope=None,
    )


def test_shared_reference_uses_actual_initial_command_and_fifteen_minute_model():
    values = args()
    state_before = deepcopy(values["context"]["state"])
    result = _shared_control_reference(**values)
    assert result is not None
    assert result["baseline"] == "shared_control_passive"
    assert result["control_step_seconds"] == 60
    assert result["thermal_step_seconds"] == 900
    assert len(values["parameters"].requests) == 16  # two matched trajectories
    assert values["controller"].calls == 16
    pilot_first_request = values["parameters"].requests[8]
    expected = (virtual_request(5, 4, 12) + 14 * virtual_request(5, 2, 12)) / 15
    assert pilot_first_request == pytest.approx(expected)
    state = values["context"]["state"]
    assert state.pi.__dict__ == state_before.pi.__dict__
    assert state.brake_ramp == state_before.brake_ramp
    assert state.preheat_ramp == state_before.preheat_ramp
    assert result["savings"] == "unverified"
    assert result["heat_storage"] == "unknown"
    assert len(result["baseline_request_fraction"]) == 8
    assert result["request_kind"] == "virtual_command_derived_not_measured_heat"


def test_shared_reference_records_actual_prebrake_transition():
    values = args()
    categories = list(values["context"]["categories"])
    categories[40:44] = ["expensive"] * 4
    values["context"]["categories"] = tuple(categories)
    result = _shared_control_reference(**values)
    assert result is not None
    assert any(mode == "pre_braking" for _, mode in result["baseline_mode_changes"])
    assert result["recovery"] == "unknown"


def test_recovery_waits_until_normal_mode_brake_ramp_has_fully_released():
    values = args()
    state = values["context"]["state"]
    state.brake_ramp = 0.8
    values["context"]["house_inertia"] = 10.0
    state.brake_last_t = NOW
    state.brake_last_expensive_t = NOW - timedelta(hours=1)
    state.was_braking_last_cycle = True
    result = _shared_control_reference(**values)
    assert result is not None
    assert result["baseline_brake_release_minutes"] > 15
    assert result["baseline_recovery_to_initial_minutes"] is not None
    assert result["baseline_recovery_to_initial_minutes"] >= 0


def test_shared_reference_starts_from_actual_outdoor_not_future_forecast():
    values = args()
    values["actual_outdoor"] = 7.0
    result = _shared_control_reference(**values)
    assert result is not None
    expected = (virtual_request(7, 4, 12) + 14 * virtual_request(5, 2, 12)) / 15
    assert values["parameters"].requests[8] == pytest.approx(expected)


def test_shared_reference_refuses_actual_outdoor_outside_validated_model_domain():
    values = args()
    values["actual_outdoor"] = -15.0
    values["outdoor_segments"] = [(a, b, 0.0) for a, b, _ in values["outdoor_segments"]]
    for row in values["weather_rows"]:
        row["temperature"] = 0.0
    values["house_scope"] = {
        "indoor_min": 10,
        "indoor_max": 30,
        "heat_min": 0,
        "heat_max": 1,
        "outdoor_min": 0,
        "outdoor_max": 10,
    }
    values["response_scope"] = {
        "request_min": 0,
        "request_max": 1,
        "heat_min": 0,
        "heat_max": 1,
    }
    assert _shared_control_reference(**values) is None


def test_shared_reference_refuses_actual_outdoor_outside_response_gain_domain():
    values = args()
    values["actual_outdoor"] = -15.0
    values["outdoor_segments"] = [(a, b, 0.0) for a, b, _ in values["outdoor_segments"]]
    for row in values["weather_rows"]:
        row["temperature"] = 0.0
    values["parameters"].outdoor_gain = 0.1
    values["parameters"].outdoor_min = 0
    values["parameters"].outdoor_max = 10
    assert _shared_control_reference(**values) is None


@pytest.mark.parametrize("invalid_scope", ["house", "response"])
def test_actual_outdoor_outside_model_domain_disables_validated_and_fallback(
    invalid_scope,
):
    values = args()
    for row in values["weather_rows"]:
        row["temperature"] = 0.0
    ctx = values["context"]
    price = SimpleNamespace(
        state="1",
        last_reported=NOW,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {"start": a.isoformat(), "end": b.isoformat(), "value": p}
                for a, b, p in values["control_price_segments"]
            ],
        },
    )
    house = {
        "loss": 0.02,
        "gain": 0.4,
        "background": 0.1,
        "mae": 0.02,
        "persistence_mae": 0.2,
        "samples": 8,
        "outdoor_min": 0 if invalid_scope == "house" else -20,
        "outdoor_max": 10,
        "indoor_min": 10,
        "indoor_max": 30,
        "heat_min": 0,
        "heat_max": 1,
    }
    parameters = ResponseParameters(
        outdoor_gain=0.1 if invalid_scope == "response" else 0.0,
        outdoor_min=0,
        outdoor_max=10,
    )
    result = hypothetical_plan(
        now=NOW,
        indoor=20.5,
        outdoor=-15,
        command=-15,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=values["weather_rows"],
        weather_unit="°C",
        response_context=(parameters, (0.4, ())),
        house_context=house,
        response_scope={
            "request_min": 0,
            "request_max": 1,
            "heat_min": 0,
            "heat_max": 1,
        },
        replay_context=ctx,
        pi_reference={
            "controller": ctx["state"].pi,
            "kp": 2.4,
            "ki": 0.035,
            "kd": 0,
            "integral_clamp": 6,
            "summer_threshold": 18,
        },
    )
    assert result["planning_status"] != "validated_passive"
    assert result["comparison"]["baseline"] == "unavailable"


def test_shared_reference_rejects_weather_gap_and_missing_holiday_context():
    values = args()
    values["weather_rows"] = values["weather_rows"][:8]
    assert _shared_control_reference(**values) is None
    values = args()
    values["context"]["holiday_context_known"] = False
    assert _shared_control_reference(**values) is None


def test_shared_reference_rejects_inconsistent_price_series():
    values = args()
    prices = list(values["context"]["active_prices"])
    prices[33] = 9.0
    values["context"]["active_prices"] = tuple(prices)
    assert _shared_control_reference(**values) is None


def test_shared_reference_requires_timestamped_prices_for_full_lookahead():
    values = args()
    values["control_price_segments"] = values["price_segments"]
    assert _shared_control_reference(**values) is None


def test_shared_reference_rejects_differing_prices_during_lookahead():
    values = args()
    active = list(values["context"]["active_prices"])
    active[41] = 9.0  # outside the plan but inside the real controller's lookahead
    values["context"]["active_prices"] = tuple(active)
    assert _shared_control_reference(**values) is None


def test_shared_reference_rejects_crossing_local_price_threshold_midnight():
    values = args()
    values["now"] = NOW.replace(hour=18)
    assert _shared_control_reference(**values) is None


@pytest.mark.parametrize("change", ["start", "end"])
def test_shared_reference_refuses_scheduled_holiday_target_change(change):
    values = args()
    values["context"].update(
        holiday_start=NOW + timedelta(minutes=30)
        if change == "start"
        else NOW - timedelta(hours=1),
        holiday_end=NOW + timedelta(hours=3)
        if change == "start"
        else NOW + timedelta(minutes=60),
        holiday_boolean_on=change == "end",
    )
    assert _shared_control_reference(**values) is None


def test_pilot_planning_calls_shared_reference_with_same_model_and_fails_closed():
    values = args()
    price = SimpleNamespace(
        state="1",
        last_reported=NOW,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {"start": a.isoformat(), "end": b.isoformat(), "value": p}
                for a, b, p in values["control_price_segments"]
            ],
        },
    )
    kwargs = dict(
        now=NOW,
        indoor=20.5,
        outdoor=5.0,
        command=4.0,
        target=21.0,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=values["weather_rows"],
        weather_unit="°C",
        replay_context=values["context"],
    )
    result = hypothetical_plan(**kwargs)
    assert result["planning_status"] == "hypothetical_unvalidated"
    assert result["comparison"]["baseline"] == "shared_control_passive"
    assert result["comparison"]["heat_storage"] == "unknown"
    assert result["comparison"]["savings"] == "unverified"
    kwargs["weather_rows"] = values["weather_rows"][:8]
    result = hypothetical_plan(**kwargs)
    assert result["planning_status"] == "hypothetical_unvalidated"
    assert result["comparison"]["baseline"] == "unavailable"
    assert (
        result["comparison"]["baseline_reason"]
        == "shared_control_context_or_coverage_unavailable"
    )
    kwargs["pi_reference"] = {
        "controller": values["context"]["state"].pi,
        "kp": 2.4,
        "ki": 0.035,
        "kd": 0,
        "integral_clamp": 6,
        "summer_threshold": 18,
    }
    result = hypothetical_plan(**kwargs)
    assert result["comparison"]["baseline"] == "conditional_pi_only"
    assert result["comparison"]["shared_control_status"] == "unavailable"


@pytest.mark.parametrize("missing", ["inputs_qualified", "holiday_context_known"])
def test_unqualified_replay_does_not_shorten_the_existing_pilot_plan(missing):
    values = args()
    values["context"][missing] = False
    price = SimpleNamespace(
        state="1",
        last_reported=NOW,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {"start": a.isoformat(), "end": b.isoformat(), "value": p}
                for a, b, p in values["control_price_segments"]
            ],
        },
    )
    result = hypothetical_plan(
        now=NOW,
        indoor=20.5,
        outdoor=5.0,
        command=4.0,
        target=21.0,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=values["weather_rows"],
        weather_unit="°C",
        replay_context=values["context"],
    )
    assert result["horizon_minutes"] == 480
    assert result["comparison"]["shared_control_status"] == "unavailable"


def test_horizon_selects_longest_candidate_before_local_midnight():
    now = NOW.replace(hour=13)
    ctx = context()
    ctx["current_slot"] = 52
    weather = [
        {"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 5.0}
        for i in range(36)
    ]
    price = SimpleNamespace(
        state="1",
        last_reported=now,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {
                    "start": (now + timedelta(minutes=15 * i)).isoformat(),
                    "end": (now + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "value": 1.0,
                }
                for i in range(44)
            ],
        },
    )
    result = hypothetical_plan(
        now=now,
        indoor=20.5,
        outdoor=5,
        command=4,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=weather,
        weather_unit="°C",
        replay_context=ctx,
    )
    assert result["horizon_minutes"] == 240
    assert result["comparison"]["baseline"] == "shared_control_passive"


def test_no_qualifying_reference_preserves_original_plan_horizon():
    now = NOW.replace(hour=20)
    ctx = context()
    ctx["current_slot"] = 80
    ctx["categories"] = ("normal",) * 128
    ctx["active_prices"] = (1.0,) * 128
    weather = [
        {"datetime": (now + timedelta(hours=i)).isoformat(), "temperature": 5.0}
        for i in range(36)
    ]
    price = SimpleNamespace(
        state="1",
        last_reported=now,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {
                    "start": (now + timedelta(minutes=15 * i)).isoformat(),
                    "end": (now + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "value": 1.0,
                }
                for i in range(32)
            ],
        },
    )
    result = hypothetical_plan(
        now=now,
        indoor=20.5,
        outdoor=5,
        command=4,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=weather,
        weather_unit="°C",
        replay_context=ctx,
    )
    assert result["horizon_minutes"] == 480
    assert result["comparison"]["shared_control_status"] == "unavailable"


def test_rejected_shared_replay_restores_longer_pilot_plan_with_one_retry(monkeypatch):
    from custom_components.pumpsteer import pilot_runtime_plan
    from custom_components.pumpsteer.pilot_vendor import response_optimizer

    values = args()
    price = SimpleNamespace(
        state="1",
        last_reported=NOW,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {"start": a.isoformat(), "end": b.isoformat(), "value": p}
                for a, b, p in values["control_price_segments"]
            ],
        },
    )
    monkeypatch.setattr(
        pilot_runtime_plan, "_shared_control_reference", lambda **kwargs: None
    )
    calls = []
    original = response_optimizer.optimize

    def count(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(response_optimizer, "optimize", count)
    result = hypothetical_plan(
        now=NOW,
        indoor=20.5,
        outdoor=5,
        command=4,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=values["weather_rows"],
        weather_unit="°C",
        replay_context=values["context"],
    )
    assert result["horizon_minutes"] == 480
    assert result["comparison"]["shared_control_status"] == "unavailable"
    assert result["comparison"]["baseline"] == "unavailable"
    assert len(calls) == 2


def test_rejected_shared_replay_keeps_short_plan_if_long_plan_outside_domain(
    monkeypatch,
):
    from custom_components.pumpsteer import pilot_runtime_plan

    values = args()
    for row in values["weather_rows"]:
        row["temperature"] = 0.0
    price = SimpleNamespace(
        state="1",
        last_reported=NOW,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {"start": a.isoformat(), "end": b.isoformat(), "value": p}
                for a, b, p in values["control_price_segments"]
            ],
        },
    )
    house = {
        "loss": 0.10,
        "gain": 0.10,
        "background": 0.0,
        "mae": 0.02,
        "persistence_mae": 0.2,
        "samples": 8,
        "outdoor_min": -10,
        "outdoor_max": 10,
        "indoor_min": 16,
        "indoor_max": 25,
        "heat_min": 0,
        "heat_max": 1,
    }
    monkeypatch.setattr(
        pilot_runtime_plan, "_shared_control_reference", lambda **kwargs: None
    )
    result = hypothetical_plan(
        now=NOW,
        indoor=20.5,
        outdoor=0,
        command=0,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=values["weather_rows"],
        weather_unit="°C",
        replay_context=values["context"],
        response_context=(ResponseParameters(), (0.5, ())),
        house_context=house,
        response_scope={
            "request_min": 0,
            "request_max": 1,
            "heat_min": 0,
            "heat_max": 1,
        },
    )
    assert result["planning_status"] == "validated_passive"
    assert result["horizon_minutes"] == 120
    assert result["comparison"]["shared_control_status"] == "unavailable"


def test_zero_aggressiveness_does_not_invent_comfort_boundaries():
    values = args()
    values["context"]["aggressiveness"] = 0
    price = SimpleNamespace(
        state="1",
        last_reported=NOW,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {"start": a.isoformat(), "end": b.isoformat(), "value": p}
                for a, b, p in values["control_price_segments"]
            ],
        },
    )
    result = hypothetical_plan(
        now=NOW,
        indoor=20.5,
        outdoor=5.0,
        command=4.0,
        target=21.0,
        floor=21.0,
        ceiling=21.0,
        price_states=[price],
        weather_rows=values["weather_rows"],
        weather_unit="°C",
        replay_context=values["context"],
    )
    assert result["comparison"]["baseline"] == "shared_control_passive"
    assert result["comfort_status"] == "no_configured_band"
    assert result["comparison"]["baseline_below_floor_degree_hours"] is None
    assert result["comparison"]["pilot_below_floor_degree_hours"] is None


@pytest.mark.parametrize("conflict", ["holiday", "precool"])
def test_conditional_pi_fallback_refuses_known_future_overlay(conflict):
    values = args()
    ctx = values["context"]
    ctx["inputs_qualified"] = False  # no qualified full replay
    if conflict == "holiday":
        ctx.update(
            holiday_start=NOW + timedelta(minutes=30),
            holiday_end=NOW + timedelta(hours=3),
        )
    else:
        values["weather_rows"][20]["temperature"] = 25
    price = SimpleNamespace(
        state="1",
        last_reported=NOW,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {"start": a.isoformat(), "end": b.isoformat(), "value": p}
                for a, b, p in values["control_price_segments"]
            ],
        },
    )
    result = hypothetical_plan(
        now=NOW,
        indoor=20.5,
        outdoor=5,
        command=4,
        target=21,
        floor=20.5,
        ceiling=21.5,
        price_states=[price],
        weather_rows=values["weather_rows"],
        weather_unit="°C",
        replay_context=ctx,
        pi_reference={
            "controller": ctx["state"].pi,
            "kp": 2.4,
            "ki": 0.035,
            "kd": 0,
            "integral_clamp": 6,
            "summer_threshold": 18,
        },
    )
    assert result["comparison"]["baseline"] == "unavailable"


def _assert_complete_comparison(result, start):
    """HCO no-horizon-end-drain contract; unknown storage is not free energy."""
    assert result["baseline_temperatures"][0] == start
    assert result["pilot_temperatures"][0] == start
    assert len(result["baseline_temperatures"]) == len(result["pilot_temperatures"])
    assert result["baseline_final_temperature"] == result["baseline_temperatures"][-1]
    assert result["pilot_final_temperature"] == result["pilot_temperatures"][-1]
    assert result["heat_storage"] == "unknown"
    assert result["savings"] == "unverified"
    assert result["proxy_definition"] == "price_times_model_heat_fraction_not_currency"


def test_complete_cycle_reports_terminal_and_comfort_deficits_without_free_storage():
    class LossyHouse:
        def _predict_temp(self, indoor, outdoor, heat):
            return indoor - 0.08 + 0.04 * heat

    values = args()
    values["controller"] = LossyHouse()
    values["pilot_virtuals"] = [20.0] * 8
    result = _shared_control_reference(**values)
    assert result is not None
    _assert_complete_comparison(result, values["indoor"])
    assert result["pilot_below_floor_degree_hours"] > 0
    assert result["baseline_below_floor_degree_hours"] > 0
    assert result["pilot_final_temperature"] < values["indoor"]
    assert result["pilot_heat_price_proxy"] < result["baseline_heat_price_proxy"]
    # An intentionally false saving claim must fail the comparison contract.
    corrupted = deepcopy(result)
    corrupted["savings"] = "verified"
    with pytest.raises(AssertionError):
        _assert_complete_comparison(corrupted, values["indoor"])
    corrupted = deepcopy(result)
    corrupted["pilot_temperatures"][0] += 1
    with pytest.raises(AssertionError):
        _assert_complete_comparison(corrupted, values["indoor"])
