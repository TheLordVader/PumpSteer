"""Numerical heat-pump faults require explicit verified value semantics."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.heatpump_alarm import HeatPumpAlarm
from test_temperature_input_safety import control, _report  # noqa: F401
from test_safe_mode_output import setup_sensor  # noqa: F401

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def sample(value, *, age=0, attrs=None):
    return SimpleNamespace(
        state=value, attributes=attrs or {}, last_reported=NOW - timedelta(minutes=age)
    )


def test_verified_numeric_alarm_deduplication_and_recovery():
    monitor = HeatPumpAlarm(
        "sensor.heat_fault", numeric_verified=True, numeric_normal=0
    )
    assert monitor.observe(NOW, sample("0.0"))[0]["status"] == "normal"
    assert monitor.observe(NOW, sample("3.0"))[1] == "alarm"
    monitor.mark_sent("alarm")
    assert monitor.observe(NOW, sample("3"))[1] is None
    assert monitor.observe(NOW, sample("0"))[1] == "recovery"
    monitor.mark_sent("recovery")
    assert monitor.observe(NOW, sample("0"))[1] is None


@pytest.mark.parametrize(
    "value", ["unknown", "unavailable", "nan", "inf", "-inf", "invalid", "", True]
)
def test_invalid_numeric_value_cannot_create_false_recovery(value):
    monitor = HeatPumpAlarm(
        "sensor.heat_fault", numeric_verified=True, numeric_normal=0
    )
    monitor.observe(NOW, sample("2"))
    monitor.mark_sent("alarm")
    status, event = monitor.observe(NOW, sample(value))
    assert status["status"] == "unknown" and status["active"] and event is None


@pytest.mark.parametrize(
    "verified,normal", [(False, 0), (True, None), (True, float("nan")), (True, True)]
)
def test_numeric_semantics_must_be_explicit_and_finite(verified, normal):
    monitor = HeatPumpAlarm(
        "sensor.heat_fault", numeric_verified=verified, numeric_normal=normal
    )
    for value in ("0", "1"):
        status, event = monitor.observe(NOW, sample(value))
        assert status["status"] == "unknown" and event is None


def test_numeric_restart_requires_new_recovery_and_keeps_deduplication():
    original = HeatPumpAlarm(
        "sensor.heat_fault", numeric_verified=True, numeric_normal=0
    )
    original.observe(NOW, sample("1"))
    original.mark_sent("alarm")
    saved, _ = original.observe(NOW, sample("1"))
    restored = HeatPumpAlarm(
        "sensor.heat_fault",
        saved,
        started_at=NOW,
        numeric_verified=True,
        numeric_normal=0,
    )
    assert restored.observe(NOW + timedelta(seconds=1), sample("1"))[1] is None
    old_normal = sample("0")
    assert restored.observe(NOW + timedelta(seconds=1), old_normal)[1] is None
    old_normal.last_reported = NOW + timedelta(seconds=2)
    assert restored.observe(old_normal.last_reported, old_normal)[1] == "recovery"


def test_changed_numeric_semantics_do_not_restore_old_alarm():
    original = HeatPumpAlarm(
        "sensor.heat_fault", numeric_verified=True, numeric_normal=0
    )
    original.observe(NOW, sample("1"))
    original.mark_sent("alarm")
    saved, _ = original.observe(NOW, sample("1"))
    changed = HeatPumpAlarm(
        "sensor.heat_fault", saved, numeric_verified=True, numeric_normal=1
    )
    status, event = changed.observe(NOW, sample("1"))
    assert status["status"] == "normal" and not status["active"] and event is None


@pytest.mark.parametrize("condition", ["stale", "restored", "future"])
def test_unqualified_numeric_report_cannot_create_recovery(condition):
    monitor = HeatPumpAlarm(
        "sensor.heat_fault", numeric_verified=True, numeric_normal=0
    )
    monitor.observe(NOW, sample("1"))
    monitor.mark_sent("alarm")
    report = sample("0", age=61 if condition == "stale" else 0)
    if condition == "restored":
        report.attributes["restored"] = True
    elif condition == "future":
        report.last_reported = NOW + timedelta(minutes=1)
    status, event = monitor.observe(NOW, report)
    assert status["status"] == "unknown" and status["active"] and event is None


def test_numeric_controller_fault_and_recovery_do_not_change_regulation(control):
    sensor, states, clock = control
    sensor._config_entry.options.update(
        thermal_alarm_entity="sensor.heat_fault",
        thermal_alarm_numeric_verified=True,
        thermal_alarm_numeric_normal=0,
        notify_service="notify.family",
    )
    states["sensor.heat_fault"] = SimpleNamespace(
        state="2", attributes={}, last_reported=clock[0]
    )

    async def scenario():
        _report(states, clock)
        for _ in range(2):
            await sensor.async_update()
            await sensor._heatpump_alarm_task
        assert sensor.extra_state_attributes["mode"] == "normal"
        assert len([c for c in sensor.hass.services.calls if c[0] == "notify"]) == 1
        states["sensor.heat_fault"].state = "nan"
        await sensor.async_update()
        assert len([c for c in sensor.hass.services.calls if c[0] == "notify"]) == 1
        clock[0] += timedelta(seconds=1)
        states["sensor.heat_fault"].state = "0.0"
        states["sensor.heat_fault"].last_reported = clock[0]
        await sensor.async_update()
        await sensor._heatpump_alarm_task
        assert len([c for c in sensor.hass.services.calls if c[0] == "notify"]) == 2
        assert sensor.extra_state_attributes["mode"] == "normal"

    asyncio.run(scenario())


def test_changed_alarm_interpretation_does_not_emit_recovery(control):
    sensor, states, clock = control
    sensor._config_entry.options.update(
        thermal_alarm_entity="sensor.heat_fault",
        thermal_alarm_numeric_verified=True,
        thermal_alarm_numeric_normal=0,
        notify_service="notify.family",
    )
    states["sensor.heat_fault"] = SimpleNamespace(
        state="1", attributes={}, last_reported=clock[0]
    )

    async def scenario():
        _report(states, clock)
        await sensor.async_update()
        await sensor._heatpump_alarm_task
        assert len([c for c in sensor.hass.services.calls if c[0] == "notify"]) == 1
        previous = sensor._heatpump_alarm
        sensor._config_entry.options["thermal_alarm_numeric_normal"] = 1
        await sensor.async_update()
        assert sensor._heatpump_alarm is not previous
        assert sensor._heatpump_alarm_status["status"] == "normal"
        assert len([c for c in sensor.hass.services.calls if c[0] == "notify"]) == 1

    asyncio.run(scenario())


def test_numeric_alarm_options_do_not_reset_control_learning(control, monkeypatch):
    sensor, states, clock = control

    async def update():
        pass

    monkeypatch.setattr(sensor, "async_update", update)
    asyncio.run(sensor.async_options_update_listener(sensor.hass, sensor._config_entry))
    pilot = sensor._pilot = object()
    sensor._config_entry.options.update(
        thermal_alarm_numeric_verified=True,
        thermal_alarm_numeric_normal=0,
    )
    asyncio.run(sensor.async_options_update_listener(sensor.hass, sensor._config_entry))
    assert sensor._pilot is pilot


def test_numeric_fault_uses_existing_persistent_fallback_without_mobile(control):
    sensor, states, clock = control
    sensor._config_entry.options.update(
        thermal_alarm_entity="sensor.heat_fault",
        thermal_alarm_numeric_verified=True,
        thermal_alarm_numeric_normal=0,
        notify_service="",
        notification_level="off",
    )
    states["sensor.heat_fault"] = SimpleNamespace(
        state="2", attributes={}, last_reported=clock[0]
    )

    async def scenario():
        _report(states, clock)
        await sensor.async_update()
        await sensor._heatpump_alarm_task
        clock[0] += timedelta(seconds=1)
        states["sensor.heat_fault"].state = "0"
        states["sensor.heat_fault"].last_reported = clock[0]
        await sensor.async_update()
        await sensor._heatpump_alarm_task
        persistent = [
            call
            for call in sensor.hass.services.calls
            if call[0] == "persistent_notification"
        ]
        assert len(persistent) == 2
        assert (
            persistent[0][2]["notification_id"] == persistent[1][2]["notification_id"]
        )

    asyncio.run(scenario())
