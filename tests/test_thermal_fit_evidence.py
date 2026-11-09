"""Only causal, observed and independently varying commands can qualify a fit."""

import math
from datetime import UTC, datetime, timedelta

from custom_components.pumpsteer.thermal_intelligence.rtm_core import (
    ModelConfig,
    Sample,
)
from custom_components.pumpsteer.thermal_intelligence.rtm_core.fit import (
    Prior,
    _Problem,
    build_dataset,
    excitation,
    fit_rc,
)
from custom_components.pumpsteer.thermal_intelligence.rtm_core.grid import (
    Grid,
    resample,
)
from custom_components.pumpsteer.thermal_intelligence.rtm_core.model import (
    RCThermalModel,
)

START = datetime(2026, 11, 1, tzinfo=UTC)
CFG = ModelConfig(step_minutes=5, max_dead_time_minutes=15)


def grid(n=100, effort=None):
    return Grid(
        5,
        START.timestamp(),
        [20 + 0.1 * math.sin(k / 7) for k in range(n)],
        [2 + math.sin(k / 11) for k in range(n)],
        list(effort) if effort is not None else [float((k // 9) % 2) for k in range(n)],
        [False] * n,
    )


def test_unknown_commands_are_not_zero_effort_evidence():
    g = grid(effort=[None] * 100)
    ds = build_dataset(g, CFG, g.end_ts)
    assert not ds.k
    result = RCThermalModel.fit_snapshot(g, CFG, g.end_ts)
    assert result["status"] == "insufficient_data"
    assert not result["excitation_sufficient"]


def test_delayed_command_cannot_cross_gap_or_excluded_segment():
    g = grid()
    g.excluded[40] = True
    g.effort[70] = None
    ds = build_dataset(g, CFG, g.end_ts)
    p = _Problem(ds, g.effort, Prior(), None)
    _, _, y, _ = p.rows(3)
    expected = [
        i
        for i, k in enumerate(ds.k)
        if all(
            j in ds.eligible and g.effort[j] is not None for j in range(k - 3, k + 1)
        )
    ]
    assert y == [ds.y[i] for i in expected]
    assert len(y) < len(ds)


def test_collinear_weather_command_is_not_identified_by_ridge():
    g = grid()
    for k in range(len(g)):
        g.effort[k] = k / len(g)
        g.outdoor[k] = 20 - 10 * g.effort[k]
        g.indoor[k] = 20
    ds = build_dataset(g, CFG, g.end_ts)
    assert excitation([g.effort[k] for k in ds.k], ds.w)[0]
    assert fit_rc(ds, g.effort, CFG) is None
    assert (
        RCThermalModel.fit_snapshot(g, CFG, g.end_ts)["status"]
        == "candidate_insufficient_excitation"
    )


def test_recency_weighted_command_variance_must_be_real():
    assert not excitation([0.0, 1.0, 1.0], [1e-8, 1.0, 1.0])[0]
    assert excitation([0.0, 1.0, 0.0, 1.0], [1.0, 1.0, 1.0, 1.0], [0.0, 0.0, 1.0, 1.0])[
        0
    ]


def test_refit_requires_new_usable_rows_not_grid_time_and_survives_restart():
    model = RCThermalModel(CFG)
    model._grid = grid(effort=[0.5] * 100)
    model._last_sample_ts = model._grid.end_ts
    now = datetime.fromtimestamp(model._grid.end_ts, UTC)
    assert model.fit_due(now)
    assert model.fit(now)["status"] == "candidate_insufficient_excitation"
    model._grid.append_rows(
        Grid(5, model._grid.end_ts, [20.0] * 20, [2.0] * 20, [None] * 20, [False] * 20)
    )
    model._last_sample_ts = model._grid.end_ts
    later = datetime.fromtimestamp(model._grid.end_ts + 3600, UTC)
    assert not model.fit_due(later)
    # Previously missing commands are genuine new usable rows, including backfill.
    model._grid.effort[-20:] = [float(k % 2) for k in range(20)]
    assert model.fit_due(later)
    model.fit(later)
    restored = RCThermalModel.from_dict(model.to_dict(), CFG)
    assert not restored.fit_due(later + timedelta(hours=2))


def test_future_grid_rows_cannot_qualify_or_train():
    g = grid()
    now = g.ts(20)
    assert len(build_dataset(g, CFG, now)) == 20
    result = RCThermalModel.fit_snapshot(g, CFG, now)
    assert result["status"] == "insufficient_data"
    assert result["rows"] == 20


def test_boundary_resets_all_carried_fields_even_with_command_only_updates():
    samples = [
        Sample(START, 20, 2, 1),
        Sample(START + timedelta(minutes=5), None, None, None, True),
        Sample(START + timedelta(minutes=10), None, None, 0),
        Sample(START + timedelta(minutes=15), 19.9, 3, None),
        Sample(START + timedelta(minutes=20), 19.8, 3, 0),
    ]
    g = resample(samples, 5, 15, 180)
    assert g.indoor == [20, None, None, 19.9]
    assert g.outdoor == [2, None, None, 3]
    assert g.effort == [1, None, 0, 0]
    assert g.excluded[1]


def test_internal_holdout_is_explicitly_not_independent():
    g = grid(200)
    result = RCThermalModel.fit_snapshot(g, CFG, g.end_ts)
    assert result["status"] == "ok"
    assert result["evaluation_independent"] is False


def test_partly_unknown_effort_interval_is_not_averaged_into_evidence():
    samples = [
        Sample(START, 20, 2, None),
        Sample(START + timedelta(minutes=2), None, None, 1),
        Sample(START + timedelta(minutes=5), 20, 2, 1),
    ]
    assert resample(samples, 5, 15).effort == [None]


def test_capture_continues_after_rejection_and_segment_boundary():
    model = RCThermalModel(CFG)
    for i in range(50):
        model.add_sample(Sample(START + timedelta(minutes=5 * i), 20, 2, 0.5))
    assert (
        model.fit(START + timedelta(minutes=250))["status"]
        == "candidate_insufficient_excitation"
    )
    before = len(model._grid)
    model.break_segment(START + timedelta(minutes=250))
    for i in range(51, 70):
        model.add_sample(Sample(START + timedelta(minutes=5 * i), 20, 2, float(i % 2)))
    assert len(model._grid) > before
    assert model._grid.excluded[50]
    assert model.fit_due(START + timedelta(minutes=350))


def test_internal_forecast_cannot_bridge_excluded_delayed_history():
    from custom_components.pumpsteer.thermal_intelligence.rtm_core.quality import (
        Params,
        bad_rows,
        forecast_rmse,
    )

    g = grid(30)
    g.excluded[2] = True
    _, _, windows = forecast_rmse(g, Params(1, 0.1, 0, 3, True), bad_rows(g, 1.5), 3, 4)
    assert windows == 0


def test_late_source_cannot_reappear_as_a_later_temperature_change():
    model = RCThermalModel(ModelConfig(step_minutes=15, max_gap_minutes=60))
    model.add_sample(Sample(START, 20, 2, 1))
    for minute in range(5, 36, 5):
        model.add_sample(Sample(START + timedelta(minutes=minute), None, None, 1))
    assert model._grid.indoor == [20, 20]
    model.add_sample(Sample(START + timedelta(minutes=15), 19, None, None))
    model.add_sample(Sample(START + timedelta(minutes=45), None, None, 1))
    assert model._grid.indoor == [20, 20, 20]
    assert all(sample.indoor_c != 19 for _, sample in model._pending)


def test_clock_rollback_boundary_severs_next_open_interval():
    model = RCThermalModel(ModelConfig(step_minutes=15, max_gap_minutes=60))
    model.add_sample(Sample(START, 20, 2, 1))
    model.add_sample(Sample(START + timedelta(minutes=30), None, None, 1))
    model.break_segment(START + timedelta(minutes=20))
    model.add_sample(Sample(START + timedelta(minutes=45), None, None, 1))
    assert model._grid.indoor[-1] is None
    assert model._grid.excluded[-1]
