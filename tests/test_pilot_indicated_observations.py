"""Indicated-operation observations stay separate from verified Pilot learning."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from custom_components.pumpsteer.heat_operation import HeatOperation
from custom_components.pumpsteer.pilot_passive import PilotPassive

START = datetime(2026, 10, 11, 8, tzinfo=UTC)


def state(value, at, *, unit=None, changed=None):
    return SimpleNamespace(
        state=str(value),
        last_reported=at,
        last_changed=changed or at - timedelta(minutes=2),
        attributes={"unit_of_measurement": unit},
    )


def indicated():
    return {
        "mode": "indicated_radiator_heating",
        "quality": "indication_only",
        "signals": ["compressor", "radiator", "hot_water", "power", "supply", "return"],
        "evidence_level": "operating_indication",
        "evidence_uncertainty": "heat_delivery_unverified",
    }


def test_indicated_intervals_are_collected_separately_without_fitting_pilot():
    pilot = PilotPassive()
    operation_model = HeatOperation()
    completed = []
    for minute in range(0, 66, 5):
        now = START + timedelta(minutes=minute)
        operation = operation_model.observe(
            now,
            compressor=state("on", now),
            radiator=state("on", now),
            water=state("off", now),
            power=state("1200", now, unit="W"),
            supply=state("35", now, unit="°C"),
            return_temp=state("30", now, unit="°C"),
        )
        assert operation["evidence_level"] == "operating_indication"
        result = pilot.observe_indicated(
            now,
            state(20.0 - minute / 600, now, unit="°C"),
            state(5, now, unit="°C"),
            operation,
        )
        if result is not None:
            completed.append(result)
    assert completed
    assert completed[0].coverage == 1
    assert len(pilot.indicated_observations) == 1
    row = pilot.indicated_observations[0]
    assert row["evidence_level"] == "operating_indication"
    assert row["interpretation"] == "apparent_indoor_response_not_heat_delivery"
    assert row["indicated_fraction"] == 1
    assert pilot.manager.model.history == []
    assert not pilot.manager.model.gain_identified
    assert pilot.validated_house(START + timedelta(hours=2)) is None


def test_indicated_observation_resets_on_missing_or_transitioning_context():
    pilot = PilotPassive()
    for minute in range(0, 30, 5):
        now = START + timedelta(minutes=minute)
        pilot.observe_indicated(
            now, state(20, now, unit="°C"), state(5, now, unit="°C"), indicated()
        )
    now = START + timedelta(minutes=30)
    assert (
        pilot.observe_indicated(
            now, state(20, now, unit="°C"), state(5, now, unit="°C"), None
        )
        is None
    )
    assert pilot.indicated_house.start is None
    stale = START
    assert (
        pilot.observe_indicated(
            now,
            state(20, stale, unit="°C"),
            state(5, now, unit="°C"),
            indicated(),
        )
        is None
    )
    assert pilot.indicated_house.start is None
    transitioning = state(20, now, unit="°C", changed=now)
    assert (
        pilot.observe_indicated(
            now, transitioning, state(5, now, unit="°C"), indicated()
        )
        is None
    )
    assert pilot.indicated_house.start is None
