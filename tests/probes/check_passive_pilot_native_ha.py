"""Replay synthetic readings through HA Core and memory-only output receivers.

Run with ``python -B tests/probes/check_passive_pilot_native_ha.py``.
Each replay uses a temporary HA directory and runs without a server.
"""

import asyncio
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from json import dumps
from math import expm1, isclose
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from custom_components.pumpsteer.sensor import PumpSteerSensor  # noqa: E402

START = datetime(2026, 11, 1, tzinfo=UTC)
ROOM, OUTDOOR = "sensor.replay_indoor", "sensor.replay_outdoor"
RADIATOR, WATER = "binary_sensor.replay_radiator", "binary_sensor.replay_water"
DUTY, LOAD = "binary_sensor.replay_space_heat", "sensor.replay_space_heat_percent"


def instant(minutes, seconds=0):
    return START + timedelta(minutes=minutes, seconds=seconds)


async def prices(cfg, now):
    return [1.0] * 24, ["normal"] * 24, 60, 0


async def weather_forecast():
    return []


async def replay(folder, inverter):
    hass = HomeAssistant(folder)
    hass.set_state(CoreState.running)
    calls = []

    async def receiver(call):
        calls.append((call.domain, call.service, dict(call.data)))

    hass.services.async_register("number", "set_value", receiver)
    hass.services.async_register("replay_sink", "set_value", receiver)
    options = dict(
        indoor_temp_entity=ROOM,
        real_outdoor_entity=OUTDOOR,
        electricity_price_entity="sensor.replay_price",
        thermal_detection_method="verified",
        thermal_heat_activity_entity=LOAD if inverter else DUTY,
        thermal_heating_mode_entity=RADIATOR,
        thermal_hot_water_mode_entity=WATER,
        adaptive_braking_enabled=False,
        aggressiveness=0,
        target_temperature=21,
        summer_threshold=18,
        house_inertia=2,
        ohmigo_entity="number.replay_sink",
        ohmigo_interval_minutes=5,
        modbus_service="replay_sink.set_value",
        modbus_payload_template="{{ {'value': fake_temp} }}",
        modbus_interval_minutes=7,
    )

    def controller(label, collect):
        entry = SimpleNamespace(
            entry_id=label,
            data={},
            options={**options, "thermal_intelligence_enabled": collect},
        )
        sensor = PumpSteerSensor(hass, entry)
        sensor._get_prices = prices
        sensor._forecast_temps = weather_forecast
        return sensor

    baseline = controller(f"{inverter}_base", False)
    candidate = controller(f"{inverter}_pilot", True)
    runtime = candidate._thermal_intelligence
    audit = candidate._plan_audit
    frozen = {}
    pairs = defaultdict(list)
    observed = {}
    indoor = 20.4
    clock = instant(0)
    prior_load = None
    prior_mode = None
    missing_checked = set()
    outputs = 0

    def publish(entity, value, t, attrs=None):
        hass.states.async_set(entity, str(value), attrs, timestamp=t.timestamp())

    # There are no helper entities in this simulation.
    empty_registry = SimpleNamespace(async_get_entity_id=lambda *_: None)
    with (
        patch.object(dt_util, "now", side_effect=lambda: clock),
        patch.object(er, "async_get", return_value=empty_registry),
    ):
        for minute in range(60 * 52):
            hour, phase = divmod(minute, 60)
            outdoor = (2.5, 5.5, 4.0, 8.0, 6.5)[hour % 5]
            if phase == 0:
                publish(OUTDOOR, outdoor, instant(minute, 2), {"unit_of_measurement": "°C"})
            water = 230 <= minute < 254
            missing = 366 <= minute < 388
            old_mode = 550 <= minute < 565
            operation = (water, old_mode)
            if minute % 2 == 0 or operation != prior_mode:
                meta = {"measurement_time": instant(minute, 6).isoformat()}
                publish(RADIATOR, "off" if water else "on", instant(minute, 7), meta)
                water_time = instant(minute, 6)
                if old_mode:
                    water_time -= timedelta(minutes=8)
                publish(
                    WATER,
                    "on" if water else "off",
                    instant(minute, 8),
                    {"measurement_time": water_time.isoformat()},
                )
                prior_mode = operation
            if inverter:
                load = 0 if 803 <= minute < 824 else (0.22, 0.76, 0.38, 0.88)[hour % 4]
            else:
                load = float(phase < (44, 16, 50, 10)[hour % 4])
            if inverter:
                state = load * 100
            elif water:
                state = "unavailable"
            else:
                state = "on" if load else "off"
            if not missing and (minute % 3 == 0 or state != prior_load):
                load_attributes = (
                    {
                        "unit_of_measurement": "%",
                        "measurement_time": instant(minute, 11).isoformat(),
                    }
                    if inverter
                    else {}
                )
                publish(LOAD if inverter else DUTY, state, instant(minute, 12), load_attributes)
                prior_load = state
            if missing and inverter and minute % 3 == 0:
                # Fresh HA receipt of a value with an unknown sample time.
                publish(LOAD, load * 100, instant(minute, 12), {"unit_of_measurement": "%"})
            if minute % 4 == 0:
                reported = instant(minute, 35)
                measured_indoor = round(indoor * 20) / 20
                publish(ROOM, measured_indoor, reported, {"unit_of_measurement": "°C"})
                observed[reported.isoformat()] = measured_indoor

            clock = instant(minute, 48)
            calls.clear()
            await baseline.async_update()
            await hass.async_block_till_done()
            base_calls = calls[:]
            calls.clear()
            await candidate.async_update()
            await hass.async_block_till_done()
            if candidate._thermal_fit_task is not None:
                await candidate._thermal_fit_task
            assert calls == base_calls, (minute, base_calls, calls)
            outputs += len(calls)
            assert (candidate.state, candidate._attributes["mode"]) == (
                baseline.state, baseline._attributes["mode"]
            )
            assert candidate._attributes["mode"] == "normal"
            assert candidate._attributes["adaptive_braking"]["multiplier"] == 1
            if water or old_mode or (missing and minute >= 373):
                assert not runtime.pilot_inputs_valid(clock), (minute, inverter)
                missing_checked.add("water" if water else "old_mode" if old_mode else "unknown")
            if minute == 810:
                assert runtime.pilot_inputs_valid(clock)
            pending = audit.fixed_pending
            if pending is not None and pending["revision"].startswith("pilot:"):
                issued = pending["issued"].isoformat()
                if issued not in frozen:
                    fit_end = float(pending["revision"].split(":")[1])
                    assert fit_end <= datetime.fromisoformat(pending["source_reported"]).timestamp()
                    assert datetime.fromisoformat(pending["source_reported"]) <= pending["issued"]
                    frozen[issued] = deepcopy(pending["curves"])
            for row in audit.fixed_records:
                if row["issued"] not in frozen or row["horizon"] in pairs[row["issued"]]:
                    continue
                assert row["observed"] in observed and row["actual"] == observed[row["observed"]]
                assert row["source_reported"] <= row["issued"] < row["observed"]
                elapsed = datetime.fromisoformat(row["observed"]) - datetime.fromisoformat(row["issued"])
                assert isclose(row["elapsed_minutes"], elapsed.total_seconds() / 60, abs_tol=1e-9)
                assert row["curves"] == frozen[row["issued"]]
                assert row["predictions"][1] == row["initial"]
                curve = frozen[row["issued"]][0]
                h = row["elapsed_minutes"] / 60
                response = -expm1(-curve["loss"] * h)
                expected = row["initial"] + response * (
                    row["outdoor"] - row["initial"] + curve["input"] / curve["loss"]
                )
                assert isclose(row["predictions"][0], expected, abs_tol=1e-9)
                pairs[row["issued"]].append(row["horizon"])
            indoor += (0.045 * (outdoor - indoor) + 1.15 * (0 if water else load) + 0.12) / 60

    summary = runtime.summary()
    history = runtime.export_state()["pilot_house"]
    with patch.object(dt_util, "now", return_value=clock):
        attrs = candidate.extra_state_attributes
    assert attrs["thermal_intelligence_learning_intervals"] == len(history["intervals"])
    assert attrs["thermal_intelligence_status"] == "learning"
    assert missing_checked == {"water", "unknown", "old_mode"}
    assert summary["active_control"] is False and summary["fit_trusted"] is False
    assert len(history["intervals"]) >= 24 and frozen
    assert [30, 60, 120] in pairs.values()
    for row in history["intervals"]:
        end = datetime.fromtimestamp(row["end"], UTC)
        start = end - timedelta(hours=row["hours"])
        for first, last in ((230, 254), (373, 388), (550, 565)):
            assert end <= instant(first) or start >= instant(last)
    print(dumps({
        "core": version("homeassistant"), "scenario": "inverter" if inverter else "on-off",
        "synthetic_hours": 52, "intervals": len(history["intervals"]),
        "fit": summary["last_fit_status"], "matching_outputs": outputs,
        "issued_pilot": len(frozen),
        "scored_horizons": dict(sorted(Counter(h for hs in pairs.values() for h in hs).items())),
        "blocked_contexts": sorted(missing_checked),
    }), flush=True)
    candidate._stop_pilot_input_listener()


async def main():
    assert version("homeassistant") == "2026.11.2"
    for inverter in (False, True):
        with TemporaryDirectory(prefix="pilot-offline-core-") as folder:
            await replay(folder, inverter)


if __name__ == "__main__":
    asyncio.run(main())
