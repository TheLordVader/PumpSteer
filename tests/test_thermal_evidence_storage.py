"""Existing Store batching and diagnostic isolation, no physical devices."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from test_safe_mode_output import setup_sensor  # noqa: F401
from test_temperature_input_safety import control  # noqa: F401


def test_thermal_store_batches_changes_and_flushes_latest_on_unload(request):
    controller, states, clock = request.getfixturevalue('control')
    saved = []
    flushed = []
    async def save(payload):
        flushed.append(payload)
    controller._pilot_store = SimpleNamespace(
        async_delay_save=lambda callback, delay: saved.append((callback(), delay)),
        async_save=save,
    )
    for minute in range(11):
        controller._thermal_intelligence.record_output({
            'at': clock[0].isoformat(), 'transport': 'ohmigo',
            'status': 'service_completed_unverified', 'requested': float(minute),
        })
        asyncio.run(controller._save_pilot_store())
        clock[0] += timedelta(minutes=1)
    assert len(saved) <= 3
    asyncio.run(controller._flush_pilot_store())
    assert flushed[-1]['thermal_intelligence'] == controller._thermal_intelligence.export_state()


def test_other_audit_updates_cannot_rewrite_full_thermal_store_every_minute(request):
    controller, states, clock = request.getfixturevalue('control')
    saved = []
    controller._pilot_store = SimpleNamespace(
        async_delay_save=lambda callback, delay: saved.append(callback()))
    controller._thermal_intelligence.record_output({
        'at': clock[0].isoformat(), 'transport': 'ohmigo',
        'status': 'service_completed_unverified', 'requested': -5,
    })
    for minute in range(11):
        controller._plan_audit.plan_unverifiable += 1
        asyncio.run(controller._save_pilot_store())
        clock[0] += timedelta(minutes=1)
    assert len(saved) <= 3
