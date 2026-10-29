"""Bounded, event-driven estimates from HA-reported electrical power.

Inspired by Heat Pump Optimizer's strict power-unit handling. This is a
zero-order-hold estimate, not an energy meter or a thermal-power calculation.
"""

import math
from copy import deepcopy
from datetime import timedelta
from itertools import pairwise
from types import SimpleNamespace

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
        self._energy_anchors = {}
        self._energy_updated = {}
        self._energy_intervals = {}
        self._meter_ids = None
        self._meter_qualified = 0.0
        self._energy_price_version = None

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
        if gap and (
            self.anchor is not None
            or self._energy_anchors
            or any(self._energy_intervals.values())
        ):
            self.gaps += 1
        self.anchor = None
        self._energy_anchors.clear()
        self._energy_updated.clear()
        self._energy_intervals = {key: [] for key in self._meter_ids or ()}

    def sync_energy_prices(self, prices, when):
        """Prevent a meter report overtaking a queued price change."""
        version = tuple(
            (
                getattr(source, "last_updated", None),
                getattr(source, "last_changed", None),
            )
            if source is not None
            else None
            for source in prices
        )
        if (
            self._energy_price_version is not None
            and version != self._energy_price_version
        ):
            self.invalidate_energy_prices(prices, when)
            for queue in self._energy_intervals.values():
                for item in queue:
                    if not self._same_energy_prices(
                        item[3], item[4], prices, when, item[0], item[1] - item[0]
                    ):
                        item[3] = item[4] = None
        self._energy_price_version = version

    @staticmethod
    def _same_energy_prices(snapshot, currency, prices, when, at, horizon):
        """Compare price evidence without treating later data as earlier knowledge."""
        now = _timestamp(when)
        sources = [source for source in prices or () if source_is_current(source, now)]
        units = [
            (getattr(source, "attributes", {}) or {}).get("unit_of_measurement")
            for source in sources
            if any(
                (getattr(source, "attributes", {}) or {}).get(k)
                for k in ("raw_today", "raw_tomorrow", "prices", "forecast")
            )
        ]
        if (
            not snapshot
            or not currency
            or not units
            or not all(unit == currency + "/kWh" for unit in units)
        ):
            return False
        comparison = tuple(
            SimpleNamespace(
                state=source.state, last_reported=at, attributes=source.attributes
            )
            for source in sources
        )
        old = price_window(snapshot, at, horizon)
        return old is not None and price_window(comparison, at, horizon) == old

    def invalidate_energy_prices(self, prices=None, when=None):
        """Keep a price snapshot only if the updated forecast agrees."""
        for key, (at, value, snapshot, currency) in list(self._energy_anchors.items()):
            if not self._same_energy_prices(
                snapshot, currency, prices, when, at, timedelta(minutes=5)
            ):
                self._energy_anchors[key] = (at, value, None, None)

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

    def can_poll_energy(self, meter_id, state):
        """Only poll reports known not to overtake queued state changes."""
        changed = _timestamp(getattr(state, "last_changed", None))
        if changed is None:
            return False
        updated = _timestamp(getattr(state, "last_updated", None)) or changed
        previous = self._energy_anchors.get(meter_id)
        if previous is None:
            return self.started_after is not None and updated <= self.started_after
        if updated != self._energy_updated.get(meter_id) or changed > previous[0]:
            return False
        attrs = getattr(state, "attributes", {}) or {}
        unit = attrs.get("unit_of_measurement")
        factor = {"Wh": 0.001, "kWh": 1.0}.get(unit) if isinstance(unit, str) else None
        try:
            return factor is not None and float(state.state) * factor == previous[1]
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False

    def observe_energy(self, when, meters, prices, *, meter_ids=None):
        """Record each meter independently, then intersect qualified intervals.

        Counter deltas have no intra-interval shape, so their allocation is
        explicitly time-weighted; it is not an instantaneous measurement.
        """
        now = _timestamp(when)
        ids = tuple(sorted(meter_ids if meter_ids is not None else meters))
        if not ids or len(set(ids)) != len(ids):
            return
        if self._meter_ids is None:
            self._meter_ids = ids
            self._energy_intervals = {key: [] for key in ids}
        if ids != self._meter_ids or now is None:
            self.reset_anchor(gap=True)
            return
        for meter_id, state in meters.items():
            if meter_id not in self._energy_intervals:
                continue
            attrs = getattr(state, "attributes", {}) or {}
            at = _timestamp(getattr(state, "last_reported", None))
            unit = attrs.get("unit_of_measurement")
            factor = (
                {"Wh": 0.001, "kWh": 1.0}.get(unit) if isinstance(unit, str) else None
            )
            try:
                value = float(state.state) * factor if factor is not None else None
            except (AttributeError, TypeError, ValueError, OverflowError):
                value = None
            if (
                at is None
                or at != now
                or attrs.get("restored")
                or value is None
                or not math.isfinite(value)
                or value < 0
                or (self.started_after is not None and at <= self.started_after)
            ):
                if (at == now or state is None) and meter_id in self._energy_anchors:
                    self._energy_anchors.pop(meter_id)
                    self._energy_updated.pop(meter_id, None)
                    self.gaps += 1
                continue
            previous = self._energy_anchors.get(meter_id)
            if previous is not None:
                start, old, price_snapshot, currency = previous
                dt = (at - start).total_seconds()
                if 0 < dt <= 300 and value >= old:
                    delta = value - old
                    self._meter_qualified += delta
                    queue = self._energy_intervals[meter_id]
                    queue.append([start, at, delta / dt, price_snapshot, currency])
                    if len(queue) > 1024:
                        queue.pop(0)
                        self.gaps += 1
                elif dt > 0:
                    self.gaps += 1
            if previous is None or at > previous[0]:
                price_snapshot = tuple(
                    SimpleNamespace(
                        state=getattr(source, "state", None),
                        last_reported=getattr(source, "last_reported", None),
                        attributes=deepcopy(getattr(source, "attributes", {}) or {}),
                    )
                    for source in prices
                    if source is not None
                )
                units = [
                    (
                        (getattr(source, "attributes", {}) or {}).get(
                            "unit_of_measurement"
                        )
                    )
                    for source in prices
                    if source_is_current(source, at)
                    and any(
                        (getattr(source, "attributes", {}) or {}).get(k)
                        for k in ("raw_today", "raw_tomorrow", "prices", "forecast")
                    )
                ]
                currency = (
                    units[0].split("/")[0]
                    if units
                    and all(unit == units[0] for unit in units)
                    and isinstance(units[0], str)
                    and units[0] in ("SEK/kWh", "EUR/kWh")
                    else None
                )
                self._energy_anchors[meter_id] = (at, value, price_snapshot, currency)
                self._energy_updated[meter_id] = _timestamp(
                    getattr(state, "last_updated", None)
                ) or _timestamp(getattr(state, "last_changed", None))
        self._collect_energy_intersections()

    def _collect_energy_intersections(self):
        queues = self._energy_intervals
        while queues and all(queues[key] for key in self._meter_ids):
            intervals = [queues[key][0] for key in self._meter_ids]
            start = max(item[0] for item in intervals)
            end = min(item[1] for item in intervals)
            if start < end:
                seconds = (end - start).total_seconds()
                rate = sum(item[2] for item in intervals)
                energy = rate * seconds
                self.energy += energy
                self.seconds += seconds
                if self.coverage_start is None:
                    self.coverage_start = start
                self.coverage_end = end
                # Split at every available price boundary. Missing or conflicting
                # prices leave only that part of the common interval unpriced.
                boundaries = {start, end}
                for item in intervals:
                    for source in item[3] or ():
                        attrs = source.attributes
                        for name in ("raw_today", "raw_tomorrow", "prices", "forecast"):
                            entries = attrs.get(name, [])
                            if not isinstance(entries, list):
                                continue
                            for entry in entries:
                                if not isinstance(entry, dict):
                                    continue
                                for field in ("start", "end"):
                                    point = _timestamp(entry.get(field))
                                    if point is not None and start < point < end:
                                        boundaries.add(point)
                ordered = sorted(boundaries)
                currencies = {item[4] for item in intervals}
                for left, right in pairwise(ordered):
                    if len(currencies) != 1 or None in currencies:
                        continue
                    windows = [
                        price_window(item[3], left, right - left) if item[3] else None
                        for item in intervals
                    ]
                    if any(window is None for window in windows) or any(
                        window != windows[0] for window in windows[1:]
                    ):
                        continue
                    currency = next(iter(currencies))
                    if self.currency is not None and self.currency != currency:
                        self.mixed_currency = True
                        continue
                    self.currency = currency
                    self.priced_energy += rate * (right - left).total_seconds()
                    self.cost += sum(
                        rate * (b - a).total_seconds() * price
                        for a, b, price in windows[0]
                    )
            # Consume exactly the common prefix; never count it twice.
            for key, item in zip(self._meter_ids, intervals):
                if item[1] <= max(start, end):
                    queues[key].pop(0)
                elif start < end:
                    item[0] = end

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
            "method": "cumulative_energy_time_weighted"
            if self._meter_ids is not None
            else "reported_power_held_between_reports",
            "qualified_meter_energy_kwh": round(self._meter_qualified, 4)
            if self._meter_ids is not None
            else None,
            "meter_count": len(self._meter_ids)
            if self._meter_ids is not None
            else None,
            "energy_allocation": "uniform_between_reports"
            if self._meter_ids is not None
            else "power_held_between_reports",
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
