import math
from datetime import UTC, datetime, timedelta

from custom_components.pumpsteer.thermal_intelligence.rtm_core import (
    ModelConfig,
    RCThermalModel,
    Sample,
)
from custom_components.pumpsteer.thermal_intelligence.runtime import (
    ThermalIntelligence,
    command_coordinate,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _cycle(at, values, *, mode="braking", outdoor=2.0, command=-8.0):
    sources = []
    for entity, weight, value, offset in values:
        sources.append(
            {
                "entity": entity,
                "weight": weight,
                "value": value,
                "reported_at": (at - timedelta(seconds=offset)).isoformat(),
            }
        )
    return {
        "at": at.isoformat(),
        "indoor": sum(row[1] * row[2] for row in values)
        / sum(row[1] for row in values),
        "indoor_reported_at": at.isoformat(),
        "outdoor": outdoor,
        "outdoor_reported_at": at.isoformat(),
        "outdoor_source": "sensor.outdoor",
        "sources": sources,
        "requested_virtual_outdoor": command,
        "mode": mode,
        "brake_factor": 1.0 if mode == "braking" else 0.0,
        "pi_demand_c": 0.0,
        "command_span": 20.0,
    }


def test_command_coordinate_is_explicitly_a_bounded_request_coordinate():
    assert command_coordinate(5.0, -15.0, 20.0) == 1.0
    assert command_coordinate(5.0, 5.0, 20.0) == 0.5
    assert command_coordinate(5.0, 25.0, 20.0) == 0.0


def test_repeated_values_do_not_create_temperature_samples():
    runtime = ThermalIntelligence()
    values = [
        ("sensor.room_a", 0.7, 21.0, 0),
        ("sensor.room_b", 0.2, 20.0, 10),
        ("sensor.room_c", 0.1, 19.0, 20),
    ]
    runtime.begin_cycle(_cycle(START, values), use_ohmigo_input=True)
    runtime.record_output(
        {
            "at": START.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": -8.0,
        }
    )
    updated = [
        (entity, weight, value, offset + 5) for entity, weight, value, offset in values
    ]
    runtime.begin_cycle(
        _cycle(START + timedelta(minutes=15), updated),
        use_ohmigo_input=True,
    )
    assert runtime._samples == 0
    assert len(runtime._observations) == 1


def test_changed_source_timestamp_supersedes_republished_aggregate_timestamp():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(
        _cycle(START, [("sensor.room", 1.0, 21.0, 0)]),
        use_ohmigo_input=True,
    )
    runtime.record_output(
        {
            "at": START.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": -8.0,
        }
    )
    runtime.begin_cycle(
        _cycle(
            START + timedelta(minutes=15),
            [("sensor.room", 1.0, 20.9, 0)],
        ),
        use_ohmigo_input=True,
    )
    cycle = _cycle(
        START + timedelta(minutes=30),
        [("sensor.room", 1.0, 20.8, 0)],
    )
    cycle["indoor_reported_at"] = (START + timedelta(minutes=15)).isoformat()
    cycle["outdoor_reported_at"] = (START + timedelta(minutes=15)).isoformat()
    runtime.begin_cycle(cycle, use_ohmigo_input=True)
    assert runtime._samples == 2
    assert runtime.latest_observation["indoor_reported_at"] == (START + timedelta(minutes=30)).isoformat()


def test_only_service_completed_ohmigo_request_enters_training():
    runtime = ThermalIntelligence()
    values = [("sensor.room", 1.0, 21.0, 0)]
    runtime.begin_cycle(_cycle(START, values), use_ohmigo_input=True)
    runtime.record_output(
        {
            "at": START.isoformat(),
            "transport": "ohmigo",
            "status": "dispatch_attempted",
            "requested": -8.0,
        }
    )
    values = [("sensor.room", 1.0, 20.9, 0)]
    runtime.begin_cycle(
        _cycle(START + timedelta(minutes=15), values), use_ohmigo_input=True
    )
    assert runtime._samples == 0
    runtime.record_output(
        {
            "at": (START + timedelta(minutes=15)).isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": -8.0,
        }
    )
    values = [("sensor.room", 1.0, 20.8, 0)]
    runtime.begin_cycle(
        _cycle(START + timedelta(minutes=30), values), use_ohmigo_input=True
    )
    assert runtime._samples == 1
    assert runtime._observations[-1]["command_coordinate"] == 0.75
    assert runtime.summary()["fit_trusted"] is False




def test_interruption_only_evidence_is_persistable():
    runtime = ThermalIntelligence()
    runtime._interrupt("required_temperature_missing", START)
    assert runtime.has_evidence
    assert len(runtime._interruptions) == 1
    assert runtime.export_state()["events"][0]["at"] == START.isoformat()


def test_generic_output_or_multiple_transport_mode_is_not_a_model_input():
    runtime = ThermalIntelligence()
    values = [("sensor.room", 1.0, 21.0, 0)]
    runtime.begin_cycle(_cycle(START, values), use_ohmigo_input=False)
    runtime.record_output(
        {
            "at": START.isoformat(),
            "transport": "generic",
            "status": "dispatched_unverified",
            "requested": None,
        }
    )
    values = [("sensor.room", 1.0, 20.9, 0)]
    runtime.begin_cycle(
        _cycle(START + timedelta(minutes=15), values),
        use_ohmigo_input=False,
    )
    assert runtime._samples == 0
    assert runtime._observations[-1]["command_coordinate"] is None


def test_restore_keeps_history_but_requires_new_value_and_new_command():
    first = ThermalIntelligence()
    values = [("sensor.room", 1.0, 21.0, 0)]
    first.begin_cycle(_cycle(START, values), use_ohmigo_input=True)
    first.record_output(
        {
            "at": START.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": -8.0,
        }
    )
    state = first.export_state()

    restored = ThermalIntelligence()
    assert restored.restore_state(state)
    restored.begin_cycle(
        _cycle(START + timedelta(minutes=15), values),
        use_ohmigo_input=True,
    )
    assert restored._samples == 0
    assert restored._last_command is None
    assert restored._interruptions[-1]["reason"] == "restart"
    assert not any(sample.effort is not None for _, sample in restored.model._pending)


def test_rtm_fits_sparse_quantized_synthetic_command_response():
    config = ModelConfig(
        step_minutes=15,
        max_gap_minutes=60,
        outdoor_max_gap_minutes=180,
        history_days=30,
    )
    model = RCThermalModel(config)
    indoor = 20.0
    at = START
    for index in range(24 * 10 * 4):
        outdoor = 2.0 + 0.5 * math.sin(index / 80)
        command = (index // 24) % 2
        indoor += 0.25 * (1.4 * command - 0.045 * (indoor - outdoor) - 0.2)
        indoor = round(indoor * 10) / 10
        if index % 4:
            at += timedelta(minutes=15)
        else:
            at += timedelta(minutes=20)
        model.add_sample(
            Sample(
                ts=at,
                indoor_c=indoor,
                outdoor_c=outdoor,
                effort=float(command),
            )
        )
    result = model.fit(at)
    assert result["status"] == "ok"
    assert model.parameters() is not None
    assert model.parameters().heating_rate_k_per_h > 0


def test_frozen_prediction_returns_finite_temperature_without_future_inputs():
    params = type(
        "Parameters",
        (),
        {
            "heating_rate_k_per_h": 1.2,
            "loss_rate_per_h": 0.1,
            "gains_k_per_h": -0.2,
            "dead_time_minutes": 60,
        },
    )()
    predicted = ThermalIntelligence._predict(20.0, 2.0, 0.7, params)
    assert predicted is not None
    assert math.isfinite(predicted)
    assert predicted != 20.0
