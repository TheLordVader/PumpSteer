"""Observed-response qualification and bounded real controller/replay decisions."""

import asyncio
import copy
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer.control_replay import (
    ControlReplayState,
    replay_control_step,
)
from test_brake_provenance import _sensor, _cycle
from test_control_replay_parity import _input

START = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


def train(model):
    # A complete training session precedes all independent holdout sessions.
    for minute in range(61):
        indoor = 21 - 0.01 * minute
        model.record_temp(START + timedelta(minutes=minute), indoor)
        model.collect_braking_sample(indoor, 5)
    assert model.fit()
    return model


def validate(model, start, error=0):
    model.start_validation_session(start, 21, 5, adaptive_observations=True)
    predicted = model.predict_drop(21, 5, 30)
    for minute in range(1, 32):
        model.update_validation_session(
            start + timedelta(minutes=minute), 21 - (predicted + error) * minute / 30, 5
        )
    model.end_validation_session(start + timedelta(minutes=31), 21 - predicted)


def qualified():
    model = train(ThermalModel())
    for hour in (2, 3, 4):
        validate(model, START + timedelta(hours=hour))
    return model


def test_fit_alone_does_not_qualify_and_holdouts_do():
    model = train(ThermalModel())
    assert (
        model.adaptive_reason(START + timedelta(hours=1), 16)
        == "insufficient_validation"
    )
    for hour in (2, 3, 4):
        validate(model, START + timedelta(hours=hour))
    assert model.adaptive_reason(START + timedelta(hours=5), 16) == "qualified"
    assert model.adaptive_reason(START + timedelta(days=8), 16) == "validation_expired"
    assert (
        model.adaptive_reason(START + timedelta(hours=5), 40)
        == "outside_validation_range"
    )
    validate(model, START + timedelta(hours=6), error=0.5)
    assert model.adaptive_reason(START + timedelta(hours=7), 16) == "validation_error"


def test_compact_restore_is_versioned_and_corruption_is_rejected():
    model = qualified()
    saved = model.adaptive_snapshot()
    restored = copy.deepcopy(model)
    restored.restore_adaptive(saved)
    assert restored.adaptive_reason(START + timedelta(hours=5), 16) == "qualified"
    for bad in (
        None,
        {"version": 0},
        {**saved, "k": math.nan},
        {**saved, "validations": [[0, math.nan, 16]]},
    ):
        restored.restore_adaptive(bad)
        assert restored.adaptive_reason(START + timedelta(hours=5), 16) != "qualified"


def replay_model(monkeypatch, *, enabled=True, model=None):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = enabled
    sensor._thermal_model = model or qualified()
    sensor._adaptive_inputs_fresh = True
    states["sensor.price"].attributes["today"] = [10.0] * 96
    initial = ControlReplayState.from_sensor(sensor)
    inputs = _input(sensor, START + timedelta(hours=5), indoor=19.9)
    inputs = replace(
        inputs, temperature_report_time=inputs.now, categories=("expensive",) * 96
    )
    return sensor, initial, inputs


def test_real_replay_off_unqualified_adaptive_and_fallback(monkeypatch):
    _, initial, inputs = replay_model(monkeypatch)
    base_state, base = replay_control_step(
        initial, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    _, unqualified = replay_control_step(
        replace(initial, adaptive_model=ThermalModel()), inputs
    )
    assert unqualified.fake_temp == base.fake_temp
    assert unqualified.attributes["brake_factor"] == base.attributes["brake_factor"]
    state, adapted = replay_control_step(initial, inputs)
    assert adapted.fake_temp < base.fake_temp
    assert adapted.attributes["adaptive_braking"]["state"] == "adapting"
    for minute in range(1, 16):
        step = replace(
            inputs,
            now=inputs.now + timedelta(minutes=minute),
            temperature_report_time=inputs.now + timedelta(minutes=minute),
        )
        state, adapted = replay_control_step(state, step)
        base_state, base = replay_control_step(
            base_state,
            replace(step, cfg={**step.cfg, "adaptive_braking_enabled": False}),
        )
        assert 0.75 <= adapted.attributes["adaptive_braking"]["multiplier"] <= 1
        assert adapted.attributes["brake_factor"] <= base.attributes["brake_factor"]
    previous = adapted.attributes["adaptive_braking"]["multiplier"]
    stale = replace(step, now=step.now + timedelta(minutes=10))
    state, fallback = replay_control_step(state, stale)
    assert (
        previous
        < fallback.attributes["adaptive_braking"]["multiplier"]
        <= previous + 0.025001
    )
    assert (
        fallback.attributes["adaptive_braking"]["reason"]
        == "temperature_reports_unavailable"
    )
    # Repeat same timestamp cannot advance the correction or integrate time twice.
    _, repeated = replay_control_step(state, stale)
    assert repeated.fake_temp == pytest.approx(fallback.fake_temp)
    cold = replace(stale, now=stale.now + timedelta(minutes=1), indoor=18)
    _, release = replay_control_step(state, cold)
    assert release.attributes["brake_factor"] < fallback.attributes["brake_factor"]


def test_basic_sensor_controller_learns_validates_then_changes_real_decision(
    monkeypatch,
):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    # Four ordinary hourly expensive periods; off-periods permit fit/ramp-out.
    prices = [1.0] * 96
    for slot in (33, 41, 49, 57):
        prices[slot : slot + 4] = [10.0] * 4
    states["sensor.price"].attributes["today"] = prices
    for minute in range(0, 485):
        phase = (minute - 15) % 120
        states["sensor.indoor"].state = str(
            21.3 - 0.01 * min(phase, 60) if phase < 60 else 21.3
        )
        _cycle(sensor, monkeypatch, minute)
    now = START + timedelta(minutes=485)
    assert sensor._thermal_model.adaptive_reason(now, 16) == "qualified"
    states["sensor.indoor"].state = "19.9"
    # Same controller state, prices and PI; only the switch differs.
    from custom_components.pumpsteer import sensor as module

    monkeypatch.setattr(module.dt_util, "now", lambda: now)
    state = ControlReplayState.from_sensor(sensor)
    inputs = _input(sensor, now, indoor=19.9)
    inputs = replace(
        inputs, temperature_report_time=now, categories=("expensive",) * 96
    )
    _, adapted = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert adapted.fake_temp < base.fake_temp
    assert sensor._thermal_model.sample_count >= 20
    assert len(sensor._thermal_model.adaptive_snapshot()["validations"]) == 3


def test_no_repeated_ha_report_is_used_as_a_training_observation(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _cycle(sensor, monkeypatch, 15)
    model = sensor._thermal_model
    before = len(model._temp_history)
    from custom_components.pumpsteer import sensor as module

    monkeypatch.setattr(module.dt_util, "now", lambda: START + timedelta(minutes=16))
    asyncio.run(
        sensor._do_update()
    )  # same HA report, a polling tick is not a measurement
    assert len(model._temp_history) == before
    assert not sensor._adaptive_new_report


def test_source_change_discards_qualification_not_base_control(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _cycle(sensor, monkeypatch, 15)
    sensor._thermal_model = qualified()
    sensor._config_entry.options["brake_delta_c"] = 9
    attrs = _cycle(sensor, monkeypatch, 16)
    assert not sensor._thermal_model.is_valid
    assert attrs["mode"] != "safe"
    assert attrs["adaptive_braking"]["multiplier"] == 1


@pytest.mark.parametrize("branch", ["summer", "price_disabled", "disabled"])
def test_adapting_status_is_cleared_when_correction_is_not_used(monkeypatch, branch):
    _, state, data = replay_model(monkeypatch)
    state, adapted = replay_control_step(state, data)
    assert adapted.attributes["adaptive_braking"]["state"] == "adapting"
    if branch == "summer":
        data = replace(data, outdoor=20)
    elif branch == "price_disabled":
        data = replace(data, aggressiveness=0)
    else:
        data = replace(
            data, cfg={**data.cfg, "adaptive_braking_enabled": False}, outdoor=20
        )
    _, decision = replay_control_step(state, data)
    assert decision.attributes["adaptive_braking"]["state"] == "base"
    assert decision.attributes["adaptive_braking"]["multiplier"] == 1


def test_adaptive_time_uses_elapsed_utc_across_dst(monkeypatch):
    from zoneinfo import ZoneInfo

    _, state, data = replay_model(monkeypatch)
    # Evidence is qualified at its own UTC date, and transition timing is separate.
    for a, b in [
        ("2026-03-29T01:59:00+01:00", "2026-03-29T03:00:00+02:00"),
        ("2026-10-25T02:59:00+02:00", "2026-10-25T02:00:00+01:00"),
    ]:
        start = datetime.fromisoformat(a).astimezone(ZoneInfo("Europe/Stockholm"))
        end = datetime.fromisoformat(b).astimezone(ZoneInfo("Europe/Stockholm"))
        sensor, _ = _sensor()
        sensor._control_modifier_multiplier(start, "adaptive", 0.75, True, 10, 10)
        multiplier = sensor._control_modifier_multiplier(
            end, "adaptive", 0.75, True, 10, 10
        )
        assert multiplier == pytest.approx(0.95)


def test_quality_failure_safe_and_restart_return_through_existing_ramps(monkeypatch):
    _, state, data = replay_model(monkeypatch)
    for minute in range(16):
        step = replace(
            data,
            now=data.now + timedelta(minutes=minute),
            temperature_report_time=data.now + timedelta(minutes=minute),
        )
        state, decision = replay_control_step(state, step)
    assert decision.attributes["adaptive_braking"]["multiplier"] == 0.75
    bad = copy.deepcopy(state.adaptive_model)
    validate(bad, step.now + timedelta(minutes=1), error=0.5)
    later = replace(
        step,
        now=step.now + timedelta(minutes=40),
        temperature_report_time=step.now + timedelta(minutes=40),
    )
    state, decision = replay_control_step(replace(state, adaptive_model=bad), later)
    assert decision.attributes["adaptive_braking"]["reason"] == "validation_error"
    assert 0.75 < decision.attributes["adaptive_braking"]["multiplier"] <= 0.775
    safe_state, safe = replay_control_step(state, replace(later, indoor=None))
    assert safe.mode == "safe_mode"
    assert safe_state.control_modifier_state is None
    # Restart retains qualified model evidence, but never restores active correction.
    state, decision = replay_control_step(
        replace(state, control_modifier_state=None, adaptive_model=qualified()), later
    )
    assert decision.attributes["adaptive_braking"]["multiplier"] >= 0.975


def test_delayed_report_uses_one_observation_timeline(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _cycle(sensor, monkeypatch, 15)
    sensor._thermal_model = qualified()
    sensor._adaptive_report = START + timedelta(minutes=16)
    sensor._adaptive_inputs_fresh = True
    sensor._adaptive_new_report = True
    sensor._thermal_learning_active = False
    sensor._update_thermal_learning(
        active=True,
        reason="active",
        now=START + timedelta(minutes=20),
        indoor=21,
        outdoor=5,
    )
    assert sensor._thermal_model._session.start_time == sensor._adaptive_report
    sensor._adaptive_report += timedelta(minutes=1)
    sensor._update_thermal_learning(
        active=True,
        reason="active",
        now=START + timedelta(minutes=21),
        indoor=20.99,
        outdoor=5,
    )
    assert sensor._thermal_model.validation_session_active
    assert sensor._thermal_model._session.last_time == sensor._adaptive_report
