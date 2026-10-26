"""Integrity checks for the finite Recorder replay, not runtime changes."""

import pytest

from thermal_history_validation import latest_before, summarize_forecasts


def test_asof_never_uses_future_or_interpolates_missing_reports():
    rows = [{"updated": 10, "state": "21"}, {"updated": 40, "state": "22"}]
    assert latest_before(rows, 9) is None
    assert latest_before(rows, 20) is rows[0]
    assert latest_before(rows, 39) is rows[0]
    assert latest_before(rows, 40) is rows[1]


def test_missing_targets_remain_in_denominator_and_empty_error_is_unknown():
    records = [
        {"horizon": 30, "error_c": 0.2, "status": "evaluated"},
        {"horizon": 30, "error_c": None, "status": "interrupted"},
        {"horizon": 60, "error_c": None, "status": "missed"},
    ]
    result = summarize_forecasts(records)
    assert result["30"]["coverage"] == 0.5
    assert result["30"]["mae_c"] == 0.2
    assert result["60"]["coverage"] == 0
    assert result["60"]["mae_c"] is None
    assert result["120"]["coverage"] is None
    assert result["120"]["mae_c"] is None


def test_restart_retains_unmatched_forecasts_in_coverage():
    from datetime import datetime, timedelta, timezone
    from custom_components.pumpsteer.thermal_intelligence import (
        TemperatureMeasurement,
        ThermalDiagnostics,
    )
    from thermal_history_validation import finish_segment

    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    diagnostics = ThermalDiagnostics()
    diagnostics.observe(
        now=now,
        indoor=TemperatureMeasurement(21, now),
        outdoor=TemperatureMeasurement(5, now),
        k_per_hour=0.04,
        model_fitted=True,
        operating_mode="braking",
    )
    records = {}
    finish_segment(diagnostics, records, 2, now + timedelta(minutes=5), "restart")
    assert len(records) == 3
    assert summarize_forecasts(list(records.values()))["30"]["coverage"] == 0
    assert {r["status"] for r in records.values()} == {"restart"}
    assert not diagnostics._pending


def test_fit_waits_for_real_release_branch_and_survives_preheat():
    from thermal_history_validation import fit_boundary

    assert fit_boundary({"mode": "braking", "brake_factor": 1}, False) == (True, False)
    assert fit_boundary({"mode": "preheating", "brake_factor": 0}, True) == (
        True,
        False,
    )
    assert fit_boundary({"mode": "normal", "brake_factor": 0.1}, True) == (True, False)
    assert fit_boundary({"mode": "normal", "brake_factor": 0}, True) == (False, True)
    assert fit_boundary({"mode": "safe_mode"}, True) == (False, False)
    assert fit_boundary({"mode": "summer_mode"}, True) == (False, False)
    assert fit_boundary(
        {"mode": "normal", "brake_factor": 0, "aggressiveness": 0}, True
    ) == (False, False)


def test_episode_can_start_at_first_row_and_keeps_last_braking_temperature():
    from thermal_history_validation import episode_summary

    rows = [
        {
            "updated": 0,
            "attributes": {
                "mode": "braking",
                "brake_factor": 1,
                "indoor_temperature": 21,
            },
        },
        {
            "updated": 30,
            "attributes": {
                "mode": "braking",
                "brake_factor": 1,
                "indoor_temperature": 20.9,
            },
        },
        {"updated": 60, "attributes": {"mode": "safe_mode"}},
    ]
    result = episode_summary(rows, 0)
    assert len(result) == 1
    assert result[0]["controller_indoor_change_c"] == pytest.approx(-0.1)


def test_unknown_control_interrupts_pending_fit_like_live_shadow():
    from thermal_history_validation import fit_boundary

    active, fit = fit_boundary({"mode": "braking", "brake_factor": 1}, False)
    assert active and not fit
    active, fit = fit_boundary({"mode": None}, active)
    assert not active and not fit
    assert fit_boundary({"mode": "normal", "brake_factor": 0}, active) == (False, False)
