"""Frozen offline traces of the unmodified f6eb7dd HA control cycle.

These are observed synthetic HA cycles, not a second implementation of the
controller. Gaps intentionally exercise the real one-minute brake ramp cap.
"""

from datetime import datetime, timedelta, timezone

import pytest

from test_brake_provenance import _cycle, _sensor

START = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)

# minute, HA state, mode, brake, preheat, PI integral, PI error, was braking,
# short-dip bridge. Values recorded from f6eb7dd, prior to extraction.
PRICE_CYCLE = [
    (0, 4.5, "pre_braking", 0.066667, 0, 0, 0.5, False, False),
    (10, 5.3, "pre_braking", 0.133333, 0, 0, 0.5, False, False),
    (11, 6.0, "pre_braking", 0.2, 0, 0, 0.5, False, False),
    (12, 6.8, "pre_braking", 0.266667, 0, 0, 0.5, False, False),
    (15, 7.5, "braking", 0.333333, 0, 0, 0.5, True, False),
    (16, 8.3, "braking", 0.4, 0, 0, 0.5, True, False),
    (17, 9.0, "braking", 0.466667, 0, 0, 0.5, True, False),
    (45, 9.0, "brake_hold", 0.466667, 0, 0, 0.5, True, True),
    (46, 9.0, "brake_hold", 0.466667, 0, 0, 0.5, True, True),
    (60, 9.8, "braking", 0.533333, 0, 0, 0.5, True, False),
    (75, 10.5, "braking", 0.6, 0, 0, 0.5, True, False),
    (89, 11.3, "braking", 0.666667, 0, 0, 0.5, True, False),
    (90, 10.5, "normal", 0.6, 0, 0, 0.5, True, False),
    (91, 9.8, "normal", 0.533333, 0, 0, 0.5, True, False),
    (92, 9.0, "normal", 0.466667, 0, 0, 0.5, True, False),
    (93, 8.3, "normal", 0.4, 0, 0, 0.5, True, False),
    (94, 7.5, "normal", 0.333333, 0, 0, 0.5, True, False),
    (95, 6.8, "normal", 0.266667, 0, 0, 0.5, True, False),
    (96, 6.0, "normal", 0.2, 0, 0, 0.5, True, False),
    (97, 5.3, "normal", 0.133333, 0, 0, 0.5, True, False),
    (98, 4.5, "normal", 0.066667, 0, 0, 0.5, True, False),
    (99, 3.8, "normal", 0.0, 0, 0, 0.5, True, False),
    (100, 3.8, "normal", 0, 0, 0.0175, 0.5, False, False),
    (101, 3.8, "normal", 0, 0, 0.035, 0.5, False, False),
]

PRICE_COMMANDS = [
    4.546667,
    5.293333,
    6.04,
    6.786667,
    7.533333,
    8.28,
    9.026667,
    9.026667,
    9.026667,
    9.773333,
    10.52,
    11.266667,
    10.52,
    9.773333,
    9.026667,
    8.28,
    7.533333,
    6.786667,
    6.04,
    5.293333,
    4.546667,
    3.8,
    3.7825,
    3.765,
]

PREHEAT_CYCLE = [
    (0, 3.5, "preheating", 0, 0.066667, 0.0175, 0.5, False, False),
    (1, 3.2, "preheating", 0, 0.133333, 0.035, 0.5, False, False),
    (2, 2.9, "preheating", 0, 0.2, 0.0525, 0.5, False, False),
    (3, 2.7, "preheating", 0, 0.266667, 0.07, 0.5, False, False),
    (4, 2.4, "preheating", 0, 0.333333, 0.0875, 0.5, False, False),
    (30, 0.6, "preheating", 0, 0.666667, 0.5425, 0.5, False, False),
    (60, -1.3, "preheating", 0, 1, 1.0675, 0.5, False, False),
    (90, -1.8, "preheating", 0, 1, 1.5925, 0.5, False, False),
    (100, -2.0, "preheating", 0, 1, 1.7675, 0.5, False, False),
    (104, -2.0, "preheating", 0, 1, 1.8375, 0.5, False, False),
    (105, 2.8, "pre_braking", 0.066667, 1, 1.8375, 0.5, False, False),
    (106, 3.7, "pre_braking", 0.133333, 1, 1.8375, 0.5, False, False),
    (110, 4.6, "pre_braking", 0.2, 1, 1.8375, 0.5, False, False),
    (114, 5.4, "pre_braking", 0.266667, 1, 1.8375, 0.5, False, False),
    (115, 6.3, "pre_braking", 0.333333, 1, 1.8375, 0.5, False, False),
    (116, 7.2, "pre_braking", 0.4, 1, 1.8375, 0.5, False, False),
    (119, 8.0, "pre_braking", 0.466667, 1, 1.8375, 0.5, False, False),
    (120, 8.9, "braking", 0.533333, 1, 1.8375, 0.5, True, False),
    (121, 9.8, "braking", 0.6, 1, 1.8375, 0.5, True, False),
    (130, 10.7, "braking", 0.666667, 1, 1.8375, 0.5, True, False),
    (180, 9.8, "normal", 0.6, 0.666667, 1.8375, 0.5, True, False),
    (181, 8.9, "normal", 0.533333, 0.6, 1.8375, 0.5, True, False),
    (182, 8.0, "normal", 0.466667, 0.533333, 1.8375, 0.5, True, False),
]

COMFORT_SAFE_CYCLE = [
    (15, 4.5, "braking", 0.066667, 0, 0, 0.5, True, False),
    (16, 5.3, "braking", 0.133333, 0, 0, 0.5, True, False),
    (17, 6.0, "braking", 0.2, 0, 0, 0.5, True, False),
    (18, 2.2, "braking", 0.133333, 0, 0, 2.0, True, False),
    (19, 1.2, "braking", 0.066667, 0, 0, 2.0, True, False),
    (20, 5.0, "safe_mode", 0, 0, 0, 0, False, False),
    (21, 4.5, "braking", 0.066667, 0, 0, 0.5, True, False),
    (22, 5.3, "braking", 0.133333, 0, 0, 0.5, True, False),
    (45, 5.3, "brake_hold", 0.133333, 0, 0, 0.5, True, True),
]


def _assert_trace(sensor, monkeypatch, rows, indoor_values=None):
    for index, expected in enumerate(rows):
        minute = expected[0]
        if indoor_values is not None:
            sensor.hass.states.get("sensor.indoor").state = str(indoor_values[index])
        attributes = _cycle(sensor, monkeypatch, minute)
        actual = (
            minute,
            sensor.state,
            attributes.get("mode"),
            round(sensor._brake_ramp, 6),
            round(sensor._preheat_ramp, 6),
            round(sensor._pi._integral, 6),
            round(sensor._pi._last_error, 6),
            sensor._was_braking_last_cycle,
            sensor._bridge_short_dip_active,
        )
        assert actual == expected
        assert sensor._pi._last_time == START + timedelta(minutes=minute)


def test_frozen_price_cycle_including_unrounded_commands(monkeypatch):
    sensor, _ = _sensor()
    original = sensor._set_state
    commands = []

    async def capture(command, mode, attrs, now):
        commands.append(command)
        await original(command, mode, attrs, now)

    monkeypatch.setattr(sensor, "_set_state", capture)
    _assert_trace(sensor, monkeypatch, PRICE_CYCLE)
    assert [round(command, 6) for command in commands] == PRICE_COMMANDS


def test_frozen_preheat_brake_and_recovery(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["preheat_boost_enabled"] = True
    states["sensor.price"].attributes["today"] = [1.0] * 40 + [10.0] * 4 + [1.0] * 52

    async def cold_forecast():
        return [-5] * 8

    monkeypatch.setattr(sensor, "_forecast_temps", cold_forecast)
    _assert_trace(sensor, monkeypatch, PREHEAT_CYCLE)


def test_frozen_comfort_floor_safe_and_return(monkeypatch):
    sensor, _ = _sensor()
    _assert_trace(
        sensor,
        monkeypatch,
        COMFORT_SAFE_CYCLE,
        [20.5, 20.5, 20.5, 19, 19, "unavailable", 20.5, 20.5, 20.5],
    )


def _context_snapshot(sensor, attrs, minute):
    return (
        minute,
        sensor.state,
        attrs.get("mode"),
        round(sensor._brake_ramp, 6),
        round(sensor._preheat_ramp, 6),
        round(sensor._pi._integral, 6),
        round(sensor._pi._last_error, 6),
        sensor._pi_precool_paused,
        sensor._was_braking_last_cycle,
        sensor._bridge_short_dip_active,
        attrs.get("target_temperature"),
    )


@pytest.mark.parametrize(
    ("holiday", "aggressiveness", "expected"),
    [
        (
            False,
            3,
            [
                (0, 3.8, "normal", 0, 0, 0.0175, 0.5, False, False, False, 21),
                (0.5, 5.5, "precool", 0.05, 0, 0.0175, 0.5, True, False, False, 21),
                (1.5, 6.5, "precool", 0.15, 0, 0.0175, 0.5, True, False, False, 21),
                (30, 4.7, "normal", 0.083333, 0, 0.0175, 0.5, False, False, False, 21),
                (31, 4.0, "normal", 0.016667, 0, 0.0175, 0.5, False, False, False, 21),
            ],
        ),
        (
            True,
            3,
            [
                (0, 3.8, "holiday", 0, 0, 0.0175, 0.5, False, False, False, 17),
                (0.5, 5.5, "precool", 0.05, 0, 0.0175, 0.5, True, False, False, 17),
                (1.5, 6.5, "precool", 0.15, 0, 0.0175, 0.5, True, False, False, 17),
                (30, 4.7, "holiday", 0.083333, 0, 0.0175, 0.5, False, False, False, 17),
                (31, 4.0, "holiday", 0.016667, 0, 0.0175, 0.5, False, False, False, 17),
            ],
        ),
        (
            True,
            0,
            [
                (0, 3.8, "normal", 0, 0, 0.000175, 0.5, False, False, False, 17),
                (0.5, 6.0, "precool", 0.1, 0, 0.000175, 0.5, True, False, False, 17),
                (1.5, 7.0, "precool", 0.2, 0, 0.000175, 0.5, True, False, False, 17),
                (30, 3.8, "normal", 0.2, 0, 0.000175, 0.5, False, False, False, 17),
                (31, 3.8, "normal", 0.2, 0, 0.017675, 0.5, False, False, False, 17),
            ],
        ),
    ],
)
def test_frozen_precool_resume_holiday_and_aggressiveness_zero(
    monkeypatch, holiday, aggressiveness, expected
):
    from test_precool_pi_clock import _cycle as cycle, _sensor as create

    sensor, _ = create(monkeypatch, holiday, aggressiveness)
    for row in expected:
        minute = row[0]
        forecast = [25.0] * 6 if minute in (0.5, 1.5) else None
        attrs = cycle(sensor, monkeypatch, minute, forecast)
        assert _context_snapshot(sensor, attrs, minute) == row


def test_frozen_summer_reset_and_long_gap(monkeypatch):
    from test_precool_pi_clock import _cycle as cycle, _sensor as create

    sensor, states = create(monkeypatch)
    expected = [
        (0, 3.8, "normal", 0, 0, 0.0175, 0.5, False, False, False, 21),
        (1, 19.0, "summer_mode", 0, 0, 0, 0, False, False, False, 21),
        (2, 3.8, "normal", 0, 0, 0.0175, 0.5, False, False, False, 21),
        (32, 3.3, "normal", 0, 0, 0.5425, 0.5, False, False, False, 21),
        (33, 3.2, "normal", 0, 0, 0.56, 0.5, False, False, False, 21),
    ]
    for row in expected:
        minute = row[0]
        states["sensor.outdoor"].state = "19" if minute == 1 else "5"
        assert (
            _context_snapshot(sensor, cycle(sensor, monkeypatch, minute), minute) == row
        )


def test_frozen_repeated_winter_hour_preserves_existing_clock_semantics(monkeypatch):
    import asyncio
    from zoneinfo import ZoneInfo
    from custom_components.pumpsteer import sensor as sensor_module
    from test_preheat_elapsed_time import _sensor as create

    sensor, states = create(monkeypatch)
    prices = [1.0] * 100
    prices[20:22] = [10.0] * 2
    states["sensor.price"].attributes["today"] = prices
    expected = [
        (0, 3.5, "preheating", 0, 0.066667, 0.0175, 0.5, False, False, False, 21),
        (1, 2.2, "preheating", 0, 0.4, 0.017675, 0.5, False, False, False, 21),
    ]
    for row in expected:
        fold = row[0]
        now = datetime(
            2026, 10, 25, 2, 15, tzinfo=ZoneInfo("Europe/Stockholm"), fold=fold
        )
        monkeypatch.setattr(sensor_module.dt_util, "now", lambda: now)
        asyncio.run(sensor._do_update())
        assert _context_snapshot(sensor, sensor.extra_state_attributes, fold) == row
        assert sensor._pi._last_time.isoformat() == now.isoformat()
        assert sensor._preheat_last_t.isoformat() == now.isoformat()
