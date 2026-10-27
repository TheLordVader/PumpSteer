"""Pilot learning consumes only actual, timely HA reports, with no output."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
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
    last = observer.last_indoor_report
    observer.observe(
        at + timedelta(minutes=1),
        state(20, at, "°C"),
        state(5, at, "°C"),
        state("on", at),
        "normal",
    )
    assert observer.last_indoor_report == last
    assert observer.house_intervals == 1


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
    assert observer.status == "waiting_for_indoor_report"


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


def test_normal_indoor_silence_does_not_fabricate_or_restart_report():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    at = T + timedelta(minutes=30)
    observer.observe(
        at, state(20, T, "°C"), state(5, at, "°C"), state("on", at), "normal"
    )
    assert observer.status == "waiting_for_indoor_report"
    assert observer.last_indoor_report == T
    assert observer.house_intervals == 0


def test_old_report_is_not_reused_after_safe():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    observer.observe(T + timedelta(minutes=1), None, None, None, "safe_mode")
    observer.observe(
        T + timedelta(minutes=2),
        state(20, T, "°C"),
        state(5, T, "°C"),
        state("on", T),
        "normal",
    )
    assert observer.manager.house.start is None
    assert observer.status == "waiting_for_indoor_report"


@pytest.mark.parametrize("period", [20, 30, 60])
def test_sparse_real_indoor_endpoints_with_continuous_verified_context(period):
    observer = PilotPassive()
    for minute in range(61):
        now = T + timedelta(minutes=minute)
        reported_minute = (minute // period) * period
        reported = T + timedelta(minutes=reported_minute)
        indoor = state(20 - reported_minute / 600, reported, "°C")
        observer.observe(now, indoor, state(5, now, "°C"), state("on", now), "normal")
    assert observer.house_intervals == 1
    sample = observer.manager.model.history[-1]
    assert sample.end == (T + timedelta(minutes=60)).timestamp()
    assert sample.start_temp == 20 and sample.end_temp == 19.9


def test_held_indoor_never_becomes_a_synthetic_endpoint():
    observer = PilotPassive()
    for minute in range(71):
        now = T + timedelta(minutes=minute)
        observer.observe(
            now, state(20, T, "°C"), state(5, now, "°C"), state("on", now), "normal"
        )
    assert observer.house_intervals == 0
    assert not observer.manager.model.history


def test_changed_indoor_value_with_same_report_time_is_not_accepted():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    now = T + timedelta(minutes=1)
    observer.observe(
        now, state(21, T, "°C"), state(5, now, "°C"), state("on", now), "normal"
    )
    assert observer.status == "inconsistent_indoor_report"
    assert observer.manager.house.start is None


def test_out_of_order_indoor_report_breaks_pending_interval():
    observer = PilotPassive()
    at = T + timedelta(minutes=2)
    observer.observe(
        at, state(20, at, "°C"), state(5, at, "°C"), state("on", at), "normal"
    )
    observer.observe(
        at + timedelta(minutes=1),
        state(19, T, "°C"),
        state(5, at, "°C"),
        state("on", at),
        "normal",
    )
    assert observer.status == "reordered_indoor_report"
    assert observer.manager.house.start is None


def test_late_genuine_report_is_not_mistaken_for_a_changed_duplicate():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    tick = T + timedelta(minutes=1)
    observer.observe(
        tick, state(20, T, "°C"), state(5, tick, "°C"), state("on", tick), "normal"
    )
    delivered = T + timedelta(minutes=2)
    observer.observe(
        delivered,
        state(19, tick, "°C"),
        state(5, tick, "°C"),
        state("on", tick),
        "normal",
    )
    assert observer.status == "late_indoor_report"
    observer.observe(
        delivered + timedelta(minutes=1),
        state(19, tick, "°C"),
        state(5, delivered, "°C"),
        state("on", delivered),
        "normal",
    )
    assert observer.status == "waiting_for_indoor_report"
    assert observer.manager.house.start is None


def test_backward_clock_step_never_becomes_new_held_temperature_anchor():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    at = T + timedelta(minutes=2)
    observer.observe(
        at, state(20, T, "°C"), state(5, at, "°C"), state("on", at), "normal"
    )
    backwards = T + timedelta(minutes=1)
    observer.observe(
        backwards,
        state(20, T, "°C"),
        state(5, backwards, "°C"),
        state("on", backwards),
        "normal",
    )
    assert observer.status == "clock_reversal"
    assert observer.manager.house.start is None


def test_asynchronous_reports_use_only_prior_known_unchanged_context():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    for minute in range(1, 61):
        now = T + timedelta(minutes=minute, seconds=6)
        report = (
            T
            if minute < 30
            else T + timedelta(minutes=30, seconds=1)
            if minute < 60
            else T + timedelta(minutes=60, seconds=1)
        )
        temp = 20 if minute < 30 else 19.95 if minute < 60 else 19.9
        context_at = T + timedelta(minutes=minute, seconds=5)
        observer.observe(
            now,
            state(temp, report, "°C"),
            state(5, context_at, "°C"),
            state("on", context_at),
            "normal",
        )
    assert observer.house_intervals == 1
    assert (
        observer.manager.model.history[-1].end
        == (T + timedelta(minutes=60, seconds=1)).timestamp()
    )


def test_reordered_or_inconsistent_context_cannot_be_used_as_fresh():
    observer = PilotPassive()
    observer.observe(T, state(20, T, "°C"), state(5, T, "°C"), state("on", T), "normal")
    at = T + timedelta(minutes=2)
    observer.observe(
        at, state(20, T, "°C"), state(5, at, "°C"), state("on", at), "normal"
    )
    observer.observe(
        at + timedelta(minutes=1),
        state(20, T, "°C"),
        state(5, T + timedelta(minutes=1), "°C"),
        state("on", at),
        "normal",
    )
    assert observer.status == "context_report_reordered"
    assert observer.manager.house.start is None
