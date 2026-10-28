"""Replay the real control code against frozen f6eb7dd offline traces."""

import asyncio
import copy
import logging
from datetime import timedelta

import pytest

from custom_components.pumpsteer import sensor as sensor_module
from custom_components.pumpsteer.control_replay import (
    ControlReplayInput,
    ControlReplayState,
    replay_control_step,
)
from test_brake_provenance import _cycle, _sensor
from test_control_golden_trace import (
    COMFORT_SAFE_CYCLE,
    PREHEAT_CYCLE,
    PRICE_COMMANDS,
    PRICE_CYCLE,
    START,
)


def _signature(state):
    return (
        vars(state.pi).copy(),
        state.pi_precool_paused,
        copy.deepcopy(state.last_pi_result),
        state.brake_ramp,
        state.preheat_ramp,
        state.preheat_last_t,
        state.brake_last_t,
        state.brake_last_expensive_t,
        state.was_braking_last_cycle,
        state.prev_aggressiveness,
        state.bridge_short_dip_active,
        state.thermal_pending_samples,
    )


def _input(sensor, now, *, indoor, outdoor=5.0, forecast=None, holiday=False):
    cfg = sensor._cfg()
    prices, categories, interval, slot = asyncio.run(sensor._get_prices(cfg, now))
    return ControlReplayInput(
        now=now,
        cfg=cfg,
        indoor=indoor,
        outdoor=outdoor,
        target=cfg["target_temperature"],
        summer_threshold=cfg["summer_threshold"],
        aggressiveness=cfg["aggressiveness"],
        holiday=holiday,
        categories=tuple(categories),
        current_slot=slot,
        interval_minutes=interval,
        house_inertia=cfg["house_inertia"],
        forecast_temps=forecast,
        outlook=None,
        preheat_enabled=cfg.get("preheat_boost_enabled", True),
        prices_available=bool(prices),
    )


def _quiet_step(state, inputs, monkeypatch, caplog):
    original = _signature(state)
    original_cfg = copy.deepcopy(inputs.cfg)
    caplog.clear()
    with caplog.at_level(logging.DEBUG), monkeypatch.context() as isolated:

        def forbidden(*args, **kwargs):
            raise AssertionError("passive replay emitted operational event")

        isolated.setattr(sensor_module, "log_event", forbidden)
        isolated.setattr(sensor_module, "log_mode_change", forbidden)
        next_state, decision = replay_control_step(state, inputs)
    assert not caplog.records
    assert _signature(state) == original
    assert inputs.cfg == original_cfg
    return next_state, decision


def _replay_trace(
    sensor, states, monkeypatch, caplog, rows, *, indoor_values=None, forecast=None
):
    state = ControlReplayState.from_sensor(sensor)
    commands = []
    for index, row in enumerate(rows):
        minute = row[0]
        now = START + timedelta(minutes=minute)
        indoor = 20.5 if indoor_values is None else indoor_values[index]
        states["sensor.indoor"].state = "unavailable" if indoor is None else str(indoor)
        inputs = _input(sensor, now, indoor=indoor, forecast=forecast)
        next_state, decision = _quiet_step(state, inputs, monkeypatch, caplog)
        assert decision.mode == row[2]
        assert round(decision.fake_temp, 1) == row[1]
        assert round(next_state.brake_ramp, 6) == row[3]
        assert round(next_state.preheat_ramp, 6) == row[4]
        assert round(next_state.pi._integral, 6) == row[5]
        assert round(next_state.pi._last_error, 6) == row[6]
        assert next_state.was_braking_last_cycle is row[7]
        assert next_state.bridge_short_dip_active is row[8]
        assert next_state.pi._last_time == now
        attrs = _cycle(sensor, monkeypatch, minute)
        assert sensor.state == row[1] and attrs["mode"] == row[2]
        live = ControlReplayState.from_sensor(sensor)
        assert round(live.brake_ramp, 6) == round(next_state.brake_ramp, 6)
        assert round(live.preheat_ramp, 6) == round(next_state.preheat_ramp, 6)
        assert round(live.pi._integral, 6) == round(next_state.pi._integral, 6)
        commands.append(decision.fake_temp)
        state = next_state
    return commands


def test_price_cycle_exactly_matches_frozen_live_and_unrounded_commands(
    monkeypatch, caplog
):
    sensor, states = _sensor()
    commands = _replay_trace(sensor, states, monkeypatch, caplog, PRICE_CYCLE)
    assert [round(value, 6) for value in commands] == PRICE_COMMANDS


def test_preheat_brake_and_recovery_matches_frozen_live(monkeypatch, caplog):
    sensor, states = _sensor()
    sensor._config_entry.options["preheat_boost_enabled"] = True
    states["sensor.price"].attributes["today"] = [1.0] * 40 + [10.0] * 4 + [1.0] * 52

    async def cold():
        return [-5] * 8

    monkeypatch.setattr(sensor, "_forecast_temps", cold)
    _replay_trace(sensor, states, monkeypatch, caplog, PREHEAT_CYCLE, forecast=[-5] * 8)


def test_comfort_floor_safe_and_recovery_matches_frozen_live(monkeypatch, caplog):
    sensor, states = _sensor()
    _replay_trace(
        sensor,
        states,
        monkeypatch,
        caplog,
        COMFORT_SAFE_CYCLE,
        indoor_values=[20.5, 20.5, 20.5, 19, 19, None, 20.5, 20.5, 20.5],
    )


@pytest.mark.parametrize(
    ("holiday", "aggressiveness", "expected"),
    [
        (
            False,
            3,
            [
                (0, 3.8, "normal", 0, 0.0175, False),
                (0.5, 5.5, "precool", 0.05, 0.0175, True),
                (1.5, 6.5, "precool", 0.15, 0.0175, True),
                (30, 4.7, "normal", 0.083333, 0.0175, False),
                (31, 4.0, "normal", 0.016667, 0.0175, False),
            ],
        ),
        (
            True,
            3,
            [
                (0, 3.8, "holiday", 0, 0.0175, False),
                (0.5, 5.5, "precool", 0.05, 0.0175, True),
                (1.5, 6.5, "precool", 0.15, 0.0175, True),
                (30, 4.7, "holiday", 0.083333, 0.0175, False),
                (31, 4.0, "holiday", 0.016667, 0.0175, False),
            ],
        ),
        (
            True,
            0,
            [
                (0, 3.8, "normal", 0, 0.000175, False),
                (0.5, 6.0, "precool", 0.1, 0.000175, True),
                (1.5, 7.0, "precool", 0.2, 0.000175, True),
                (30, 3.8, "normal", 0.2, 0.000175, False),
                (31, 3.8, "normal", 0.2, 0.017675, False),
            ],
        ),
    ],
)
def test_precool_holiday_and_zero_aggressiveness_match_frozen_live(
    monkeypatch, caplog, holiday, aggressiveness, expected
):
    from test_precool_pi_clock import _cycle as cycle, _sensor as create, _time

    sensor, states = create(monkeypatch, holiday, aggressiveness)
    state = ControlReplayState.from_sensor(sensor)
    for minute, fake, mode, brake, integral, paused in expected:
        forecast = [25.0] * 6 if minute in (0.5, 1.5) else None
        inputs = _input(
            sensor,
            _time(minute),
            indoor=float(states["sensor.indoor"].state),
            holiday=holiday,
            forecast=forecast,
        )
        next_state, decision = _quiet_step(state, inputs, monkeypatch, caplog)
        assert (round(decision.fake_temp, 1), decision.mode) == (fake, mode)
        assert round(next_state.brake_ramp, 6) == brake
        assert round(next_state.pi._integral, 6) == integral
        assert next_state.pi_precool_paused is paused
        attrs = cycle(sensor, monkeypatch, minute, forecast)
        assert (sensor.state, attrs["mode"]) == (fake, mode)
        state = next_state


def test_summer_reset_and_long_gap_match_frozen_live(monkeypatch, caplog):
    from test_precool_pi_clock import _cycle as cycle, _sensor as create, _time

    sensor, states = create(monkeypatch)
    state = ControlReplayState.from_sensor(sensor)
    expected = [
        (0, 5.0, 3.8, "normal", 0.0175),
        (1, 19.0, 19.0, "summer_mode", 0),
        (2, 5.0, 3.8, "normal", 0.0175),
        (32, 5.0, 3.3, "normal", 0.5425),
        (33, 5.0, 3.2, "normal", 0.56),
    ]
    for minute, outdoor, fake, mode, integral in expected:
        states["sensor.outdoor"].state = str(outdoor)
        inputs = _input(sensor, _time(minute), indoor=20.5, outdoor=outdoor)
        state, decision = _quiet_step(state, inputs, monkeypatch, caplog)
        assert (round(decision.fake_temp, 1), decision.mode) == (fake, mode)
        assert round(state.pi._integral, 6) == integral
        attrs = cycle(sensor, monkeypatch, minute)
        assert (sensor.state, attrs["mode"]) == (fake, mode)


def test_restart_mid_brake_matches_restored_frozen_live(monkeypatch, caplog):
    from types import SimpleNamespace
    import homeassistant.helpers.restore_state as restore

    before, _ = _sensor()
    for minute in (15, 16, 17):
        _cycle(before, monkeypatch, minute)
    monkeypatch.setattr(restore, "ExtraStoredData", object, raising=False)
    saved = before.extra_restore_state_data.as_dict()
    sensor, states = _sensor()

    async def restored_extra():
        return SimpleNamespace(as_dict=lambda: saved)

    monkeypatch.setattr(
        sensor, "async_get_last_extra_data", restored_extra, raising=False
    )
    asyncio.run(sensor.async_added_to_hass())
    rows = [
        (45, 6.0, "brake_hold", 0.2, 0, 0, 0.5, True, True),
        (46, 6.0, "brake_hold", 0.2, 0, 0, 0.5, True, True),
        (60, 6.8, "braking", 0.266667, 0, 0, 0.5, True, False),
        (75, 7.5, "braking", 0.333333, 0, 0, 0.5, True, False),
        (90, 6.8, "normal", 0.266667, 0, 0, 0.5, True, False),
        (91, 6.0, "normal", 0.2, 0, 0, 0.5, True, False),
    ]
    _replay_trace(sensor, states, monkeypatch, caplog, rows)


def test_winter_time_fold_matches_frozen_live(monkeypatch, caplog):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from test_preheat_elapsed_time import _sensor as create

    sensor, states = create(monkeypatch)
    prices = [1.0] * 100
    prices[20:22] = [10.0] * 2
    states["sensor.price"].attributes["today"] = prices
    state = ControlReplayState.from_sensor(sensor)
    for fold, fake, preheat, integral in (
        (0, 3.5, 0.066667, 0.0175),
        (1, 2.2, 0.4, 0.017675),
    ):
        now = datetime(
            2026, 10, 25, 2, 15, tzinfo=ZoneInfo("Europe/Stockholm"), fold=fold
        )
        inputs = _input(sensor, now, indoor=20.5, forecast=[0.0] * 6)
        state, decision = _quiet_step(state, inputs, monkeypatch, caplog)
        assert (round(decision.fake_temp, 1), decision.mode) == (fake, "preheating")
        assert round(state.preheat_ramp, 6) == preheat
        assert round(state.pi._integral, 6) == integral
        assert state.preheat_last_t.isoformat() == now.isoformat()
        assert state.pi._last_time.isoformat() == now.isoformat()
        monkeypatch.setattr(sensor_module.dt_util, "now", lambda: now)
        asyncio.run(sensor._do_update())
        assert (sensor.state, sensor.extra_state_attributes["mode"]) == (
            fake,
            "preheating",
        )


def test_safe_during_bridge_preserves_existing_live_semantics(monkeypatch, caplog):
    sensor, states = _sensor()
    rows = [
        (15, 4.5, "braking", 0.066667, 0, 0, 0.5, True, False),
        (16, 5.3, "braking", 0.133333, 0, 0, 0.5, True, False),
        (17, 6.0, "braking", 0.2, 0, 0, 0.5, True, False),
        (45, 6.0, "brake_hold", 0.2, 0, 0, 0.5, True, True),
        (46, 5.0, "safe_mode", 0, 0, 0, 0, False, True),
        (47, 4.5, "pre_braking", 0.066667, 0, 0, 0.5, False, False),
        (60, 5.3, "braking", 0.133333, 0, 0, 0.5, True, False),
    ]
    _replay_trace(
        sensor,
        states,
        monkeypatch,
        caplog,
        rows,
        indoor_values=[20.5, 20.5, 20.5, 20.5, None, 20.5, 20.5],
    )


@pytest.mark.parametrize("failure", ["indoor_nan", "outdoor_unknown", "no_prices"])
def test_invalid_indata_safe_matches_live_and_does_not_mutate_state(
    monkeypatch, caplog, failure
):
    sensor, states = _sensor()
    _cycle(sensor, monkeypatch, 0)
    state = ControlReplayState.from_sensor(sensor)
    indoor, outdoor = 20.5, 5.0
    if failure == "indoor_nan":
        indoor = float("nan")
        states["sensor.indoor"].state = "nan"
    elif failure == "outdoor_unknown":
        outdoor = None
        states["sensor.outdoor"].state = "unknown"
    else:
        states["sensor.price"].attributes["today"] = []
    inputs = _input(
        sensor, START + timedelta(minutes=1), indoor=indoor, outdoor=outdoor
    )
    next_state, decision = _quiet_step(state, inputs, monkeypatch, caplog)
    live_attrs = _cycle(sensor, monkeypatch, 1)
    assert decision.mode == live_attrs["mode"] == "safe_mode"
    assert decision.fake_temp == sensor.state
    assert next_state.brake_ramp == sensor._brake_ramp == 0
    assert next_state.preheat_ramp == sensor._preheat_ramp == 0
    assert next_state.pi._integral == sensor._pi._integral == 0
