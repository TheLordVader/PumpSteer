"""Bounded, event-driven estimates from HA-reported electrical power.

Inspired by Heat Pump Optimizer's strict power-unit handling. This is a
zero-order-hold estimate, not an energy meter or a thermal-power calculation.
"""

from datetime import timedelta
import math
from .pilot_forecast import _timestamp, price_window, source_is_current


class PowerLedger:
    def __init__(self, *, started_after=None):
        self.started_after = _timestamp(started_after)
        self.coverage_start = None
        self.coverage_end = None
        self.anchor = None
        self.energy = self.priced_energy = self.cost = 0.0
        self.radiator = self.hot_water = 0.0
        self.radiator_cost = self.hot_water_cost = 0.0
        self.radiator_priced = self.hot_water_priced = 0.0
        self.seconds = self.gaps = 0
        self.radiator_seconds = self.hot_water_seconds = 0
        self.currency = None
        self.mixed_currency = False
        self._price_cache = None

    @staticmethod
    def _value(state, now, *, power=False, started_after=None):
        if state is None or getattr(state, "state", None) in ("unknown", "unavailable"):
            return None
        attrs = getattr(state, "attributes", {}) or {}
        at = _timestamp(getattr(state, "last_reported", None))
        changed = _timestamp(getattr(state, "last_changed", None))
        if (
            attrs.get("restored")
            or at is None
            or changed is None
            or changed > at
            or not timedelta(0) <= now - at <= timedelta(minutes=5)
            or (started_after is not None and at <= started_after)
        ):
            return None
        if not power:
            return (at, changed, state.state) if state.state in ("on", "off") else None
        unit = attrs.get("unit_of_measurement")
        factor = {"W": 0.001, "kW": 1.0}.get(unit) if isinstance(unit, str) else None
        try:
            value = float(state.state)
        except (TypeError, ValueError, OverflowError):
            return None
        if factor is None or not math.isfinite(value) or not 0 <= value * factor <= 100:
            return None
        return at, changed, value * factor

    def reset_anchor(self, *, gap=False):
        if gap and self.anchor is not None:
            self.gaps += 1
        self.anchor = None

    def observe(
        self,
        when,
        power,
        heating,
        hot_water,
        prices,
        *,
        heating_configured=False,
        hot_water_configured=False,
    ):
        now = _timestamp(when)
        if now is None:
            self.anchor = None
            return
        current = self._value(power, now, power=True, started_after=self.started_after)
        # The endpoint must itself be an HA power report, not an unrelated
        # event carrying the last known power state.
        if current is not None and current[0] != now:
            current = None
        heat = self._value(heating, now)
        water = self._value(hot_water, now)
        previous = self.anchor
        if previous is not None:
            dt = (now - previous["time"]).total_seconds()
            if dt < 0:
                self.anchor = None
                self.gaps += 1
                return
            valid = (
                current is not None
                and 0 < dt <= 300
                and now - previous["power_at"] <= timedelta(minutes=5)
            )
            for signal in (current, heat, water):
                if signal is not None and previous["time"] < signal[
                    1
                ] < now - timedelta(seconds=1):
                    valid = False
            if valid:
                energy = previous["power"] * dt / 3600
                self.energy += energy
                self.seconds += dt
                if self.coverage_start is None:
                    self.coverage_start = previous["time"]
                self.coverage_end = now
                mode_current = previous["mode_at"] is not None and now - previous[
                    "mode_at"
                ] <= timedelta(minutes=5)
                if mode_current and previous["mode"] == "radiator":
                    self.radiator += energy
                    self.radiator_seconds += dt
                elif mode_current and previous["mode"] == "hot_water":
                    self.hot_water += energy
                    self.hot_water_seconds += dt
                segments = previous["prices"]
                if (
                    segments
                    and previous["currency"]
                    and (self.currency is None or self.currency == previous["currency"])
                ):
                    priced = sum(
                        previous["power"]
                        * max(
                            0.0,
                            (
                                min(now, end) - max(previous["time"], start)
                            ).total_seconds(),
                        )
                        / 3600
                        * price
                        for start, end, price in segments
                    )
                    self.cost += priced
                    if mode_current and previous["mode"] == "radiator":
                        self.radiator_cost += priced
                        self.radiator_priced += energy
                    elif mode_current and previous["mode"] == "hot_water":
                        self.hot_water_cost += priced
                        self.hot_water_priced += energy
                    self.priced_energy += energy
                    self.currency = previous["currency"]
                elif (
                    segments
                    and self.currency is not None
                    and previous["currency"] is not None
                    and self.currency != previous["currency"]
                ):
                    self.mixed_currency = True
            elif dt > 0:
                self.gaps += 1
        if current is None:
            self.anchor = None
            return
        mode = "unclassified"
        if (
            heat is not None
            and heat[2] == "on"
            and (
                (water is not None and water[2] == "off")
                or (water is None and not hot_water_configured)
            )
        ):
            mode = "radiator"
        elif (
            water is not None
            and water[2] == "on"
            and (
                (heat is not None and heat[2] == "off")
                or (heat is None and not heating_configured)
            )
        ):
            mode = "hot_water"
        signature = tuple(
            (
                getattr(source, "last_reported", None),
                getattr(source, "state", None),
                repr(getattr(source, "attributes", None)),
            )
            for source in prices
        )
        cache = self._price_cache
        if (
            cache is None
            or cache[0] != signature
            or not cache[1]
            or cache[1][0][0] > now
            or cache[1][-1][1] < now + timedelta(minutes=5)
        ):
            segments = price_window(prices, now, timedelta(minutes=15)) or price_window(
                prices, now, timedelta(minutes=5)
            )
            self._price_cache = (signature, segments)
        else:
            segments = cache[1]
        currency = None
        for source in prices:
            if (
                source is not None
                and source_is_current(source, now)
                and any(
                    (getattr(source, "attributes", {}) or {}).get(key)
                    for key in ("raw_today", "raw_tomorrow", "prices", "forecast")
                )
            ):
                unit = (getattr(source, "attributes", {}) or {}).get(
                    "unit_of_measurement"
                )
                if unit in ("SEK/kWh", "EUR/kWh"):
                    currency = unit.split("/")[0]
                    break
        self.anchor = {
            "time": now,
            "power_at": current[0],
            "power": current[2],
            "mode": mode,
            "mode_at": min(item[0] for item in (heat, water) if item is not None)
            if mode != "unclassified"
            else None,
            "prices": segments,
            "currency": currency,
        }

    def summary(self):
        # Do not treat a partial-price cost as the price of all observed energy.
        priced = (
            self.currency is not None and not self.mixed_currency and self.energy > 0
        )
        complete = priced and abs(self.priced_energy - self.energy) <= 1e-9
        return {
            "price_coverage": "complete"
            if complete
            else "partial"
            if priced and self.priced_energy > 0
            else "unavailable",
            "effective_price_per_kwh": round(self.cost / self.energy, 4)
            if complete
            else None,
            "price_basis": "configured_price_entity_not_verified_full_tariff",
            "price_coverage_scope": "observed_intervals_only",
            "method": "reported_power_held_between_reports",
            "measurement_time": "HA_report_not_physical",
            "energy_kwh_estimate": round(self.energy, 4),
            "priced_energy_kwh_estimate": round(self.priced_energy, 4),
            "cost_estimate": None
            if self.currency is None or self.mixed_currency
            else round(self.cost, 4),
            "currency": self.currency if not self.mixed_currency else None,
            "radiator_mode_kwh_estimate": round(self.radiator, 4)
            if self.radiator_seconds
            else None,
            "hot_water_mode_kwh_estimate": round(self.hot_water, 4)
            if self.hot_water_seconds
            else None,
            "radiator_cost_estimate": None
            if not self.radiator_seconds
            or self.currency is None
            or self.mixed_currency
            or abs(self.radiator_priced - self.radiator) > 1e-9
            else round(self.radiator_cost, 4),
            "hot_water_cost_estimate": None
            if not self.hot_water_seconds
            or self.currency is None
            or self.mixed_currency
            or abs(self.hot_water_priced - self.hot_water) > 1e-9
            else round(self.hot_water_cost, 4),
            "session_started": self.started_after.isoformat()
            if self.started_after
            else None,
            "coverage_start": self.coverage_start.isoformat()
            if self.coverage_start
            else None,
            "coverage_end": self.coverage_end.isoformat()
            if self.coverage_end
            else None,
            "radiator_priced_kwh_estimate": round(self.radiator_priced, 4),
            "hot_water_priced_kwh_estimate": round(self.hot_water_priced, 4),
            "unclassified_kwh_estimate": round(
                self.energy - self.radiator - self.hot_water, 4
            ),
            "radiator_seconds": int(self.radiator_seconds),
            "hot_water_seconds": int(self.hot_water_seconds),
            "cost_coverage": "priced_intervals_only",
            "qualified_seconds": int(self.seconds),
            "gap_count": self.gaps,
            "charges": "selected_price_only_not_verified_bill",
        }
