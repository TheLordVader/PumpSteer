"""Passive Pilot uses dated heat reports, real endpoints and the common audit."""

import math
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_pilot_response_evidence import report, time
from test_safe_mode_output import setup_sensor  # noqa: F401
from test_temperature_input_safety import control  # noqa: F401

from custom_components.pumpsteer.heat_operation import HeatOperation
from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit
from custom_components.pumpsteer.pilot_passive import PilotPassive


def _context():
    return [
        {"kind": "context", "at": time(0), "regime": "normal_pi", "gos_active": False}
    ]


@pytest.mark.parametrize(
    ("semantics", "values", "expected"),
    [
        ("space_heat_duty", (0, 1, 0), 0.5),
        ("space_heat_intensity", (0.2, 0.8, 0.2), 0.5),
    ],
)
def test_house_uses_real_endpoints_and_asynchronous_heat_without_receiver(
    semantics, values, expected
):
    heat = [
        report("heat", second, value, semantics=semantics)
        for second, value in zip((0, 900, 2700), values)
    ]
    temperatures = [report("outdoor", 0, 5.0), report("outdoor", 1650, 6.0)]
    inputs = sorted(heat + temperatures, key=lambda event: event["at"])
    rows = PilotPassive.house_interval(
        _context(), inputs, start=(time(0), 20.0), end=(time(3600), 20.3)
    )
    assert rows.end == time(3600).timestamp()
    assert rows.heat == pytest.approx(expected)
    assert rows.outdoor == pytest.approx((1650 * 5 + 1950 * 6) / 3600)
    assert rows.start_temp == 20 and rows.end_temp == 20.3
    assert rows.request is None


def test_house_rejects_expired_heat_and_interrupted_context():
    inputs = [
        report("outdoor", 0, 5.0),
        report("heat", 0, 1, valid_until=time(300)),
        report("heat", 301, 1),
    ]
    span = {"start": (time(0), 20.0), "end": (time(3600), 20.25)}
    assert PilotPassive.house_interval(_context(), inputs, **span) is None
    cut = _context() + [{"kind": "interruption", "at": time(1500), "reason": "safe"}]
    inputs = [report("outdoor", 0, 5.0), report("heat", 0, 1)]
    assert PilotPassive.house_interval(cut, inputs, **span) is None


def _state(value, at, *, unit=None, changed=None):
    return SimpleNamespace(
        state=str(value),
        last_reported=at,
        last_changed=changed or at,
        attributes={"unit_of_measurement": unit},
    )


def test_verified_space_heat_does_not_need_fresh_supporting_bits():
    at = time(3600)
    verified = _state("on", at)
    assert HeatOperation.learning_report(at, verified) is None
    stale = _state("off", at - timedelta(minutes=30))
    assert HeatOperation.learning_report(
        at, verified, compressor=stale, radiator=stale, allow_report_only=True
    ) == (at, 1, None)
    assert (
        HeatOperation.learning_report(
            at, verified, compressor=_state("off", at), allow_report_only=True
        )
        is None
    )
    assert (
        HeatOperation.learning_report(
            at, verified, water=_state("on", at), allow_report_only=True
        )
        is None
    )
    assert HeatOperation.learning_report(at, None, compressor=_state("on", at)) is None
    assert HeatOperation.learning_report(
        at, _state("off", at), allow_report_only=True
    ) == (at, 0, None)


@pytest.mark.parametrize("source_time", [time(0), time(1201), "not-a-date", None])
def test_fresh_ha_write_cannot_qualify_invalid_source_time(source_time):
    at = time(1200)
    heat = _state("on", at)
    heat.attributes["measurement_time"] = source_time
    assert HeatOperation.learning_report(at, heat, allow_report_only=True) is None


def test_republication_cannot_keep_a_pilot_heat_measurement_alive():
    from custom_components.pumpsteer.thermal_intelligence.runtime import (
        ThermalIntelligence,
    )

    runtime = ThermalIntelligence(pilot_only=True)
    _start_cycle(runtime, time(10), 20)
    _inputs(runtime, 10)
    runtime.record_pilot_input(
        time(11),
        "heat",
        "binary_sensor.space_heat",
        (time(11), 1, time(1)),
    )
    runtime.record_pilot_input(
        time(250),
        "heat",
        "binary_sensor.space_heat",
        (time(250), 1, time(1)),
    )
    assert runtime.pilot_inputs_valid(time(300))
    assert not runtime.pilot_inputs_valid(time(301))
    assert (
        runtime.export_state()["events"][-1]["source_observed_at"]
        == time(1).isoformat()
    )
    runtime.record_pilot_input(
        time(310), "heat", "binary_sensor.space_heat", (time(310), 1, None)
    )
    assert not runtime.pilot_inputs_valid(time(310))
    restored = ThermalIntelligence(pilot_only=True)
    assert restored.restore_state(runtime.export_state())
    _start_cycle(restored, time(320), 20.1)
    _inputs(restored, 320)
    assert not restored.pilot_inputs_valid(time(320))
    restored.record_pilot_input(
        time(321), "heat", "binary_sensor.space_heat", (time(321), 1, time(320))
    )
    assert restored.pilot_inputs_valid(time(321))


def test_intensity_requires_a_dated_percentage_of_space_heating():
    at = time(1200)
    load = _state("42", at, unit="%")
    assert HeatOperation.learning_report(at, load, intensity=True) is None
    load.attributes["measurement_time"] = time(1198).timestamp()
    report = HeatOperation.learning_report(at, load, intensity=True)
    assert report.reported_at == at
    assert report.source_observed_at == time(1198)
    assert report.value == pytest.approx(0.42)
    assert (
        HeatOperation.learning_report(at, load, intensity=True, water=_state("on", at))
        is None
    )
    load.attributes["unit_of_measurement"] = "W"
    assert HeatOperation.learning_report(at, load, intensity=True) is None


def test_dated_heat_report_keeps_both_clocks_and_rejects_a_pre_restart_sample():
    at, observed = time(1200), time(1198)
    heat = _state("off", at)
    heat.attributes["observed"] = observed.isoformat()
    report = HeatOperation.learning_report(at, heat, timestamp_attribute="observed")
    assert report == (at, 0, observed)
    assert (
        HeatOperation.learning_report(
            at, heat, timestamp_attribute="observed", reported_after=time(1199)
        )
        is None
    )


def test_observed_source_clock_remains_required_after_an_invalid_first_report():
    from custom_components.pumpsteer.thermal_intelligence.runtime import (
        ThermalIntelligence,
    )

    runtime = ThermalIntelligence(pilot_only=True)
    _start_cycle(runtime, time(10), 20)
    runtime.record_pilot_input(
        time(10),
        "heat",
        "binary_sensor.space_heat",
        None,
        source_time_required=True,
    )
    # For example, an initially stale source omits its attribute after reconnect.
    _inputs(runtime, 15)
    assert not runtime.pilot_inputs_valid(time(15))
    restored = ThermalIntelligence(pilot_only=True)
    assert restored.restore_state(runtime.export_state())
    _start_cycle(restored, time(20), 20.1)
    _inputs(restored, 20)
    assert not restored.pilot_inputs_valid(time(20))


def test_shared_audit_freezes_pilot_and_restores_completed_pairs():
    audit = ForecastAudit()
    thermal = {"loss": 0.1, "gain": 1.0, "background": 0.1, "heat": 0.6}
    identity = ("inside", "outside")
    arguments = {
        "revision": "pilot:r1",
        "context_id": "1",
        "command": None,
        "mode": "normal",
    }
    assert audit.issue_shared(
        time(0), 20, 5, None, identity, pilot=thermal, **arguments
    )
    thermal["heat"] = 0.0
    at = time(1860)
    assert audit.observe_shared(
        at,
        _state(19.6, at, unit="°C"),
        identity,
        context_id="1",
        command=None,
        mode="normal",
    )
    row = audit.fixed_records[-1]
    assert row["model_order"] == ["pilot_house", "persistence"]
    assert row["curves"][0]["heat"] == 0.6
    assert row["elapsed_minutes"] == pytest.approx(31)
    pilot, persistence = row["predictions"]
    assert pilot == pytest.approx(12 + 8 * math.exp(-0.1 * 31 / 60))
    assert persistence == 20.0
    restored = ForecastAudit()
    restored.restore_plan(audit.export_plan(), at)
    assert restored.fixed_records == audit.fixed_records
    assert restored.fixed_pending is None


def _start_cycle(
    runtime, at, temperature, outdoor=5, *, heat_source="binary_sensor.space_heat"
):
    from test_thermal_intelligence_runtime import _cycle

    cycle = _cycle(
        at,
        [("sensor.room", 1, 0 if temperature is None else temperature, 0)],
        mode="normal",
        outdoor=outdoor,
    )
    if temperature is None:
        cycle["indoor"] = None
        cycle["sources"][0]["value"] = None
    cycle.update(gos_active=False, pilot_heat_source=heat_source)
    runtime.begin_cycle(cycle, use_ohmigo_input=False)


def _inputs(
    runtime, seconds, *, outdoor=5, heat=1, heat_source="binary_sensor.space_heat"
):
    at = time(seconds)
    runtime.record_pilot_input(at, "outdoor", "sensor.outdoor", (at, outdoor, None))
    observed_at = (
        at - timedelta(seconds=1) if heat_source.startswith("sensor.") else None
    )
    runtime.record_pilot_input(at, "heat", heat_source, (at, heat, observed_at))


def test_runtime_collects_without_serving_new_rc_and_restores_closed_house_intervals():
    from custom_components.pumpsteer.thermal_intelligence.runtime import (
        ThermalIntelligence,
    )

    runtime = ThermalIntelligence(pilot_only=True)
    _start_cycle(runtime, time(0), 20)
    _inputs(runtime, 0)
    assert runtime.collect_pilot_house(time(0)) is None
    for seconds in range(240, 3600, 240):
        _inputs(runtime, seconds, heat=int(seconds < 1800))
    _start_cycle(runtime, time(3600), 20.2)
    _inputs(runtime, 3600, heat=0)
    interval = runtime.collect_pilot_house(time(3600))
    assert interval.hours == 1
    assert interval.heat == pytest.approx(1920 / 3600)
    assert runtime.summary()["model"] == "heat_pump_pilot_house"
    assert runtime.model._pending == []
    assert runtime.prepare_fit_job(time(3600)) is None
    restored = ThermalIntelligence(pilot_only=True)
    assert restored.restore_state(runtime.export_state())
    assert restored.summary()["pilot_house_intervals"] == 1
    assert restored.collect_pilot_house(time(3700)) is None
    assert restored.pilot_forecast(time(3700)) is None
    _start_cycle(restored, time(3710), None)
    _start_cycle(restored, time(3800), 20.21)
    assert restored.summary()["pilot_house_intervals"] == 1
    # Legacy readers can still restore the common history on rollback.
    assert ThermalIntelligence().restore_state(runtime.export_state())
    legacy = runtime.export_state()
    legacy["pilot_house"].pop("source_time_version")
    assert restored.restore_state(legacy)
    assert restored.summary()["pilot_house_intervals"] == 0
    assert restored.summary()["evidence_events"] >= len(legacy["events"])


def test_continuously_on_heat_does_not_qualify_a_model():
    from custom_components.pumpsteer.pilot_vendor.learning_interval import (
        LearningInterval,
    )
    from custom_components.pumpsteer.thermal_intelligence.runtime import (
        ThermalIntelligence,
    )

    rows = tuple(
        LearningInterval(
            time(hour * 3600).timestamp(), 1, 20, 20.1, 3 + (hour % 3) * 2, 1, None, 1
        )
        for hour in range(1, 26)
    )
    result = ThermalIntelligence.static_fit({"pilot_house": rows})
    assert result["status"] == "gain_frozen"
    assert "candidate" not in result


@pytest.mark.parametrize(
    ("heat_source", "heat_levels"),
    [
        pytest.param("binary_sensor.space_heat", (0, 1), id="on-off"),
        pytest.param("sensor.space_heat_percent", (0.2, 0.8), id="inverter"),
    ],
)
def test_runtime_fits_pilot_and_scores_a_later_observation_in_the_controller_audit(
    request, heat_source, heat_levels
):
    from custom_components.pumpsteer.thermal_intelligence.runtime import (
        ThermalIntelligence,
    )

    runtime = ThermalIntelligence(pilot_only=True)
    sensor, _, _ = request.getfixturevalue("control")
    sensor._thermal_intelligence = runtime
    indoor = 20.0
    for hour in range(26):
        end = time(hour * 3600)
        weather = (3, 10, 6)[hour % 3]
        heat = heat_levels[hour % 2]
        _start_cycle(runtime, end, indoor, weather, heat_source=heat_source)
        _inputs(
            runtime, hour * 3600, outdoor=weather, heat=heat, heat_source=heat_source
        )
        runtime.collect_pilot_house(end)
        if hour == 24:
            sensor._update_shared_thermal_audit(end, sensor._cfg())
            assert sensor._plan_audit.fixed_pending["revision"] == "persistence"
        if hour == 25:
            break
        for seconds in range(hour * 3600 + 240, (hour + 1) * 3600, 240):
            _inputs(
                runtime, seconds, outdoor=weather, heat=heat, heat_source=heat_source
            )
        indoor += 0.07 * (weather - indoor) + 1.4 * heat + 0.25
    job = runtime.prepare_fit_job(end)
    assert job is not None
    token, snapshot = job
    assert runtime.apply_fit_result(token, runtime.static_fit(snapshot))
    assert runtime.prepare_fit_job(end) is None
    assert runtime.summary()["pilot_house_intervals"] == 25
    forecast = runtime.pilot_forecast(end)
    assert forecast["heat"] == pytest.approx(heat_levels[0])  # Last completed hour.
    assert forecast["loss"] > 0 and forecast["gain"] > 0
    assert runtime.summary()["fit_trusted"] is False
    assert runtime.summary()["active_control"] is False

    sensor._update_shared_thermal_audit(end, sensor._cfg())
    issued = sensor._plan_audit.fixed_pending
    assert issued["revision"].startswith(f"pilot:{end.timestamp()}:")
    assert issued["curves"][0]["heat"] == pytest.approx(heat_levels[0])

    for seconds in range(25 * 3600 + 240, 25 * 3600 + 1800, 240):
        _inputs(
            runtime, seconds, outdoor=weather, heat=heat_levels[1], heat_source=heat_source
        )
    observed = end + timedelta(minutes=30)
    _start_cycle(runtime, observed, indoor - 0.15, weather, heat_source=heat_source)
    _inputs(
        runtime,
        25 * 3600 + 1800,
        outdoor=weather,
        heat=heat_levels[1],
        heat_source=heat_source,
    )
    assert runtime.collect_pilot_house(observed) is None
    sensor._update_shared_thermal_audit(observed, sensor._cfg())
    result = sensor._plan_audit.fixed_records[-1]
    assert result["curves"][0]["heat"] == pytest.approx(heat_levels[0])
    assert result["actual"] == pytest.approx(indoor - 0.15)
    assert result["observed"] == observed.isoformat()
    assert result["model_order"] == ["pilot_house", "persistence"]
    comparison = sensor._plan_audit.shared_summary()[issued["revision"]]["30"]
    assert comparison["count"] == 1
    assert comparison["persistence_mae"] == pytest.approx(0.15)
    assert comparison["assumption"] == "constant_outdoor_and_latest_observed_house_heat_share"

    lost_at = observed + timedelta(minutes=1)
    runtime.record_pilot_input(lost_at, "heat", heat_source, None)
    assert runtime.pilot_forecast(lost_at) is None
    sensor._update_shared_thermal_audit(lost_at, sensor._cfg())
    assert not sensor._plan_audit.fixed_pending["revision"].startswith("pilot:")

    # A boundary invalidates forecasts, while an in-flight fit on intact
    # completed evidence can still finish. A new heat source invalidates it.
    runtime._pilot_attempted_end = None
    token, snapshot = runtime.prepare_fit_job(lost_at)
    runtime.suspend("safe_mode", lost_at)
    assert runtime.apply_fit_result(token, runtime.static_fit(snapshot))
    assert runtime.pilot_forecast(lost_at) is None
    # A heartbeat during SAFE cannot seed the next normal operating interval.
    _inputs(runtime, 25 * 3600 + 1860, heat=heat_levels[1], heat_source=heat_source)
    resumed = lost_at + timedelta(seconds=10)
    _start_cycle(runtime, resumed, indoor - 0.2, weather, heat_source=heat_source)
    assert not runtime.pilot_inputs_valid(resumed)
    runtime._pilot_attempted_end = None
    token, snapshot = runtime.prepare_fit_job(resumed)
    from test_thermal_intelligence_runtime import _cycle

    new_source = _cycle(resumed, [("sensor.room", 1, indoor, 0)], mode="normal")
    new_source.update(gos_active=False, pilot_heat_source="binary_sensor.replacement")
    runtime.begin_cycle(new_source, use_ohmigo_input=False)
    assert not runtime.apply_fit_result(token, runtime.static_fit(snapshot))


@pytest.mark.parametrize(
    "external_heat,intensity", [(False, False), (True, False), (False, True)]
)
def test_controller_collects_heat_changes_between_ticks_with_one_sensor(
    request, monkeypatch, external_heat, intensity
):
    import asyncio
    from importlib import import_module

    ha_event = import_module("homeassistant.helpers.event")

    sensor, states, clock = request.getfixturevalue("control")
    heat_source = (
        "sensor.space_heat_percent" if intensity else "binary_sensor.space_heat"
    )
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        thermal_heat_activity_entity=heat_source,
        thermal_detection_method="verified",
        adaptive_braking_enabled=False,
        modbus_service="",
    )
    if external_heat:
        sensor._config_entry.options["thermal_external_heat_entity"] = (
            "binary_sensor.stove"
        )
    callbacks = {}

    def subscribe(hass, entities, handler):
        for entity in entities:
            callbacks[entity] = handler
        return lambda: None

    monkeypatch.setattr(ha_event, "async_track_state_change_event", subscribe)
    began = clock[0]
    for entity in ("sensor.indoor", "sensor.outdoor"):
        states[entity].last_reported = began
        states[entity].attributes = {"unit_of_measurement": "°C"}

    def heat_state(active, at):
        if intensity:
            state = _state("50" if active else "0", at, unit="%")
            state.attributes["measurement_time"] = (
                at - timedelta(seconds=1)
            ).isoformat()
            return state
        return _state("on" if active else "off", at)

    states[heat_source] = heat_state(False, began)
    asyncio.run(sensor._do_update())
    assert heat_source in callbacks
    outputs = len(sensor.hass.services.calls)
    for minute in sorted(set(range(4, 60, 4)) | {10, 40}):
        clock[0] = began + timedelta(minutes=minute)
        if external_heat and minute == 20:
            stove = "binary_sensor.stove"
            states[stove] = _state("on", clock[0])
            states[stove].attributes["measurement_time"] = clock[0].isoformat()
            callbacks[stove](
                SimpleNamespace(
                    time_fired=clock[0],
                    data={
                        "entity_id": stove,
                        "new_state": states[stove],
                    },
                )
            )
        states[heat_source] = heat_state(10 <= minute < 40, clock[0])
        callbacks[heat_source](
            SimpleNamespace(
                time_fired=clock[0],
                data={"entity_id": heat_source, "new_state": states[heat_source]},
            )
        )
    assert len(sensor.hass.services.calls) == outputs
    clock[0] = began + timedelta(hours=1)
    states["sensor.indoor"].state = "20.9"
    for entity in ("sensor.indoor", "sensor.outdoor"):
        states[entity].last_reported = clock[0]
    states[heat_source] = heat_state(False, clock[0])
    asyncio.run(sensor._do_update())
    history = sensor._thermal_intelligence.export_state()["pilot_house"]["intervals"]
    if external_heat:
        assert not history
    else:
        expected_heat = 0.25 if intensity else 0.5
        assert len(history) == 1 and history[0]["heat"] == pytest.approx(expected_heat)
    assert sensor._thermal_intelligence.model.parameters() is None
    assert sensor._attributes["adaptive_braking"]["multiplier"] == 1


def test_pilot_retracts_completed_interval_when_a_dated_heat_change_arrives_late():
    from custom_components.pumpsteer.thermal_intelligence.runtime import (
        ThermalIntelligence,
    )

    runtime = ThermalIntelligence(pilot_only=True)
    _start_cycle(runtime, time(0), 20)
    _inputs(runtime, 0)
    runtime.collect_pilot_house(time(0))
    for seconds in range(240, 3600, 240):
        _inputs(runtime, seconds)
    _start_cycle(runtime, time(3600), 20.2)
    assert runtime.collect_pilot_house(time(3600)) is not None
    previous = runtime.pilot_context_id
    runtime.record_pilot_input(
        time(3603), "heat", "binary_sensor.space_heat", (time(3570), 0, None)
    )
    assert runtime.summary()["pilot_house_intervals"] == 0
    assert runtime.pilot_context_id != previous
    # The original reported timestamp remains available for inspection.
    assert runtime.export_state()["events"][-1]["reported_at"] == time(3570).isoformat()
