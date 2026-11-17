"""Synthetic HA inputs: availability is not a different weighted thermometer."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_safe_mode_output import setup_sensor  # noqa: F401
from test_temperature_input_safety import control  # noqa: F401

from custom_components.pumpsteer.thermal_intelligence.rtm_core import Sample
from custom_components.pumpsteer.thermal_intelligence.runtime import ThermalIntelligence


@pytest.fixture
def chain(request):
    sensor, states, clock = request.getfixturevalue("control")
    cfg = sensor._cfg()
    cfg.update(
        indoor_temp_entity_2="sensor.room_b",
        indoor_temp_entity_3="sensor.room_c",
        indoor_temp_weight_1=70,
        indoor_temp_weight_2=20,
        indoor_temp_weight_3=10,
        modbus_service="",
    )
    for name, value in [
        ("sensor.indoor", 21),
        ("sensor.room_b", 20),
        ("sensor.room_c", 19),
        ("sensor.outdoor", 2),
    ]:
        states[name] = SimpleNamespace(
            state=str(value),
            last_reported=clock[0],
            attributes={"unit_of_measurement": "°C"},
        )

    def cycle():
        indoor = sensor._temperature_value(cfg, "indoor_temp_entity")
        return sensor._thermal_cycle(
            clock[0], indoor, 2, -8, "braking", {"brake_factor": 1}, cfg
        )

    runtime = ThermalIntelligence()
    for i in range(4):
        runtime.model.add_sample(
            Sample(clock[0] - timedelta(minutes=60 - 15 * i), 21 - i * 0.1, 2, 0.5)
        )
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    runtime.model._params = {
        "a": 0.5,
        "b": 0.03,
        "c": 0.01,
        "d_minutes": 0,
        "use_outdoor": True,
    }
    restored = ThermalIntelligence()
    assert restored.restore_state(runtime.export_state())
    assert restored.model._grid.valid_indoor_count() > 0
    restored.begin_cycle(cycle(), use_ohmigo_input=True)
    restored.record_output(
        {
            "at": clock[0],
            "transport": "ohmigo",
            "requested": -8,
            "status": "service_completed_unverified",
        }
    )
    assert restored.current_command["coordinate"] is not None, (
        cycle(),
        restored._active_cycle,
    )
    return sensor, states, clock, cfg, cycle, restored


@pytest.mark.parametrize(
    "failure", ["unavailable", "restored", "unit", "stale", "missing_time"]
)
def test_partial_base_aggregate_does_not_replace_or_train_restored_model(
    chain, failure
):
    sensor, states, clock, _cfg, cycle, runtime = chain
    model = runtime.model
    good = model._grid.valid_indoor_count()
    observations = len(runtime._observations)
    for key in ("sensor.indoor", "sensor.room_b"):
        s = states[key]
        if failure == "unavailable":
            s.state = "unavailable"
        elif failure == "restored":
            s.attributes["restored"] = True
        elif failure == "unit":
            s.attributes["unit_of_measurement"] = "°F"
        elif failure == "stale":
            s.last_reported = clock[0] - timedelta(hours=4)
        else:
            s.last_reported = None
    # Startup cutoff also rejects missing report metadata through the Base helper.
    sensor._temperature_reported_after = clock[0] - timedelta(minutes=1)
    clock[0] += timedelta(seconds=1)
    states["sensor.room_c"].last_reported = clock[0]
    current = cycle()
    assert current["indoor"] == 19  # Base fallback remains usable.
    runtime.begin_cycle(current, use_ohmigo_input=True)
    assert len(current["sources"]) == 3
    assert runtime.model is model and model.parameters() is not None
    assert model._grid.valid_indoor_count() == good
    assert len(runtime._observations) == observations
    assert (
        runtime._interruptions[-1]["reason"] == "required_temperature_missing_or_stale"
    )
    assert runtime._last_command is None
    assert runtime._active_cycle is None
    # Restored, unchanged republishes must not become new temperature evidence.
    for key, value in [("sensor.indoor", "21"), ("sensor.room_b", "20")]:
        states[key].state = value
        states[key].attributes = {"unit_of_measurement": "°C"}
        states[key].last_reported = clock[0] + timedelta(seconds=1)
    clock[0] += timedelta(seconds=1)
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    assert runtime.model is model and model._grid.valid_indoor_count() == good
    assert len(runtime._observations) == observations
    assert runtime.summary()["active_control"] is False
    assert runtime.summary()["fit_trusted"] is False


@pytest.mark.parametrize("change", ["entity", "weight", "receiver", "outdoor"])
def test_actual_source_configuration_change_still_replaces_model(chain, change):
    _sensor, states, _clock, cfg, cycle, runtime = chain
    model = runtime.model
    if change == "entity":
        states["sensor.other"] = states["sensor.room_c"]
        cfg["indoor_temp_entity_3"] = "sensor.other"
    elif change == "weight":
        cfg["indoor_temp_weight_3"] = 15
    elif change == "receiver":
        cfg["ohmigo_entity"] = "number.other"
    else:
        states["sensor.other_outside"] = states["sensor.outdoor"]
        cfg["real_outdoor_entity"] = "sensor.other_outside"
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    assert runtime.model is not model and runtime.model.parameters() is None


def test_disabled_weights_and_duplicates_are_not_required_components(chain):
    _sensor, _states, _clock, cfg, cycle, _runtime = chain
    cfg["indoor_temp_weight_2"] = 0
    cfg["indoor_temp_entity_3"] = cfg["indoor_temp_entity"]
    rows = cycle()["sources"]
    assert [(r["entity"], r["weight"]) for r in rows] == [("sensor.indoor", 70)]


def test_recovered_real_change_starts_in_new_segment_without_old_command(chain):
    _sensor, states, clock, _cfg, cycle, runtime = chain
    model = runtime.model
    observations = len(runtime._observations)
    old_segment = runtime._segment
    states["sensor.indoor"].state = "unavailable"
    clock[0] += timedelta(seconds=1)
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    assert runtime._segment > old_segment
    assert runtime.current_command is None
    pending = list(model._pending)
    runtime.record_output(
        {
            "at": clock[0],
            "transport": "ohmigo",
            "requested": -8,
            "status": "service_completed_unverified",
        }
    )
    assert runtime.current_command["coordinate"] is None
    assert model._pending == pending
    states["sensor.indoor"].state = "20.9"
    clock[0] += timedelta(seconds=1)
    states["sensor.indoor"].last_reported = clock[0]
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    assert runtime.model is model
    assert len(runtime._observations) == observations + 1
    row = runtime._observations[-1]
    assert row["segment"] == runtime._segment
    assert row["command_coordinate"] is None
    # A real new dispatch during the interruption is recorded independently,
    # but it cannot carry an old coordinate or become a grid training command.
    assert row["command_at"] != (clock[0] - timedelta(seconds=2)).isoformat()
    assert row["data_quality"] == "command_unknown"


def test_single_external_aggregate_keeps_declared_underlying_provenance(chain):
    _sensor, states, _clock, cfg, cycle, runtime = chain
    cfg["indoor_temp_entity_2"] = cfg["indoor_temp_entity_3"] = None
    states["sensor.indoor"].attributes.update(
        source_temperatures_used={"sensor.room_b": 20, "sensor.room_c": 19},
        source_weights={"sensor.room_b": 2, "sensor.room_c": 1},
    )
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    model = runtime.model
    assert [(s["entity"], s["weight"]) for s in cycle()["sources"]] == [
        ("sensor.room_b", 2),
        ("sensor.room_c", 1),
    ]
    states["sensor.indoor"].attributes["source_temperatures_used"] = {
        "sensor.room_b": 20
    }
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    assert runtime.model is not model
