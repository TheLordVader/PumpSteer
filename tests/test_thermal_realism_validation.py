"""Verification-tool integrity: frozen versions and control context must be real."""

import shutil
import pytest
from pathlib import Path

from test_thermal_observer import NOW, report
from thermal_irregular_validation import Audit, load_baseline


def copy_model(root):
    target = root / "custom_components/pumpsteer"
    target.mkdir(parents=True)
    source = Path(__file__).parents[1] / "custom_components/pumpsteer"
    for name in ("thermal_model.py", "thermal_observer.py", "thermal_intelligence.py"):
        shutil.copyfile(source / name, target / name)
    return target


def test_frozen_loader_does_not_reuse_another_directories_model(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    copy_model(a)
    model = copy_model(b) / "thermal_model.py"
    model.write_text(
        model.read_text().replace("_FALLBACK_K = 0.05", "_FALLBACK_K = 0.07")
    )
    first = load_baseline(a)
    second = load_baseline(b)
    assert first.model.k == 0.05
    assert second.model.k == 0.07


def test_frozen_observer_receives_the_same_control_context_as_candidate(tmp_path):
    copy_model(tmp_path)
    obs = load_baseline(tmp_path)
    audit = Audit(obs, NOW)
    audit.observe(NOW, report(21), report(5), True, control_context=("braking", 0.5))
    assert obs._control_context == (True, ("braking", 0.5))


@pytest.mark.parametrize("boundary", ["source", "brake_depth"])
def test_incompatible_temperature_response_samples_do_not_form_a_new_fit(boundary):
    from test_thermal_observer import observer
    from datetime import timedelta
    import math

    obs = observer()
    for minute in range(202):
        at = NOW + timedelta(minutes=minute)
        first = minute <= 100
        elapsed = minute if first else minute - 101
        value = 5 + 16 * math.exp(-(0.02 if first else 0.08) * elapsed / 60)
        source = "a" if first or boundary == "brake_depth" else "b"
        factor = 0.5 if first and boundary == "brake_depth" else 1.0
        obs.observe(
            at,
            report(value, at, selected_source=source),
            report(5, at),
            braking=True,
            control_context=("braking", factor),
        )
    assert obs.model.pending_samples == 10
    obs.model.fit()
    assert (
        not obs.model.is_valid
    )  # ten windows from each context are not twenty comparable windows

    for minute in range(202, 302):
        at = NOW + timedelta(minutes=minute)
        value = 5 + 16 * math.exp(-0.08 * (minute - 101) / 60)
        obs.observe(
            at,
            report(value, at, selected_source="b" if boundary == "source" else "a"),
            report(5, at),
            braking=True,
            control_context=("braking", 1.0),
        )
    obs.model.fit()
    assert obs.model.is_valid
    assert obs.model.k == pytest.approx(0.08, abs=0.001)


def test_same_calibration_context_accumulates_across_completed_brakes():
    from test_thermal_observer import observer
    from datetime import timedelta
    import math

    obs = observer()
    for minute in range(232):
        at = NOW + timedelta(minutes=minute)
        braking = not 100 < minute < 131
        elapsed = minute if minute <= 100 else max(0, minute - 131)
        value = 5 + 16 * math.exp(-0.03 * elapsed / 60)
        obs.observe(
            at,
            report(value, at, selected_source="same"),
            report(5, at),
            braking=braking,
            control_context=("braking", 1.0) if braking else ("normal", 0.0),
        )
    assert obs.model.pending_samples == 20
    obs.model.fit()
    assert obs.model.is_valid
    assert obs.model.k == pytest.approx(0.03, abs=0.001)


def test_short_brake_ramps_preserve_comparable_completed_samples():
    from test_thermal_observer import observer
    from datetime import timedelta
    import math

    obs = observer()
    minute = 0
    # Each full-depth episode supplies ten windows. Short ramps contain no
    # closed ten-minute window, and must not discard the previous episode.
    for episode in range(2):
        for factor in (0.1, 0.4, 0.7):
            at = NOW + timedelta(minutes=minute)
            obs.observe(
                at,
                report(21, at),
                report(5, at),
                braking=True,
                control_context=("braking", factor),
            )
            minute += 1
        for elapsed in range(101):
            at = NOW + timedelta(minutes=minute)
            obs.observe(
                at,
                report(5 + 16 * math.exp(-0.03 * elapsed / 60), at),
                report(5, at),
                braking=True,
                control_context=("braking", 1.0),
            )
            minute += 1
        for factor in (0.9, 0.6, 0.3):
            at = NOW + timedelta(minutes=minute)
            obs.observe(
                at,
                report(20.2, at),
                report(5, at),
                braking=True,
                control_context=("braking", factor),
            )
            minute += 1
        # The adapter fits before its factor-zero observer callback.
        assert obs.model.pending_samples == (episode + 1) * 10
        if episode == 1:
            obs.model.fit()
        at = NOW + timedelta(minutes=minute)
        obs.observe(
            at,
            report(20.2, at),
            report(5, at),
            braking=False,
            control_context=("normal", 0.0),
        )
        minute += 1
    assert obs.model.is_valid
    assert obs.model.k == pytest.approx(0.03, abs=0.001)
