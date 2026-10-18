"""Standalone real-Core lifecycle probe; no conftest/stubs or external network.

Run with the existing HA test interpreter: python -B tests/thermal_core_probe.py.
Time, temperatures, price categories and receiver are synthetic. Core startup,
integration/template reload, SQLite and restore storage are real.
"""

import asyncio
import json
import logging
from pathlib import Path
import shutil
import socket
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.const import __version__
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.pumpsteer.sensor import PumpSteerSensor

ROOT = Path(__file__).resolve().parents[1]


async def main():
    (ROOT / ".local-audit").mkdir(exist_ok=True)
    clock = [datetime(2026, 10, 18, 8, tzinfo=timezone.utc)]
    braking = [True]
    calls = []
    target_errors = []

    class TargetErrors(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                target_errors.append(record.getMessage())

    logging.getLogger("custom_components.pumpsteer").addHandler(TargetErrors())
    hass = None

    async def prices(self, cfg, now):
        return [1.0] * 96, [("expensive" if braking[0] else "normal")] * 96, 15, 0

    async def forecast(self):
        return []

    def offline(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("External network attempted by isolated probe")
        return connect(sock, address)

    connect = socket.socket.connect
    with tempfile.TemporaryDirectory(
        dir=ROOT / ".local-audit", prefix="native-"
    ) as temp:
        directory = Path(temp)
        shutil.copytree(
            ROOT / "custom_components/pumpsteer",
            directory / "custom_components/pumpsteer",
        )
        (directory / "configuration.yaml").write_text("template: []\n")

        async def boot(*, restored=False):
            nonlocal hass
            hass = HomeAssistant(str(directory))
            hass.config.skip_pip = True
            loader.async_setup(hass)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            assert (
                await bootstrap.async_from_config_dict(
                    {
                        "homeassistant": {
                            "latitude": 0,
                            "longitude": 0,
                            "elevation": 0,
                            "time_zone": "Europe/Stockholm",
                        },
                        "http": {"server_host": "127.0.0.1", "server_port": port},
                        "template": [],
                        "recorder": {
                            "db_url": "sqlite:///" + str(directory / "test.db"),
                            "commit_interval": 0,
                        },
                    },
                    hass,
                )
                is hass
            )
            entry = hass.config_entries.async_get_entry("thermal-native")
            if entry is None:
                entry = MockConfigEntry(
                    domain="pumpsteer",
                    entry_id="thermal-native",
                    version=1,
                    data={},
                    options={
                        "indoor_temp_entity": "sensor.inside",
                        "real_outdoor_entity": "sensor.outside",
                        "electricity_price_entity": "sensor.price",
                        "target_temperature": 21,
                        "aggressiveness": 3,
                        "house_inertia": 2,
                        "summer_threshold": 18,
                        "ohmigo_entity": "number.receiver",
                        "ohmigo_interval_minutes": 5,
                        "preheat_boost_enabled": False,
                    },
                )
                entry.add_to_hass(hass)
                assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_start()
            await hass.async_block_till_done()

            async def receive(call):
                calls.append({"utc": clock[0].isoformat(), "value": call.data["value"]})

            hass.services.async_register("number", "set_value", receive)
            registry = er.async_get(hass)
            push = registry.async_get_entity_id(
                "switch", "pumpsteer", entry.entry_id + "_ohmigo_enabled"
            )
            if push:
                hass.states.async_set(push, "on")
            entity = next(
                e
                for e in hass.data["sensor"].entities
                if isinstance(e, PumpSteerSensor)
            )
            if restored:
                hass.states.async_set(
                    "sensor.inside",
                    "20.7",
                    {"unit_of_measurement": "°C", "restored": True},
                )
                hass.states.async_set(
                    "sensor.outside",
                    "5",
                    {"unit_of_measurement": "°C", "restored": True},
                )
                await entity.async_update()
                assert entity._thermal_model.pending_samples == 0
            return entity, entry

        async def sample(entity, temperature):
            clock[0] += timedelta(minutes=1)
            hass.states.async_set(
                "sensor.inside", str(temperature), {"unit_of_measurement": "°C"}
            )
            hass.states.async_set("sensor.outside", "5", {"unit_of_measurement": "°C"})
            await entity.async_update()
            entity.async_write_ha_state()
            await hass.async_block_till_done()

        try:
            with (
                patch.object(dt_util, "now", side_effect=lambda: clock[0]),
                patch.object(dt_util, "utcnow", side_effect=lambda: clock[0]),
                patch(
                    "homeassistant.core.time.time",
                    side_effect=lambda: clock[0].timestamp(),
                ),
                patch.object(PumpSteerSensor, "_get_prices", prices),
                patch.object(PumpSteerSensor, "_forecast_temps", forecast),
                patch.object(socket.socket, "connect", offline),
                patch(
                    "homeassistant.components.network.async_get_loaded_adapters",
                    return_value=[],
                ),
            ):
                entity, entry = await boot()
                for minute in range(30):
                    await sample(entity, 21.3 - minute * 0.01)
                assert entity._thermal_model.pending_samples >= 20, (
                    entity.extra_state_attributes
                )
                braking[0] = False
                for minute in range(40):
                    await sample(entity, 21 - minute * 0.001)
                model = entity._thermal_model
                assert model.is_valid and model.sample_count >= 20, (
                    entity.extra_state_attributes
                )
                fitted = (model.k, model.sample_count)
                braking[0] = True
                for minute in range(32):
                    await sample(entity, 20.95 - minute * 0.001)
                assert (
                    entity.extra_state_attributes["thermal_diagnostics"]["quality"][
                        "30"
                    ]["count"]
                    >= 1
                )
                # Exercise real HA options/state reception for the voluntary marker.
                hass.states.async_set("input_boolean.external_heat", "on")
                hass.config_entries.async_update_entry(
                    entry,
                    options={
                        **entry.options,
                        "external_heat_entity": "input_boolean.external_heat",
                    },
                )
                await hass.async_block_till_done()
                await sample(entity, 20.9)
                assert model.pending_samples == 0
                assert entity._thermal_observer.diagnostics.pending_count == 0
                hass.states.async_set("input_boolean.external_heat", "unknown")
                await sample(entity, 20.89)
                assert model.pending_samples == 0
                hass.states.async_set("input_boolean.external_heat", "off")
                await entity.async_update()
                assert not model._temp_history
                await sample(entity, 20.88)
                assert len(model._temp_history) == 1
                assert (model.k, model.sample_count) == fitted
                options = dict(entry.options)
                options.pop("external_heat_entity")
                hass.config_entries.async_update_entry(entry, options=options)
                await hass.async_block_till_done()
                await hass.services.async_call("template", "reload", blocking=True)
                await hass.async_block_till_done()
                assert (model.k, model.sample_count) == fitted
                assert model.pending_samples == 0
                assert entity._thermal_observer.diagnostics.pending_count == 0
                await entity.async_update()
                assert model.pending_samples == 0
                assert await hass.config_entries.async_reload(entry.entry_id)
                await hass.async_block_till_done()
                entity = next(
                    e
                    for e in hass.data["sensor"].entities
                    if isinstance(e, PumpSteerSensor)
                )
                assert (
                    entity._thermal_model.k,
                    entity._thermal_model.sample_count,
                ) == fitted
                assert entity._thermal_model.pending_samples == 0
                # Valid parameters survive even a safe-mode attribute snapshot.
                entity._attributes = {
                    "mode": "safe_mode",
                    "status": "missing_temperature",
                }
                entity.async_write_ha_state()
                await hass.async_stop()
                saved = json.loads(
                    (directory / ".storage/core.restore_state").read_text()
                )
                stored = next(
                    x
                    for x in saved["data"]
                    if x["state"]["entity_id"] == entity.entity_id
                )
                assert stored["extra_data"]["thermal_model"]["k"] == fitted[0]
                clock[0] += timedelta(seconds=30)
                entity, entry = await boot(restored=True)
                assert (
                    entity._thermal_model.k,
                    entity._thermal_model.sample_count,
                ) == fitted
                assert entity._thermal_model.pending_samples == 0
                await hass.async_stop()
                clock[0] += timedelta(days=4)
                entity, entry = await boot(restored=True)
                assert (
                    entity._thermal_model.k,
                    entity._thermal_model.sample_count,
                ) == fitted
                assert entity._thermal_model.pending_samples == 0
                await sample(entity, 21)
                assert entity._thermal_model.pending_samples == 0
                assert calls, (
                    "Exercise actual Ohmigo code against the simulated service"
                )
                assert not target_errors, target_errors
                print(
                    json.dumps(
                        {
                            "core": __version__,
                            "external_heat_options_and_recovery": True,
                            "actual_integration_reload": True,
                            "actual_template_reload": True,
                            "normal_disk_restart": True,
                            "disk_restart_after_days": 4,
                            "target_errors": target_errors,
                            "restored_fit_count": fitted[1],
                            "restored_k": fitted[0],
                            "receiver_dispatches": len(calls),
                            "pending_measurements_after_restart": 0,
                            "status": "PASS",
                        }
                    )
                )
        finally:
            if hass:
                await hass.async_stop(force=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    asyncio.run(main())
