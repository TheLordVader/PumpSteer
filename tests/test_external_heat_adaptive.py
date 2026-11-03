"""Possible intermittent heat is not evidence of current heat delivery."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import pytest

from test_adaptive_braking import replay_model, START
from custom_components.pumpsteer.control_replay import replay_control_step


def test_possible_external_heat_does_not_block_good_independent_validation(monkeypatch):
    _, state, inputs = replay_model(monkeypatch)
    inputs = replace(inputs, cfg={**inputs.cfg, "external_heat_sources": True})
    _, adaptive = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert adaptive.fake_temp < base.fake_temp
    assert adaptive.attributes["adaptive_braking"]["state"] == "adapting"


def test_warming_window_discards_pending_samples_and_old_qualification():
    from test_adaptive_braking import qualified

    model = qualified()
    model.start_validation_session(
        START + timedelta(hours=5),
        20,
        5,
        adaptive_observations=True,
        screen_external_heat=True,
    )
    for minute, temp in [(0, 20.0), (5, 19.9), (10, 19.9), (15, 19.8), (20, 20.5)]:
        model.record_temp(
            START + timedelta(hours=5, minutes=minute), temp, adaptive_reports=True
        )
        model.collect_braking_sample(temp, 5)
    assert model.pending_samples > 0
    assert model.screen_possible_external_heat(START + timedelta(hours=5, minutes=20))
    assert model.pending_samples == 0
    assert not model.adaptive_snapshot()["validations"]
    assert model.is_valid  # experience retained, use paused/requalification required
    assert (
        model.adaptive_reason(START + timedelta(hours=5, minutes=10), 16)
        == "temperature_response_disturbed"
    )


def test_extra_cooling_error_still_drives_existing_refit():
    from test_adaptive_braking import qualified

    model = qualified()
    for hour in (6, 7, 8):
        start = START + timedelta(hours=hour)
        model.start_validation_session(
            start, 21, 5, adaptive_observations=True, screen_external_heat=True
        )
        pred = model.predict_drop(21, 5, 30)
        for minute in (10, 20, 30):
            model.update_validation_session(
                start + timedelta(minutes=minute), 21 - (pred + 0.25) * minute / 30, 5
            )
        model.end_validation_session(start + timedelta(minutes=30), 21 - pred - 0.25)
    assert model.adaptive_reason(START + timedelta(hours=9), 16) == "validation_error"
    assert len(model.adaptive_snapshot()["validations"]) == 3


@pytest.mark.parametrize(
    "interval,weighted", [(5, False), (10, False), (15, False), (15, True)]
)
def test_possible_heat_basic_or_mixed_reports_really_learn_and_adapt(
    monkeypatch, interval, weighted
):
    from test_adaptive_report_intervals import exercise

    sensor, states, accepted, count = exercise(
        monkeypatch,
        interval,
        weighted=weighted,
        jitter=weighted,
        periods=(5, 10, 15) if weighted else None,
        external_possible=True,
    )
    now = START + timedelta(minutes=2881)
    assert sensor._thermal_model.adaptive_reason(now, 16) == "qualified"
    from test_adaptive_report_intervals import cold_step

    state, inputs, actual = cold_step(sensor, states, monkeypatch, 4 * 720 + 1)
    _, a = replay_control_step(state, inputs)
    _, b = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert a.fake_temp < b.fake_temp
    assert a.attributes["adaptive_braking"]["external_heat"] == "possible_unverified"


def test_stove_burst_discards_whole_window_then_later_stable_mixed_periods_recover(
    monkeypatch,
):
    from test_adaptive_report_intervals import exercise
    from custom_components.pumpsteer.thermal_model import ThermalModel

    rejections = []
    original = ThermalModel.mark_external_disturbance

    def reject(model, reason="temperature_response_disturbed"):
        before = len(model.adaptive_snapshot()["validations"])
        pending_before = model.pending_samples
        original(model, reason)
        rejections.append(
            (
                before,
                pending_before,
                model.pending_samples,
                len(model.adaptive_snapshot()["validations"]),
            )
        )

    monkeypatch.setattr(ThermalModel, "mark_external_disturbance", reject)

    def profile(minute, ordinary):
        phase = minute - 4 * 720
        if 120 <= phase <= 180:
            return ordinary + min((phase - 120) / 20, (180 - phase) / 20, 1.0)
        return ordinary

    sensor, states, accepted, count = exercise(
        monkeypatch,
        15,
        weighted=True,
        jitter=True,
        periods=(5, 10, 15),
        external_possible=True,
        temperature_profile=profile,
        episodes=8,
    )
    assert any(
        before == 3 and pending_before > 0 and pending == 0 and after == 0
        for before, pending_before, pending, after in rejections
    )
    assert (
        sensor._thermal_model.adaptive_reason(START + timedelta(minutes=8 * 720), 16)
        == "qualified"
    )
    assert len(sensor._thermal_model.adaptive_snapshot()["validations"]) == 3

    from test_adaptive_report_intervals import cold_step

    state, inputs, actual = cold_step(sensor, states, monkeypatch, 8 * 720 + 1)
    _, restored = replay_control_step(state, inputs)
    _, base = replay_control_step(
        state, replace(inputs, cfg={**inputs.cfg, "adaptive_braking_enabled": False})
    )
    assert restored.fake_temp < base.fake_temp
    assert restored.attributes["adaptive_braking"]["state"] == "adapting"


@pytest.mark.parametrize(
    "state,measured,expected",
    [
        ("on", True, "external_heat_verified_active"),
        ("off", False, "external_heat_signal_unavailable"),
        ("unavailable", True, "external_heat_signal_unavailable"),
    ],
)
def test_optional_external_signal_needs_real_source_time_and_pauses(
    monkeypatch, state, measured, expected
):
    _, model, data = replay_model(monkeypatch)
    source = SimpleNamespace(
        state=state,
        last_reported=data.now,
        attributes={"measurement_time": data.now.isoformat()} if measured else {},
    )
    cfg = {
        **data.cfg,
        "external_heat_sources": True,
        "thermal_external_heat_entity": "binary_sensor.stove",
    }
    _, decision = replay_control_step(
        model,
        replace(
            data, cfg=cfg, control_observation_states={"binary_sensor.stove": source}
        ),
    )
    assert decision.attributes["adaptive_braking"]["reason"] == expected
    assert decision.attributes["adaptive_braking"]["multiplier"] == 1


def test_off_verified_external_signal_does_not_prove_absence_of_all_extra_heat(
    monkeypatch,
):
    _, model, data = replay_model(monkeypatch)
    source = SimpleNamespace(
        state="off",
        last_reported=data.now,
        attributes={"measurement_time": data.now.isoformat()},
    )
    cfg = {
        **data.cfg,
        "external_heat_sources": True,
        "thermal_external_heat_entity": "binary_sensor.stove",
    }
    _, decision = replay_control_step(
        model,
        replace(
            data, cfg=cfg, control_observation_states={"binary_sensor.stove": source}
        ),
    )
    assert decision.attributes["adaptive_braking"]["state"] == "adapting"
    assert (
        decision.attributes["adaptive_braking"]["external_heat"] == "verified_inactive"
    )


def test_preheat_or_pi_recovery_history_is_not_mislabeled_extra_heat(monkeypatch):
    _, state, inputs = replay_model(monkeypatch)
    for minute, temp in [(-20, 19.5), (-10, 20.0), (0, 20.5)]:
        state.adaptive_model.record_temp(
            inputs.now + timedelta(minutes=minute), temp, adaptive_reports=True
        )
    # No baselinebrake validation session yet: earlierPI/preheat mustnot be screened.
    cfg = {**inputs.cfg, "external_heat_sources": True}
    _, decision = replay_control_step(state, replace(inputs, cfg=cfg))
    assert decision.attributes["adaptive_braking"]["reason"] == "qualified"
    assert decision.attributes["adaptive_braking"]["state"] == "adapting"


def test_republished_old_external_activity_never_becomes_verified(monkeypatch):
    _, model, data = replay_model(monkeypatch)
    source = SimpleNamespace(
        state="off",
        last_reported=data.now,
        attributes={"measurement_time": (data.now - timedelta(hours=1)).isoformat()},
    )
    cfg = {**data.cfg, "thermal_external_heat_entity": "binary_sensor.stove"}
    _, decision = replay_control_step(
        model,
        replace(
            data, cfg=cfg, control_observation_states={"binary_sensor.stove": source}
        ),
    )
    assert decision.attributes["adaptive_braking"]["external_heat"] == "unknown"
    assert decision.attributes["adaptive_braking"]["multiplier"] == 1


def test_old_unscreened_qualification_cannot_activate_after_upgrade():
    from test_adaptive_braking import qualified

    model = qualified()
    legacy = {**model.adaptive_snapshot(), "version": 2}
    coefficient = model.k
    model.restore_adaptive(legacy)
    assert model.is_valid and model.k == coefficient
    assert (
        model.adaptive_reason(START + timedelta(hours=5), 16)
        == "training_evidence_unverified"
    )
    assert not model.adaptive_snapshot()["validations"]


def test_flat_quantized_period_is_not_cherry_picked_into_cooling_evidence():
    from custom_components.pumpsteer.thermal_model import ThermalModel

    model = ThermalModel()
    model.start_validation_session(
        START, 21, 5, adaptive_observations=True, screen_external_heat=True
    )
    for minute in range(91):
        model.record_temp(
            START + timedelta(minutes=minute), 21.0, adaptive_reports=True
        )
        model.collect_braking_sample(21.0, 5)
    assert model.pending_samples >= 20
    assert not model.screen_possible_external_heat(START + timedelta(minutes=90))
    assert not model.fit()
    assert not model.is_valid
