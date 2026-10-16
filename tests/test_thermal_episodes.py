"""Episode boundaries describe observed context, never delivered heat."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from custom_components.pumpsteer.thermal_episodes import EpisodeTracker

START = datetime(2026, 10, 16, tzinfo=timezone.utc)


def at(minute):
    return START + timedelta(minutes=minute)


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "preheating",
        "pre_braking",
        "braking",
        "brake_hold",
        "precool",
        "safe_mode",
    ],
)
def test_source_modes_are_explicit_and_never_invent_heat(mode):
    episode = EpisodeTracker("run-a").observe(at(0), mode)
    assert episode.mode == mode and episode.source_mode == mode
    assert episode.heating_kind == "unknown"


@pytest.mark.parametrize("kind", ["known", "estimated", "unknown"])
def test_heating_evidence_kind_is_retained_without_inference(kind):
    e = EpisodeTracker("run-a").observe(at(0), "normal", heating_kind=kind)
    assert e.heating_kind == kind


def test_boundaries_include_heat_evidence_and_recovery_requires_explicit_metadata():
    tracker = EpisodeTracker("run-a")
    first = tracker.observe(at(0), "braking")
    second = tracker.observe(at(1), "brake_hold")
    third = tracker.observe(at(2), "normal", brake_releasing=True)
    fourth = tracker.observe(at(3), "normal")
    fifth = tracker.observe(at(4), "normal", heating_kind="estimated")
    assert [first.mode, second.mode, third.mode, fourth.mode] == [
        "braking",
        "brake_hold",
        "recovery",
        "normal",
    ]
    assert third.source_mode == "normal" and third.heating_kind == "unknown"
    assert len({e.number for e in (first, second, third, fourth, fifth)}) == 5
    assert tracker.history[0].ended_at == at(1)
    with pytest.raises(FrozenInstanceError):
        first.mode = "normal"


def test_brake_to_normal_alone_does_not_prove_recovery_duration_or_heat():
    tracker = EpisodeTracker("run-a")
    tracker.observe(at(0), "braking")
    assert tracker.observe(at(1), "normal").mode == "normal"


def test_sensor_interruption_and_gap_do_not_claim_continuous_episodes():
    tracker = EpisodeTracker("run-a")
    first = tracker.observe(at(0), "normal")
    missing = tracker.observe(at(1), "normal", sensor_valid=False)
    recovered = tracker.observe(at(2), "normal")
    after_gap = tracker.observe(at(10), "normal")
    assert missing.mode == "sensor_interruption"
    assert recovered.number != first.number != after_gap.number
    assert tracker.episode_at(at(6)).mode == "sensor_interruption"
    assert tracker.episode_at(at(11)) is None  # no future context implied
    assert after_gap.boundary_reason == "observation_gap"


def test_duplicates_are_idempotent_and_conflicting_timestamps_break_context():
    tracker = EpisodeTracker("run-a")
    first = tracker.observe(at(0), "normal")
    assert tracker.observe(at(0), "normal") == first
    assert tracker.observe(at(0), "braking") is None
    assert tracker.reason == "conflicting_timestamp"
    assert tracker.observe(at(1), "normal").number != first.number


def test_clock_reset_and_restart_never_reuse_episode_identity():
    tracker = EpisodeTracker("run-a")
    old = tracker.observe(at(2), "normal")
    assert tracker.observe(at(1), "normal") is None
    new = tracker.observe(at(3), "normal")
    assert new.epoch != old.epoch
    assert tracker.episode_at(at(2)) is None
    restarted = EpisodeTracker("run-b").observe(at(3), "normal")
    assert restarted.run_id != new.run_id


def test_history_is_bounded_with_frequent_transitions_and_unknown_modes_abstain():
    tracker = EpisodeTracker("run-a")
    for minute in range(100):
        tracker.observe(at(minute), "normal" if minute % 2 else "braking")
        assert len(tracker.history) <= 32
    assert tracker.episode_at(at(0)) is None
    assert tracker.observe(at(100), "unrecognized-state").mode == "unknown"
    assert tracker.observe(at(101), "normal", heating_kind="invented") is None


@pytest.mark.parametrize(
    "start",
    [
        datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
    ],
)
def test_dst_context_uses_utc_and_naive_times_are_unavailable(start):
    tracker = EpisodeTracker("run-a")
    zone = ZoneInfo("Europe/Stockholm")
    for minute in range(61):
        episode = tracker.observe(
            (start + timedelta(minutes=minute)).astimezone(zone), "normal"
        )
    assert episode.observed_until.timestamp() - episode.started_at.timestamp() == 3600
    assert tracker.observe(start.replace(tzinfo=None), "normal") is None


@pytest.mark.parametrize("bad", ["heat", "time", "flag"])
def test_invalid_context_breaks_continuity_before_later_valid_observations(bad):
    tracker = EpisodeTracker("run-a")
    first = tracker.observe(at(0), "normal", heating_kind="estimated")
    when = at(1).replace(tzinfo=None) if bad == "time" else at(1)
    kwargs = {"heating_kind": "invented" if bad == "heat" else "estimated"}
    if bad == "flag":
        kwargs["sensor_valid"] = "not-a-boolean"
    assert tracker.observe(when, "normal", **kwargs) is None
    recovered = tracker.observe(at(2), "normal", heating_kind="estimated")
    assert recovered.number != first.number
