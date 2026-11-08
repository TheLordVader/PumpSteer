"""Frozen ramp semantics shared by live control and isolated steps.

The expected values are from the controller before extraction, not from a
second implementation of the ramp algorithm.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.pumpsteer.control_ramps import brake_ramp_step, preheat_ramp_step
from test_brake_provenance import _sensor


T = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "last,now,expected",
    [
        (None, T, 1 / 15),
        (T, T + timedelta(seconds=30), 1 / 30),
        (T, T + timedelta(seconds=60), 1 / 15),
        (T, T + timedelta(minutes=15), 1 / 15),
        (T, T, 1 / 900),
        (T, T - timedelta(minutes=5), 1 / 900),
    ],
    ids=["startup", "half-minute", "minute", "large-gap", "duplicate", "backwards"],
)
def test_brake_step_matches_original_clock_bounds(last, now, expected):
    sensor, _ = _sensor()
    sensor._brake_ramp = 0.0
    sensor._brake_last_t = last
    factor, returned_last, last_expensive = brake_ramp_step(
        0.0, last, None, True, now, 15, 20, 0
    )
    assert factor == pytest.approx(expected)
    assert sensor._update_brake_ramp(True, now, 15, 20, 0) == pytest.approx(expected)
    assert returned_last == last_expensive == now
    assert sensor._brake_last_t == sensor._brake_last_expensive_t == now


@pytest.mark.parametrize(
    "elapsed,hold,expected",
    [(60, 2, 0.5), (120, 2, 0.45), (900, 2, 0.45), (60, 0, 0.45)],
    ids=["held", "hold-boundary", "long-gap", "no-hold"],
)
def test_brake_release_preserves_hold_and_one_minute_cap(elapsed, hold, expected):
    sensor, _ = _sensor()
    sensor._brake_ramp = 0.5
    sensor._brake_last_t = T
    sensor._brake_last_expensive_t = T
    now = T + timedelta(seconds=elapsed)
    factor, last, expensive = brake_ramp_step(0.5, T, T, False, now, 15, 20, hold)
    assert factor == pytest.approx(expected)
    assert sensor._update_brake_ramp(False, now, 15, 20, hold) == pytest.approx(
        expected
    )
    assert last == now and expensive == T


def test_brake_step_preserves_original_dst_fold_wall_clock_semantics():
    zone = ZoneInfo("Europe/Stockholm")
    first = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=0)
    second = datetime(2026, 10, 25, 2, 15, tzinfo=zone, fold=1)
    assert second.timestamp() - first.timestamp() == 3600
    sensor, _ = _sensor()
    sensor._brake_last_t = first
    factor, _, _ = brake_ramp_step(0.0, first, None, True, second, 15, 20, 0)
    # The existing controller subtracts same-zone wall times here and applies
    # its one-second minimum. This is parity, not a claim about physical time.
    assert factor == pytest.approx(1 / 900)
    assert sensor._update_brake_ramp(True, second, 15, 20, 0) == pytest.approx(factor)


@pytest.mark.parametrize(
    "requested,dt,expected",
    [
        (True, -60, 0.5),
        (True, 0, 0.5),
        (True, 60, 0.5 + 1 / 15),
        (True, 120, 0.5 + 2 / 15),
        (True, 1200, 0.5 + 5 / 15),
        (False, 1200, 0.25),
    ],
    ids=["negative", "duplicate", "minute", "two-minutes", "up-cap", "down-cap"],
)
def test_preheat_step_matches_original_five_minute_cap(requested, dt, expected):
    sensor, _ = _sensor()
    sensor._preheat_ramp = 0.5
    assert preheat_ramp_step(0.5, requested, 15, 20, dt) == pytest.approx(expected)
    assert sensor._update_preheat_ramp(requested, 15, 20, dt) == pytest.approx(expected)
