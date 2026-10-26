"""As-of thermal inputs preserve real source times and fail closed on gaps."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[1] / "custom_components/pumpsteer/forecast.py"
_SPEC = importlib.util.spec_from_file_location("thermal_forecast_inputs", _PATH)
_MOD = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MOD
_SPEC.loader.exec_module(_MOD)


def instant(text="2026-10-25T00:00:00+00:00"):
    return datetime.fromisoformat(text)


def point(at, temperature):
    return _MOD.ForecastPoint(at, None, temperature, None, None)


def state(rows, reported=None, attr="raw_today"):
    return SimpleNamespace(attributes={attr: rows}, last_reported=reported)


def snapshot(points=(), **kwargs):
    return _MOD.make_thermal_forecast(points, received_at=instant(), **kwargs)


def test_import_has_no_homeassistant_dependency():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import importlib.util,sys; s=importlib.util.spec_from_file_location('forecast', {str(_PATH)!r}); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); assert not any(k.startswith('homeassistant') for k in sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_weather_stepwise_path_is_copied_immutable_and_celsius():
    now = instant()
    points = [point(now, 32), point(now + timedelta(hours=1), 50)]
    forecast = snapshot(points, weather_source="weather.synthetic", weather_unit="°F")
    points[0].outdoor_temp = 100
    points.clear()
    assert forecast.weather_path(now, now + timedelta(hours=2), -5) == (
        (now + timedelta(hours=1), 0.0),
        (now + timedelta(hours=2), 10.0),
    )
    assert forecast.weather_source == "weather.synthetic"
    assert forecast.weather_issued_at is None
    with pytest.raises(FrozenInstanceError):
        forecast.weather[0].temperature_c = 99
    with pytest.raises(FrozenInstanceError):
        forecast.received_at = now + timedelta(hours=1)


def test_current_outdoor_only_bridges_to_first_point_within_one_hour():
    now = instant()
    forecast = snapshot([point(now + timedelta(minutes=30), 5)])
    assert forecast.weather_path(now, now + timedelta(hours=1), -2) == (
        (now + timedelta(minutes=30), -2.0),
        (now + timedelta(hours=1), 5.0),
    )
    assert (
        snapshot([point(now + timedelta(minutes=61), 5)]).weather_path(
            now, now + timedelta(hours=2), -2
        )
        is None
    )
    assert forecast.weather_path(now, now + timedelta(hours=1), float("nan")) is None


def test_weather_gaps_and_uncovered_tail_are_unavailable():
    now = instant()
    forecast = snapshot([point(now, 1), point(now + timedelta(hours=2), 3)])
    assert forecast.weather_path(now, now + timedelta(hours=3), 0) is None
    assert forecast.weather_path(now, now + timedelta(minutes=30), 0) == (
        (now + timedelta(minutes=30), 1.0),
    )
    assert (
        snapshot([point(now, 1)]).weather_path(
            now, now + timedelta(hours=1, seconds=1), 0
        )
        is None
    )


@pytest.mark.parametrize("age", [-1, 5401])
def test_receipt_is_not_future_or_older_than_ninety_minutes(age):
    now = instant()
    forecast = snapshot([point(now, 2), point(now + timedelta(hours=1), 3)])
    start = now + timedelta(seconds=age)
    assert forecast.weather_path(start, start + timedelta(minutes=1), 0) is None


@pytest.mark.parametrize(
    "issued",
    ["2026-10-25T00:00:01+00:00", "2026-10-24T17:59:59+00:00", "2026-10-25T00:00:00"],
)
def test_invalid_weather_provenance_rejects_weather(issued):
    now = instant()
    assert (
        snapshot([point(now, 1)], weather_issued_at=instant(issued)).weather_path(
            now, now + timedelta(minutes=1), 0
        )
        is None
    )


@pytest.mark.parametrize("kind", ["nan", "infinite", "naive", "duplicate", "unit"])
def test_invalid_weather_source_is_not_partially_accepted(kind):
    now = instant()
    rows = [point(now, 1)]
    kwargs = {}
    if kind == "nan":
        rows.append(point(now + timedelta(hours=1), float("nan")))
    elif kind == "infinite":
        rows[0].outdoor_temp = float("inf")
    elif kind == "naive":
        rows[0].timestamp = now.replace(tzinfo=None)
    elif kind == "duplicate":
        rows.append(point(now, 2))
    else:
        kwargs["weather_unit"] = "kelvin"
    assert (
        snapshot(rows, **kwargs).weather_path(now, now + timedelta(minutes=1), 0)
        is None
    )


def test_prices_keep_quarter_hours_negative_values_and_clip_at_utc_dst_boundary():
    now = instant()
    rows = [
        {
            "start": "2026-10-25T02:00:00+02:00",
            "end": "2026-10-25T02:15:00+02:00",
            "value": -1.5,
        },
        {
            "start": "2026-10-25T02:15:00+02:00",
            "end": "2026-10-25T02:30:00+02:00",
            "value": 2,
        },
    ]
    source = state(rows, now - timedelta(hours=20))
    forecast = snapshot(price_state=source, price_source="sensor.synthetic")
    rows[0]["value"] = 99
    result = forecast.prices_between(
        now + timedelta(minutes=5), now + timedelta(minutes=25)
    )
    assert [(p.start, p.end, p.price) for p in result] == [
        (now + timedelta(minutes=5), now + timedelta(minutes=15), -1.5),
        (now + timedelta(minutes=15), now + timedelta(minutes=25), 2.0),
    ]
    assert forecast.price_reported_at == now - timedelta(hours=20)
    assert forecast.price_source == "sensor.synthetic"


def test_timestamped_adjacent_price_rows_define_intervals_without_hour_rounding():
    now = instant()
    rows = [
        {"startsAt": now.isoformat(), "total": 1},
        {"startsAt": (now + timedelta(minutes=30)).isoformat(), "total": 2},
        {"startsAt": (now + timedelta(hours=1)).isoformat(), "total": 3},
    ]
    forecast = snapshot(price_state=state(rows, now, "prices"))
    result = forecast.prices_between(now, now + timedelta(hours=1))
    assert [(p.end, p.price) for p in result] == [
        (now + timedelta(minutes=30), 1.0),
        (now + timedelta(hours=1), 2.0),
    ]


@pytest.mark.parametrize(
    "problem",
    [
        "numeric",
        "nan",
        "naive",
        "gap",
        "overlap",
        "duplicate",
        "future",
        "stale",
        "unreported",
    ],
)
def test_prices_fail_closed_on_bad_rows_provenance_or_coverage(problem):
    now = instant()
    rows = [
        {"start": now, "end": now + timedelta(minutes=15), "value": 1},
        {
            "start": now + timedelta(minutes=15),
            "end": now + timedelta(minutes=30),
            "value": 2,
        },
    ]
    reported = now
    if problem == "numeric":
        rows = [1, 2]
    elif problem == "nan":
        rows[0]["value"] = float("nan")
    elif problem == "naive":
        rows[0]["start"] = now.replace(tzinfo=None)
    elif problem == "gap":
        rows[1]["start"] += timedelta(minutes=1)
    elif problem == "overlap":
        rows[1]["start"] -= timedelta(minutes=1)
    elif problem == "duplicate":
        rows[1] = dict(rows[0])
    elif problem == "future":
        reported += timedelta(seconds=1)
    elif problem == "stale":
        reported -= timedelta(hours=36, seconds=1)
    elif problem == "unreported":
        reported = None
    forecast = snapshot(price_state=state(rows, reported))
    assert forecast.prices_between(now, now + timedelta(minutes=30)) is None


def test_tomorrow_prices_cross_day_boundary_and_snapshot_is_causal():
    now = instant("2026-10-25T23:45:00+00:00")
    today = state([{"start": now, "end": now + timedelta(minutes=15), "value": 1}], now)
    tomorrow = state(
        [
            {
                "start": now + timedelta(minutes=15),
                "end": now + timedelta(minutes=45),
                "value": 3,
            }
        ],
        now,
    )
    old = _MOD.make_thermal_forecast([], received_at=now, price_state=today)
    new = _MOD.make_thermal_forecast(
        [], received_at=now, price_state=today, tomorrow_state=tomorrow
    )
    assert old.prices_between(now, now + timedelta(minutes=45)) is None
    assert [p.price for p in new.prices_between(now, now + timedelta(minutes=45))] == [
        1.0,
        3.0,
    ]
    assert (
        new.prices_between(now - timedelta(seconds=1), now + timedelta(minutes=15))
        is None
    )


def test_price_receipt_ttl_and_known_issue_age_apply_at_start():
    now = instant()
    forecast = snapshot(
        price_state=state(
            [{"start": now, "end": now + timedelta(hours=1), "value": 1}], now
        )
    )
    assert (
        forecast.prices_between(
            now + timedelta(seconds=5401), now + timedelta(seconds=5402)
        )
        is None
    )
    issued = now - timedelta(hours=6)
    weather = snapshot([point(now, 1)], weather_issued_at=issued)
    assert weather.weather_path(now, now + timedelta(minutes=1), 0) is not None
    assert (
        weather.weather_path(now + timedelta(seconds=1), now + timedelta(minutes=1), 0)
        is None
    )


def test_snapshot_requires_real_aware_receipt_and_bounds_inputs():
    with pytest.raises(ValueError):
        _MOD.make_thermal_forecast([], received_at=instant().replace(tzinfo=None))
    now = instant()
    forecast = snapshot([point(now + timedelta(hours=i), i) for i in range(513)])
    assert forecast.weather_path(now, now + timedelta(minutes=1), 0) is None


def test_prices_do_not_hide_future_tomorrow_provenance_behind_older_today_state():
    now = instant()
    today = state([{"start": now, "end": now + timedelta(minutes=15), "value": 1}], now)
    tomorrow = state(
        [
            {
                "start": now + timedelta(minutes=15),
                "end": now + timedelta(minutes=30),
                "value": 2,
            }
        ],
        now + timedelta(seconds=1),
    )
    assert (
        snapshot(price_state=today, tomorrow_state=tomorrow).prices_between(
            now, now + timedelta(minutes=30)
        )
        is None
    )


@pytest.mark.parametrize("value", [True, False])
def test_boolean_prices_are_not_physical_price_values(value):
    now = instant()
    assert (
        snapshot(
            price_state=state(
                [{"start": now, "end": now + timedelta(minutes=15), "value": value}],
                now,
            )
        ).prices_between(now, now + timedelta(minutes=15))
        is None
    )


def test_missing_price_tail_and_unavailable_entity_are_not_filled():
    now = instant()
    rows = [
        {"start": now, "value": 1},
        {"start": now + timedelta(minutes=15), "value": 2},
    ]
    assert (
        snapshot(price_state=state(rows, now)).prices_between(
            now, now + timedelta(minutes=30)
        )
        is None
    )
    source = state(
        [{"start": now, "end": now + timedelta(minutes=15), "value": 1}], now
    )
    source.state = "unavailable"
    assert (
        snapshot(price_state=source).prices_between(now, now + timedelta(minutes=15))
        is None
    )


@pytest.mark.parametrize("channel", ["weather", "price"])
def test_out_of_range_numeric_input_is_unavailable_without_raising(channel):
    now = instant()
    if channel == "weather":
        forecast = snapshot([point(now, 10**1000)])
        assert forecast.weather_path(now, now + timedelta(minutes=1), 0) is None
    else:
        forecast = snapshot(
            price_state=state(
                [{"start": now, "end": now + timedelta(minutes=15), "value": 10**1000}],
                now,
            )
        )
        assert forecast.prices_between(now, now + timedelta(minutes=15)) is None


def test_out_of_range_weather_end_is_unavailable_without_raising():
    now = instant()
    forecast = snapshot([point(datetime.max.replace(tzinfo=now.tzinfo), 1)])
    assert forecast.weather_path(now, now + timedelta(minutes=1), 0) is None


def test_malformed_reported_time_is_not_hidden_by_last_updated():
    now = instant()
    source = state(
        [{"start": now, "end": now + timedelta(minutes=15), "value": 1}],
        now.replace(tzinfo=None),
    )
    source.last_updated = now
    assert (
        snapshot(price_state=source).prices_between(now, now + timedelta(minutes=15))
        is None
    )
