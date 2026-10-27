"""Pilot learning consumes only actual, timely HA reports, with no output."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from custom_components.pumpsteer.pilot_passive import PilotPassive

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def state(value, at, unit=None, restored=False, changed=T):
    return SimpleNamespace(
        state=str(value),
        last_reported=at,
        last_changed=changed,
        attributes={"unit_of_measurement": unit, "restored": restored},
    )


def test_real_indoor_reports_drive_pilot_intervals_and_repeats_do_not():
    observer = PilotPassive()
    for minute in range(0, 71, 5):
        at = T + timedelta(minutes=minute)
        observer.observe(
            at,
            state(20 - minute / 600, at, "°C"),
            state(5, at, "°C"),
            state("on", at),
            "normal",
        )
    assert observer.house_intervals == 1
    last = observer.manager.house.last
    observer.observe(
        at + timedelta(minutes=1),
        state(20, at, "°C"),
        state(5, at, "°C"),
        state("on", at),
        "normal",
    )
    assert observer.manager.house.last == last


def test_missing_or_uncertain_heat_and_safe_reset_interval():
    observer = PilotPassive()
    at = T
    observer.observe(
        at, state(20, at, "°C"), state(5, at, "°C"), state("on", at), "normal"
    )
    observer.observe(
        at + timedelta(minutes=5),
        state(20, at + timedelta(minutes=5), "°C"),
        state(5, at, "°C"),
        None,
        "normal",
    )
    assert observer.status == "heat_signal_unavailable"
    assert observer.manager.house.start is None
    observer.observe(
        at + timedelta(minutes=6),
        state(20, at + timedelta(minutes=6), "°C"),
        state(5, at + timedelta(minutes=6), "°C"),
        state("on", at + timedelta(minutes=6), restored=True),
        "normal",
    )
    assert observer.status == "heat_signal_unavailable"
    observer.observe(at + timedelta(minutes=7), None, None, None, "safe_mode")
    assert observer.status == "suspended_safe"


def test_sources_after_indoor_report_are_not_retroactively_used():
    observer = PilotPassive()
    at = T + timedelta(minutes=1)
    observer.observe(
        at, state(20, T, "°C"), state(5, at, "°C"), state("on", at), "normal"
    )
    assert observer.status == "awaiting_aligned_reports"
    assert observer.manager.house.start is None


def test_restored_reports_are_not_new_after_startup():
    observer = PilotPassive()
    observer.observe(
        T + timedelta(minutes=1),
        state(20, T, "°C"),
        state(5, T, "°C"),
        state("on", T),
        "normal",
        reported_after=T,
    )
    assert observer.status == "heat_signal_unavailable"
    assert observer.manager.house.start is None


def test_heat_transition_breaks_interval_even_if_indoor_did_not_report():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    at = T + timedelta(minutes=3)
    observer.observe(
        at,
        state(20, T, "°C"),
        state(5, at, "°C"),
        state("on", at, changed=at),
        "normal",
    )
    assert observer.manager.house.start is None
    assert observer.status in ("awaiting_aligned_reports", "heat_transition")


def test_outdoor_transition_between_indoor_reports_breaks_interval():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    changed = T + timedelta(minutes=3)
    at = T + timedelta(minutes=5)
    observer.observe(
        at,
        state(19.9, at, "°C"),
        state(4, changed, "°C", changed=changed),
        state("on", at),
        "normal",
    )
    assert observer.manager.house.start == at.timestamp()
    assert observer.manager.house.known == 0


def test_heat_transition_at_new_indoor_timestamp_cannot_hide_prior_changes():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    at = T + timedelta(minutes=5)
    observer.observe(
        at,
        state(19.9, at, "°C"),
        state(5, at, "°C"),
        state("on", at, changed=at),
        "normal",
    )
    assert observer.manager.house.start == at.timestamp()
    assert observer.manager.house.known == 0


def test_unknown_control_mode_cannot_supply_learning():
    observer = PilotPassive()
    observer.observe(
        T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "unknown"
    )
    assert observer.status == "control_context_unqualified"
    assert observer.manager.house.start is None


def test_visible_underlying_source_change_discards_prior_interval():
    observer = PilotPassive()
    inside = state(20, T, "°C")
    inside.attributes["source_temperatures_used"] = {"sensor.a": 20}
    observer.observe(T, inside, state(5, T, "°C"), state("on", T), "normal")
    old_manager = observer.manager
    at = T + timedelta(minutes=5)
    new_inside = state(20, at, "°C")
    new_inside.attributes["source_temperatures_used"] = {"sensor.b": 20}
    observer.observe(at, new_inside, state(5, at, "°C"), state("on", at), "normal")
    assert observer.manager is not old_manager
    assert observer.manager.house.start == at.timestamp()
    assert observer.manager.house.known == 0


def test_pilot_parameters_change_only_after_many_real_qualifying_intervals():
    import math

    observer = PilotPassive()
    initial_loss = observer.manager.model.heat_loss_coeff
    initial_gain = observer.manager.model.heat_gain_coeff
    temp = 20.0
    for index in range(52 * 12):
        at = T + timedelta(minutes=5 * index)
        block = index // 24
        block_at = T + timedelta(minutes=120 * block)
        outside = 5.0 + 4.0 * math.sin(block * 0.7)
        heat = (block % 2) == 0
        observer.observe(
            at,
            state(temp, at, "°C"),
            state(outside, at, "°C", changed=block_at),
            state("on" if heat else "off", at, changed=block_at),
            "normal",
        )
        temp += (0.02 * (outside - temp) + 0.4 * heat + 0.1) / 12
    model = observer.manager.model
    assert observer.house_intervals >= 24
    assert model.gain_identified is True
    assert model.status == "learning"
    assert (model.heat_loss_coeff, model.heat_gain_coeff) != (
        initial_loss,
        initial_gain,
    )
