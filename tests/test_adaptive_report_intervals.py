"""Real controller observations: sparse/quantized reports are not polling samples."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer import sensor as module
from test_brake_provenance import _sensor
from test_adaptive_braking import START
from test_control_replay_parity import _input
from custom_components.pumpsteer.control_replay import (
    ControlReplayState,
    replay_control_step,
)


def exercise(
    monkeypatch,
    interval,
    *,
    jitter=False,
    weighted=False,
    periods=None,
    external_possible=False,
    temperature_profile=None,
    episodes=4,
    event_driven=False,
):
    sensor, states = _sensor()
    cfg = sensor._config_entry.options
    cfg.update(adaptive_braking_enabled=True, external_heat_sources=external_possible)
    if weighted:
        for n in (2, 3):
            cfg[f"indoor_temp_entity_{n}"] = f"sensor.indoor{n}"
            states[f"sensor.indoor{n}"] = SimpleNamespace(
                state="23", attributes={}, last_reported=START
            )
    now = [START]
    monkeypatch.setattr(module.dt_util, "now", lambda: now[0])

    async def prices(_cfg, at):
        # Explicit isolated tariff provider; ordinary controller/ramps/PI remain real.
        minute = int((at - START).total_seconds() / 60)
        expensive = minute % 720 < 360
        return (
            [10 if expensive else 1] * 96,
            ["expensive" if expensive else "normal"] * 96,
            15,
            at.hour * 4 + at.minute // 15,
        )

    monkeypatch.setattr(sensor, "_get_prices", prices)
    sources = ["sensor.indoor"] + (
        ["sensor.indoor2", "sensor.indoor3"] if weighted else []
    )
    source_intervals = dict(zip(sources, periods or [interval] * len(sources)))
    due = {name: i * source_intervals[name] / 3 for i, name in enumerate(sources)}
    count = {name: int(hasattr(states[name], "last_reported")) for name in sources}
    accepted = []
    last = None
    for minute in range(episodes * 720 + 1):
        now[0] = START + timedelta(minutes=minute)
        phase = minute % 720
        actual = 23 - 0.008 * min(phase, 360) if phase < 360 else 23
        if temperature_profile is not None:
            actual = temperature_profile(minute, actual)
        for name in sources:
            if minute >= due[name]:
                value = str(round(actual, 1))
                if not event_driven or value != states[name].state:
                    states[name].state = value
                    states[name].last_reported = now[0]
                    count[name] += 1
                due[name] += source_intervals[name] + (
                    (-1 if count[name] % 2 else 1) if jitter else 0
                )
        if minute % interval == 0:
            states["sensor.outdoor"].last_reported = now[0]
        asyncio.run(sensor._do_update())
        if sensor._adaptive_new_report:
            if last is None or sensor._adaptive_report != last:
                accepted.append(sensor._adaptive_report)
                last = sensor._adaptive_report
    return sensor, states, accepted, count


def cold_step(sensor, states, monkeypatch, minute):
    before = ControlReplayState.from_sensor(sensor)
    at = START + timedelta(minutes=minute)
    for key in ("indoor_temp_entity", "indoor_temp_entity_2", "indoor_temp_entity_3"):
        entity = sensor._cfg().get(key)
        if entity:
            states[entity].state = "19.9"
            states[entity].last_reported = at
    from test_brake_provenance import _cycle

    _cycle(sensor, monkeypatch, minute)
    coverage = min(
        sensor._adaptive_component_reports.values(), key=lambda value: value.timestamp()
    )
    inputs = replace(_input(sensor, at, indoor=19.9), temperature_report_time=coverage)
    return before, inputs, sensor.state


@pytest.mark.parametrize("interval", [1, 5, 10, 15])
def test_quantized_reports_can_qualify_without_extra_telemetry(monkeypatch, interval):
    sensor, states, accepted, count = exercise(monkeypatch, interval)
    assert (
        sensor._thermal_model.adaptive_reason(START + timedelta(minutes=4 * 720), 16)
        == "qualified"
    )
    assert len(accepted) <= count["sensor.indoor"]
    assert sensor._thermal_model.fit_rmse <= 0.3
    # A real new changed value, not an unchanged report promoted to fresh data.
    state, inputs, actual = cold_step(sensor, states, monkeypatch, 4 * 720 + 1)
    _, adapted = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert round(adapted.fake_temp, 1) == actual
    assert adapted.fake_temp < base.fake_temp
    assert adapted.attributes["adaptive_braking"]["multiplier"] >= 0.75
    # Continue the actual ramp: the correction must reach the 0.1 C output,
    # not merely change an unrounded replay intermediate.
    state, inputs, actual = cold_step(sensor, states, monkeypatch, 4 * 720 + 6)
    _, adapted = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert actual == round(adapted.fake_temp, 1) < round(base.fake_temp, 1)


@pytest.mark.parametrize("interval", [5, 10, 15])
def test_jitter_and_three_asynchronous_weighted_sensors(monkeypatch, interval):
    sensor, states, accepted, count = exercise(
        monkeypatch, interval, jitter=True, weighted=True
    )
    assert (
        sensor._thermal_model.adaptive_reason(START + timedelta(minutes=4 * 720), 16)
        == "qualified"
    )
    # Each accepted aggregate requires fresh evidence from all contributors.
    assert len(accepted) <= min(count.values())
    state, inputs, actual = cold_step(sensor, states, monkeypatch, 4 * 720 + 1)
    _, adaptive = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert adaptive.fake_temp < base.fake_temp
    assert adaptive.attributes["adaptive_braking"]["state"] == "adapting"


@pytest.mark.parametrize("jitter", [False, True])
def test_thirty_minute_reports_can_validate_real_thirty_minute_outcomes(
    monkeypatch, jitter
):
    sensor, states, accepted, count = exercise(
        monkeypatch, 30, episodes=6, jitter=jitter
    )
    now = START + timedelta(minutes=6 * 720)
    assert sensor._thermal_model.is_valid
    assert sensor._thermal_model.adaptive_reason(now, 16) == "qualified"
    assert all(
        row[3] <= 45 for row in sensor._thermal_model.adaptive_snapshot()["validations"]
    )


def test_irregular_validation_uses_frozen_trajectory_and_real_endpoint():
    from custom_components.pumpsteer.thermal_model import ThermalModel

    model = ThermalModel()
    model.restore_k(0.04)
    model.start_validation_session(START, 21, 5, adaptive_observations=True)
    for minute in (10, 20, 35):
        # Current outdoor changes after issue; cannot rewrite the frozen forecast.
        indoor = 21 - 0.04 * (21 - 5) * minute / 60
        assert model.update_validation_session(
            START + timedelta(minutes=minute), indoor, -5
        )
    row = model.adaptive_snapshot()["validations"][-1]
    assert row[0] == (START + timedelta(minutes=35)).timestamp()
    assert row[1] == pytest.approx(0, abs=1e-12)
    assert model._session.last_indoor == indoor
    assert (
        model._session.reports == 4
    )  # start and three actual reports, no synthetic30m


def test_genuinely_mixed_five_ten_fifteen_minute_sources_learn_and_adapt(monkeypatch):
    sensor, states, accepted, count = exercise(
        monkeypatch, 15, jitter=True, weighted=True, periods=(5, 10, 15)
    )
    now = START + timedelta(minutes=2881)
    assert count["sensor.indoor"] > count["sensor.indoor2"] > count["sensor.indoor3"]
    assert len(accepted) <= min(count.values())
    assert sensor._thermal_model.adaptive_reason(now, 16) == "qualified"
    assert sensor._thermal_model.sample_count >= 20
    state, inputs, actual = cold_step(sensor, states, monkeypatch, 4 * 720 + 1)
    _, adapted = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert adapted.fake_temp < base.fake_temp
    assert adapted.attributes["adaptive_braking"]["state"] == "adapting"
