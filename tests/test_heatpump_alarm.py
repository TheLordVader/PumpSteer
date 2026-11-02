from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from custom_components.pumpsteer.heatpump_alarm import HeatPumpAlarm


def test_alarm_deduplicates_and_requires_fresh_report_for_recovery():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    state = SimpleNamespace(state="on", attributes={}, last_reported=now)
    m = HeatPumpAlarm("binary_sensor.alarm")
    status, event = m.observe(now, state)
    assert status["active"] and event == "alarm"
    m.mark_sent("alarm")
    saved, event = m.observe(now, state)
    assert event is None
    resumed = HeatPumpAlarm("binary_sensor.alarm", saved, started_at=now)
    state.state = "off"
    status, event = resumed.observe(now + timedelta(minutes=1), state)
    assert status["active"] and event is None
    state.last_reported = now + timedelta(minutes=2)
    status, event = resumed.observe(state.last_reported, state)
    assert not status["active"] and event == "recovery"
    resumed.mark_sent(event)
    assert resumed.observe(state.last_reported, state)[1] is None


def test_missing_data_does_not_fabricate_recovery():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    state = SimpleNamespace(state="on", attributes={}, last_reported=now)
    m = HeatPumpAlarm("binary_sensor.alarm")
    m.observe(now, state)
    m.mark_sent("alarm")
    state.state = "unavailable"
    status, event = m.observe(now + timedelta(minutes=1), state)
    assert status["active"] and status["status"] == "unknown" and event is None


def test_alarm_delivery_acknowledged_after_off_queues_recovery():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = HeatPumpAlarm("binary_sensor.alarm")
    state = SimpleNamespace(state="on", attributes={}, last_reported=now)
    assert monitor.observe(now, state)[1] == "alarm"
    generation = monitor.generation
    state.state = "off"
    state.last_reported = now + timedelta(minutes=1)
    assert monitor.observe(state.last_reported, state)[1] is None
    assert monitor.mark_delivered("alarm", generation)
    assert monitor.observe(state.last_reported, state)[1] == "recovery"
    assert monitor.mark_delivered("recovery", generation)
    assert monitor.observe(state.last_reported, state)[1] is None


def test_alarm_delivery_after_unknown_waits_for_confirmed_off():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = HeatPumpAlarm("binary_sensor.alarm")
    state = SimpleNamespace(state="on", attributes={}, last_reported=now)
    monitor.observe(now, state)
    generation = monitor.generation
    state.state = "unavailable"
    assert monitor.observe(now, state)[1] is None
    assert monitor.mark_delivered("alarm", generation)
    assert monitor.observe(now, state)[1] is None
    state.state = "off"
    state.last_reported = now + timedelta(minutes=1)
    assert monitor.observe(state.last_reported, state)[1] == "recovery"


def test_delayed_prior_generation_cannot_acknowledge_new_alarm_or_recovery():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = HeatPumpAlarm("binary_sensor.alarm")
    state = SimpleNamespace(state="on", attributes={}, last_reported=now)
    monitor.observe(now, state)
    old_generation = monitor.generation
    state.state, state.last_reported = "off", now + timedelta(minutes=1)
    monitor.observe(state.last_reported, state)
    state.state, state.last_reported = "on", now + timedelta(minutes=2)
    assert monitor.observe(state.last_reported, state)[1] == "alarm"
    assert monitor.generation != old_generation
    assert not monitor.mark_delivered("alarm", old_generation)
    assert monitor.observe(state.last_reported, state)[1] == "alarm"
    assert not monitor.mark_delivered("recovery", old_generation)


def test_live_active_fault_before_integration_load_is_not_suppressed():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = HeatPumpAlarm(
        "binary_sensor.fault",
        {
            "entity": "binary_sensor.fault",
            "active": False,
            "sent": False,
        },
        started_at=now,
    )
    state = SimpleNamespace(
        state="on", attributes={}, last_reported=now - timedelta(seconds=10)
    )
    assert monitor.observe(now, state)[1] == "alarm"


def test_restored_active_fault_is_not_new_evidence():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = HeatPumpAlarm("binary_sensor.fault", started_at=now)
    state = SimpleNamespace(
        state="on", attributes={"restored": True}, last_reported=now
    )
    assert monitor.observe(now, state)[1] is None


def test_prestart_active_report_does_not_clear_recovery_start_barrier():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = HeatPumpAlarm(
        "binary_sensor.fault",
        {"entity": "binary_sensor.fault", "active": True, "sent": True},
        started_at=now,
    )
    state = SimpleNamespace(
        state="on", attributes={}, last_reported=now - timedelta(seconds=1)
    )
    assert monitor.observe(now, state)[1] is None
    state.state = "off"
    assert monitor.observe(now, state)[1] is None
    state.last_reported = now + timedelta(seconds=1)
    assert monitor.observe(state.last_reported, state)[1] == "recovery"
