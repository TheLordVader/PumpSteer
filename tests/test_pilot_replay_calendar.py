"""Published price days must replay the real local-day cache and UTC slots."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.pumpsteer.pilot_runtime_plan import (
    _build_replay_price_calendar,
    _calendar_covers_lookahead,
    hypothetical_plan,
)
from custom_components.pumpsteer.control_replay import ControlReplayState
from test_brake_provenance import _sensor

ZONE = ZoneInfo("Europe/Stockholm")


def _rows(day, minutes, offset=0):
    begin = datetime(day.year, day.month, day.day, tzinfo=ZONE).astimezone(timezone.utc)
    local_next = datetime(day.year, day.month, day.day, tzinfo=ZONE) + timedelta(days=1)
    end = local_next.astimezone(timezone.utc)
    rows = []
    while begin < end:
        following = begin + timedelta(minutes=minutes)
        rows.append(
            {
                "start": begin.isoformat(),
                "end": following.isoformat(),
                "value": float(offset + len(rows) % 9),
            }
        )
        begin = following
    return rows


def _state(today, tomorrow, at):
    return SimpleNamespace(
        state="1",
        last_reported=at,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": today,
            "raw_tomorrow": tomorrow,
        },
    )


@pytest.mark.parametrize("minutes", [15, 30, 60])
@pytest.mark.parametrize(
    "day", [datetime(2026, 3, 28), datetime(2026, 10, 24), datetime(2026, 10, 17)]
)
def test_calendar_matches_real_live_cache_and_slots_across_dst(day, minutes):
    sensor, states = _sensor()
    tomorrow = day + timedelta(days=1)
    rows_today, rows_next = _rows(day, minutes), _rows(tomorrow, minutes, 3)
    issue = datetime(day.year, day.month, day.day, 23, 45, tzinfo=ZONE)
    states["sensor.price"] = _state(rows_today, rows_next, issue)
    cfg = sensor._cfg()
    live_prices, categories, interval, slot = asyncio.run(
        sensor._get_prices(cfg, issue)
    )
    context = {
        "calendar_tz": ZONE,
        "interval_minutes": interval,
        "current_slot": slot,
        "active_prices": live_prices,
        "categories": categories,
        "price_entity_id": "sensor.price",
        "cached_price_date": sensor._price_thresholds_cached_date,
        "cached_price_entity_id": sensor._price_thresholds_entity_id,
        "cached_p30": sensor._cached_p30,
        "cached_p80": sensor._cached_p80,
    }
    calendar = _build_replay_price_calendar([states["sensor.price"]], context, issue)
    assert calendar is not None
    for hours, mins in [(0, 0), (0, 14), (0, 15), (1, 30), (2, 30), (3, 30), (4, 0)]:
        # UTC arithmetic preserves real elapsed time and repeated winter hour.
        local = (
            datetime(
                tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=ZONE
            ).astimezone(timezone.utc)
            + timedelta(hours=hours, minutes=mins)
        ).astimezone(ZONE)
        states["sensor.price"] = _state(rows_next, [], local)
        prices, expected, _, current = asyncio.run(sensor._get_prices(cfg, local))
        actual = calendar.at(local)
        assert actual is not None
        assert actual.categories == tuple(expected)
        assert actual.slot == current
        assert actual.prices == tuple(prices)
        assert actual.p30 == pytest.approx(sensor._p30)
        assert actual.p80 == pytest.approx(sensor._p80)


def test_calendar_rejects_missing_or_conflicting_entire_tomorrow():
    sensor, states = _sensor()
    day = datetime(2026, 10, 24)
    issue = datetime(2026, 10, 24, 23, 45, tzinfo=ZONE)
    today, tomorrow = _rows(day, 15), _rows(day + timedelta(days=1), 15)
    states["sensor.price"] = _state(today, tomorrow, issue)
    prices, categories, interval, slot = asyncio.run(
        sensor._get_prices(sensor._cfg(), issue)
    )
    context = {
        "calendar_tz": ZONE,
        "interval_minutes": interval,
        "current_slot": slot,
        "active_prices": prices,
        "categories": categories,
        "price_entity_id": "sensor.price",
        "cached_price_date": sensor._price_thresholds_cached_date,
        "cached_price_entity_id": sensor._price_thresholds_entity_id,
        "cached_p30": sensor._cached_p30,
        "cached_p80": sensor._cached_p80,
    }
    states["sensor.price"].attributes["raw_tomorrow"] = tomorrow[:-1]
    assert (
        _build_replay_price_calendar([states["sensor.price"]], context, issue) is None
    )


def test_midnight_cold_start_does_not_invent_yesterday_thresholds():
    sensor, states = _sensor()
    day = datetime(2026, 10, 25)
    issue = datetime(2026, 10, 25, 0, 5, tzinfo=ZONE)
    today, tomorrow = _rows(day, 15), _rows(day + timedelta(days=1), 15, 3)
    states["sensor.price"] = _state(today, tomorrow, issue)
    prices, categories, interval, slot = asyncio.run(
        sensor._get_prices(sensor._cfg(), issue)
    )
    assert sensor._p30 == sensor._p80 == 0
    context = {
        "calendar_tz": ZONE,
        "interval_minutes": interval,
        "current_slot": slot,
        "active_prices": prices,
        "categories": categories,
        "price_entity_id": "sensor.price",
        "cached_price_date": sensor._price_thresholds_cached_date,
        "cached_price_entity_id": sensor._price_thresholds_entity_id,
        "cached_p30": sensor._cached_p30,
        "cached_p80": sensor._cached_p80,
    }
    calendar = _build_replay_price_calendar([states["sensor.price"]], context, issue)
    assert calendar is not None
    assert calendar.at(issue + timedelta(minutes=9)).p80 == 0
    at = issue + timedelta(minutes=10)
    actual = calendar.at(at)
    _, categories, _, slot = asyncio.run(sensor._get_prices(sensor._cfg(), at))
    assert actual.categories == tuple(categories)
    assert actual.slot == slot
    assert actual.p80 == pytest.approx(sensor._p80)
    assert actual.p80 > 0


def test_unknown_next_price_day_cannot_influence_qualified_peak_filter():
    sensor, states = _sensor()
    day = datetime(2026, 10, 17)
    issue = datetime(2026, 10, 17, 23, 45, tzinfo=ZONE)
    today, tomorrow = _rows(day, 15), _rows(day + timedelta(days=1), 15, 3)
    states["sensor.price"] = _state(today, tomorrow, issue)
    prices, categories, interval, slot = asyncio.run(
        sensor._get_prices(sensor._cfg(), issue)
    )
    context = {
        "calendar_tz": ZONE,
        "interval_minutes": interval,
        "current_slot": slot,
        "active_prices": prices,
        "categories": categories,
        "price_entity_id": "sensor.price",
        "cached_price_date": sensor._price_thresholds_cached_date,
        "cached_price_entity_id": sensor._price_thresholds_entity_id,
        "cached_p30": sensor._cached_p30,
        "cached_p80": sensor._cached_p80,
    }
    calendar = _build_replay_price_calendar([states["sensor.price"]], context, issue)
    next_midnight = datetime(2026, 10, 19, tzinfo=ZONE).astimezone(timezone.utc)
    assert _calendar_covers_lookahead(calendar, next_midnight - timedelta(minutes=15))
    assert not _calendar_covers_lookahead(
        calendar, next_midnight - timedelta(minutes=14)
    )


@pytest.mark.parametrize("day", [datetime(2026, 3, 28), datetime(2026, 10, 24)])
def test_passive_ha_plan_crosses_midnight_and_dst_with_published_prices(day):
    sensor, states = _sensor()
    issue = datetime(day.year, day.month, day.day, 23, 45, tzinfo=ZONE)
    today, tomorrow = _rows(day, 15), _rows(day + timedelta(days=1), 15, 3)
    source = _state(today, tomorrow, issue)
    states["sensor.price"] = source
    prices, categories, interval, slot = asyncio.run(
        sensor._get_prices(sensor._cfg(), issue)
    )
    context = {
        "calendar_tz": ZONE,
        "interval_minutes": interval,
        "current_slot": slot,
        "active_prices": prices,
        "categories": categories,
        "price_entity_id": "sensor.price",
        "cached_price_date": sensor._price_thresholds_cached_date,
        "cached_price_entity_id": sensor._price_thresholds_entity_id,
        "cached_p30": sensor._cached_p30,
        "cached_p80": sensor._cached_p80,
        "state": ControlReplayState(),
        "cfg": {},
        "summer_threshold": 18.0,
        "aggressiveness": 3,
        "house_inertia": 2.0,
        "preheat_enabled": False,
        "configured_target": 21.0,
        "inputs_qualified": True,
        "holiday_context_known": True,
        "holiday_start": None,
        "holiday_end": None,
        "holiday_boolean_on": False,
    }
    current_hour = issue.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    weather = [
        {
            "datetime": (current_hour + timedelta(hours=i)).isoformat(),
            "temperature": 5.0,
        }
        for i in range(40)
    ]
    result = hypothetical_plan(
        now=issue,
        indoor=20.5,
        outdoor=5.0,
        command=4.0,
        target=21.0,
        floor=20.5,
        ceiling=21.5,
        price_states=[source],
        weather_rows=weather,
        weather_unit="°C",
        replay_context=context,
    )
    assert result["planning_status"] == "hypothetical_unvalidated"
    assert result["comparison"]["baseline"] == "shared_control_passive"
    initial = result["initial_context"]
    assert initial["indoor_temperature"] == 20.5
    assert initial["outdoor_temperature"] == 5.0
    assert initial["target_temperature"] == 21.0
    assert initial["comfort_floor"] == 20.5
    assert initial["comfort_ceiling"] == 21.5
    assert initial["virtual_temperature_command"] == 4.0
    assert initial["physical_measurement_time"] == "unknown"
    assert initial["controller"]["pi_integral"] == 0.0
    assert initial["controller"]["brake_ramp"] == 0.0
    assert initial["controller"]["pi_last_time"] is None
    assert (
        result["comparison"]["price_publication_assumption"]
        == "frozen_intervals_available_as_today_not_verified_future_ha"
    )
    assert result["comparison"]["savings"] == "unverified"
    assert result["comparison"]["heat_storage"] == "unknown"


def test_preheat_brake_and_release_across_local_midnight():
    sensor, states = _sensor()
    day = datetime(2026, 10, 17)
    issue = datetime(2026, 10, 17, 22, 45, tzinfo=ZONE)
    today, tomorrow = _rows(day, 15), _rows(day + timedelta(days=1), 15)
    for row in today + tomorrow:
        row["value"] = 1.0
    for row in tomorrow[2:6]:
        row["value"] = 10.0
    source = _state(today, tomorrow, issue)
    states["sensor.price"] = source
    prices, categories, interval, slot = asyncio.run(
        sensor._get_prices(sensor._cfg(), issue)
    )
    context = {
        "calendar_tz": ZONE,
        "interval_minutes": interval,
        "current_slot": slot,
        "active_prices": prices,
        "categories": categories,
        "price_entity_id": "sensor.price",
        "cached_price_date": sensor._price_thresholds_cached_date,
        "cached_price_entity_id": sensor._price_thresholds_entity_id,
        "cached_p30": sensor._cached_p30,
        "cached_p80": sensor._cached_p80,
        "state": ControlReplayState(),
        "cfg": {},
        "summer_threshold": 18.0,
        "aggressiveness": 3,
        "house_inertia": 2.0,
        "preheat_enabled": True,
        "configured_target": 21.0,
        "inputs_qualified": True,
        "holiday_context_known": True,
        "holiday_start": None,
        "holiday_end": None,
        "holiday_boolean_on": False,
    }
    current_hour = issue.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    )
    weather = [
        {
            "datetime": (current_hour + timedelta(hours=i)).isoformat(),
            "temperature": 0.0,
        }
        for i in range(40)
    ]
    result = hypothetical_plan(
        now=issue,
        indoor=20.5,
        outdoor=0.0,
        command=-1.0,
        target=21.0,
        floor=20.5,
        ceiling=21.5,
        price_states=[source],
        weather_rows=weather,
        weather_unit="°C",
        replay_context=context,
    )
    assert result["comparison"]["baseline"] == "shared_control_passive"
    changes = result["comparison"]["baseline_mode_changes"]
    modes = [mode for _, mode in changes]
    assert "preheating" in modes
    assert "pre_braking" in modes
    assert "braking" in modes
    assert any(mode == "normal" for _, mode in changes[modes.index("braking") + 1 :])


def test_actual_ha_cycle_executes_dst_reference_without_changing_control(monkeypatch):
    from custom_components.pumpsteer import forecast
    from custom_components.pumpsteer import sensor as sensor_module

    base, base_states = _sensor()
    candidate, states = _sensor()
    issue = datetime(2026, 10, 24, 23, 45, tzinfo=ZONE)
    day = datetime(2026, 10, 24)
    today, tomorrow = _rows(day, 15), _rows(day + timedelta(days=1), 15, 3)
    for ctrl, source_states in ((base, base_states), (candidate, states)):
        ctrl._config_entry.options["weather_entity"] = "weather.test"
        source_states["sensor.price"] = _state(today, tomorrow, issue)
        source_states["weather.test"] = SimpleNamespace(
            state="cloudy",
            last_reported=issue,
            last_changed=issue,
            attributes={"temperature_unit": "°C"},
        )
        for name in ("sensor.indoor", "sensor.outdoor"):
            source_states[name].attributes["unit_of_measurement"] = "°C"
            source_states[name].last_reported = issue
            source_states[name].last_changed = issue
    candidate._config_entry.options["thermal_intelligence_enabled"] = True
    hour = issue.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    weather = [
        {"datetime": (hour + timedelta(hours=i)).isoformat(), "temperature": 5.0}
        for i in range(40)
    ]

    async def forecast_stub(*args, **kwargs):
        callback = kwargs.get("raw_weather_callback")
        if callback:
            callback(weather)
        return []

    base_pushes, candidate_pushes = [], []

    async def base_push(value, *args):
        base_pushes.append(value)

    async def candidate_push(value, *args):
        candidate_pushes.append(value)

    monkeypatch.setattr(forecast, "async_build_forecast", forecast_stub)
    monkeypatch.setattr(sensor_module.dt_util, "now", lambda: issue)
    monkeypatch.setattr(base, "_push_validated_temperature", base_push)
    monkeypatch.setattr(candidate, "_push_validated_temperature", candidate_push)
    asyncio.run(base._do_update())
    asyncio.run(candidate._do_update())
    assert base.state == candidate.state
    assert base_pushes == candidate_pushes
    assert (
        candidate.extra_state_attributes["thermal_intelligence_plan_detail"][
            "comparison"
        ]["baseline"]
        == "shared_control_passive"
    )
