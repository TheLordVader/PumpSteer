"""Safety notices reuse the existing channel and persistent fallback."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.notify import (
    async_send_notification,
    heatpump_alarm_message,
)


@pytest.mark.parametrize("language, fragment", [("sv", "fellarm"), ("en", "fault")])
def test_fault_language(language, fragment):
    hass = SimpleNamespace(config=SimpleNamespace(language=language))
    assert fragment in heatpump_alarm_message(hass, "alarm")
    assert fragment in heatpump_alarm_message(hass, "recovery")


def test_mobile_recovery_updates_existing_persistent_fault():
    calls = []

    async def call(domain, service, data, **kwargs):
        calls.append((domain, service, data, kwargs))

    hass = SimpleNamespace(services=SimpleNamespace(async_call=call))
    entry = SimpleNamespace(data={}, options={"notify_service": "notify.mobile"})
    route = asyncio.run(
        async_send_notification(
            hass,
            entry,
            "PumpSteer",
            "Recovered",
            "unique_fault",
            confirmed=True,
            persistent_existing=True,
        )
    )
    assert route == "persistent"
    assert [c[0] for c in calls] == ["notify", "persistent_notification"]
    assert all(c[3]["blocking"] for c in calls)
    assert calls[-1][2]["notification_id"] == "unique_fault"


def test_persistent_channel_survives_alarm_restore():
    from custom_components.pumpsteer.heatpump_alarm import HeatPumpAlarm

    monitor = HeatPumpAlarm(
        "binary_sensor.fault",
        {
            "entity": "binary_sensor.fault",
            "persistent": True,
            "active": True,
            "sent": True,
        },
    )
    assert monitor.persistent
    assert monitor.active and monitor.sent
