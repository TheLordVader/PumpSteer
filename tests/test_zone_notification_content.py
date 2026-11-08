"""Actionable zone alerts only use qualified, passive observations."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio

import pytest

from custom_components.pumpsteer.extra_zones import (
    ExtraZones,
    zone_notification_message,
)
from test_temperature_input_safety import control, _report  # noqa: F401
from test_safe_mode_output import setup_sensor  # noqa: F401


def test_temperature_notice_with_qualified_thermostat_and_heat_response():
    alert = {
        "event": "temperature",
        "name": "Timmerhuset",
        "temperature": 18.2,
        "threshold": 19.0,
        "trend_c_per_hour": -0.4,
        "climate_setpoint": 20.0,
        "climate_temperature_unit": "°C",
        "climate_configured": True,
        "heat_demand": True,
        "heat_demand_source": "heating_demand",
        "heat_response_qualified": True,
    }
    message = zone_notification_message(alert, "sv")
    for fragment in (
        "Timmerhuset",
        "18,2 °C",
        "19,0 °C",
        "20,0 °C",
        "begär värme",
        "endast övervakat",
        "Kontrollera",
    ):
        assert fragment in message
    assert "ventilen är öppen" not in message.lower()


@pytest.mark.parametrize(
    "action,claimed", [("heating", "värmedrift"), ("idle", "ingen pågående värmedrift")]
)
def test_hvac_action_is_not_presented_as_an_explicit_heat_request(action, claimed):
    alert = {
        "event": "temperature",
        "name": "Rum",
        "temperature": 4,
        "threshold": 5,
        "heat_demand": action == "heating",
        "heat_demand_source": "hvac_action",
    }
    message = zone_notification_message(alert, "sv")
    assert claimed in message
    assert "begär värme" not in message


@pytest.mark.parametrize(
    "attributes",
    [
        {"hvac_action": "heating", "heating_demand": 0},
        {"hvac_action": "heating", "valve_position": 30, "heating_demand": 0},
        {"pi_heating_demand": 30, "heating_demand": 0},
    ],
)
def test_contradictory_explicit_thermostat_signals_are_unknown(attributes):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "climate": "climate.room",
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(
        state="9", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    climate = SimpleNamespace(state="heat", attributes=attributes, last_reported=now)
    live = {"sensor.room": sensor, "climate.room": climate}
    monitor.observe(now, cfg, live)
    sensor.last_reported = climate.last_reported = now + timedelta(minutes=1)
    _, alerts = monitor.observe(sensor.last_reported, cfg, live)
    assert alerts[0]["heat_demand"] is None
    assert "värmebegäran är okänd" in zone_notification_message(alerts[0], "sv")


def test_unknown_thermostat_and_unqualified_trend_are_not_guessed():
    alert = {
        "event": "temperature",
        "name": "Room",
        "temperature": 4.0,
        "threshold": 5.0,
        "climate_configured": False,
        "heat_demand": None,
        "trend_c_per_hour": None,
        "heat_response_qualified": False,
    }
    msg = zone_notification_message(alert, "en")
    assert "unknown" in msg.lower() and "monitor" in msg.lower()
    assert "trend" not in msg.lower()
    assert "no recovery" not in msg.lower()
    assert "valve is open" not in msg.lower()


def test_simultaneous_temperature_and_heat_shortage_produce_one_notice():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    config = [
        {
            "entity": "sensor.room",
            "name": "Room",
            "threshold": 10,
            "enabled": True,
            "climate": "climate.room",
            "alarm_delay_minutes": 30,
        }
    ]
    temperature = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    climate = SimpleNamespace(
        state="heat",
        attributes={
            "hvac_action": "heating",
            "temperature": 20,
            "temperature_unit": "°C",
        },
    )
    for minute in range(31):
        now = start + timedelta(minutes=minute)
        temperature.last_reported = climate.last_reported = now
        _, alerts = monitor.observe(
            now, config, {"sensor.room": temperature, "climate.room": climate}
        )
    assert len(alerts) == 1
    assert alerts[0]["event"] == "temperature"
    assert alerts[0]["related_events"] == ["heat_shortage"]
    assert alerts[0]["heat_response_qualified"] is True
    assert alerts[0]["trend_c_per_hour"] == pytest.approx(0)


def test_queued_notice_drops_thermostat_claim_after_signal_is_lost():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "climate": "climate.room",
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    climate = SimpleNamespace(
        state="heat",
        attributes={
            "hvac_action": "heating",
            "temperature": 20,
            "temperature_unit": "°C",
        },
    )
    live = {"sensor.room": sensor, "climate.room": climate}
    for minute in (0, 1):
        now = start + timedelta(minutes=minute)
        sensor.last_reported = climate.last_reported = now
        _, alerts = monitor.observe(now, cfg, live)
    del live["climate.room"]
    payload = monitor.notification_payload(alerts[0], now, live)
    assert payload["heat_demand"] is None
    assert payload["climate_setpoint"] is None
    assert "begär värme" not in zone_notification_message(payload, "sv")


def test_queued_alarm_is_not_sent_after_the_room_sensor_changes():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    for minute in (0, 1):
        now = start + timedelta(minutes=minute)
        sensor.last_reported = now
        _, alerts = monitor.observe(now, cfg, {"sensor.room": sensor})
    sensor.state = "unavailable"
    assert monitor.notification_payload(alerts[0], now, {"sensor.room": sensor}) is None


def test_no_recovery_notice_when_alarm_never_delivered():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    for minute in (0, 1):
        now = start + timedelta(minutes=minute)
        sensor.last_reported = now
        _, alerts = monitor.observe(now, cfg, {"sensor.room": sensor})
    assert alerts[0]["event"] == "temperature"
    sensor.state = "11"
    sensor.last_reported += timedelta(minutes=1)
    _, recovery = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    assert not any(alert["event"] == "recovery" for alert in recovery)


def test_no_recovery_notice_after_an_undelivered_sensor_fault():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [{"entity": "sensor.room", "threshold": 10, "enabled": True}]
    _, alerts = monitor.observe(start + timedelta(minutes=61), cfg, {})
    # Startup grace is counted from the first observation, not construction.
    assert not alerts
    now = start + timedelta(minutes=122)
    _, alerts = monitor.observe(now, cfg, {})
    assert [alert["event"] for alert in alerts] == ["sensor_fault"]
    sensor = SimpleNamespace(
        state="11",
        attributes={"unit_of_measurement": "°C"},
        last_reported=now + timedelta(minutes=1),
    )
    _, recovery = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    assert not any(alert["event"] == "recovery" for alert in recovery)


def test_legacy_recovery_without_delivery_evidence_is_not_asserted():
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.room", "threshold": 10, "enabled": True}]
    saved = {
        "1": {
            "entity": "sensor.room",
            "threshold": 10,
            "profile": "maintenance",
            "enabled": True,
            "alarm_delay_minutes": 15,
            "climate": None,
            "events": {},
            "notifications_sent": {},
            "episode_active": False,
            "recovery_pending": True,
        }
    }
    monitor = ExtraZones(saved, started_at=now)
    now += timedelta(seconds=1)
    sensor = SimpleNamespace(
        state="11", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    _, alerts = monitor.observe(now, cfg, {"sensor.room": sensor})
    assert not any(alert["event"] == "recovery" for alert in alerts)


def test_delivery_acknowledged_after_recovery_still_queues_recovery():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    for minute in (0, 1):
        now = start + timedelta(minutes=minute)
        sensor.last_reported = now
        _, alerts = monitor.observe(now, cfg, {"sensor.room": sensor})
    alert = alerts[0]
    sensor.state = "11"
    sensor.last_reported += timedelta(minutes=1)
    _, recovery = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    assert not recovery
    assert monitor.mark_delivered(alert)
    _, recovery = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    assert [item["event"] for item in recovery] == ["recovery"]


def test_late_delivery_during_second_unconfirmed_drop_still_recovers():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    for seconds, value in ((0, "9"), (60, "9")):
        sensor.state = value
        sensor.last_reported = start + timedelta(seconds=seconds)
        _, alerts = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    alarm = alerts[0]
    for seconds, value in ((61, "11"), (62, "9")):
        sensor.state = value
        sensor.last_reported = start + timedelta(seconds=seconds)
        _, alerts = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
        assert not alerts
    assert monitor.mark_delivered(alarm)
    sensor.state = "11"
    sensor.last_reported = start + timedelta(seconds=63)
    _, alerts = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    assert [alert["event"] for alert in alerts] == ["recovery"]


def test_late_delivery_episode_survives_restart_before_next_observation():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    for seconds, value in ((0, "9"), (60, "9")):
        sensor.state = value
        sensor.last_reported = start + timedelta(seconds=seconds)
        _, alerts = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    alarm = alerts[0]
    for seconds, value in ((61, "11"), (62, "9")):
        sensor.state = value
        sensor.last_reported = start + timedelta(seconds=seconds)
        saved, _ = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    saved["1"].update(monitor.mark_delivered(alarm))
    restarted = ExtraZones(saved, started_at=sensor.last_reported)
    sensor.state = "11"
    sensor.last_reported = start + timedelta(seconds=63)
    _, alerts = restarted.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    assert [alert["event"] for alert in alerts] == ["recovery"]


def test_late_delivery_from_old_episode_does_not_acknowledge_new_alarm():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    for seconds, value in ((0, "9"), (60, "9")):
        sensor.state = value
        sensor.last_reported = start + timedelta(seconds=seconds)
        _, alerts = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    old_alert = alerts[0]
    for seconds, value in ((61, "11"), (62, "9"), (122, "9")):
        sensor.state = value
        sensor.last_reported = start + timedelta(seconds=seconds)
        _, alerts = monitor.observe(sensor.last_reported, cfg, {"sensor.room": sensor})
    assert [a["event"] for a in alerts] == ["temperature"]
    assert monitor.mark_delivered(old_alert) is None
    assert monitor.notification_current(alerts[0])


def test_old_heat_shortage_is_not_renotified_without_new_trend_coverage():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [
        {
            "entity": "sensor.room",
            "threshold": 10,
            "enabled": True,
            "climate": "climate.room",
            "alarm_delay_minutes": 1,
        }
    ]
    sensor = SimpleNamespace(state="9", attributes={"unit_of_measurement": "°C"})
    climate = SimpleNamespace(state="heat", attributes={"hvac_action": "heating"})
    live = {"sensor.room": sensor, "climate.room": climate}
    for minute in range(31):
        now = start + timedelta(minutes=minute)
        sensor.last_reported = climate.last_reported = now
        _, alerts = monitor.observe(now, cfg, live)
        for alert in alerts:
            if alert["event"] == "temperature":
                monitor.mark_sent(1, "temperature")
    assert any(alert["event"] == "heat_shortage" for alert in alerts)
    now += timedelta(minutes=36)
    sensor.last_reported = climate.last_reported = now
    status, alerts = monitor.observe(now, cfg, live)
    assert status["1"]["trend_quality"] == "insufficient_data"
    assert not any(alert["event"] == "heat_shortage" for alert in alerts)


def test_controller_sends_one_combined_alert_and_one_recovery(control):
    sensor, all_states, clock = control
    sensor._config_entry.options.update(
        zone_1_entity="sensor.zone",
        zone_1_name="Garage",
        zone_1_threshold=10,
        zone_1_enabled=True,
        zone_1_climate="climate.zone",
        zone_1_alarm_delay_minutes=30,
        zone_1_notify_service="notify.family",
    )
    all_states["sensor.zone"] = SimpleNamespace(
        state="9", attributes={"unit_of_measurement": "°C"}
    )
    all_states["climate.zone"] = SimpleNamespace(
        state="heat",
        attributes={
            "hvac_action": "heating",
            "temperature": 20,
            "temperature_unit": "°C",
        },
    )

    async def cycle():
        for minute in range(31):
            if minute:
                clock[0] += timedelta(minutes=1)
            _report(all_states, clock)
            for entity in ("sensor.zone", "climate.zone"):
                all_states[entity].last_reported = clock[0]
            await sensor.async_update()
            await asyncio.gather(
                *(
                    task
                    for _, task in getattr(sensor, "_zone_notify_tasks", {}).values()
                )
            )
        messages = [
            c[2]["message"] for c in sensor.hass.services.calls if c[0] == "notify"
        ]
        assert len(messages) == 1
        assert "Temperaturlarm" in messages[0]
        assert "20,0 °C" in messages[0] and "ingen tydlig återhämtning" in messages[0]
        assert sensor._zone_diagnostics["1"]["notifications_sent"]["heat_shortage"]
        clock[0] += timedelta(minutes=1)
        _report(all_states, clock)
        all_states["sensor.zone"].state = "11"
        for entity in ("sensor.zone", "climate.zone"):
            all_states[entity].last_reported = clock[0]
        await sensor.async_update()
        await asyncio.gather(
            *(task for _, task in getattr(sensor, "_zone_notify_tasks", {}).values())
        )
        messages = [
            c[2]["message"] for c in sensor.hass.services.calls if c[0] == "notify"
        ]
        assert len(messages) == 2
        assert "Återhämtning" in messages[-1]
        assert sensor.extra_state_attributes["mode"] == "normal"

    asyncio.run(cycle())


def test_controller_recovers_after_alarm_delivery_finishes_late(control, monkeypatch):
    sensor, all_states, clock = control
    sensor._config_entry.options.update(
        zone_1_entity="sensor.zone",
        zone_1_name="Garage",
        zone_1_threshold=10,
        zone_1_enabled=True,
        zone_1_alarm_delay_minutes=1,
        zone_1_notify_service="notify.family",
    )
    all_states["sensor.zone"] = SimpleNamespace(
        state="9", attributes={"unit_of_measurement": "°C"}
    )

    async def scenario():
        sent = []
        started, release = asyncio.Event(), asyncio.Event()
        original = sensor.hass.services.async_call

        async def delayed(domain, service, data, blocking=False):
            if domain == "notify":
                sent.append(data["message"])
                if len(sent) == 1:
                    started.set()
                    await release.wait()
            else:
                await original(domain, service, data, blocking=blocking)

        monkeypatch.setattr(sensor.hass.services, "async_call", delayed)
        for minute in (0, 1):
            if minute:
                clock[0] += timedelta(minutes=1)
            _report(all_states, clock)
            all_states["sensor.zone"].last_reported = clock[0]
            await sensor.async_update()
        await started.wait()
        clock[0] += timedelta(minutes=1)
        _report(all_states, clock)
        all_states["sensor.zone"].state = "11"
        all_states["sensor.zone"].last_reported = clock[0]
        await sensor.async_update()
        assert len(sent) == 1
        release.set()
        await asyncio.gather(*(task for _, task in sensor._zone_notify_tasks.values()))
        await sensor.async_update()
        await asyncio.gather(*(task for _, task in sensor._zone_notify_tasks.values()))
        assert len(sent) == 2
        assert "Temperaturlarm" in sent[0] and "Återhämtning" in sent[1]

    asyncio.run(scenario())
