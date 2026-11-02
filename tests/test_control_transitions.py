"""Sequential, synthetic control transitions; no inference about savings."""

from dataclasses import replace
from datetime import timedelta
import ast
import subprocess
from zoneinfo import ZoneInfo

import pytest

from custom_components.pumpsteer.control_replay import (
    ControlReplayState,
    replay_control_step,
)
from custom_components.pumpsteer import sensor as sensor_module
from custom_components.pumpsteer import control_replay as replay
from test_control_intelligence import NOW, config, inputs, states


def _step(state, cfg, minute, *, expensive, live=True):
    now = NOW + timedelta(minutes=minute)
    observations = states(now) if live else {}
    return replay_control_step(
        state,
        replace(inputs(cfg, expensive=expensive, observations=observations), now=now),
    )


@pytest.mark.parametrize("expensive", [False, True])
def test_modifier_has_bounded_activation_dropout_recovery_and_flapping(expensive):
    cfg = config()
    if expensive:
        cfg["control_aux_preheat_limit"] = False
    else:
        cfg["control_balance_brake_limit"] = False
    state = ControlReplayState(
        brake_ramp=1 if expensive else 0,
        preheat_ramp=1,
        prev_aggressiveness=3,
    )
    outputs = []
    for minute, live in [
        (0, True),
        (1, True),
        (2, True),
        (3, False),
        (4, True),
        (5, False),
    ]:
        state, decision = _step(state, cfg, minute, expensive=expensive, live=live)
        outputs.append(decision.fake_temp)
    # With fixed inputs and a fully ramped base, modifier changes are gradual.
    # A changing PI term can add a small drift for preheat.
    assert all(abs(b - a) <= 0.8 for a, b in zip(outputs, outputs[1:])), outputs
    assert len(set(round(x, 3) for x in outputs)) > 2


def test_zero_elapsed_time_does_not_advance_modifier_twice():
    cfg = config()
    state = ControlReplayState(brake_ramp=1, prev_aggressiveness=3)
    state, first = _step(state, cfg, 0, expensive=True)
    state, second = _step(state, cfg, 0, expensive=True)
    assert second.attributes["brake_factor"] == first.attributes["brake_factor"]


def test_preheat_modifier_survives_one_normal_cycle_without_boost_jump():
    cfg = config()
    cfg["control_balance_brake_limit"] = False
    state = ControlReplayState(preheat_ramp=1, prev_aggressiveness=3)
    for minute in range(20):
        state, limited = _step(state, cfg, minute, expensive=False)
    assert limited.attributes["preheat_boost_c"] == 0
    now = NOW + timedelta(minutes=20)
    normal = replace(
        inputs(cfg, observations=states(now)),
        now=now,
        categories=("normal",) * 96,
    )
    state, between = replay_control_step(state, normal)
    assert between.mode == "normal"
    state, resumed = _step(state, cfg, 21, expensive=False)
    assert resumed.mode == "preheating"
    assert resumed.attributes["preheat_boost_c"] <= 0.4


def test_old_preheat_transition_does_not_suppress_new_phase_after_long_gap():
    cfg = config()
    cfg["control_balance_brake_limit"] = False
    state = ControlReplayState(preheat_ramp=1, prev_aggressiveness=3)
    for minute in range(20):
        state, _ = _step(state, cfg, minute, expensive=False)
    state, _ = _step(state, cfg, 20, expensive=True)
    state, _ = _step(state, cfg, 140, expensive=True)
    now = NOW + timedelta(minutes=141)
    observations = states(now)
    observations["binary_sensor.aux"].state = "off"
    state, resumed = replay_control_step(
        state,
        replace(inputs(cfg, observations=observations), now=now),
    )
    assert resumed.mode == "preheating"
    assert resumed.attributes["preheat_boost_c"] > 3.8


def test_new_brake_phase_does_not_reuse_prior_brake_correction():
    cfg = config()
    state = ControlReplayState(brake_ramp=1, preheat_ramp=1, prev_aggressiveness=3)
    for minute in range(20):
        state, _ = _step(state, cfg, minute, expensive=True)
    state, preheat = _step(state, cfg, 20, expensive=False)
    assert preheat.mode == "preheating"
    now = NOW + timedelta(minutes=21)
    observations = states(now)
    observations["sensor.balance"].state = "-50"
    data = replace(inputs(cfg, expensive=True, observations=observations), now=now)
    _, actual = replay_control_step(state, data)
    _, expected = replay_control_step(state, replace(data, cfg={}))
    assert actual.fake_temp == pytest.approx(expected.fake_temp)


@pytest.mark.parametrize("expensive", [False, True])
def test_expiry_at_301_seconds_returns_gradually_without_reusing_sample(expensive):
    cfg, observations = config(), states(NOW)
    state = ControlReplayState(
        brake_ramp=1 if expensive else 0,
        preheat_ramp=1,
        prev_aggressiveness=3,
    )
    for seconds in (0, 60, 120, 180, 240, 300):
        now = NOW + timedelta(seconds=seconds)
        state, before = replay_control_step(
            state,
            replace(
                inputs(cfg, expensive=expensive, observations=observations), now=now
            ),
        )
    state, expired = replay_control_step(
        state,
        replace(
            inputs(cfg, expensive=expensive, observations=observations),
            now=NOW + timedelta(seconds=301),
        ),
    )
    assert expired.attributes["control_intelligence"]["active"] is False
    assert (
        expired.attributes["control_intelligence"]["transition"] == "returning_to_base"
    )
    assert abs(expired.fake_temp - before.fake_temp) < 0.1


def test_safe_and_comfort_floor_take_precedence_over_modifier_transition():
    cfg = config()
    current = ControlReplayState(brake_ramp=1, prev_aggressiveness=3)
    for minute in range(10):
        current, _ = _step(current, cfg, minute, expensive=True)
    now = NOW + timedelta(minutes=10)
    floor_input = replace(
        inputs(cfg, expensive=True, observations={}), now=now, indoor=15
    )
    _, with_transition = replay_control_step(current, floor_input)
    _, base = replay_control_step(current, replace(floor_input, cfg={}))
    assert with_transition.fake_temp <= base.fake_temp
    safe_state, safe = replay_control_step(
        current, replace(floor_input, prices_available=False)
    )
    assert safe.mode == "safe_mode"
    assert safe_state.control_modifier_state is None


@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_modifier_transition_uses_real_elapsed_time_across_dst(day):
    from datetime import datetime

    first = datetime.fromisoformat(day + "T00:59:00+00:00")
    second = first + timedelta(minutes=1)
    local = ZoneInfo("Europe/Stockholm")
    cfg = config()
    initial = ControlReplayState(brake_ramp=1, prev_aggressiveness=3)
    initial, a = replay_control_step(
        initial,
        replace(
            inputs(cfg, expensive=True, observations=states(first)),
            now=first.astimezone(local),
        ),
    )
    _, b = replay_control_step(
        initial,
        replace(
            inputs(cfg, expensive=True, observations=states(second)),
            now=second.astimezone(local),
        ),
    )
    assert 0 < a.fake_temp - b.fake_temp < 0.8


@pytest.mark.parametrize("day", ["2026-03-29", "2026-10-25"])
def test_sequential_base_matches_installed_reference_across_dst(day, monkeypatch):
    from datetime import datetime

    source = subprocess.check_output(
        ["git", "show", "b273afc:custom_components/pumpsteer/sensor.py"], text=True
    )
    cls = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.ClassDef) and n.name == "PumpSteerSensor"
    )
    branch = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_control_logic"
    )
    scope = dict(vars(sensor_module))
    exec(
        compile(ast.Module(body=[branch], type_ignores=[]), "<reference>", "exec"),
        scope,
    )
    reference = scope["_run_control_logic"]
    start = datetime.fromisoformat(day + "T00:55:00+00:00")
    base = old = ControlReplayState(
        brake_ramp=0.8, preheat_ramp=0.7, prev_aggressiveness=3
    )
    for minute, expensive, indoor in [
        (0, False, 20.8),
        (3, True, 20.8),
        (6, True, 19),
        (9, True, 20.8),
        (12, False, 20.8),
        (15, False, 20.8),
    ]:
        now = (start + timedelta(minutes=minute)).astimezone(
            ZoneInfo("Europe/Stockholm")
        )
        data = replace(inputs(expensive=expensive), now=now, indoor=indoor)
        base, actual = replay_control_step(base, data)
        with monkeypatch.context() as isolated:
            isolated.setattr(replay._Scratch, "_run_control_logic", reference)
            old, expected = replay_control_step(old, data)
        assert actual == expected
        assert vars(base.pi) == vars(old.pi)
        assert base.brake_ramp == old.brake_ramp
        assert base.preheat_ramp == old.preheat_ramp
