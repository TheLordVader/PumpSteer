"""The real daily price cache rule must also be usable without HA side effects."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pumpsteer import electricity_price as prices


ZONE = ZoneInfo("Europe/Stockholm")


def step(now, cached_date=None, cached_entity=None, p30=0.0, p80=0.0, day=None):
    return prices.price_cache_transition(
        now=now,
        entity_id="sensor.price",
        today_prices=list(range(24)) if day is None else day,
        cached_date=cached_date,
        cached_entity_id=cached_entity,
        cached_p30=p30,
        cached_p80=p80,
    )


def test_midnight_grace_keeps_yesterday_and_refreshes_at_0015():
    midnight = datetime(2026, 10, 12, tzinfo=ZONE)
    prev = ("2026-10-11", "sensor.price", 3.0, 8.0)
    for minute in (0, 14):
        result = step(midnight + timedelta(minutes=minute), *prev)
        assert (result.p30, result.p80, result.date, result.entity_id) == (
            3,
            8,
            *prev[:2],
        )
        assert not result.refreshed
    result = step(midnight + timedelta(minutes=15), *prev)
    assert result.refreshed
    assert (result.p30, result.p80) == prices.compute_price_thresholds(
        [], list(range(24))
    )
    assert result.date == "2026-10-12"
    assert result.entity_id == "sensor.price"


def test_restart_in_midnight_grace_keeps_unknown_zero_cache():
    result = step(datetime(2026, 10, 12, 0, 5, tzinfo=ZONE))
    assert (result.p30, result.p80, result.date, result.entity_id) == (0, 0, None, None)
    assert not result.refreshed


def test_same_day_retains_cache_and_entity_change_refreshes_after_grace():
    now = datetime(2026, 10, 12, 10, tzinfo=ZONE)
    unchanged = step(now, "2026-10-12", "sensor.price", 4, 9)
    assert not unchanged.refreshed and (unchanged.p30, unchanged.p80) == (4, 9)
    changed = step(now, "2026-10-12", "sensor.old", 4, 9)
    assert changed.refreshed and changed.entity_id == "sensor.price"


@pytest.mark.parametrize("slots", [23, 25, 46, 50, 92, 100])
def test_complete_summer_and_winter_day_thresholds_use_all_slots(slots):
    now = datetime(2026, 10, 25, 0, 15, tzinfo=ZONE)
    day = [float(i) / 10 for i in range(slots)]
    result = step(now, "2026-10-24", "sensor.price", 0.5, 1.0, day)
    assert result.refreshed
    assert (result.p30, result.p80) == prices.compute_price_thresholds([], day)


def test_shared_threshold_calculation_matches_async_live_entrypoint():
    day = [0.1, 0.4, 0.9, 1.7, 2.0]
    assert asyncio.run(
        prices.async_get_price_thresholds(day)
    ) == prices.compute_price_thresholds([], day)


def test_no_data_never_replaces_cache():
    result = step(
        datetime(2026, 10, 12, 10, tzinfo=ZONE), "2026-10-11", "sensor.price", 3, 8, []
    )
    assert not result.refreshed and (result.p30, result.p80, result.date) == (
        3,
        8,
        "2026-10-11",
    )


def test_live_explicit_thresholds_follow_shared_refresh_rule():
    now = datetime(2026, 10, 12, 0, 15, tzinfo=ZONE)
    result = prices.price_cache_transition(
        now=now,
        entity_id="sensor.price",
        today_prices=[1, 2, 3],
        cached_date="2026-10-11",
        cached_entity_id="sensor.price",
        cached_p30=7,
        cached_p80=8,
        thresholds=(0.25, 0.75),
    )
    assert result.refreshed and (result.p30, result.p80) == (0.25, 0.75)
    assert prices.price_cache_refresh_due(
        now=now,
        entity_id="sensor.price",
        today_prices=[1, 2, 3],
        cached_date="2026-10-11",
        cached_entity_id="sensor.price",
    )
