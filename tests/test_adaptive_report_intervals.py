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


def exercise(monkeypatch, interval, *, jitter=False, weighted=False):
    sensor, states = _sensor()
    cfg = sensor._config_entry.options
    cfg.update(adaptive_braking_enabled=True)
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
    due = {name: i * interval / 3 for i, name in enumerate(sources)}
    count = {name: int(hasattr(states[name], "last_reported")) for name in sources}
    accepted = []
    last = None
    for minute in range(4 * 720 + 1):
        now[0] = START + timedelta(minutes=minute)
        phase = minute % 720
        actual = 23 - 0.005 * min(phase, 360) if phase < 360 else 23
        for name in sources:
            if minute >= due[name]:
                states[name].state = str(round(actual, 1))  # ordinary 0.1 C room sensor
                states[name].last_reported = now[0]
                count[name] += 1
                due[name] += interval + (
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


@pytest.mark.parametrize("interval", [1, 5, 10, 15])
def test_quantized_reports_can_qualify_without_extra_telemetry(monkeypatch, interval):
    sensor, states, accepted, count = exercise(monkeypatch, interval)
    assert (
        sensor._thermal_model.adaptive_reason(START + timedelta(minutes=4 * 720), 16)
        == "qualified"
    )
    assert len(accepted) <= count["sensor.indoor"]
    assert sensor._thermal_model.fit_rmse <= 0.3
    # This model came from the real sparse-report controller chain, not injected k.
    now = START + timedelta(minutes=4 * 720)
    adaptive = ControlReplayState.from_sensor(sensor)
    base = adaptive
    report = sensor._adaptive_report
    for minute in range(16):
        at = now + timedelta(minutes=minute)
        if minute % interval == 0:
            report = at
        inputs = replace(
            _input(sensor, at, indoor=19.9), temperature_report_time=report
        )
        adaptive, changed = replay_control_step(adaptive, inputs)
        base, unchanged = replay_control_step(
            base, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
        )
    assert changed.attributes["adaptive_braking"]["state"] == "adapting"
    assert changed.fake_temp < unchanged.fake_temp
    assert changed.attributes["adaptive_braking"]["multiplier"] >= 0.75


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
    now = START + timedelta(minutes=4 * 720 + 1)
    inputs = replace(
        _input(sensor, now, indoor=19.9),
        temperature_report_time=sensor._adaptive_report,
    )
    state = ControlReplayState.from_sensor(sensor)
    _, adaptive = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert adaptive.fake_temp < base.fake_temp
    assert adaptive.attributes["adaptive_braking"]["state"] == "adapting"


def test_thirty_minute_series_remains_explicitly_underresolved(monkeypatch):
    sensor, states, accepted, count = exercise(monkeypatch, 30)
    assert sensor._adaptive_braking["reason"] == "report_interval_too_sparse"
    assert sensor._adaptive_braking["multiplier"] == 1
    assert (
        sensor._thermal_model.adaptive_reason(START + timedelta(minutes=4 * 720), 16)
        != "qualified"
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
