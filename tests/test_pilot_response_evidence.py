"""Qualified source reports, shared continuity, and passive Pilot intervals."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.heat_operation import HeatOperation
from custom_components.pumpsteer.thermal_intelligence.runtime import ThermalIntelligence
from test_thermal_intelligence_runtime import START, _cycle


def time(second):
    return START + timedelta(seconds=second)


def state(value, at):
    return SimpleNamespace(state=value, last_reported=at, last_changed=at, attributes={})


def report(role, second, value, **overrides):
    at = time(second)
    event = {
        "at": at, "reported_at": at, "valid_until": time(second + 3600),
        "role": role, "source": role + "_source", "value": value, "qualified": True,
    }
    if role == "heat":
        on = "on" if value else "off"
        event.update(
            semantics="space_heat_duty",
            operation=HeatOperation().observe(at, heat=state(on, at), compressor=state(on, at)),
        )
    return event | overrides


def runtime():
    evidence = ThermalIntelligence()
    evidence.begin_cycle(_cycle(START, [("room", 1, 20.0, 0)], mode="normal") | {"gos_active": False},
                         use_ohmigo_input=True)
    return evidence


def sources():
    return [report("outdoor", 0, 5.0), report("receiver", 0, 5.0)]


@pytest.mark.parametrize(("semantics", "levels", "expected"), [
    ("space_heat_duty", (0, 1, 0), 1 / 3),
    ("space_heat_intensity", (0.25, 0.75, 0.5), 0.49166666666666664),
])
def test_retains_two_timed_transitions_between_ticks(semantics, levels, expected):
    evidence = runtime()
    before = evidence.export_state()
    reports = sources() + [report("heat", edge, value, semantics=semantics)
                          for edge, value in zip((0, 330, 630), levels)]
    rows = evidence.pilot_response_intervals(reports, through=time(900), output_clamp=12)
    assert len(rows) == 1
    assert rows[0].end == time(900).timestamp()
    assert rows[0].coverage == 1.0
    assert rows[0].heat == pytest.approx(expected)
    assert rows[0].request == 0.5
    assert evidence.export_state() == before


def test_lease_expiry_leaves_gap_and_restarts_interval():
    evidence = runtime()
    reports = sources() + [report("heat", 0, 1, valid_until=time(300)), report("heat", 360, 1)]
    rows = evidence.pilot_response_intervals(reports, through=time(1260), output_clamp=12)
    assert [row.end for row in rows] == [time(1260).timestamp()]
    assert rows[0].coverage == 1.0


def test_duplicate_cannot_renew_source_validity():
    evidence = runtime()
    original = report("heat", 0, 1, valid_until=time(300))
    duplicate = original | {"at": time(180), "valid_until": time(1200)}
    assert evidence.pilot_response_intervals(sources() + [original, duplicate],
                                             through=time(900), output_clamp=12) == []


@pytest.mark.parametrize("observation_key", ["measured_at", "source_observed_at"])
def test_ha_republication_cannot_extend_same_physical_measurement(observation_key):
    evidence = runtime()
    original = report("heat", 0, 1, valid_until=time(300), **{observation_key: time(-1)})
    duplicate = report("heat", 180, 1, **{observation_key: time(-1)})
    assert evidence.pilot_response_intervals(sources() + [original, duplicate],
                                             through=time(900), output_clamp=12) == []


def test_late_transition_invalidates_affected_interval_without_backdating():
    evidence = runtime()
    original = report("heat", 0, 1)
    late = report("heat", 960, 0, reported_at=time(840), measured_at=time(830))
    reports = sources() + [original, late]
    assert len(evidence.pilot_response_intervals(reports, through=time(900), output_clamp=12)) == 1
    assert evidence.pilot_response_intervals(reports, through=time(960), output_clamp=12) == []
    assert late["reported_at"] == time(840) and late["measured_at"] == time(830)


def test_late_physical_hot_water_evidence_revokes_overlapping_row():
    evidence = runtime()
    water = HeatOperation().observe(time(930), water=state("on", time(930)))
    late_water = report("heat", 960, 0, reported_at=time(930), measured_at=time(880), operation=water)
    reports = sources() + [report("heat", 0, 1), late_water]
    assert len(evidence.pilot_response_intervals(reports, through=time(900), output_clamp=12)) == 1
    assert evidence.pilot_response_intervals(reports, through=time(960), output_clamp=12) == []


def test_hot_water_breaks_space_heating_interval():
    evidence = runtime()
    water = HeatOperation().observe(time(240), water=state("on", time(240)))
    reports = sources() + [report("heat", 0, 1), report("heat", 240, 0, operation=water),
                           report("heat", 420, 1)]
    rows = evidence.pilot_response_intervals(reports, through=time(1320), output_clamp=12)
    assert [(row.end, row.heat) for row in rows] == [(time(1320).timestamp(), 1.0)]


@pytest.mark.parametrize("bad", [
    {"reported_at": None}, {"measured_at": time(1)}, {"qualified": False},
    {"semantics": "space_heat_duty", "value": 0.7}, {"semantics": "electric_power"},
])
def test_unknown_time_or_unqualified_semantics_never_become_heat(bad):
    evidence = runtime()
    assert evidence.pilot_response_intervals(sources() + [report("heat", 0, 1) | bad],
                                             through=time(900), output_clamp=12) == []


def test_command_reception_and_shared_segment_are_required():
    evidence = runtime()
    evidence.record_output({"at": START, "transport": "ohmigo", "requested": 5.0,
                            "status": "service_completed_unverified"})
    assert evidence.pilot_response_intervals(sources()[:1] + [report("heat", 0, 1)],
                                             through=time(900), output_clamp=12) == []
    evidence.suspend("restart", time(600))
    assert evidence.pilot_response_intervals(sources() + [report("heat", 0, 1)],
                                             through=time(1500), output_clamp=12) == []


def test_new_dispatch_without_matching_receiver_creates_unknown_time():
    evidence = runtime()
    evidence.record_output({"at": time(450), "transport": "ohmigo", "requested": 2.0,
                            "status": "service_completed_unverified"})
    reports = sources() + [report("heat", 0, 1), report("receiver", 480, 2.0)]
    assert evidence.pilot_response_intervals(reports, through=time(900), output_clamp=12) == []
    rows = evidence.pilot_response_intervals(reports, through=time(1380), output_clamp=12)
    assert [(row.end, row.request) for row in rows] == [(time(1380).timestamp(), 0.625)]


def test_timed_receiver_change_uses_old_and_new_value_at_correct_times():
    evidence = runtime()
    evidence.record_output({"at": time(450), "transport": "ohmigo", "requested": 2.0,
                            "status": "service_completed_unverified"})
    reports = sources() + [report("heat", 0, 1), report("receiver", 450, 2.0)]
    rows = evidence.pilot_response_intervals(reports, through=time(900), output_clamp=12)
    assert [(row.end, row.request) for row in rows] == [(time(900).timestamp(), 0.5625)]


def test_resume_requires_a_new_shared_context_after_interruption():
    evidence = runtime()
    evidence.suspend("restart", time(300))
    reports = sources() + [report("heat", 0, 1), report("outdoor", 420, 5.0),
                           report("receiver", 420, 5.0), report("heat", 420, 0)]
    assert evidence.pilot_response_intervals(reports, through=time(1320), output_clamp=12) == []
    evidence.begin_cycle(_cycle(time(420), [("room", 1, 20.0, 0)], mode="normal") | {"gos_active": False},
                         use_ohmigo_input=True)
    rows = evidence.pilot_response_intervals(reports, through=time(1320), output_clamp=12)
    assert [(row.end, row.heat) for row in rows] == [(time(1320).timestamp(), 0.0)]


def test_source_contracts_cannot_be_pooled_in_one_replay():
    reports = sources() + [report("heat", 0, 1), report("heat", 930, 0.5, semantics="space_heat_intensity")]
    assert runtime().pilot_response_intervals(reports, through=time(1830), output_clamp=12) == []
