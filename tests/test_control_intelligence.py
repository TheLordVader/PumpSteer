"""Synthetic output comparisons, not claims of measured energy savings."""

import ast
import subprocess
from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from custom_components.pumpsteer import sensor as sensor_module
from custom_components.pumpsteer import control_replay as replay
from custom_components.pumpsteer.control_intelligence import evaluate_limits

NOW = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)


def config():
    return dict(
        control_signals_verified=True,
        control_aux_preheat_limit=True,
        control_balance_brake_limit=True,
        control_balance_verified=True,
        control_balance_scale=-1,
        control_balance_offset=0,
        control_balance_deficit_threshold=100,
        control_balance_deficit_full=300,
        thermal_aux_heat_entity="binary_sensor.aux",
        thermal_compressor_entity="binary_sensor.compressor",
        thermal_heating_mode_entity="binary_sensor.heating",
        thermal_hot_water_mode_entity="binary_sensor.water",
        thermal_balance_entity="sensor.balance",
    )


def states(now=NOW):
    return {
        key: SimpleNamespace(
            state=value,
            last_reported=now,
            attributes={"measurement_time": now.isoformat()},
        )
        for key, value in [
            ("binary_sensor.aux", "on"),
            ("binary_sensor.compressor", "on"),
            ("binary_sensor.heating", "on"),
            ("binary_sensor.water", "off"),
            ("sensor.balance", "-300"),
        ]
    }


def inputs(cfg=None, *, expensive=False, observations=None):
    categories = (
        ("expensive",) * 96
        if expensive
        else ("normal",) * 40 + ("expensive",) * 8 + ("normal",) * 48
    )
    return replay.ControlReplayInput(
        NOW,
        cfg or {},
        20.8,
        0,
        21,
        18,
        3,
        False,
        categories,
        32,
        15,
        2,
        [-5] * 8,
        None,
        True,
        control_observation_states=observations,
    )


def test_auxiliary_removes_only_extra_preheat_without_freezing_pi():
    initial = replay.ControlReplayState(preheat_ramp=1, prev_aggressiveness=3)
    base, a = replay.replay_control_step(initial, inputs())
    enhanced, b = replay.replay_control_step(
        initial, inputs(config(), observations=states())
    )
    assert a.mode == b.mode == "preheating"
    assert b.fake_temp > a.fake_temp
    assert b.attributes["preheat_boost_c"] == 0
    assert enhanced.pi._integral == base.pi._integral
    assert b.fake_temp - a.fake_temp == pytest.approx(
        a.attributes["preheat_boost_c"], abs=0.01
    )


def test_verified_balance_releases_half_brake_but_preserves_integral_and_ramp():
    initial = replay.ControlReplayState(brake_ramp=1, prev_aggressiveness=3)
    base, a = replay.replay_control_step(initial, inputs(expensive=True))
    enhanced, b = replay.replay_control_step(
        initial, inputs(config(), expensive=True, observations=states())
    )
    assert a.mode == b.mode == "braking"
    assert b.fake_temp < a.fake_temp
    assert b.attributes["brake_factor"] == 0.5
    assert enhanced.pi._integral == base.pi._integral
    assert enhanced.brake_ramp == base.brake_ramp
    assert not any(
        details.get("active")
        for name, details in b.intentions
        if name == "thermal_learning"
    )


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "old",
        "republished",
        "restored",
        "water",
        "compressor",
        "brine",
        "unverified",
        "scale",
        "threshold",
    ],
)
def test_bad_or_unverified_signals_revert_to_base_output(failure):
    cfg, obs = config(), states()
    if failure == "missing":
        obs.pop("binary_sensor.heating")
    elif failure in ("old", "republished"):
        obs["binary_sensor.heating"].attributes["measurement_time"] = (
            NOW - timedelta(hours=1)
        ).isoformat()
        if failure == "old":
            obs["binary_sensor.heating"].last_reported = NOW - timedelta(hours=1)
    elif failure == "restored":
        obs["binary_sensor.heating"].attributes["restored"] = True
    elif failure == "water":
        obs["binary_sensor.water"].state = "on"
    elif failure == "compressor":
        obs["binary_sensor.compressor"].state = "off"
    elif failure == "brine":
        cfg["thermal_brine_pump_entity"] = "binary_sensor.missing"
    elif failure == "unverified":
        cfg["control_signals_verified"] = False
    elif failure == "scale":
        cfg["control_balance_scale"] = float("nan")
        cfg["control_aux_preheat_limit"] = False
    elif failure == "threshold":
        cfg["control_balance_deficit_full"] = 50
        cfg["control_aux_preheat_limit"] = False
    for expensive in (True, False):
        initial = replay.ControlReplayState(
            brake_ramp=1 if expensive else 0, preheat_ramp=1, prev_aggressiveness=3
        )
        base, a = replay.replay_control_step(initial, inputs(expensive=expensive))
        enhanced, b = replay.replay_control_step(
            initial, inputs(cfg, expensive=expensive, observations=obs)
        )
        assert a.fake_temp == b.fake_temp
        assert base.pi._integral == enhanced.pi._integral


def test_linear_balance_limit_and_opposite_verified_scale():
    cfg, obs = config(), states()
    for raw, multiplier in [
        ("-50", 1),
        ("-100", 1),
        ("-200", 0.75),
        ("-300", 0.5),
        ("-900", 0.5),
    ]:
        obs["sensor.balance"].state = raw
        assert evaluate_limits(NOW, cfg, obs)["brake_multiplier"] == multiplier
        cfg["control_balance_scale"] = 1
        obs["sensor.balance"].state = str(-float(raw))
        assert evaluate_limits(NOW, cfg, obs)["brake_multiplier"] == multiplier
        cfg["control_balance_scale"] = -1


def test_loss_of_signal_discards_previous_limit():
    initial = replay.ControlReplayState(brake_ramp=1, prev_aggressiveness=3)
    enhanced, _ = replay.replay_control_step(
        initial, inputs(config(), expensive=True, observations=states())
    )
    _, actual = replay.replay_control_step(
        enhanced,
        replace(inputs(config(), expensive=True), now=NOW + timedelta(minutes=1)),
    )
    _, expected = replay.replay_control_step(
        enhanced, replace(inputs(expensive=True), now=NOW + timedelta(minutes=1))
    )
    assert actual.fake_temp == expected.fake_temp


def test_base_matches_installed_reference_control_branches(monkeypatch):
    """Execute installed commit's real branch method against the same state and inputs."""
    source = subprocess.check_output(
        ["git", "show", "b273afc:custom_components/pumpsteer/sensor.py"], text=True
    )
    reference_class = next(
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.ClassDef) and n.name == "PumpSteerSensor"
    )
    baseline = next(
        n
        for n in reference_class.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_control_logic"
    )
    scope = dict(vars(sensor_module))
    exec(
        compile(
            ast.Module(body=[baseline], type_ignores=[]), "<b273afc control>", "exec"
        ),
        scope,
    )
    reference = scope["_run_control_logic"]
    for module in ("control.py", "control_ramps.py", "settings.py"):
        original = subprocess.check_output(
            ["git", "show", f"b273afc:custom_components/pumpsteer/{module}"], text=True
        )
        current = Path(sensor_module.__file__).with_name(module).read_text()
        assert ast.dump(ast.parse(original)) == ast.dump(ast.parse(current))
    # Helpers invoked by the branch method must also be unchanged against reference.
    current_class = next(
        n
        for n in ast.parse(Path(sensor_module.__file__).read_text()).body
        if isinstance(n, ast.ClassDef) and n.name == "PumpSteerSensor"
    )
    for name in (
        "_pi_output",
        "_update_brake_ramp",
        "_update_preheat_ramp",
        "_brake_temp",
        "_should_precool",
        "_compute_ramp_minutes",
        "_comfort_floor",
    ):
        old = next(
            n
            for n in reference_class.body
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        new = next(
            n
            for n in current_class.body
            if isinstance(n, ast.FunctionDef) and n.name == name
        )
        assert ast.dump(old) == ast.dump(new)
    for expensive in (False, True):
        for indoor in (19, 20.8, 22):
            for outdoor in (-15, 0, 20):
                for aggression in (0, 3, 5):
                    initial = replay.ControlReplayState(
                        brake_ramp=0.8, preheat_ramp=0.7, prev_aggressiveness=3
                    )
                    data = replace(
                        inputs(expensive=expensive),
                        indoor=indoor,
                        outdoor=outdoor,
                        aggressiveness=aggression,
                    )
                    current, actual = replay.replay_control_step(initial, data)
                    with monkeypatch.context() as m:
                        m.setattr(replay._Scratch, "_run_control_logic", reference)
                        old, expected = replay.replay_control_step(initial, data)
                    assert actual == expected
                    assert vars(current.pi) == vars(old.pi)
                    assert current.brake_ramp == old.brake_ramp
                    assert current.preheat_ramp == old.preheat_ramp


@pytest.mark.parametrize(
    "date", ["2026-03-29T00:59:00+00:00", "2026-10-25T00:59:00+00:00"]
)
def test_physical_freshness_uses_elapsed_time_across_dst(date):
    from zoneinfo import ZoneInfo

    report = datetime.fromisoformat(date).astimezone(ZoneInfo("Europe/Stockholm"))
    cfg, obs = config(), states(report)
    current = (report.astimezone(timezone.utc) + timedelta(minutes=2)).astimezone(
        report.tzinfo
    )
    assert evaluate_limits(current, cfg, obs)["active"]
    current = (report.astimezone(timezone.utc) + timedelta(minutes=6)).astimezone(
        report.tzinfo
    )
    assert not evaluate_limits(current, cfg, obs)["active"]


@pytest.mark.parametrize(
    "overrides", [{"outdoor": 20}, {"aggressiveness": 0}, {"prices_available": False}]
)
def test_no_intervention_in_summer_pure_pi_or_safe(overrides):
    initial = replay.ControlReplayState(brake_ramp=1, prev_aggressiveness=3)
    _, base = replay.replay_control_step(
        initial, replace(inputs(expensive=True), **overrides)
    )
    _, enhanced = replay.replay_control_step(
        initial,
        replace(inputs(config(), expensive=True, observations=states()), **overrides),
    )
    assert enhanced.fake_temp == base.fake_temp
    assert enhanced.mode == base.mode


def test_selecting_sensors_alone_never_enables_control():
    cfg = config()
    cfg.pop("control_aux_preheat_limit")
    cfg.pop("control_balance_brake_limit")
    for expensive in (False, True):
        initial = replay.ControlReplayState(
            brake_ramp=1, preheat_ramp=1, prev_aggressiveness=3
        )
        _, base = replay.replay_control_step(initial, inputs(expensive=expensive))
        _, observed = replay.replay_control_step(
            initial, inputs(cfg, expensive=expensive, observations=states())
        )
        assert observed == base


@pytest.mark.parametrize(
    "alias",
    [
        "thermal_heating_mode_entity",
        "thermal_hot_water_mode_entity",
        "thermal_aux_heat_entity",
        "thermal_brine_pump_entity",
    ],
)
def test_aliased_signals_cannot_verify_auxiliary_intervention(alias):
    cfg = config()
    cfg["control_balance_brake_limit"] = False
    cfg[alias] = cfg["thermal_compressor_entity"]
    assert not evaluate_limits(NOW, cfg, states())["active"]


def test_auxiliary_release_restores_bounded_base_boost():
    state = replay.ControlReplayState(preheat_ramp=0.5, prev_aggressiveness=3)
    next_state, limited = replay.replay_control_step(
        state, inputs(config(), observations=states())
    )
    _, restored = replay.replay_control_step(
        next_state, replace(inputs(config()), now=NOW + timedelta(minutes=1))
    )
    _, base = replay.replay_control_step(
        next_state, replace(inputs(), now=NOW + timedelta(minutes=1))
    )
    assert restored.fake_temp == base.fake_temp
    assert 0 <= restored.attributes["preheat_boost_c"] <= 4


@pytest.mark.parametrize(
    "raw,expected",
    [("off", True), ("on", False), ("unknown", False), ("unavailable", False)],
)
def test_verified_inverted_heating_mode_uses_existing_semantics(raw, expected):
    cfg, obs = config(), states()
    cfg["thermal_heating_mode_inverted"] = True
    obs["binary_sensor.heating"].state = raw
    limits = evaluate_limits(NOW, cfg, obs)
    assert limits["active"] is expected
    assert (limits["preheat_multiplier"] < 1) is expected
    assert (limits["brake_multiplier"] < 1) is expected
