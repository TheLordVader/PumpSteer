"""Passive cost comparison for externally supplied electrical forecasts.

Piecewise opportunity-cost principle adapted from Heat Pump Cost Optimizer
pv.py, MIT, copyright 2026 contributors, revision beb570c817c4efcae0d5f6f4bba52f3383baf8db.
No source code copied. Electrical power forecasts are assumptions, never heat output.
"""

import math
from datetime import UTC


def compare_costs(rows):
    """Compare matched intervals; never guess missing battery or tariff details."""
    unavailable = {"status": "unavailable", "saving_verified": False}
    if not isinstance(rows, (tuple, list)) or not 1 <= len(rows) <= 192:
        return unavailable
    sums = [0.0, 0.0]
    previous = None
    peak_fee_seen = False
    currency = None
    try:
        for row in rows:
            unit = row["currency"]
            if unit not in ("SEK/kWh", "EUR/kWh") or (
                currency is not None and unit != currency
            ):
                return unavailable
            currency = unit
            start, end = row["start"], row["end"]
            if start.tzinfo is None or end.tzinfo is None:
                return unavailable
            start, end = start.astimezone(UTC), end.astimezone(UTC)
            hours = (end - start).total_seconds() / 3600
            if not 0 < hours <= 1 or (previous is not None and start != previous):
                return unavailable
            previous = end
            battery = row["battery_dispatch"]
            fee = row["peak_fee"]
            if battery == "none":
                battery_kw = (0.0, 0.0)
                battery_price = 0.0
            elif isinstance(battery, dict) and battery.get("verified") is True:
                battery_kw = (battery["baseline_kw"], battery["pilot_kw"])
                battery_price = battery["opportunity_price"]
            else:
                return unavailable
            if fee == "none":
                peak_cost = (0.0, 0.0)
            elif isinstance(fee, dict) and fee.get("verified") is True:
                if peak_fee_seen:
                    return unavailable
                peak_fee_seen = True
                peak_cost = (fee["baseline_increment"], fee["pilot_increment"])
            else:
                return unavailable
            values = (*battery_kw, battery_price, *peak_cost)
            if any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in values
            ):
                return unavailable
            if any(v < 0 for v in (*battery_kw, *peak_cost)):
                return unavailable
            fields = [
                row[k]
                for k in (
                    "baseline_kw",
                    "pilot_kw",
                    "surplus_kw",
                    "import_price",
                    "export_price",
                )
            ]
            if fields[2] == 0 and fields[4] is None:
                fields[4] = 0.0  # Multiplied by zero; no export assumption is used.
            if any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for v in fields
            ):
                return unavailable
            baseline, pilot, surplus, imported, exported = fields
            if not all(0 <= v <= 100 for v in (baseline, pilot, surplus)):
                return unavailable
            for index, power in enumerate((baseline, pilot)):
                if battery_kw[index] > power:
                    return unavailable
                residual = power - battery_kw[index]
                covered = min(residual, surplus)
                sums[index] += (
                    hours
                    * (
                        battery_kw[index] * battery_price
                        + covered * exported
                        + (residual - covered) * imported
                    )
                    + peak_cost[index]
                )
    except (KeyError, TypeError, AttributeError, OverflowError):
        return unavailable
    return {
        "status": "estimated",
        "currency": currency.split("/")[0],
        "baseline_cost": sums[0],
        "pilot_cost": sums[1],
        "saving_verified": False,
        "basis": "externally_supplied_electrical_power_forecasts",
        "comparison_bound_to_replay": False,
        "fee_and_battery_verification": "provided_by_caller_not_independently_verified",
    }


def grid_import_forecast(states, now, horizon, *, intervals=None):
    """Conditional price per extra imported kWh, not total marginal cost.

    Reuse the existing timestamp/unit/coverage checks. Neither onsite generation,
    battery dispatch, unrepresented fees nor upstream forecast issue time are known.
    """
    from .pilot_forecast import price_window, source_is_current

    if intervals is None:
        intervals = price_window(states, now, horizon)
    if not intervals or len(intervals) > 128:
        return {"status": "unavailable", "reason": "price_forecast_unavailable"}
    units = {
        state.attributes.get("unit_of_measurement")
        for state in states
        if state is not None
        and source_is_current(state, now)
        and any(
            state.attributes.get(key)
            for key in ("raw_today", "raw_tomorrow", "prices", "forecast")
        )
    }
    if len(units) != 1 or next(iter(units), None) not in ("SEK/kWh", "EUR/kWh"):
        return {"status": "unavailable", "reason": "price_unit_unavailable"}
    return {
        "status": "available_conditional_grid_import",
        "unit": units.pop(),
        "intervals": [
            {"start": a.isoformat(), "end": b.isoformat(), "price_per_extra_kwh": v}
            for a, b, v in intervals
        ],
        "scope": "one_extra_kwh_imported_not_a_heat_plan",
        "price_basis": "configured_price_entity_fees_not_verified",
        "forecast_issue_time": "unknown",
        "solar_battery_and_peak_tariff": "unknown",
        "strategy_comparison": "unavailable",
    }
