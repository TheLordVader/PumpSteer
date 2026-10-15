"""Offline shadow calibration; independent episodes, never live model updates."""

import json

import pytest

from thermal_learning_experiment import benchmark, evaluate, fit_bias, forecasts


def episode(number, offset=0.4):
    # Hand-checked passive references for Tin=21, Tout=5, k=0.05, plus known bias.
    return {
        "id": number,
        "mode": "braking",
        "k": 0.05,
        "readings": {
            -10: (21.1, 5),
            0: (21, 5),
            30: (20.60495859245332 + offset, 5),
            60: (20.219670792011424 + offset, 5),
            120: (19.47739868857535 + offset, 5),
        },
        "missing": (),
    }


def test_fit_bias_uses_whole_independent_episodes_and_recovers_known_offset():
    correction = fit_bias([episode(0, 0.2), episode(1, 0.6)])
    assert correction == pytest.approx(
        {("braking", 30): 0.4, ("braking", 60): 0.4, ("braking", 120): 0.4}
    )
    assert fit_bias([episode(0)]) == {}  # not enough independent evidence


def test_duplicate_training_episode_cannot_fake_independent_evidence():
    with pytest.raises(ValueError):
        fit_bias([episode(0), episode(0)])


def test_calibration_refuses_incompatible_coefficients_and_unbounded_training():
    changed = episode(1)
    changed["k"] = 0.06
    with pytest.raises(ValueError):
        fit_bias([episode(0), changed])
    with pytest.raises(ValueError):
        fit_bias([episode(i) for i in range(13)])


def test_real_linear_helper_reference_and_recent_trend_remain_distinct_baselines():
    prediction = forecasts(episode(2))
    assert prediction["linear"][30] == pytest.approx(20.6)
    assert prediction["reference"][30] == pytest.approx(20.60495859245332)
    assert prediction["recent_trend"][30] == pytest.approx(20.7)


def test_frozen_calibration_improves_known_bias_on_later_unused_episode():
    correction = fit_bias([episode(0), episode(1)])
    rows = evaluate([episode(2)], correction)
    for row in rows:
        if row["method"] == "adaptive_bias":
            assert row["mae_c"] == pytest.approx(0, abs=1e-10)
        elif row["method"] == "reference":
            assert row["mae_c"] == pytest.approx(0.4)
    assert correction[("braking", 30)] == pytest.approx(0.4)


def test_missing_target_reduces_coverage_without_fabricating_zero_error():
    candidate = episode(2)
    candidate["missing"] = (60,)
    rows = evaluate([candidate], fit_bias([episode(0), episode(1)]))
    for row in rows:
        if row["horizon_minutes"] == 60:
            assert (row["eligible_count"], row["count"], row["coverage"]) == (1, 0, 0)
            assert row["mae_c"] is None and row["bias_c"] is None


def test_learned_correction_can_hurt_under_heating_distribution_shift():
    correction = fit_bias([episode(0), episode(1)])
    rows = evaluate([episode(2, offset=0)], correction)
    reference = next(
        r for r in rows if r["method"] == "reference" and r["horizon_minutes"] == 120
    )
    adaptive = next(
        r
        for r in rows
        if r["method"] == "adaptive_bias" and r["horizon_minutes"] == 120
    )
    assert reference["mae_c"] == pytest.approx(0)
    assert adaptive["mae_c"] == pytest.approx(0.4)


def test_chronological_synthetic_benchmark_reports_conditions_and_all_horizons():
    result = benchmark()
    assert len(result["training_ids"]) == len(result["validation_ids"]) == 12
    assert max(result["training_ids"]) < min(result["validation_ids"])
    assert set(result["training_ids"]).isdisjoint(result["validation_ids"])
    for group in (result["validation"], result["shifted_validation"]):
        assert len(group) == 36  # 3 modes × 3 horizons × 4 predictors
        assert {r["horizon_minutes"] for r in group} == {30, 60, 120}
        assert {r["mode"] for r in group} == {"normal", "braking", "preheating"}
        assert all(0 <= r["coverage"] <= 1 for r in group)
    print(json.dumps(result, sort_keys=True))
