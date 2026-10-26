"""Finite, causal efficacy holdout using real PI code and simulated hardware.

Run with: python tests/thermal_complete_validation.py --output /private/holdout
Six parameter cases are fixed below before results. No model restore, injected
fit, threshold tuning, future truth forecast, HA core, network, or actuation.
The separately predeclared three-case blind suite requires --suite blind.
"""

import argparse
import asyncio
from collections import Counter, deque
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import resource
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]
import ha_test_stubs  # noqa: E402,F401
from test_brake_provenance import _sensor  # noqa: E402
from custom_components.pumpsteer import sensor as sensor_module  # noqa: E402
from custom_components.pumpsteer.forecast import (  # noqa: E402
    ThermalForecast,
    WeatherSlot,
    PriceSlot,
)
from custom_components.pumpsteer.thermal_assist import simulate_assistance  # noqa: E402
from thermal_observation import Shadow, Journal, accept_reading, recover_records, stamp  # noqa: E402
from thermal_observation_evaluate import summarize_shadow  # noqa: E402

START = datetime(2026, 11, 2, tzinfo=timezone.utc)
TRAIN_MINUTES = 4 * 24 * 60
TOTAL_MINUTES = 6 * 24 * 60
IDS = ("sensor.inside", "sensor.outside", "sensor.controller")
MODEL = ROOT / "custom_components/pumpsteer"


@dataclass(frozen=True)
class Case:
    name: str
    air_capacity: float
    mass_capacity: float
    coupling: float
    air_loss: float
    mass_loss: float
    delivery_lag_hours: float
    reporting_minutes: tuple
    extra_kw: float = 0
    dropout: bool = False
    drift: bool = False
    missing_channels: bool = False
    heat_curve_kw_per_c: float = 0.28


# This finite case matrix is an input specification, never selected by outcomes.
CASES = (
    Case("slow_5min", 2.2, 24, 1.4, 0.15, 0.08, 0.3, (5,)),
    Case(
        "fast_5_15min", 1.2, 8, 1.0, 0.24, 0.10, 0.25, (5, 15), heat_curve_kw_per_c=0.35
    ),
    Case(
        "stove_irregular", 1.8, 18, 1.2, 0.18, 0.10, 0.4, (5, 15, 5, 30), extra_kw=3.5
    ),
    Case("lagged_15_30min", 2.0, 22, 1.3, 0.18, 0.09, 1.0, (15, 30)),
    Case(
        "front_dropout_60min",
        1.6,
        14,
        1.1,
        0.20,
        0.10,
        0.5,
        (5, 60, 15, 5),
        extra_kw=2.5,
        dropout=True,
    ),
    Case(
        "drift_missing",
        1.8,
        18,
        1.2,
        0.18,
        0.10,
        0.4,
        (5, 15, 30, 60),
        extra_kw=3,
        drift=True,
        missing_channels=True,
    ),
)


# Independent final holdout, locked before seeing the residual-state prototype.
# These cases are only selected through the explicit --suite blind CLI option.
BLIND_CASES = (
    Case(
        "blind_slow", 2.6, 30, 1.1, 0.14, 0.06, 0.55, (5, 15), heat_curve_kw_per_c=0.23
    ),
    Case(
        "blind_fast", 1.6, 6, 0.9, 0.26, 0.07, 0.15, (15, 30), heat_curve_kw_per_c=0.33
    ),
    Case(
        "blind_lagged",
        1.9,
        26,
        1.5,
        0.20,
        0.06,
        1.5,
        (5, 30, 60),
        extra_kw=2.2,
        drift=True,
        dropout=True,
        missing_channels=True,
        heat_curve_kw_per_c=0.28,
    ),
)


def phase(minute):
    return "train" if minute < TRAIN_MINUTES else "eval"


def classify_heat_window(baseline_confirmed, suspected, truth):
    """An unqualified reference abstains; it cannot earn TN or incur FN."""
    if baseline_confirmed is not True:
        return "abstained_heat" if truth else "abstained_no_heat"
    if suspected:
        return "tp" if truth else "fp"
    return "fn" if truth else "tn"


def select_offline_advice(
    *, cached, result, now, previous_context, next_context, latest, forecast
):
    """One bounded proposal; recheck evidence every cycle without redating it."""
    current = result.get("recommendation", {})
    if current.get("eligible") is True:
        candidate = dict(recommendation=dict(current), context=dict(previous_context))
    elif current.get("reason") == "awaiting_current_forecast_issuance":
        candidate = cached
    else:
        return None
    if (
        candidate is None
        or result.get("status") != "conditional"
        or result.get("response", {}).get("confirmed") is not True
        or result.get("model_version") is None
    ):
        return None
    rec = candidate["recommendation"]
    issued = stamp(rec.get("issued_at"))
    valid_for = rec.get("valid_for_minutes")
    if (
        issued is None
        or type(valid_for) not in (int, float)
        or not 0 < valid_for <= 30
        or not 0 <= (now - issued).total_seconds() <= 60 * valid_for
        or rec.get("model_version") != result["model_version"]
        or rec.get("operating_mode") != next_context.get("mode")
        or next_context.get("mode")
        not in ("normal", "holiday", "braking", "brake_hold", "recovery", "preheating")
    ):
        return None
    for key in (
        "mode",
        "brake_factor",
        "target_temperature",
        "aggressiveness",
        "preheat_enabled",
    ):
        if (
            not candidate["context"].get(key)
            == previous_context.get(key)
            == next_context.get(key)
        ):
            return None
    try:
        for entity in IDS:
            raw = latest.get(entity, {})
            attrs = raw.get("attributes", {})
            reported = stamp(raw.get("last_reported"))
            number = raw.get("state")
            if (
                reported is None
                or not 0
                <= (now - reported).total_seconds()
                <= (120 if entity == IDS[2] else 900)
                or isinstance(number, bool)
                or not math.isfinite(float(number))
                or raw.get("_quarantined")
                or attrs.get("restored")
                or attrs.get("unit_of_measurement") != "°C"
            ):
                return None
        outdoor = float(latest[IDS[1]]["state"])
        end = now + timedelta(hours=2)
        if (
            forecast is None
            or not forecast.weather_path(now, end, outdoor)
            or not forecast.prices_between(now, end)
            or not forecast.price_unit
        ):
            return None
    except (TypeError, ValueError):
        return None
    return candidate


def climatology(hour):
    return 4 + 2.3 * math.sin(2 * math.pi * (hour - 8) / 24)


def truth_weather(case, minute):
    hour = minute / 60
    # Fronts are hidden disturbances, absent from the published forecast law.
    front = -3.2 * (math.tanh((hour - 42) / 2) - math.tanh((hour - 62) / 3)) / 2
    front += 4 * (math.tanh((hour - 108) / 1.5) - math.tanh((hour - 129) / 3)) / 2
    return climatology(hour) + front + 0.25 * math.sin(hour * 0.67)


def extra_power(case, minute):
    day, within = divmod(minute, 1440)
    if day < 4:
        on = day in (1, 3) and 18 * 60 <= within < 20 * 60
    else:
        on = (day == 4 and 6 * 60 <= within < 8 * 60) or (
            day == 5 and 14 * 60 <= within < 16 * 60
        )
    return case.extra_kw if on else 0


def internal_power(minute):
    hour = minute / 60
    solar = 0.45 * max(0, math.sin(math.pi * (hour % 24 - 7) / 12))
    occupancy = 0.15 if 17 <= hour % 24 < 23 else 0
    return 0.30 + solar + occupancy + 0.05 * math.sin(hour * 1.3)


def spot_price(minute):
    day, within = divmod(minute, 1440)
    hour = within / 60
    peaks = ((7, 9), (17, 19)) if day < 4 else ((10, 12), (19, 21))
    peak = any(a <= hour < b for a, b in peaks)
    return (
        0.65 + 0.12 * math.sin(hour * math.pi / 12) + (2.2 + 0.15 * day if peak else 0)
    )


def channels_available(case, minute):
    within = minute % 1440
    weather = not (case.missing_channels and 10 * 60 <= within < 14 * 60)
    price = not (case.missing_channels and 2 * 60 <= within < 3 * 60)
    return weather, price


def published_forecast(case, minute, observed_outdoor):
    """As-of climatology plus a decaying current offset, with no truth lookahead."""
    at = START + timedelta(minutes=minute)
    issued = at.replace(minute=0, second=0, microsecond=0)
    observed_offset = observed_outdoor - climatology(minute / 60)
    weather_ok, price_ok = channels_available(case, minute)
    weather = (
        tuple(
            WeatherSlot(
                issued + timedelta(hours=h),
                issued + timedelta(hours=h + 1),
                climatology((issued - START).total_seconds() / 3600 + h + 0.5)
                + observed_offset * math.exp(-h / 6),
            )
            for h in range(25)
        )
        if weather_ok
        else ()
    )
    midnight = at.replace(hour=0, minute=0, second=0, microsecond=0)
    # Tomorrow is genuinely published only after 13:00; no future price slots
    # are admitted before this publication, even though the truth law is fixed.
    slots = 192 if at.hour >= 13 else 96
    prices = (
        tuple(
            PriceSlot(
                midnight + timedelta(minutes=15 * i),
                midnight + timedelta(minutes=15 * (i + 1)),
                spot_price(int((midnight - START).total_seconds() / 60) + 15 * i),
            )
            for i in range(slots)
        )
        if price_ok
        else ()
    )
    return ThermalForecast(
        at,
        "weather.synthetic",
        "sensor.price",
        issued if weather_ok else None,
        weather,
        prices,
        at if price_ok else None,
        "SEK/kWh" if price_ok else None,
    )


class Plant:
    """Two thermal stores (kWh/°C), conductances (kW/°C), lagged heat curve."""

    def __init__(self, case):
        self.case = case
        self.air = self.mass = 21.0
        self.delivered_kw = case.heat_curve_kw_per_c * (20.2 - truth_weather(case, 0))
        self.heat_kwh = self.electric_kwh = 0.0

    def step(self, outdoor, actual_fake, internal_kw, extra_kw, minute):
        c = self.case
        drift = 1.22 if c.drift and minute >= TRAIN_MINUTES else 1.0
        gain = 0.9 if c.drift and minute >= TRAIN_MINUTES else 1.0
        demand = max(0.0, min(9.0, gain * c.heat_curve_kw_per_c * (20.2 - actual_fake)))
        previous = self.delivered_kw
        self.delivered_kw = demand + (previous - demand) * math.exp(
            -1 / (60 * c.delivery_lag_hours)
        )
        delivered = (previous + self.delivered_kw) / 2
        exchange = c.coupling * (self.mass - self.air)
        air_flow = (
            c.air_loss * drift * (outdoor - self.air)
            + exchange
            + 0.85 * delivered
            + 0.7 * internal_kw
            + 0.8 * extra_kw
        )
        mass_flow = (
            c.mass_loss * drift * (outdoor - self.mass)
            - exchange
            + 0.15 * delivered
            + 0.3 * internal_kw
            + 0.2 * extra_kw
        )
        self.air += air_flow / (60 * c.air_capacity)
        self.mass += mass_flow / (60 * c.mass_capacity)
        cop = max(1.8, min(4.6, 3.2 + 0.045 * outdoor - 0.035 * delivered))
        heat = delivered / 60
        electric = heat / cop
        self.heat_kwh += heat
        self.electric_kwh += electric
        return heat, electric


class OriginalOutcomes:
    """All-original forecast score, including subsequent unknown PI changes.

    Unlike runtime qualification this audit never discards a forecast because
    control/mode later changed. Only physical report absence or end censors it.
    """

    def __init__(self, cutoff):
        self.cutoff = cutoff
        self.pending = {}
        self.groups = {}

    def issue(self, event):
        p = event["prediction"]
        issued = stamp(p["issued_at"])
        period = "train" if issued < self.cutoff else "eval"
        key = (
            period,
            p["model_version"],
            event["context"]["mode"],
            p["horizon_minutes"],
        )
        group = self.groups.setdefault(
            key,
            dict(
                issued=0,
                evaluated=0,
                model_abs=0.0,
                persist_abs=0.0,
                bias=0.0,
                outcomes=Counter(),
            ),
        )
        group["issued"] += 1
        self.pending[event["forecast_id"]] = (p, key)

    def report(self, at, indoor):
        for ident, (p, key) in list(self.pending.items()):
            due = stamp(p["due_at"])
            if at < due:
                continue
            group = self.groups[key]
            if (at - due).total_seconds() <= 120:
                error = indoor - p["temperature_c"]
                group["evaluated"] += 1
                group["model_abs"] += abs(error)
                group["persist_abs"] += abs(indoor - p["indoor_temperature_c"])
                group["bias"] += error
                group["outcomes"]["evaluated"] += 1
            else:
                group["outcomes"]["no_report_within_tolerance"] += 1
            del self.pending[ident]

    def finish(self):
        for p, key in self.pending.values():
            self.groups[key]["outcomes"]["right_censored"] += 1
        self.pending.clear()

    def summary(self, period):
        result = {}
        for (part, revision, mode, horizon), group in self.groups.items():
            if part != period:
                continue
            n = group["evaluated"]
            result[f"revision={revision}|mode={mode}|horizon={horizon}"] = dict(
                issued=group["issued"],
                evaluated=n,
                coverage=n / group["issued"],
                model_mae_c=group["model_abs"] / n if n else None,
                persistence_mae_c=group["persist_abs"] / n if n else None,
                model_bias_c=group["bias"] / n if n else None,
                outcomes=dict(group["outcomes"]),
            )
        return result


def retained_bytes(value, seen=None):
    """Approximate retained Python object bytes; excludes interpreter/modules."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    total = sys.getsizeof(value)
    if isinstance(value, dict):
        total += sum(
            retained_bytes(k, seen) + retained_bytes(v, seen) for k, v in value.items()
        )
    elif isinstance(value, (list, tuple, deque, set)):
        total += sum(retained_bytes(v, seen) for v in value)
    elif hasattr(value, "__dict__") and not callable(value):
        total += retained_bytes(value.__dict__, seen)
    return total


def phase_records(records, period):
    """Partition original issue cohorts while retaining their later outcomes."""
    pending = {}
    cutoff = START + timedelta(minutes=TRAIN_MINUTES)
    for event in records:
        kind = event.get("kind")
        if kind == "forecast":
            selected = (stamp(event["prediction"]["issued_at"]) < cutoff) == (
                period == "train"
            )
            pending[event["forecast_id"]] = selected
        elif kind == "resolution":
            selected = pending.pop(event["forecast_id"], False)
        elif kind in ("forecast_inputs", "shadow_boundary"):
            selected = True
        else:
            selected = (stamp(event["received_at"]) < cutoff) == (period == "train")
        if selected:
            yield event


class Arm:
    def __init__(self, case, name, directory):
        self.case, self.name = case, name
        self.sensor, self.states = _sensor()
        self.sensor._config_entry.entry_id = "offline-" + case.name + "-" + name
        self.sensor._config_entry.options.update(
            weather_entity="weather.synthetic", preheat_boost_enabled=True
        )
        self.states["weather.synthetic"] = SimpleNamespace(
            state="cloudy", attributes={"temperature_unit": "°C"}
        )
        self.states["sensor.price"].attributes = {}
        self.plant = Plant(case)
        self.latest, self.admitted = {}, {}
        self.forecast = None
        self.audit = OriginalOutcomes(START + timedelta(minutes=TRAIN_MINUTES))
        self.journal = Journal(directory / (name + "-events"))
        self.shadow = Shadow(
            MODEL,
            entities=IDS,
            operational=True,
            external_heat_sources=case.extra_kw > 0,
            emit=self.emit,
            execution_basis="offline_simulation",
            pi_config=self.sensor._cfg(),
        )
        self.shadow.boundary(START - timedelta(seconds=1), "offline_start")
        self.applied = truth_weather(case, 0)
        self.context = dict(
            mode="normal",
            brake_factor=0.0,
            target_temperature=21.0,
            aggressiveness=3,
            preheat_enabled=True,
            pi_i_term=None,
            pi_error_c=None,
            preheat_boost_c=None,
        )
        self.cached_advice = None
        self.next_report = self.report_index = 0
        self.fits = []
        self.last_fit = None
        self.last_window = None
        self.metrics = {
            p: dict(
                minutes=0,
                below_target_degree_minutes=0.0,
                below_floor_degree_minutes=0.0,
                over_ceiling_degree_minutes=0.0,
                minimum_indoor_c=100.0,
                maximum_indoor_c=-100.0,
                heat_kwh=0.0,
                electric_kwh=0.0,
                cost_sek=0.0,
                controller_cpu_seconds=0.0,
                shadow_cpu_seconds=0.0,
                report_count=0,
                advice=Counter(),
                applications=Counter(),
                offline_policy=Counter(),
                evidence=Counter(),
                confusion=Counter(),
                heat_abstentions=Counter(),
                modes=Counter(),
                peak_runtime_bytes=0,
                peak_snapshot_bytes=0,
            )
            for p in ("train", "eval")
        }
        self.windows = []
        self.pulse_detections = {}
        self.current_minute = 0
        self.sensor.hass.services = SimpleNamespace(async_call=self.fake_service)

    def emit(self, event):
        self.journal.add(event)
        if event["kind"] == "forecast":
            self.audit.issue(event)

    async def fake_service(self, domain, service, payload, **kwargs):
        if (domain, service) != ("weather", "get_forecasts"):
            raise AssertionError("Offline harness forbids all output service calls")
        return {
            "weather.synthetic": {
                "forecast": [
                    dict(datetime=s.start.isoformat(), temperature=s.temperature_c)
                    for s in self.forecast.weather
                ]
            }
        }

    def report(self, entity, value, at, attrs=None):
        raw = dict(
            entity_id=entity,
            state=str(value),
            last_reported=at.isoformat(),
            last_updated=at.isoformat(),
            last_changed=at.isoformat(),
            attributes={"unit_of_measurement": "°C", **(attrs or {})},
        )
        admission, _ = accept_reading(self.latest, self.admitted, raw, at, entities=IDS)
        assert admission == "report"

    def observe(self, minute, outdoor):
        at = START + timedelta(minutes=minute)
        self.current_minute = minute
        m = self.metrics[phase(minute)]
        due = minute >= self.next_report
        if due:
            self.next_report = (
                minute
                + self.case.reporting_minutes[
                    self.report_index % len(self.case.reporting_minutes)
                ]
            )
            self.report_index += 1
            dropped = (
                self.case.dropout
                and minute // 1440 in (2, 4)
                and 11 * 60 <= minute % 1440 < 12 * 60 + 30
            )
            if not dropped:
                indoor, outside = round(self.plant.air, 1), round(outdoor, 1)
                self.report(IDS[0], indoor, at)
                self.report(IDS[1], outside, at)
                self.audit.report(at, indoor)
                self.states["sensor.indoor"] = SimpleNamespace(
                    state=str(indoor), attributes={}, last_reported=at
                )
                self.states["sensor.outdoor"] = SimpleNamespace(
                    state=str(outside), attributes={}, last_reported=at
                )
                m["report_count"] += 1
        # Report the command that really drove the interval just completed.
        self.report(IDS[2], self.applied, at, self.context)
        observed_outdoor = float(self.states["sensor.outdoor"].state)
        if self.forecast is None or minute % 60 == 0:
            self.forecast = published_forecast(self.case, minute, observed_outdoor)
            self.shadow.set_forecast(self.forecast, at)
            rows = [
                dict(start=p.start.isoformat(), end=p.end.isoformat(), value=p.price)
                for p in self.forecast.prices
            ]
            today = [p for p in rows if stamp(p["start"]).date() == at.date()]
            tomorrow = [p for p in rows if stamp(p["start"]).date() > at.date()]
            self.states["sensor.price"] = SimpleNamespace(
                state=str(spot_price(minute)),
                attributes=dict(
                    raw_today=today,
                    raw_tomorrow=tomorrow,
                    unit_of_measurement="SEK/kWh",
                ),
                last_reported=at,
            )
        began = time.process_time()
        result = self.shadow.update(self.latest, at)
        m["shadow_cpu_seconds"] += time.process_time() - began
        fit = self.shadow.observer.model.response
        if fit is not None and fit is not self.last_fit:
            self.fits.append(
                dict(at=at.isoformat(), phase=phase(minute), **asdict(fit))
            )
            self.last_fit = fit
        evidence = result["heat_evidence"]
        window = evidence.get("last_window")
        if window and window["ended_at"] != self.last_window:
            self.last_window = window["ended_at"]
            start = int((stamp(window["started_at"]) - START).total_seconds() / 60)
            end = int((stamp(window["ended_at"]) - START).total_seconds() / 60)
            truth = (
                sum(extra_power(self.case, t) for t in range(start, end))
                / max(1, end - start)
                > 0.25
            )
            suspect = evidence["status"] == "suspected_extra_heat"
            key = classify_heat_window(
                evidence.get("baseline_confirmed"), suspect, truth
            )
            m["heat_abstentions" if key.startswith("abstained_") else "confusion"][
                key
            ] += 1
            m["evidence"][evidence["status"]] += 1
            self.windows.append(
                dict(
                    start=start,
                    end=end,
                    truth=truth,
                    suspected=suspect,
                    baseline_confirmed=evidence.get("baseline_confirmed", False),
                )
            )
        if minute % 60 == 0:
            m["peak_runtime_bytes"] = max(
                m["peak_runtime_bytes"],
                retained_bytes(self.shadow.observer) + retained_bytes(self.sensor._pi),
            )
            snapshot = dict(
                observer=result, response=self.shadow.observer.model.response_state()
            )
            m["peak_snapshot_bytes"] = max(
                m["peak_snapshot_bytes"], len(json.dumps(snapshot, default=str))
            )
        return result

    async def control(self, minute, result):
        at = START + timedelta(minutes=minute)
        m = self.metrics[phase(minute)]
        began = time.process_time()
        await self.sensor._do_update()
        m["controller_cpu_seconds"] += time.process_time() - began
        attrs = self.sensor.extra_state_attributes
        base = float(self.sensor._state)
        next_context = {k: attrs.get(k) for k in self.context}
        next_context["brake_factor"] = attrs.get("brake_factor", 0.0)
        next_context["preheat_enabled"] = attrs.get("preheat_enabled", True)
        rec = result["recommendation"]
        m["advice"][rec["action"] if rec["eligible"] else rec["reason"]] += 1
        previous_cached = self.cached_advice
        self.cached_advice = select_offline_advice(
            cached=previous_cached,
            result=result,
            now=at,
            previous_context=self.context,
            next_context=next_context,
            latest=self.latest,
            forecast=self.forecast,
        )
        if self.cached_advice is not None:
            m["offline_policy"][
                "reused" if self.cached_advice is previous_cached else "fresh"
            ] += 1
        elif previous_cached is not None:
            m["offline_policy"]["cleared"] += 1
        selected = self.cached_advice["recommendation"] if self.cached_advice else rec
        actual = base
        if self.name == "ti":
            actual = simulate_assistance(
                base,
                selected,
                now=at,
                indoor=float(self.states["sensor.indoor"].state),
                comfort_floor=19.5,
                ceiling=21.7,
                mode=next_context["mode"],
                qualified=self.cached_advice is not None,
                safe=next_context["mode"] == "safe_mode",
                model_version=result.get("model_version"),
            )
        if actual != base:
            m["applications"][selected["action"]] += 1
        self.applied, self.context = actual, next_context
        m["modes"][self.context["mode"]] += 1
        return base, actual, selected

    def advance(self, minute, outdoor):
        m = self.metrics[phase(minute)]
        heat, electric = self.plant.step(
            outdoor,
            self.applied,
            internal_power(minute),
            extra_power(self.case, minute),
            minute,
        )
        m["minutes"] += 1
        m["heat_kwh"] += heat
        m["electric_kwh"] += electric
        m["cost_sek"] += electric * spot_price(minute)
        m["below_target_degree_minutes"] += max(0, 21 - self.plant.air)
        m["below_floor_degree_minutes"] += max(0, 19.5 - self.plant.air)
        m["over_ceiling_degree_minutes"] += max(0, self.plant.air - 21.7)
        m["minimum_indoor_c"] = min(m["minimum_indoor_c"], self.plant.air)
        m["maximum_indoor_c"] = max(m["maximum_indoor_c"], self.plant.air)

    def summary(self):
        self.shadow.finish(START + timedelta(minutes=TOTAL_MINUTES))
        self.audit.finish()
        self.journal.flush()
        paths = list(self.journal.directory.glob("*.jsonl.gz"))

        def records():
            for path in paths:
                yield from recover_records(path)

        output = {}
        for part, metrics in self.metrics.items():
            metrics["effective_cop"] = (
                metrics["heat_kwh"] / metrics["electric_kwh"]
                if metrics["electric_kwh"]
                else None
            )
            metrics["controller_cpu_seconds_per_simulated_day"] = (
                metrics["controller_cpu_seconds"] * 1440 / metrics["minutes"]
            )
            metrics["shadow_cpu_seconds_per_simulated_day"] = (
                metrics["shadow_cpu_seconds"] * 1440 / metrics["minutes"]
            )
            output[part] = dict(
                plant_and_controller=metrics,
                fits=[fit for fit in self.fits if fit["phase"] == part],
                runtime_original_events=summarize_shadow(
                    phase_records(records(), part)
                ),
                all_original_forecasts_without_posthoc_control_censoring=self.audit.summary(
                    part
                ),
                heat_pulses=self.pulse_summary(part),
            )
        return dict(
            arm=self.name,
            periods=output,
            fit_total=len(self.fits),
            retained_fit=self.shadow.observer.model.response_state(),
            event_journal_bytes=self.journal.size,
            final_pending_samples=self.shadow.observer.model.response_pending_samples,
            pi_state_time_basis="latest_reported_applied_control_not_physical_calculation_timestamp",
        )

    def pulse_summary(self, period):
        starts = [
            minute
            for minute in range(TOTAL_MINUTES)
            if extra_power(self.case, minute) > 0
            and (minute == 0 or extra_power(self.case, minute - 1) == 0)
            and phase(minute) == period
        ]
        result = []
        for start in starts:
            end = start
            while extra_power(self.case, end) > 0:
                end += 1
            related = [w for w in self.windows if start < w["end"] <= end + 30]
            detections = [
                w["end"]
                for w in related
                if w["suspected"] and w["baseline_confirmed"] is True
            ]
            no_reference = not any(w["baseline_confirmed"] is True for w in related)
            result.append(
                dict(
                    start_minute=start,
                    end_minute=end,
                    detected=bool(detections),
                    delay_minutes=min(detections) - start if detections else None,
                    missed=not detections,
                    missed_due_abstention=not detections and no_reference,
                    no_comparable_reference=no_reference,
                )
            )
        return result


async def run_case(case, directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    arms = [Arm(case, name, directory) for name in ("baseline", "ti")]
    clock = [START]
    began, cpu = time.perf_counter(), time.process_time()
    with gzip.open(directory / "actual_commands.csv.gz", "wt", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            (
                "minute",
                "phase",
                "arm",
                "physical_air_before_c",
                "outdoor_truth_c",
                "mode",
                "brake_factor",
                "base_pi_fake_c",
                "actual_applied_fake_c",
                "delivered_heat_kw_before",
                "extra_heat_kw",
                "advice",
                "eligible",
                "advice_original_issued_at",
                "advice_valid_for_minutes",
                "reported_pi_i_term",
                "reported_pi_error_c",
                "reported_preheat_boost_c",
            )
        )
        with (
            patch.object(sensor_module.dt_util, "now", lambda: clock[0]),
            patch.object(
                sensor_module.dt_util, "utcnow", lambda: clock[0], create=True
            ),
            patch.object(
                sensor_module.dt_util,
                "as_utc",
                lambda value: value.astimezone(timezone.utc),
                create=True,
            ),
        ):
            for minute in range(TOTAL_MINUTES):
                clock[0] = START + timedelta(minutes=minute)
                outdoor = truth_weather(case, minute)
                for arm in arms:
                    result = arm.observe(minute, outdoor)
                    base, actual, rec = await arm.control(minute, result)
                    writer.writerow(
                        (
                            minute,
                            phase(minute),
                            arm.name,
                            arm.plant.air,
                            outdoor,
                            arm.context["mode"],
                            arm.context["brake_factor"],
                            base,
                            actual,
                            arm.plant.delivered_kw,
                            extra_power(case, minute),
                            rec["action"],
                            rec["eligible"],
                            rec.get("issued_at"),
                            rec.get("valid_for_minutes"),
                            arm.context["pi_i_term"],
                            arm.context["pi_error_c"],
                            arm.context["preheat_boost_c"],
                        )
                    )
                    arm.advance(minute, outdoor)
    summaries = {arm.name: arm.summary() for arm in arms}
    deltas = {}
    for part in ("train", "eval"):
        base = summaries["baseline"]["periods"][part]["plant_and_controller"]
        ti = summaries["ti"]["periods"][part]["plant_and_controller"]
        deltas[part] = {
            key: ti[key] - base[key]
            for key in (
                "electric_kwh",
                "cost_sek",
                "below_target_degree_minutes",
                "below_floor_degree_minutes",
                "over_ceiling_degree_minutes",
                "minimum_indoor_c",
                "maximum_indoor_c",
            )
        }
    return dict(
        case=asdict(case),
        arms=summaries,
        ti_minus_baseline=deltas,
        wall_seconds=time.perf_counter() - began,
        process_cpu_seconds=time.process_time() - cpu,
        process_peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    )


def write_outputs(output, results, *, suite="original"):
    summary = dict(
        protocol=(
            "fixed_six_case_causal_two_node_holdout_v1"
            if suite == "original"
            else "predeclared_blind_three_case_causal_two_node_holdout_v1"
        ),
        suite=suite,
        execution_basis="offline_simulation",
        training_days=4,
        evaluation_days=2,
        physical_step_minutes=1,
        outcome_tolerance_minutes=2,
        pi_state_time_basis="latest_reported_applied_control_not_physical_calculation_timestamp",
        pi_projection_enabled=True,
        offline_advice_validity_minutes=30,
        offline_advice_rechecked_each_cycle=True,
        parameters_predeclared=True,
        no_parameter_or_threshold_tuning=True,
        limitations=[
            "Empirical response cannot identify source type, delivered heat or plant drift.",
            "Forecasts reuse copied actual PI state when reported, with conditional constant mode/target/brake/preheat context; held-correction fallback is explicit when PI state is unavailable.",
            "Future hidden weather, context changes and offline assistance remain unknown; their errors stay in all-original scores.",
            "Runtime-qualified scores separately retain interrupted/missing/right-censored outcome counts.",
            "Only sensor report times are outcomes; physical truth at intervening minutes is not a forecast outcome.",
            f"{len(results)} fixed synthetic cases are finite evidence, not production efficacy or source-identification validation.",
            "Shadow recommendations remain unapplied; only pure simulate_assistance in the separate TI plant applies them.",
            "Python retained-byte estimate excludes interpreter overhead; process RSS includes both arms and harness.",
        ],
        validation_source_sha256=hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        results=results,
    )
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n"
    )
    fields = [
        "case",
        "arm",
        "period",
        "fits",
        "original_issued",
        "runtime_evaluated",
        "all_original_evaluated",
        "eligible",
        "applied",
        "electric_kwh",
        "cost_sek",
        "minimum_indoor_c",
        "below_floor_degree_minutes",
        "over_ceiling_degree_minutes",
        "peak_runtime_bytes",
        "peak_snapshot_bytes",
    ]
    with (output / "summary.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for result in results:
            for name, arm in result["arms"].items():
                for part, values in arm["periods"].items():
                    metrics = values["plant_and_controller"]
                    runtime = values["runtime_original_events"]
                    writer.writerow(
                        dict(
                            case=result["case"]["name"],
                            arm=name,
                            period=part,
                            fits=len(values["fits"]),
                            original_issued=runtime["original_forecast_count"],
                            runtime_evaluated=sum(
                                g["evaluated"] for g in runtime["groups"]
                            ),
                            all_original_evaluated=sum(
                                g["evaluated"]
                                for g in values[
                                    "all_original_forecasts_without_posthoc_control_censoring"
                                ].values()
                            ),
                            eligible=runtime["recommendations"]["eligible"],
                            applied=sum(metrics["applications"].values()),
                            **{key: metrics[key] for key in fields[9:]},
                        )
                    )
    fits = sum(
        arm["fit_total"] for result in results for arm in result["arms"].values()
    )
    applications = sum(
        sum(arm["periods"]["eval"]["plant_and_controller"]["applications"].values())
        for result in results
        for arm in result["arms"].values()
    )
    text = f"Fixed {suite} suite ({len(results)} cases), chronological offline validation: {fits} fitted revisions; {applications} evaluation applications.\n"
    if not applications:
        text += "No end-to-end assistance efficacy demonstrated: evaluation assistance count was zero.\n"
    text += "See summary.csv for each train/eval arm, summary.json for original forecast groups, abstentions, heat detection and physical costs.\n"
    (output / "summary.txt").write_text(text)
    return text


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", choices=("original", "blind"), default="original")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (args.output / "summary.json").exists():
        raise SystemExit(
            "Refusing to overwrite an existing holdout; choose a fresh directory"
        )
    results = []
    for case in BLIND_CASES if args.suite == "blind" else CASES:
        print("Starting " + case.name, flush=True)
        result = asyncio.run(run_case(case, args.output / case.name))
        results.append(result)
        (args.output / (case.name + ".json")).write_text(
            json.dumps(result, indent=2, allow_nan=False) + "\n"
        )
        print(
            case.name
            + ": "
            + str({name: arm["fit_total"] for name, arm in result["arms"].items()})
            + " fits",
            flush=True,
        )
    print(write_outputs(args.output, results, suite=args.suite))


if __name__ == "__main__":
    main()
