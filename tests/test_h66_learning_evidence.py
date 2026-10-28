"""Synthetic operational cases reflecting observed report-time limitations."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.heat_operation import HeatOperation
from custom_components.pumpsteer.pilot_passive import PilotPassive

T = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


def report(value, at, unit=None, *, changed=None, restored=False):
    return SimpleNamespace(
        state=str(value),
        last_reported=at,
        last_changed=changed or at,
        last_updated=changed or at,
        attributes={"unit_of_measurement": unit, "restored": restored},
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "stable_valve",
        "aligned_heating",
        "hot_water",
        "circulation",
        "cached_status",
        "disconnect",
        "contradiction",
        "no_telemetry",
        "restart",
    ],
)
def test_unverified_equipment_scenarios_cannot_create_pilot_heat_intervals(scenario):
    operation = HeatOperation()
    observer = PilotPassive()
    for minute in range(16):
        now = T + timedelta(minutes=minute)
        # No direct heat-delivery sensor: these synthetic values are operating
        # indications, not a measured radiator-heat label.
        old = scenario in {"stable_valve", "cached_status"}
        at = T if old else now
        compressor = report("on", at, changed=T)
        radiator = report("on", at, changed=T)
        water = report("off", at, changed=T)
        power = report("1200", now, "W", changed=T)
        supply = report("35", now, "°C", changed=T)
        return_temp = report("30", now, "°C", changed=T)
        if scenario == "hot_water":
            radiator = report("off", now, changed=T)
            water = report("on", now, changed=T)
        elif scenario == "circulation":
            compressor = report("off", now, changed=T)
            power = report("110", now, "W", changed=T)
            supply = report("25.5", now, "°C", changed=T)
            return_temp = report("25", now, "°C", changed=T)
        elif scenario == "disconnect" and 5 <= minute < 10:
            water = report("unavailable", now, changed=now)
        elif scenario == "contradiction":
            compressor = report("off", now, changed=T)
        elif scenario == "no_telemetry":
            compressor = radiator = water = power = supply = return_temp = None
        elif scenario == "restart":
            radiator = report("on", now, changed=T, restored=True)
            water = report("off", now, changed=T, restored=True)
        decision = operation.observe(
            now,
            compressor=compressor,
            radiator=radiator,
            water=water,
            power=power,
            supply=supply,
            return_temp=return_temp,
        )
        assert not decision["learning_qualified"]
        if scenario == "hot_water":
            assert decision["mode"] == "hot_water"
        # Real Pilot observer is called without a manufactured heat state.
        observer.observe_response(
            now,
            report("5", now, "°C", changed=T),
            None,
            report("4", now, "°C", changed=T),
            "normal",
            output_clamp=12,
        )
    assert not observer.manager.pump.history
    assert observer.manager.pump.reason == "response_input_unavailable"


def test_new_communication_does_not_freshen_an_old_valve():
    now = T + timedelta(minutes=30)
    operation = HeatOperation()
    result = operation.observe(
        now,
        compressor=report("on", now, changed=T),
        radiator=report("on", T),
        water=report("off", T),
        supply=report("35", now, "°C"),
        return_temp=report("30", now, "°C"),
        power=report("1200", now, "W"),
    )
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]
