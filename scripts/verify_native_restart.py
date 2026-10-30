"""Run in separate HA pytest processes with a shared, temporary config dir.

Set PUMPSTEER_RESTART_DIR to an empty directory under /tmp and run this file
first with PUMPSTEER_RESTART_STAGE=create and then with stage=restore.
Each invocation starts and stops a real isolated HA core; all inputs are
synthetic. A separate copy with an invalid model can be tested as stage=corrupt.
"""

import os
import json
import logging
from pathlib import Path
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.setup import async_setup_component
from homeassistant.components.sensor import DOMAIN as SENSOR
from homeassistant.util import dt as dt_util

pytestmark = pytest.mark.asyncio
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
DIR = Path(os.environ["PUMPSTEER_RESTART_DIR"]).resolve()
ID = "01H00000000000000000000000"
OFF_ID = "01H00000000000000000000001"


@pytest.fixture(autouse=True)
def local_component_namespace(monkeypatch):
    """Point the HA test plugin's component namespace at this checkout."""
    import custom_components

    source = str(Path(__file__).resolve().parents[1] / "custom_components")
    monkeypatch.setattr(
        custom_components, "__path__", [source, *custom_components.__path__]
    )


@pytest.fixture
def hass_storage():
    # Use actual Store files, not the HA pytest plugin's in-memory replacement.
    return {}


@pytest.fixture
def hass_config_dir():
    if not DIR.is_relative_to(Path("/tmp")) or DIR == Path("/tmp"):
        raise ValueError("Restart verification requires a dedicated /tmp directory")
    custom = DIR / "custom_components"
    custom.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / "custom_components/pumpsteer"
    link = custom / "pumpsteer"
    if not link.exists():
        link.symlink_to(source)
    if not link.is_symlink() or link.resolve() != source.resolve():
        raise ValueError("The isolated config must point to this checkout")
    return str(DIR)


async def test_restart(
    recorder_mock, hass, hass_config_dir, enable_custom_integrations, monkeypatch
):
    # The installed HA pytest plugin does not consume hass_config_dir itself.
    hass.config.config_dir = hass_config_dir
    from custom_components.pumpsteer import pump_log
    from custom_components.pumpsteer.pilot_vendor.learning_interval import (
        LearningInterval,
    )

    monkeypatch.setattr(pump_log, "_PUMP_LOG_PATH", str(DIR / "pump.log"))
    stage = os.environ["PUMPSTEER_RESTART_STAGE"]
    if stage == "create":
        e = MockConfigEntry(
            entry_id=ID,
            domain="pumpsteer",
            title="Synthetic test",
            data={},
            options={
                "indoor_temp_entity": "sensor.indoor",
                "real_outdoor_entity": "sensor.outdoor",
                "electricity_price_entity": "sensor.price",
                "thermal_heat_activity_entity": "binary_sensor.heat",
                "thermal_intelligence_enabled": True,
                "external_heat_sources": True,
                "aggressiveness": 0,
            },
        )
        e.add_to_hass(hass)
        off = MockConfigEntry(
            entry_id=OFF_ID,
            domain="pumpsteer",
            title="Synthetic off",
            data={},
            options={
                **dict(e.options),
                "thermal_intelligence_enabled": False,
                "external_heat_sources": False,
            },
        )
        off.add_to_hass(hass)
    else:
        await hass.config_entries.async_initialize()
        e = hass.config_entries.async_get_entry(ID)
        assert e is not None
        assert e.options["thermal_intelligence_enabled"] is True
        assert e.options["external_heat_sources"] is True
        off = hass.config_entries.async_get_entry(OFF_ID)
        assert off is not None
        assert off.options["thermal_intelligence_enabled"] is False
        assert off.options["external_heat_sources"] is False
    await hass.async_start()
    await hass.config.async_set_time_zone("UTC")
    assert await async_setup_component(hass, "pumpsteer", {})
    await hass.async_block_till_done()
    entity = next(x for x in hass.data[SENSOR].entities if x.unique_id == ID)
    assert entity.extra_state_attributes["mode"] == "safe_mode"
    if stage in ("restore", "corrupt"):
        assert entity._plan_audit_loaded
        assert not entity._plan_audit.plan_pending
        assert entity._plan_audit.plan_unverifiable >= 3
        assert entity._pilot_plan["planning_status"] == "unavailable"
    now = dt_util.utcnow()
    hass.states.async_set("sensor.indoor", "20.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.outdoor", "5", {"unit_of_measurement": "°C"})
    hass.states.async_set("binary_sensor.heat", "on")
    hass.states.async_set(
        "sensor.price", "1", {"today": [1.0] * 24, "unit_of_measurement": "SEK/kWh"}
    )
    await hass.async_block_till_done()
    await entity.async_update()
    assert entity.extra_state_attributes["mode"] == "normal"
    assert entity._pilot_plan["reason"] != "planning_error"
    assert entity._pilot is not None
    if stage == "create":
        sample = LearningInterval(now.timestamp() - 60, 1, 20, 20.1, 5, 0.3, None, 1)
        entity._pilot.manager.model.add_interval(sample)
        assert len(entity._pilot.manager.model.history) == 1
        entity._pilot.house_intervals = 1
        # A synthetic previously ready pump model must require fresh validation
        # after the second HA process starts.
        pump = entity._pilot.manager.pump
        pump.history = [(now.timestamp() - 30, 0.4, 0.3, 5.0)]
        pump.validated_at = now.timestamp() - 30
        pump.ready = True
        assert entity._plan_audit.issue_plan(
            forecast_start=now,
            issued_at=now,
            temperatures=[20.5] * 9,
            revision="synthetic",
            status="hypothetical_unvalidated",
            source_entity="sensor.indoor",
            source_identity=(None, None, "sensor.outdoor"),
            source_reported=now,
        )
        await entity._save_pilot_store()
        await entity._pilot_store._async_handle_write_data()
        hass.config_entries.async_update_entry(e, title="Synthetic saved")
        await hass.config_entries._store._async_handle_write_data()
        assert Path(entity._pilot_store.path).exists()
        stored = json.loads(Path(entity._pilot_store.path).read_text())["data"]
        assert stored["pump"]["validated_at"] is not None
        assert len(stored["pump"]["history"]) == 1
        assert stored["plan_audit"]["pending_count"] > 0
    else:
        assert len(entity._pilot.manager.model.history) == (
            1 if stage == "restore" else 0
        )
        assert not entity._pilot.manager.pump.ready
        assert entity._pilot.manager.pump.fresh_samples == 0
        if stage == "restore":
            assert len(entity._pilot.manager.pump.history) == 1
        assert (
            entity._pilot.manager.house.start is None
            or entity._pilot.manager.house.start >= now.timestamp() - 2
        )
    print(
        "RESTART_STAGE",
        stage,
        "PID",
        os.getpid(),
        "HISTORY",
        len(entity._pilot.manager.model.history),
        "READY",
        entity._pilot.manager.pump.ready,
        "UNVERIFIABLE",
        entity._plan_audit.plan_unverifiable,
        flush=True,
    )
