"""Optional integration contract against pinned external RoomMind; ALL data synthetic."""

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = os.environ.get("PUMPSTEER_ROOMMIND_ROOT")
pytestmark = pytest.mark.skipif(
    not ROOT, reason="external pinned RoomMind checkout required"
)
if ROOT:
    sys.path.insert(0, str(Path(ROOT) / "dev/sim"))


@pytest.mark.parametrize(
    "building,emitter", [("us_wood_frame", "generic_trv"), ("passivhaus", "ufh_zone")]
)
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "heat_off",
        "dhw",
        "restart",
        "history_gap",
        "sensor_loss",
        "weather_only",
        "response_delay",
        "undetected_heat_off",
    ],
)
def test_realistic_chain(building, emitter, fault):
    from offline.roommind_adapter import replay

    result = replay(building, emitter, fault=fault)
    assert result["synthetic"] is True
    assert result["reports"] > result["changes"] > 0
    assert all(abs(v * 10 - round(v * 10)) < 1e-8 for v in result["reported_values"])
    events = result["runtime"].export_state()["events"]
    assert any(e["kind"] == "observation" for e in events)
    assert all(
        not e["physical_time_known"] for e in events if e["kind"] == "temperature"
    )
    assert all(
        not e["physical_application_verified"] for e in events if e["kind"] == "output"
    )
    assert result["runtime"].model.parameters() is None  # replay never calls fit
    if fault in ("heat_off", "dhw"):
        assert all(
            r["space_heat_requested"] is False
            for r in result["truth"]
            if 7200 <= r["elapsed"] < 10800
        )
        assert any(e.get("reason") == "synthetic_" + fault for e in events)
    if fault in ("restart", "history_gap"):
        assert any(e["kind"] == "interruption" for e in events)
    if fault == "undetected_heat_off":
        assert not any(e.get("reason") == "synthetic_heat_off" for e in events)
        assert any(not r["space_heat_requested"] for r in result["truth"])
    if fault == "weather_only":
        assert len([e for e in events if e["kind"] == "output"]) == 1


def test_upstream_revision_guard(tmp_path):
    from offline.roommind_adapter import verify_upstream

    with pytest.raises(ValueError):
        verify_upstream(tmp_path)


def test_loss_excludes_observations_then_recovers():
    from offline.roommind_adapter import replay

    result = replay("us_wood_frame", "generic_trv", fault="sensor_loss")
    events = result["runtime"].export_state()["events"]
    assert any(
        e.get("reason") == "required_temperature_missing_or_stale" for e in events
    )
    offsets = [
        datetime.fromisoformat(e["at"]).timestamp() - result["start"]
        for e in events
        if e["kind"] == "observation"
    ]
    assert not any(7200 <= t < 10800 for t in offsets)
    assert any(t >= 10800 for t in offsets)


def test_sensor_housing_delay_changes_the_observed_trajectory():
    from offline.roommind_adapter import replay

    baseline = replay("us_wood_frame", "generic_trv")
    delayed = replay("us_wood_frame", "generic_trv", fault="response_delay")
    assert delayed["truth"] == baseline["truth"]
    assert delayed["reported_values"] != baseline["reported_values"]
