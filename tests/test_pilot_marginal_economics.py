from datetime import UTC, datetime, timedelta

import pytest

from custom_components.pumpsteer.pilot_marginal import compare_costs

T = datetime(2026, 10, 1, tzinfo=UTC)


def row(i, **kw):
    d = {
        "start": T + timedelta(minutes=15 * i),
        "end": T + timedelta(minutes=15 * (i + 1)),
        "baseline_kw": 2,
        "pilot_kw": 1,
        "surplus_kw": 0.5,
        "import_price": 1.0,
        "export_price": 0.2,
        "battery_dispatch": "none",
        "peak_fee": "none",
        "currency": "SEK/kWh",
    }
    d.update(kw)
    return d


def test_solar_partial_and_signed_negative_prices():
    result = compare_costs([row(0), row(1, import_price=-0.5)])
    assert result["status"] == "estimated"
    assert result["baseline_cost"] == 0.4 + 0.25 * (-0.75 + 0.1)
    assert result["pilot_cost"] == 0.15 + 0.25 * (-0.25 + 0.1)
    assert result["saving_verified"] is False


def test_unknown_battery_and_unverified_peak_fee_abstain():
    for changes in (
        {"battery_dispatch": "unknown"},
        {"peak_fee": 1.0},
        {"export_price": None},
        {"surplus_kw": -1},
    ):
        assert compare_costs([row(0, **changes)])["status"] == "unavailable"


def test_missing_timestamps_and_gaps_abstain():
    assert compare_costs([row(0), row(2)])["status"] == "unavailable"


def test_basic_installation_needs_no_export_price_when_no_surplus():
    result = compare_costs([row(0, surplus_kw=0.0, export_price=None)])
    assert result["status"] == "estimated"
    assert result["baseline_cost"] == 0.5


def test_alignment_follows_utc_over_repeated_swedish_hour():
    from zoneinfo import ZoneInfo

    from custom_components.pumpsteer.pilot_runtime_plan import (
        _economic_intervals_aligned,
    )

    start = datetime(2026, 10, 25, 2, 45, tzinfo=ZoneInfo("Europe/Stockholm"), fold=0)
    utc = start.astimezone(UTC)
    rows = [
        {
            "issued_at": utc,
            "valid_until": utc + timedelta(hours=1),
            "start": utc + timedelta(minutes=15 * i),
            "end": utc + timedelta(minutes=15 * (i + 1)),
        }
        for i in range(4)
    ]
    assert _economic_intervals_aligned(rows, start, 4)
    rows[1]["start"] += timedelta(minutes=15)
    assert not _economic_intervals_aligned(rows, start, 4)


def test_explicit_battery_opportunity_cost_and_verified_peak_delta():
    battery = {
        "verified": True,
        "baseline_kw": 0.5,
        "pilot_kw": 0.25,
        "opportunity_price": 0.4,
    }
    fee = {"verified": True, "baseline_increment": 1.2, "pilot_increment": 0.3}
    result = compare_costs(
        [
            row(
                0,
                surplus_kw=0,
                export_price=None,
                battery_dispatch=battery,
                peak_fee=fee,
            )
        ]
    )
    assert result["status"] == "estimated"
    assert result["baseline_cost"] == pytest.approx(0.25 * (1.5 + 0.5 * 0.4) + 1.2)
    assert result["pilot_cost"] == pytest.approx(0.25 * (0.75 + 0.25 * 0.4) + 0.3)
    assert result["saving_verified"] is False


def test_repeated_peak_period_charge_is_rejected():
    fee = {"verified": True, "baseline_increment": 1.0, "pilot_increment": 0.5}
    assert (
        compare_costs([row(0, peak_fee=fee), row(1, peak_fee=fee)])["status"]
        == "unavailable"
    )


def test_unknown_peak_fee_abstains():
    assert compare_costs([row(0, peak_fee=None)])["status"] == "unavailable"


def test_currency_changes_are_not_summed():
    assert (
        compare_costs([row(0, currency="SEK/kWh"), row(1, currency="EUR/kWh")])[
            "status"
        ]
        == "unavailable"
    )


def test_future_issued_economic_forecast_is_rejected():
    from custom_components.pumpsteer.pilot_runtime_plan import (
        _economic_intervals_aligned,
    )

    r = {
        "start": T,
        "end": T + timedelta(minutes=15),
        "issued_at": T + timedelta(seconds=1),
        "valid_until": T + timedelta(hours=1),
    }
    assert not _economic_intervals_aligned([r], T, 1)


def test_expired_forecast_is_rejected():
    from custom_components.pumpsteer.pilot_runtime_plan import (
        _economic_intervals_aligned,
    )

    r = {
        "start": T,
        "end": T + timedelta(minutes=15),
        "issued_at": T - timedelta(days=1),
        "valid_until": T,
    }
    assert not _economic_intervals_aligned([r], T, 1)
