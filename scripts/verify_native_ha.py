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


@pytest.fixture(autouse=True)
def local_component_namespace(monkeypatch):
    """Include this checkout when the HA plugin supplies its own test package."""
    import custom_components

    monkeypatch.setattr(
        custom_components,
        "__path__",
        [str(REPO / "custom_components"), *custom_components.__path__],
    )


@pytest.fixture(autouse=True)
def hass_config_dir(monkeypatch, tmp_path):
    # Select the directory before HA/Recorder fixtures run. Depending on hass
    # here would start HA before recorder_db_url has prepared its database.
    from pytest_homeassistant_custom_component import common

    monkeypatch.setattr(
        common, "get_test_config_dir", lambda *parts: str(tmp_path.joinpath(*parts))
    )
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
    from custom_components.pumpsteer import pump_log, sensor as sensor_module
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
                now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i + 1)
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
        "price_tomorrow_entity": "sensor.test_price",
        "weather_entity": "weather.test",
        "target_temperature": 21,
        "summer_threshold": 18,
        "aggressiveness": 0,
        "house_inertia": 2,
        "thermal_intelligence_enabled": False,
        "thermal_supply_temp_entity": "sensor.test_supply",
        "thermal_heating_mode_entity": "binary_sensor.test_valve",
        "thermal_heating_mode_inverted": True,
        "thermal_hot_water_mode_entity": "binary_sensor.test_valve",
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
    hass.states.async_set("binary_sensor.test_valve", "off")
    hass.states.async_set("sensor.test_supply", "35", {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "sensor.test_price",
        "1",
        {
            "today": [1.0] * 96,
            "raw_today": raw,
            "raw_tomorrow": raw_tomorrow,
            "prices": [{"start": midnight.isoformat(), "value": 1.0}],
            "unit_of_measurement": "SEK/kWh",
        },
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
    companions = [
        e
        for e in hass.data[SENSOR].entities
        if getattr(e, "_controller", None) is entity
    ]
    assert len(companions) == 3
    temperatures = [
        e
        for e in companions
        if isinstance(e, sensor_module.PumpSteerInputTemperatureSensor)
    ]
    assert {e.native_value for e in temperatures} == {20.5, 5.0}
    assert all(e.device_info == entity.device_info for e in companions)
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
    assert entity._pilot_plan["weather_initial_assumption"] == (
        "current_outdoor_held_until_first_forecast"
    )
    assert entity.extra_state_attributes["thermal_intelligence_price_forecast"] == (
        "coverage_complete_issue_time_unknown"
    )
    assert entity._pilot_heat_source == "supply_temperature_indicator"
    diagnostic = next(
        e
        for e in companions
        if isinstance(e, sensor_module.PumpSteerThermalStatusSensor)
    )
    assert diagnostic.native_value == "hypothetical_plan"
    assert diagnostic.extra_state_attributes["passive"] is True
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
    hass.states.async_set("binary_sensor.test_valve", "on")
    await entity.async_update()
    assert entity._pilot_status == "hot_water_active"
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


async def test_native_supply_detector_uses_heating_mode_and_refuses_hot_water(
    hass, freezer
):
    from custom_components.pumpsteer.pilot_passive import SupplyHeatDetector

    freezer.move_to("2026-10-20 10:00:00+00:00")
    await hass.async_start()
    hass.states.async_set("binary_sensor.radiator_mode", "on")
    hass.states.async_set("sensor.supply", "35", {"unit_of_measurement": "°C"})
    detector = SupplyHeatDetector()
    assert (
        detector.observe(
            dt_util.utcnow(),
            hass.states.get("sensor.supply"),
            hass.states.get("binary_sensor.radiator_mode"),
        )
        is True
    )
    hass.states.async_set("binary_sensor.radiator_mode", "off")
    assert (
        detector.observe(
            dt_util.utcnow(),
            hass.states.get("sensor.supply"),
            hass.states.get("binary_sensor.radiator_mode"),
        )
        is None
    )


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


async def test_active_price_and_fake_ohmigo(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path, freezer
):
    from custom_components.pumpsteer import pump_log, relay_guard

    monkeypatch.setattr(relay_guard, "_VERIFY_SECONDS", 0)
    monkeypatch.setattr(relay_guard, "_STABILIZE_SECONDS", 0)
    from custom_components.pumpsteer.pilot_vendor import response_optimizer

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    freezer.move_to("2026-10-11 08:00:00+00:00")
    await hass.async_start()
    await hass.config.async_set_time_zone("UTC")
    assert await async_setup_component(hass, "persistent_notification", {})
    now = dt_util.utcnow()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    vals = [1.0] * 40 + [10.0] * 4 + [1.0] * 52
    raw = [
        {
            "start": (day + timedelta(minutes=15 * i)).isoformat(),
            "end": (day + timedelta(minutes=15 * (i + 1))).isoformat(),
            "value": v,
        }
        for i, v in enumerate(vals)
    ]
    weather = [
        {"datetime": (now + timedelta(hours=i + 1)).isoformat(), "temperature": -5.0}
        for i in range(26)
    ]

    async def forecast(call):
        return {"weather.test": {"forecast": weather}}

    hass.services.async_register(
        "weather", "get_forecasts", forecast, supports_response=SupportsResponse.ONLY
    )

    def options(enable, entity):
        return {
            "indoor_temp_entity": "sensor.indoor",
            "real_outdoor_entity": "sensor.outdoor",
            "electricity_price_entity": "sensor.price",
            "weather_entity": "weather.test",
            "ohmigo_entity": entity,
            "ohmigo_relay_entity": (
                "switch.relay_on" if enable else "switch.relay_off"
            ),
            "ohmigo_interval_minutes": 1,
            "target_temperature": 21,
            "summer_threshold": 17,
            "aggressiveness": 3,
            "house_inertia": 2,
            "thermal_intelligence_enabled": enable,
            "thermal_detection_method": "automatic",
        }

    hass.states.async_set("switch.relay_off", "on")
    hass.states.async_set("switch.relay_on", "on")
    a = MockConfigEntry(
        domain="pumpsteer",
        title="A",
        data={},
        options=options(False, "number.test_off"),
    )
    b = MockConfigEntry(
        domain="pumpsteer", title="B", data={}, options=options(True, "number.test_on")
    )
    a.add_to_hass(hass)
    b.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    if b.state.value != "loaded":
        assert await hass.config_entries.async_setup(b.entry_id)
    await hass.async_block_till_done()
    entities = {e.unique_id: e for e in hass.data[SENSOR].entities}
    sa, sb = entities[a.entry_id], entities[b.entry_id]
    sa.platform.async_unsub_polling()
    sb.platform.async_unsub_polling()
    switches = {e.unique_id: e for e in hass.data["switch"].entities}
    await switches[a.entry_id + "_ohmigo_relay_guard"].async_turn_on()
    await switches[b.entry_id + "_ohmigo_relay_guard"].async_turn_on()
    await hass.async_block_till_done()
    pushes = []

    async def output(call):
        pushes.append((call.data["entity_id"], call.data["value"]))
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]))

    hass.services.async_register("number", "set_value", output)
    relay_calls = []

    async def relay_output(call):
        relay_calls.append(call.data["entity_id"])
        hass.states.async_set(call.data["entity_id"], "on")

    hass.services.async_register("switch", "turn_on", relay_output)
    for ent, val, attrs in [
        ("sensor.indoor", "20.5", {"unit_of_measurement": "°C"}),
        ("sensor.outdoor", "5", {"unit_of_measurement": "°C"}),
        (
            "sensor.price",
            "1",
            {"today": vals, "raw_today": raw, "unit_of_measurement": "SEK/kWh"},
        ),
        ("weather.test", "cloudy", {"temperature_unit": "°C"}),
        ("number.test_off", "0", {}),
        ("number.test_on", "0", {}),
    ]:
        hass.states.async_set(ent, val, attrs)
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=1))
    for ent, val in [("sensor.indoor", "20.5"), ("sensor.outdoor", "5")]:
        hass.states.async_set(
            ent, val, {"unit_of_measurement": "°C"}, force_update=True
        )
    await hass.async_block_till_done()
    original = response_optimizer.optimize
    count = 0

    def optimizer(*args, **kwargs):
        nonlocal count
        count += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(response_optimizer, "optimize", optimizer)
    prev = 0
    seen = []
    for minute in [
        0,
        1,
        2,
        3,
        4,
        30,
        60,
        90,
        100,
        104,
        105,
        106,
        110,
        114,
        115,
        116,
        119,
        120,
        121,
        130,
        180,
        181,
        182,
        183,
        184,
        185,
        186,
        187,
        188,
        189,
        190,
        191,
        192,
        193,
        194,
        195,
        196,
    ]:
        freezer.tick(timedelta(minutes=minute - prev))
        prev = minute
        # An actual fresh HA report (no artificial physical timestamp claim).
        for ent, val in [("sensor.indoor", "20.5"), ("sensor.outdoor", "5")]:
            hass.states.async_set(
                ent, val, {"unit_of_measurement": "°C"}, force_update=True
            )
        await hass.async_block_till_done()
        pushes.clear()
        await sa.async_update()
        await sb.async_update()
        await hass.async_block_till_done()
        va = [v for eid, v in pushes if eid == "number.test_off"]
        vb = [v for eid, v in pushes if eid == "number.test_on"]
        aa, ab = sa.extra_state_attributes, sb.extra_state_attributes
        if minute == 0:
            assert sb._pilot_plan["planning_status"] == "hypothetical_unvalidated"
            assert sb._pilot_plan["weather_initial_assumption"] == (
                "current_outdoor_held_until_first_forecast"
            )
        assert (
            sa.state,
            aa.get("mode"),
            round(sa._pi._integral, 8),
            round(sa._brake_ramp, 8),
            round(sa._preheat_ramp, 8),
            va,
        ) == (
            sb.state,
            ab.get("mode"),
            round(sb._pi._integral, 8),
            round(sb._brake_ramp, 8),
            round(sb._preheat_ramp, 8),
            vb,
        ), (minute, aa, ab, va, vb)
        assert (
            hass.data["pumpsteer"][a.entry_id]["relay_guard"].diagnostics["status"]
            == hass.data["pumpsteer"][b.entry_id]["relay_guard"].diagnostics["status"]
        )
        seen.append((minute, aa.get("mode"), va))
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set("sensor.indoor", "unavailable")
    pushes.clear()
    await sa.async_update()
    await sb.async_update()
    await hass.async_block_till_done()
    assert (
        sa.extra_state_attributes["mode"]
        == sb.extra_state_attributes["mode"]
        == "safe_mode"
    )
    assert [v for eid, v in pushes if eid == "number.test_off"] == [
        v for eid, v in pushes if eid == "number.test_on"
    ]
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set(
        "sensor.indoor", "20.5", {"unit_of_measurement": "°C"}, force_update=True
    )
    hass.states.async_set(
        "sensor.outdoor", "5", {"unit_of_measurement": "°C"}, force_update=True
    )
    pushes.clear()
    await sa.async_update()
    await sb.async_update()
    await hass.async_block_till_done()
    assert (
        sa.extra_state_attributes["mode"]
        == sb.extra_state_attributes["mode"]
        == "normal"
    )
    assert sa.state == sb.state
    assert [v for eid, v in pushes if eid == "number.test_off"] == [
        v for eid, v in pushes if eid == "number.test_on"
    ]
    print("ACTIVE_NATIVE", seen, "optimizer_calls", count, "safe_and_recovery", True)
    assert {"preheating", "pre_braking", "braking", "normal"} <= {
        mode for _, mode, _ in seen
    }
    assert sum(len(v) for _, _, v in seen) > 0
    assert count > 0
    assert relay_calls.count("switch.relay_off") == relay_calls.count("switch.relay_on")
    print("GUARD_RELAY_WRITES", len(relay_calls))


@pytest.mark.parametrize("stop_kind", ["unload", "shutdown", "disable"])
async def test_unload_inflight(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path, stop_kind
):
    from custom_components.pumpsteer import pump_log
    from custom_components.pumpsteer.pilot_vendor import response_optimizer

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    entered = threading.Event()
    release = threading.Event()
    done = threading.Event()
    orig = response_optimizer.optimize

    def slow(*a, **kw):
        entered.set()
        try:
            if not release.wait(5):
                raise TimeoutError("worker was not released")
            return orig(*a, **kw)
        finally:
            done.set()

    monkeypatch.setattr(response_optimizer, "optimize", slow)
    schedules = []
    orig_save = Store.async_delay_save

    def save(store, *a, **kw):
        if store.key.startswith("pumpsteer.pilot."):
            schedules.append(store.key)
        return orig_save(store, *a, **kw)

    monkeypatch.setattr(Store, "async_delay_save", save)
    await hass.async_start()
    await hass.config.async_set_time_zone("UTC")
    now = dt_util.utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def raw(day):
        return [
            {
                "start": (midnight + timedelta(days=day, minutes=15 * i)).isoformat(),
                "end": (
                    midnight + timedelta(days=day, minutes=15 * (i + 1))
                ).isoformat(),
                "value": 1.0,
            }
            for i in range(96)
        ]

    async def weather(call):
        return {
            "weather.test": {
                "forecast": [
                    {
                        "datetime": (
                            now.replace(minute=0, second=0, microsecond=0)
                            + timedelta(hours=i)
                        ).isoformat(),
                        "temperature": 5,
                    }
                    for i in range(26)
                ]
            }
        }

    hass.services.async_register(
        "weather", "get_forecasts", weather, supports_response=SupportsResponse.ONLY
    )
    opts = {
        "indoor_temp_entity": "sensor.indoor",
        "real_outdoor_entity": "sensor.outdoor",
        "electricity_price_entity": "sensor.price",
        "price_tomorrow_entity": "sensor.tomorrow",
        "weather_entity": "weather.test",
        "target_temperature": 21,
        "summer_threshold": 18,
        "aggressiveness": 0,
        "house_inertia": 2,
        "thermal_intelligence_enabled": False,
    }
    e = MockConfigEntry(domain="pumpsteer", title="test", data={}, options=opts)
    e.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    sensor = next(x for x in hass.data[SENSOR].entities if x.unique_id == e.entry_id)
    for name, value, attrs in [
        ("sensor.indoor", "20.5", {"unit_of_measurement": "°C"}),
        ("sensor.outdoor", "5", {"unit_of_measurement": "°C"}),
        (
            "sensor.price",
            "1",
            {"today": [1] * 96, "raw_today": raw(0), "unit_of_measurement": "SEK/kWh"},
        ),
        (
            "sensor.tomorrow",
            "1",
            {
                "tomorrow": [1] * 96,
                "raw_tomorrow": raw(1),
                "unit_of_measurement": "SEK/kWh",
            },
        ),
        ("weather.test", "cloudy", {"temperature_unit": "°C"}),
    ]:
        hass.states.async_set(name, value, attrs)
    await hass.async_block_till_done()
    await sensor.async_update()
    hass.config_entries.async_update_entry(
        e, options={**opts, "thermal_intelligence_enabled": True}
    )
    assert await asyncio.to_thread(entered.wait, 3)
    try:
        if stop_kind == "unload":
            assert await asyncio.wait_for(
                hass.config_entries.async_unload(e.entry_id), 3
            )
            before = len(schedules)
        elif stop_kind == "shutdown":
            shutdown = asyncio.create_task(hass.async_stop(force=True))
            await asyncio.sleep(0.05)
        else:
            hass.config_entries.async_update_entry(
                e, options={**opts, "thermal_intelligence_enabled": False}
            )
            await asyncio.sleep(0.05)
            before = len(schedules)
    finally:
        release.set()
    if stop_kind == "shutdown":
        await asyncio.wait_for(shutdown, 5)
        before = len(schedules)
    assert await asyncio.to_thread(done.wait, 3)
    if stop_kind in ("unload", "disable"):
        await hass.async_block_till_done()
        if stop_kind == "unload":
            assert sensor._temperature_unloaded
        assert sensor._pilot_plan["planning_status"] == "unavailable"
    else:
        await asyncio.sleep(0.05)
    assert len(schedules) == before
    print(
        "INFLIGHT_STOP",
        stop_kind,
        "worker_finished",
        done.is_set(),
        "post_stop_saves",
        len(schedules) - before,
        flush=True,
    )


@pytest.mark.parametrize(
    "transition", ["reload", "disable", "disable_write_failure", "disable_read_failure"]
)
async def test_pending_model_store_is_flushed_before_reload(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path, transition
):
    from custom_components.pumpsteer import pump_log
    from custom_components.pumpsteer.pilot_vendor.learning_interval import (
        LearningInterval,
    )

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    await hass.async_start()
    await hass.config.async_set_time_zone("UTC")
    options = {
        "indoor_temp_entity": "sensor.indoor",
        "real_outdoor_entity": "sensor.outdoor",
        "electricity_price_entity": "sensor.price",
        "thermal_heat_activity_entity": "binary_sensor.heat",
        "thermal_intelligence_enabled": True,
        "aggressiveness": 0,
    }
    entry = MockConfigEntry(domain="pumpsteer", title="test", data={}, options=options)
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    sensor = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    for name, value, attrs in (
        ("sensor.indoor", "20.5", {"unit_of_measurement": "°C"}),
        ("sensor.outdoor", "5", {"unit_of_measurement": "°C"}),
        ("sensor.price", "1", {"today": [1.0] * 24, "unit_of_measurement": "SEK/kWh"}),
        ("binary_sensor.heat", "on", {}),
    ):
        hass.states.async_set(name, value, attrs)
    await hass.async_block_till_done()
    await sensor.async_update()
    assert sensor._pilot is not None
    sample = LearningInterval(
        dt_util.utcnow().timestamp() - 60, 1, 20, 20.1, 5, 0.3, None, 1
    )
    sensor._pilot.manager.model.add_interval(sample)
    sensor._pilot.house_intervals = 1
    await sensor._save_pilot_store()
    old_store = sensor._pilot_store
    assert old_store is not None and old_store._delay_handle is not None
    if transition == "reload":
        assert await hass.config_entries.async_reload(entry.entry_id)
    else:
        writes = []
        real_write = Store._async_write_data

        async def count_old_store(store, data):
            if store.key == old_store.key:
                writes.append(True)
                if transition == "disable_write_failure" and len(writes) == 1:
                    from homeassistant.util.file import WriteError

                    raise WriteError("synthetic disk failure")
            return await real_write(store, data)

        monkeypatch.setattr(Store, "_async_write_data", count_old_store)
        hass.config_entries.async_update_entry(
            entry, options={**options, "thermal_intelligence_enabled": False}
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert old_store._delay_handle is None
        assert len(writes) == 1
        hass.config_entries.async_update_entry(
            entry,
            options={
                **options,
                "thermal_intelligence_enabled": False,
                "target_temperature": 22,
            },
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(writes) == 1
        if transition == "disable_read_failure":
            real_load = Store.async_load

            async def fail_pilot_load(store):
                if store.key == old_store.key:
                    raise OSError("synthetic read failure")
                return await real_load(store)

            monkeypatch.setattr(Store, "async_load", fail_pilot_load)
        hass.config_entries.async_update_entry(entry, options=options)
    await hass.async_block_till_done()
    restored = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    for name, value in (("sensor.indoor", "20.5"), ("sensor.outdoor", "5")):
        hass.states.async_set(
            name, value, {"unit_of_measurement": "°C"}, force_update=True
        )
    hass.states.async_set("binary_sensor.heat", "on", force_update=True)
    await restored.async_update()
    assert restored._pilot is not None
    assert len(restored._pilot.manager.model.history) == 1
    if transition == "reload":
        assert old_store._delay_handle is None


async def test_initial_store_read_failure_can_recover_plan_history(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path
):
    from custom_components.pumpsteer import pump_log
    from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    await hass.async_start()
    entry = MockConfigEntry(
        domain="pumpsteer",
        title="test",
        data={},
        options={
            "indoor_temp_entity": "sensor.indoor",
            "real_outdoor_entity": "sensor.outdoor",
            "electricity_price_entity": "sensor.price",
            "thermal_intelligence_enabled": True,
        },
    )
    entry.add_to_hass(hass)
    key = f"pumpsteer.pilot.{entry.entry_id}"
    audit = ForecastAudit()
    audit.plan_unverifiable = 3
    await Store(hass, 1, key).async_save(
        {"version": 1, "plan_audit": audit.export_plan()}
    )
    original = Store.async_load
    failures = []

    async def fail_once(store):
        if store.key == key and len(failures) < 2:
            failures.append(True)
            raise OSError("synthetic transient read failure")
        return await original(store)

    monkeypatch.setattr(Store, "async_load", fail_once)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    sensor = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    assert failures
    assert not sensor._plan_audit_loaded
    original_file = Path(sensor._pilot_store.path).read_bytes()
    for name, value, attrs in (
        ("sensor.indoor", "20.5", {"unit_of_measurement": "°C"}),
        ("sensor.outdoor", "5", {"unit_of_measurement": "°C"}),
        ("sensor.price", "1", {"today": [1.0] * 24, "unit_of_measurement": "SEK/kWh"}),
    ):
        hass.states.async_set(name, value, attrs)
    await hass.async_block_till_done()
    await sensor.async_update()
    assert sensor.extra_state_attributes["mode"] == "normal"
    assert sensor._pilot_store_read_failed
    assert sensor._pilot is None
    assert sensor._pilot_plan["reason"] == "history_unavailable"
    assert not sensor._plan_audit.plan_pending
    assert sensor._pilot_store._delay_handle is None
    assert Path(sensor._pilot_store.path).read_bytes() == original_file
    await sensor.async_update()
    assert sensor._plan_audit_loaded
    assert sensor._plan_audit.plan_unverifiable == 3
    assert not sensor._plan_audit.plan_pending


async def test_unknown_store_version_is_not_overwritten(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path
):
    from custom_components.pumpsteer import pump_log

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    await hass.async_start()
    entry = MockConfigEntry(
        domain="pumpsteer",
        title="test",
        data={},
        options={
            "indoor_temp_entity": "sensor.indoor",
            "real_outdoor_entity": "sensor.outdoor",
            "electricity_price_entity": "sensor.price",
            "thermal_intelligence_enabled": True,
        },
    )
    entry.add_to_hass(hass)
    key = f"pumpsteer.pilot.{entry.entry_id}"
    store = Store(hass, 1, key)
    await store.async_save({"version": 2, "future_data": "preserve"})
    original = Path(store.path).read_bytes()
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    sensor = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    for name, value, attrs in (
        ("sensor.indoor", "20.5", {"unit_of_measurement": "°C"}),
        ("sensor.outdoor", "5", {"unit_of_measurement": "°C"}),
        ("sensor.price", "1", {"today": [1.0] * 24, "unit_of_measurement": "SEK/kWh"}),
    ):
        hass.states.async_set(name, value, attrs)
    await hass.async_block_till_done()
    await sensor.async_update()
    assert sensor.extra_state_attributes["mode"] == "normal"
    assert sensor._pilot_store_read_failed
    assert sensor._pilot_plan["reason"] == "history_unavailable"
    assert sensor._pilot_store._delay_handle is None
    assert Path(store.path).read_bytes() == original
    # Even a previously cached older payload must not replace the unknown one.
    sensor._pilot_persisted = {"version": 1, "plan_audit": {"records": []}}

    real_load = Store.async_load

    async def fail_read(_store):
        raise OSError("synthetic transient read failure")

    monkeypatch.setattr(Store, "async_load", fail_read)
    await sensor._load_pilot_store()
    assert sensor._pilot_store_read_failed
    hass.config_entries.async_update_entry(
        entry, options={**dict(entry.options), "thermal_intelligence_enabled": False}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert Path(store.path).read_bytes() == original
    await store.async_save({"version": 1, "plan_audit": {"records": []}})
    monkeypatch.setattr(Store, "async_load", real_load)
    hass.config_entries.async_update_entry(
        entry, options={**dict(entry.options), "thermal_intelligence_enabled": True}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not sensor._pilot_store_read_failed


async def test_native_operation_store_restores_unvalidated_samples(hass):
    from custom_components.pumpsteer.heat_operation import HeatOperation
    from custom_components.pumpsteer.sensor import PumpSteerSensor

    await hass.async_start()
    entry = MockConfigEntry(domain="pumpsteer", title="test", data={}, options={})
    sensor = PumpSteerSensor(hass, entry)
    await sensor._load_pilot_store()
    sensor._heat_operation = HeatOperation()
    sensor._heat_operation_signature = ("sensor.test",)
    sensor._heat_operation.samples = {"idle": [0.1] * 3, "heating": [1.2] * 3}
    sensor._heat_operation.sample_count = 6
    await sensor._save_pilot_store()
    await sensor._flush_pilot_store()
    restored_sensor = PumpSteerSensor(hass, entry)
    await restored_sensor._load_pilot_store()
    saved = restored_sensor._pilot_persisted["heat_operation"]
    assert saved["sources"] == ["sensor.test"]
    restored = HeatOperation()
    assert restored.restore(saved["samples"])
    assert restored.samples["heating"] == [1.2] * 3
    assert restored.fresh_labels == set()


async def test_native_options_menu_preserves_other_pages(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path
):
    from custom_components.pumpsteer import pump_log

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    await hass.async_start()
    for entity, value, attrs in [
        (
            "sensor.room",
            "20",
            {"unit_of_measurement": "°C", "device_class": "temperature"},
        ),
        (
            "sensor.outside",
            "5",
            {"unit_of_measurement": "°C", "device_class": "temperature"},
        ),
        ("sensor.price", "1", {"unit_of_measurement": "SEK/kWh"}),
    ]:
        hass.states.async_set(entity, value, attrs)
    entry = MockConfigEntry(
        domain="pumpsteer",
        title="test",
        data={},
        options={
            "indoor_temp_entity": "sensor.room",
            "real_outdoor_entity": "sensor.outside",
            "electricity_price_entity": "sensor.price",
            "price_tomorrow_entity": "sensor.price",
            "notification_level": "braking_only",
            "notification_cooldown_minutes": "60",
            "thermal_power_entity": "sensor.test_power",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "thermal"}
    )
    assert result["type"] == "form" and result["step_id"] == "thermal"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"thermal_intelligence_enabled": True, "external_heat_sources": True},
    )
    assert result["type"] == "create_entry"
    assert entry.options["notification_level"] == "braking_only"
    assert entry.options["notification_cooldown_minutes"] == "60"
    assert entry.options["indoor_temp_entity"] == "sensor.room"
    assert entry.options["thermal_intelligence_enabled"] is True
    for entity, value, attrs in [
        (
            "sensor.test_power",
            "0.5",
            {"unit_of_measurement": "kW", "device_class": "power"},
        ),
        (
            "sensor.supply",
            "35",
            {"unit_of_measurement": "°C", "device_class": "temperature"},
        ),
        (
            "sensor.return",
            "30",
            {"unit_of_measurement": "°C", "device_class": "temperature"},
        ),
        ("binary_sensor.heat", "on", {}),
        ("binary_sensor.compressor", "on", {}),
        ("binary_sensor.radiator", "on", {}),
    ]:
        hass.states.async_set(entity, value, attrs)
    for method in ("automatic", "manual", "verified"):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "telemetry"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "thermal_detection_method": method,
                "thermal_heat_activity_entity": "binary_sensor.heat",
                "thermal_compressor_entity": "binary_sensor.compressor",
                "thermal_supply_temp_entity": "sensor.supply",
                "thermal_return_temp_entity": "sensor.return",
                "thermal_heating_mode_entity": "binary_sensor.radiator",
                "thermal_heating_mode_inverted": False,
                "thermal_supply_threshold": 30,
                "thermal_power_entity": "sensor.test_power",
            },
        )
        assert result["type"] == "create_entry", result
        assert entry.options["thermal_detection_method"] == method
        assert entry.options["notification_level"] == "braking_only"
    hass.states.async_set("sensor.room", "unavailable")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "thermal"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"thermal_intelligence_enabled": False, "external_heat_sources": True},
    )
    assert result["type"] == "create_entry"
    assert entry.options["thermal_intelligence_enabled"] is False
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "telemetry"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"thermal_detection_method": "automatic"}
    )
    assert result["type"] == "create_entry"
    assert entry.options["thermal_power_entity"] == ""
    assert entry.options["notification_cooldown_minutes"] == "60"
    hass.states.async_set("number.test_output", "5")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == [
        "basic",
        "thermal",
        "telemetry",
        "zones",
        "notifications",
        "ohmigo",
    ]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "ohmigo"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"ohmigo_entity": "number.test_output", "ohmigo_interval_minutes": 5},
    )
    assert result["type"] == "create_entry"
    assert entry.options["ohmigo_entity"] == "number.test_output"
    assert entry.options["notification_cooldown_minutes"] == "60"


async def test_native_counter_reports_keep_async_deltas_until_shared_window(
    hass, freezer
):
    """Exercise real HA state events, including a stationary third meter."""
    from custom_components.pumpsteer.sensor import PumpSteerSensor

    freezer.move_to("2026-01-02T00:00:00+00:00")
    entry = MockConfigEntry(
        domain="pumpsteer",
        data={},
        options={
            "thermal_intelligence_enabled": True,
            "thermal_energy_entities": ["sensor.a", "sensor.b", "sensor.c"],
        },
    )
    entry.add_to_hass(hass)
    controller = PumpSteerSensor(hass, entry)
    controller._setup_power_telemetry()
    try:

        async def report(minute, name, value):
            freezer.move_to(f"2026-01-02T00:{minute:02d}:00+00:00")
            hass.states.async_set(
                "sensor." + name,
                value,
                {
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "unit_of_measurement": "kWh",
                },
            )
            await hass.async_block_till_done()

        for name in ("a", "b", "c"):
            await report(1, name, 10)
        await report(2, "a", 11)
        await report(3, "b", 11)
        assert controller._power_ledger.summary()["qualified_meter_energy_kwh"] == 2
        assert controller._power_ledger.energy == 0
        for name, value in (("a", 12), ("b", 12), ("c", 10)):
            await report(4, name, value)
        # Unchanged HA reports have no state_changed event: the existing cycle
        # must consume last_reported for the stationary meter.
        controller._consume_power_report(dt_util.now())
        result = controller._power_ledger.summary()
        assert result["energy_kwh_estimate"] == 4
        assert result["qualified_seconds"] == 180
        assert result["cost_estimate"] is None
    finally:
        controller._stop_power_telemetry()


async def test_native_basic_temperature_redundancy_options(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path
):
    """Real HA options selectors accept and retain optional sensors and weights."""
    from custom_components.pumpsteer import pump_log

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    await hass.async_start()
    for entity, value in (
        ("sensor.room", "20"),
        ("sensor.room2", "21"),
        ("sensor.outside", "5"),
        ("sensor.backup", "4"),
    ):
        hass.states.async_set(
            entity, value, {"unit_of_measurement": "°C", "device_class": "temperature"}
        )
    hass.states.async_set("sensor.price", "1", {"unit_of_measurement": "SEK/kWh"})
    entry = MockConfigEntry(
        domain="pumpsteer",
        title="test",
        data={},
        options={
            "indoor_temp_entity": "sensor.room",
            "real_outdoor_entity": "sensor.outside",
            "electricity_price_entity": "sensor.price",
            "price_tomorrow_entity": "sensor.price",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    menu = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        menu["flow_id"], {"next_step_id": "basic"}
    )
    assert form["type"] == "form"
    saved = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "indoor_temp_entity": "sensor.room",
            "indoor_temp_entity_2": "sensor.room2",
            "indoor_temp_weight_1": 5,
            "indoor_temp_weight_2": 3,
            "indoor_temp_weight_3": 1,
            "real_outdoor_entity": "sensor.outside",
            "outdoor_temp_entity_2": "sensor.backup",
            "electricity_price_entity": "sensor.price",
            "price_tomorrow_entity": "sensor.price",
            "temperature_report_max_age_minutes": 360,
            "outdoor_temperature_report_max_age_minutes": 75,
        },
    )
    assert saved["type"] == "create_entry", saved
    assert entry.options["indoor_temp_entity_2"] == "sensor.room2"
    assert entry.options["indoor_temp_weight_2"] == 3
    assert entry.options["outdoor_temp_entity_2"] == "sensor.backup"
    assert entry.options["outdoor_temperature_report_max_age_minutes"] == 75


async def test_native_zone_options_and_passive_notifications(
    recorder_mock, hass, enable_custom_integrations, monkeypatch, tmp_path, freezer
):
    """Real options flow, last_reported, service dispatch and RestoreEntity data."""
    from custom_components.pumpsteer import pump_log

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(tmp_path / "pump.log"))
    freezer.move_to("2026-10-31T00:00:00+00:00")
    await hass.async_start()
    for name, value in (("room", "21"), ("outdoor", "0"), ("zone", "4")):
        hass.states.async_set(
            "sensor." + name,
            value,
            {"unit_of_measurement": "°C", "device_class": "temperature"},
        )
    hass.states.async_set("sensor.price", "1", {"unit_of_measurement": "SEK/kWh"})
    hass.states.async_set(
        "climate.zone", "heat", {"temperature": 12, "hvac_action": "heating"}
    )
    received = []
    attempts = []

    async def notify(call):
        attempts.append(call.data["message"])
        if len(attempts) == 1:
            raise RuntimeError("Temporary notification failure")
        received.append(call.data["message"])

    hass.services.async_register("notify", "zone_test", notify)
    entry = MockConfigEntry(
        domain="pumpsteer",
        data={},
        options={
            "indoor_temp_entity": "sensor.room",
            "real_outdoor_entity": "sensor.outdoor",
            "electricity_price_entity": "sensor.price",
            "price_tomorrow_entity": "sensor.price",
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    form = await hass.config_entries.options.async_init(entry.entry_id)
    form = await hass.config_entries.options.async_configure(
        form["flow_id"], {"next_step_id": "zones"}
    )
    saved = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {
            "zone_1_entity": "sensor.zone",
            "zone_1_name": "Garage",
            "zone_1_enabled": True,
            "zone_1_threshold": 5,
            "zone_1_profile": "maintenance",
            "zone_1_comfort_threshold": 18,
            "zone_1_climate": "climate.zone",
            "zone_1_alarm_delay_minutes": 2,
            "zone_1_notify_service": "notify.zone_test",
        },
    )
    assert saved["type"] == "create_entry", saved
    await hass.async_block_till_done()
    assert entry.options["zone_1_alarm_delay_minutes"] == 2
    assert entry.options["indoor_temp_entity"] == "sensor.room"
    controller = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    service_calls = []
    hass.bus.async_listen(
        "call_service", lambda event: service_calls.append(event.data["domain"])
    )
    for minute in range(4):
        freezer.move_to(f"2026-10-31T00:0{minute}:00+00:00")
        hass.states.async_set("sensor.zone", "4", {"unit_of_measurement": "°C"})
        await controller.async_update()
        await hass.async_block_till_done()
    assert len(attempts) == 2
    assert len(received) == 1
    assert "Temperaturlarm" in received[0]
    controller.async_write_ha_state()
    await hass.async_block_till_done()
    status = next(
        e
        for e in hass.data[SENSOR].entities
        if e.unique_id == f"{entry.entry_id}_thermal_intelligence"
    )
    assert hass.states.get(status.entity_id).attributes["extra_temperature_zones"]["1"][
        "events"
    ]["temperature"]
    persisted = controller.extra_restore_state_data.as_dict()
    assert persisted["extra_temperature_zones"]["1"]["notifications_sent"][
        "temperature"
    ]
    # Full reload exercises RestoreEntity's real extra-data cache, even when
    # the main temperature is unavailable and HA suppresses its attributes.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    resumed = next(
        e for e in hass.data[SENSOR].entities if e.unique_id == entry.entry_id
    )
    await resumed.async_update()
    await hass.async_block_till_done()
    assert len(received) == 1
    freezer.move_to("2026-10-31T01:10:00+00:00")
    await resumed.async_update()
    await hass.async_block_till_done()
    assert len(received) == 2
    assert "Givarfel" in received[-1]
    hass.states.async_set("sensor.zone", "5.2", {"unit_of_measurement": "°C"})
    await resumed.async_update()
    await hass.async_block_till_done()
    assert len(received) == 2  # Hysteresis: the temperature alarm still holds.
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.zone", "5.5", {"unit_of_measurement": "°C"})
    await resumed.async_update()
    await hass.async_block_till_done()
    await resumed.async_update()
    await hass.async_block_till_done()
    assert len(received) == 3
    assert "Återhämtning" in received[-1]
    assert (
        resumed.extra_state_attributes["extra_temperature_zones"]["1"]["temperature"]
        == 5.5
    )
    assert set(service_calls) == {"notify"}


async def test_native_zone_forecast_and_heat_response(hass, freezer):
    """Actual repeated HA reports feed forecast and demand-response events."""
    from custom_components.pumpsteer.sensor import PumpSteerSensor

    freezer.move_to("2026-10-31T00:00:00+00:00")
    await hass.async_start()
    received = []

    async def notify(call):
        received.append(call.data["message"])

    hass.services.async_register("notify", "zones", notify)
    entry = MockConfigEntry(
        domain="pumpsteer",
        data={},
        options={
            "zone_1_entity": "sensor.zone",
            "zone_1_enabled": True,
            "zone_1_threshold": 5,
            "zone_1_climate": "climate.zone",
            "zone_notify_service": "notify.zones",
            "real_outdoor_entity": "sensor.outdoor",
        },
    )
    entry.add_to_hass(hass)
    controller = PumpSteerSensor(hass, entry)
    start = dt_util.utcnow()
    for minute in range(77):
        freezer.move_to(start + timedelta(minutes=minute))
        hass.states.async_set(
            "sensor.zone", str(7 - minute / 30), {"unit_of_measurement": "°C"}
        )
        hass.states.async_set(
            "sensor.outdoor", str(-minute / 30), {"unit_of_measurement": "°C"}
        )
        # Attributes may include vendor extensions, but no vendor is assumed.
        hass.states.async_set(
            "climate.zone", "heat", {"temperature": 12, "valve_position": 60}
        )
        await controller.async_update()
        await hass.async_block_till_done()
        if minute == 30:
            zone = controller.extra_state_attributes["extra_temperature_zones"]["1"]
            assert zone["events"]["forecast"]
            assert zone["relative_trend_c_per_hour"] == pytest.approx(0)
    assert len(received) == 3
    assert "Förvarning" in received[0]
    assert "Misstänkt värmebrist" in received[1]
    assert "Temperaturlarm" in received[2]
    zone = controller.extra_state_attributes["extra_temperature_zones"]["1"]
    assert zone["temperature"] == pytest.approx(4.4666666667)
    assert zone["heat_response"] == "suspected_insufficient"
    assert zone["heat_response_c_per_hour"] == pytest.approx(-2)
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("sensor.zone", "8", {"unit_of_measurement": "°C"})
    await controller.async_update()
    await hass.async_block_till_done()
    await controller.async_update()
    await hass.async_block_till_done()
    assert len(received) == 4
    assert "Återhämtning" in received[-1]
    assert controller._pilot is None


@pytest.mark.parametrize("interval", [10, 15, 30])
async def test_native_sparse_zone_reports(hass, freezer, interval):
    """Real HA last_reported timestamps qualify separate low readings."""
    from custom_components.pumpsteer.sensor import PumpSteerSensor

    freezer.move_to("2026-10-31T00:00:00+00:00")
    await hass.async_start()
    entry = MockConfigEntry(
        domain="pumpsteer",
        data={},
        options={
            "zone_1_entity": "sensor.zone",
            "zone_1_enabled": True,
            "zone_1_threshold": 5,
        },
    )
    entry.add_to_hass(hass)
    controller = PumpSteerSensor(hass, entry)
    start = dt_util.utcnow()
    for minute in range(0, 31, interval):
        freezer.move_to(start + timedelta(minutes=minute))
        hass.states.async_set("sensor.zone", "4", {"unit_of_measurement": "°C"})
        await controller.async_update()
    zone = controller.extra_state_attributes["extra_temperature_zones"]["1"]
    assert zone["alarm_active"]
    assert controller._pilot is None
