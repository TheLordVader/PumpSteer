"""Offline only: external MIT RoomMind physics → existing PumpSteer collector.

No HA connection, service calls, fitting or private inputs. Simulator truth is
returned separately and is never passed as measured heat or physical timestamps.
"""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from custom_components.pumpsteer.thermal_intelligence.runtime import ThermalIntelligence

REVISION = "d374baa7ba7d7adc0e6e9beec03ae7e9d3c91119"


def verify_upstream(root):
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
        check=False,
        capture_output=True,
        text=True,
    )
    if (
        result.returncode
        or result.stdout.strip() != REVISION
        or dirty.returncode
        or dirty.stdout.strip()
    ):
        raise ValueError("RoomMind must be a clean checkout of the documented revision")


def replay(building, emitter, *, fault=None):
    """Six synthetic hours; known disturbance in hours 2–3, no learned trust."""
    verify_upstream(Path(os.environ["PUMPSTEER_ROOMMIND_ROOT"]))
    from roommind_sim.scenario.loader import load_scenario_dict
    from roommind_sim.world import World

    scenario = load_scenario_dict(
        {
            "name": "pumpsteer-synthetic",
            "start": "2026-01-12T00:00",
            "duration": "6h",
            "weather": {"profile": "temperate"},
            "house": {
                "building": building,
                "rooms": {
                    str(i): {
                        "name": f"Synthetic {i}",
                        "floor_area_m2": 25,
                        "devices": [
                            {"entity_id": f"climate.synthetic_{i}", "profile": emitter}
                        ],
                        "sensors": {
                            "temperature": {
                                "entity_id": f"sensor.synthetic_{i}",
                                "noise": 0.0,
                                "time_constant_s": 600
                                if fault == "response_delay"
                                else 0,
                                "resolution": 0.1,
                                "report": {
                                    "min_interval_s": 120 + i * 60,
                                    "min_delta": 0.1,
                                    "heartbeat_s": 120 + i * 60,
                                },
                            }
                        },
                    }
                    for i in range(3)
                },
            },
        }
    )
    world = World(scenario)
    runtime = ThermalIntelligence()
    reports = changes = 0
    values = []
    truth = []
    previous = {}
    for elapsed in range(0, 21600, 10):
        now = scenario.start + elapsed
        at = datetime.fromtimestamp(now, UTC)
        interrupted = 7200 <= elapsed < 10800
        for dev in world.devices.values():
            dev.hvac_mode = (
                "off"
                if interrupted and fault in ("heat_off", "dhw", "undetected_heat_off")
                else "heat"
            )
            dev.target = 22.0 if elapsed < 14400 else 19.0
        world.step(0 if elapsed == 0 else 10, now)
        truth.append(
            {
                "elapsed": elapsed,
                "synthetic": True,
                "space_heat_requested": any(
                    d.hvac_mode == "heat" for d in world.devices.values()
                ),
                "device_active": any(d.active for d in world.devices.values()),
                "delivered_w": sum(
                    z.last_gains.conv_w + z.last_gains.rad_w
                    for z in world.zones.values()
                ),
            }
        )
        if fault == "restart" and elapsed == 10800:
            restored = ThermalIntelligence()
            restored.restore_state(runtime.export_state())
            runtime = restored
        if fault == "history_gap" and interrupted:
            continue
        if fault in ("heat_off", "dhw") and interrupted:
            runtime.suspend("synthetic_" + fault, at)
            continue
        if elapsed % 30:
            continue
        sources = []
        for i, sensor in enumerate(world.sensors.values()):
            if sensor.value is None:
                continue
            if previous.get(sensor.entity_id) != sensor.last_report:
                reports += 1
                values.append(sensor.value)
                if previous.get((sensor.entity_id, "value")) != sensor.value:
                    changes += 1
                previous[sensor.entity_id] = sensor.last_report
                previous[(sensor.entity_id, "value")] = sensor.value
            sources.append(
                {
                    "entity": sensor.entity_id,
                    "weight": (0.7, 0.2, 0.1)[i],
                    "value": None
                    if fault == "sensor_loss" and interrupted and i == 0
                    else sensor.value,
                    "reported_at": datetime.fromtimestamp(
                        sensor.last_report, UTC
                    ).isoformat(),
                }
            )
        if len(sources) != 3:
            continue
        command = -8 if fault == "weather_only" else (-8 if elapsed < 14400 else -4)
        runtime.begin_cycle(
            {
                "at": at.isoformat(),
                "sources": sources,
                "indoor": sum((s["value"] or 0) * s["weight"] for s in sources),
                "indoor_reported_at": at.isoformat(),
                "outdoor": world.outdoor.temp_c,
                "outdoor_reported_at": at.isoformat(),
                "outdoor_source": "sensor.synthetic_outdoor",
                "requested_virtual_outdoor": command,
                "mode": "normal",
                "brake_factor": 0.0,
                "pi_demand_c": 2.0,
                "command_span": 20.0,
            },
            use_ohmigo_input=True,
        )
        runtime.record_output(
            {
                "at": at.isoformat(),
                "transport": "ohmigo",
                "requested": command,
                "status": "service_completed_unverified",
            }
        )
    return {
        "synthetic": True,
        "start": scenario.start,
        "runtime": runtime,
        "reports": reports,
        "changes": changes,
        "reported_values": values,
        "truth": truth,
    }
