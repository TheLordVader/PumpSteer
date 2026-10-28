"""Optional native HA checks; run this file explicitly with the HA pytest plugin.

The normal project suite uses HA stubs. These tests instead use real Home
Assistant, an in-memory SQLite Recorder and real Store files in a temporary
configuration directory. No production systems or credentials are used.
Run: PYTHONPATH=. pytest -s -q -o asyncio_mode=auto scripts/verify_native_ha.py
"""

import json
import logging
import asyncio
import time
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.setup import async_setup_component
from homeassistant.components.recorder import get_instance
from homeassistant.components.sensor import DOMAIN as SENSOR
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.util import dt as dt_util
from homeassistant.helpers.storage import Store
from sqlalchemy import text

pytestmark = pytest.mark.asyncio
REPO = Path(__file__).resolve().parents[1]
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


@pytest.fixture
def hass_config_dir(tmp_path):
    custom = tmp_path / "custom_components"
    custom.mkdir()
    (custom / "pumpsteer").symlink_to(REPO / "custom_components/pumpsteer")
    return str(tmp_path)


@pytest.fixture
def hass_storage():
    # The default HA test fixture replaces Store with an in-memory mock.
    # This probe needs the real on-disk Store in the temporary config dir.
    return {}


async def test_native_running_cycles_and_real_store(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path
):
    from custom_components.pumpsteer import pump_log
    from custom_components.pumpsteer.pilot_vendor import response_optimizer

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    calls = []
    hass.bus.async_listen(
        "call_service",
        lambda event: calls.append(
            (event.data.get("domain"), event.data.get("service"))
        ),
    )
    counts = {"optimize": 0, "store_writes": 0, "states": 0}
    loop_thread = threading.get_ident()
    optimizer_threads = []
    original_optimize = response_optimizer.optimize

    def optimize(*args, **kwargs):
        counts["optimize"] += 1
        optimizer_threads.append(threading.get_ident())
        return original_optimize(*args, **kwargs)

    monkeypatch.setattr(response_optimizer, "optimize", optimize)
    original_write = Store._async_write_data

    async def write(store, data):
        if store.key.startswith("pumpsteer.pilot."):
            counts["store_writes"] += 1
        return await original_write(store, data)

    monkeypatch.setattr(Store, "_async_write_data", write)
    await hass.async_start()
    await hass.config.async_set_time_zone("UTC")
    now = dt_util.utcnow()
    midnight = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    raw = [
        {
            "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
            "end": (midnight + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": 1.0,
        }
        for i in range(96)
    ]
    raw_tomorrow = [
        {
            "start": (midnight + timedelta(days=1, minutes=15 * i)).isoformat(),
            "end": (midnight + timedelta(days=1, minutes=15 * (i + 1))).isoformat(),
            "value": 1.0,
        }
        for i in range(96)
    ]
    weather_rows = [
        {
            "datetime": (
                now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)
            ).isoformat(),
            "temperature": 5.0,
        }
        for i in range(26)
    ]

    async def forecast(call):
        return {"weather.test": {"forecast": weather_rows}}

    hass.services.async_register(
        "weather", "get_forecasts", forecast, supports_response=SupportsResponse.ONLY
    )
    options = {
        "indoor_temp_entity": "sensor.test_indoor",
        "real_outdoor_entity": "sensor.test_outdoor",
        "electricity_price_entity": "sensor.test_price",
        "price_tomorrow_entity": "sensor.test_tomorrow",
        "weather_entity": "weather.test",
        "target_temperature": 21,
        "summer_threshold": 18,
        "aggressiveness": 0,
        "house_inertia": 2,
        "thermal_intelligence_enabled": False,
    }
    entry = MockConfigEntry(domain="pumpsteer", title="test", data={}, options=options)
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    entity = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    assert {
        "thermal_intelligence_plan_detail",
        "thermal_intelligence_plan_outcomes",
    } <= (type(entity)._unrecorded_attributes)
    hass.bus.async_listen(
        "state_changed",
        lambda event: (
            counts.__setitem__("states", counts["states"] + 1)
            if event.data.get("entity_id") == entity.entity_id
            else None
        ),
    )
    hass.states.async_set("sensor.test_indoor", "20.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.test_outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "sensor.test_price",
        "1",
        {"today": [1.0] * 96, "raw_today": raw, "unit_of_measurement": "SEK/kWh"},
    )
    hass.states.async_set(
        "sensor.test_tomorrow",
        "1",
        {
            "tomorrow": [1.0] * 96,
            "raw_tomorrow": raw_tomorrow,
            "unit_of_measurement": "SEK/kWh",
        },
    )
    hass.states.async_set("weather.test", "cloudy", {"temperature_unit": "°C"})
    await hass.async_block_till_done()
    for _ in range(3):
        await entity.async_update()
        entity.async_write_ha_state()
        await asyncio.sleep(0.01)
    assert entity.extra_state_attributes["mode"] == "normal"
    baseline = entity.state
    assert counts["optimize"] == counts["store_writes"] == 0
    gaps = []
    running = True

    async def probe_loop():
        previous = time.perf_counter()
        while running:
            await asyncio.sleep(0.01)
            now_tick = time.perf_counter()
            gaps.append(now_tick - previous)
            previous = now_tick

    probe = asyncio.create_task(probe_loop())
    await asyncio.sleep(0)
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    hass.config_entries.async_update_entry(
        entry, options={**options, "thermal_intelligence_enabled": True}
    )
    await hass.async_block_till_done()
    print(
        "INITIAL_PLAN_WALL_MS",
        round((time.perf_counter() - wall_started) * 1000, 1),
        "PROCESS_CPU_MS",
        round((time.process_time() - cpu_started) * 1000, 1),
    )
    assert entity._pilot_plan.get("planning_status") == "hypothetical_unvalidated"
    assert entity._plan_audit.plan_pending
    entity.async_write_ha_state()
    current = hass.states.get(entity.entity_id)
    assert current.attributes["thermal_intelligence_plan_detail"][
        "planning_status"
    ] == ("hypothetical_unvalidated")
    assert (
        len(current.attributes["thermal_intelligence_plan_detail"]["temperatures"]) >= 8
    )
    assert "thermal_intelligence_plan_outcomes" in current.attributes
    for _ in range(3):
        await entity.async_update()
        entity.async_write_ha_state()
        await asyncio.sleep(0.01)
    assert entity.state == baseline
    assert counts["optimize"] == 1
    assert optimizer_threads[0] != loop_thread
    assert entity._pilot_store is not None
    running = False
    await probe
    print("LOOP_MAX_GAP_MS", round(max(gaps, default=0) * 1000, 1))
    print(
        "PLAN",
        entity._pilot_plan.get("planning_status"),
        entity._pilot_plan.get("reason"),
    )
    assert entity._pilot_plan.get("planning_status") in (
        "hypothetical_unvalidated",
        "unavailable",
    )
    # Wait for the real 30-second Store debounce, including its disk writer.
    await asyncio.sleep(30.5)
    await hass.async_block_till_done()
    stored = json.loads(Path(entity._pilot_store.path).read_text())
    assert stored["data"]["plan_audit"]["pending_count"] > 0
    # A fresh HA object and Store must read from disk, not the old HA manager's
    # in-memory cache. No old pending plan can become fresh after restart.
    from custom_components.pumpsteer.sensor import PumpSteerSensor

    new_hass = HomeAssistant(str(tmp_path))
    restored_from_disk = PumpSteerSensor(new_hass, entry)
    await restored_from_disk._load_pilot_store()
    assert restored_from_disk._plan_audit_loaded
    assert restored_from_disk._plan_audit.plan_unverifiable > 0
    assert not restored_from_disk._plan_audit.plan_pending
    await get_instance(hass).async_block_till_done()
    with recorder_mock.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT sa.shared_attrs FROM states s JOIN states_meta sm ON s.metadata_id=sm.metadata_id JOIN state_attributes sa ON s.attributes_id=sa.attributes_id WHERE sm.entity_id=:entity"
            ),
            {"entity": entity.entity_id},
        ).fetchall()
    assert rows
    assert any(
        json.loads(row[0]).get("thermal_intelligence_planning_status")
        == "hypothetical_unvalidated"
        for row in rows
    )
    assert all(
        not {"thermal_intelligence_plan_detail", "thermal_intelligence_plan_outcomes"}
        & json.loads(row[0]).keys()
        for row in rows
    )
    print(
        "CYCLES_RECORDER_ROWS",
        len(rows),
        "REAL_STORE_WRITES",
        counts["store_writes"],
        "STATE_EVENTS",
        counts["states"],
        "OPTIMIZATIONS",
        counts["optimize"],
    )
    assert counts["store_writes"] == 1
    assert all(domain not in {"number", "switch"} for domain, _ in calls)
    hass.states.async_set("sensor.test_indoor", "unavailable")
    await entity.async_update()
    assert entity.extra_state_attributes["mode"] == "safe_mode"
    assert (
        entity.extra_state_attributes["thermal_intelligence_planning_status"]
        == "unavailable"
    )
    hass.states.async_set("sensor.test_indoor", "20.6", {"unit_of_measurement": "°C"})
    await entity.async_update()
    assert entity.extra_state_attributes["mode"] == "normal"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    assert restored is not entity
    assert restored._pilot_store is not None
    assert restored._plan_audit_loaded
    assert restored._plan_audit.plan_unverifiable > 0
    assert not restored._plan_audit.plan_pending
    assert restored._pilot_plan["planning_status"] == "unavailable"
    before = counts["optimize"]
    hass.config_entries.async_update_entry(
        entry, options={**options, "thermal_intelligence_enabled": False}
    )
    await hass.async_block_till_done()
    await restored.async_update()
    assert counts["optimize"] == before
    assert "thermal_intelligence_plan_detail" not in restored.extra_state_attributes
    assert all(domain not in {"number", "switch"} for domain, _ in calls)


async def test_native_power_events_estimate_energy_and_price(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path, freezer
):
    from custom_components.pumpsteer import pump_log

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    freezer.move_to("2026-10-20 10:00:00+00:00")
    await hass.async_start()
    midnight = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    raw = [
        {
            "start": (midnight + timedelta(minutes=15 * i)).isoformat(),
            "end": (midnight + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": 1.0,
        }
        for i in range(96)
    ]
    entry = MockConfigEntry(
        domain="pumpsteer",
        title="test",
        data={},
        options={
            "indoor_temp_entity": "sensor.test_indoor",
            "real_outdoor_entity": "sensor.test_outdoor",
            "electricity_price_entity": "sensor.test_price",
            "target_temperature": 21,
            "summer_threshold": 18,
            "aggressiveness": 0,
            "house_inertia": 2,
            "thermal_intelligence_enabled": True,
            "thermal_power_entity": "sensor.test_power",
            "thermal_heating_mode_entity": "binary_sensor.test_heat",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    entity = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    hass.states.async_set(
        "sensor.test_price",
        "1",
        {"today": [1.0] * 96, "raw_today": raw, "unit_of_measurement": "SEK/kWh"},
    )
    hass.states.async_set("binary_sensor.test_heat", "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.test_power", "1000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=60))
    hass.states.async_set("sensor.test_power", "2000", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    result = entity._power_ledger.summary()
    assert result["energy_kwh_estimate"] == 0.0167
    assert result["cost_estimate"] == 0.0167
    assert result["radiator_mode_kwh_estimate"] == 0.0167
    print(
        "NATIVE_POWER",
        result["energy_kwh_estimate"],
        result["cost_estimate"],
        result["measurement_time"],
    )
