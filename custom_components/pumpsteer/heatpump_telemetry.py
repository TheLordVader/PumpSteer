"""Optional observations of heat-pump reports, never heat delivery or control.

A report timestamp is Home Assistant's timestamp, not physical measurement time.
The heat-balance value and trend are raw; no device scaling or sign is assumed.
"""

from collections import deque
from datetime import datetime, timedelta, timezone

from .utils import safe_float

_MAX_AGE = timedelta(minutes=60)
_MAX_BALANCE_GAP = timedelta(minutes=35)
_WINDOW = timedelta(hours=6)
# Heating-water reports may be hotter than a room sensor during hot water cycles.
_TEMPERATURES = {
    "supply_target": (-50, 110),
    "supply": (-50, 110),
    "return": (-50, 110),
    "brine_in": (-50, 60),
    "brine_out": (-50, 60),
}
_BINARIES = ("compressor", "hot_water", "brine_pump", "circulation_pump", "auxiliary")


def _report(state, now):
    """Return a fresh HA report time; restored and future reports are not evidence."""
    if state is None or not isinstance(now, datetime) or now.utcoffset() is None:
        return None
    attrs = getattr(state, "attributes", {}) or {}
    report = getattr(state, "last_reported", None)
    try:
        if (
            attrs.get("restored")
            or not isinstance(report, datetime)
            or report.utcoffset() is None
        ):
            return None
        report = report.astimezone(timezone.utc)
        if not timedelta(0) <= now.astimezone(timezone.utc) - report <= _MAX_AGE:
            return None
        return report
    except (ValueError, TypeError, OverflowError):
        return None


def _binary(state, report):
    if report is None:
        return None
    value = getattr(state, "state", None)
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def _transition(previous, report, value):
    """Count only separately reported off-to-on transitions within a known gap."""
    if report is None or value is None:
        return None, False
    if previous is None:
        return (report, value), False
    if report < previous[0] or (report == previous[0] and value != previous[1]):
        return None, False
    if report == previous[0]:
        return previous, False
    return (report, value), report - previous[0] <= _MAX_AGE and previous[
        1
    ] is False and value is True


class HeatPumpTelemetry:
    """Keep only bounded, passive data for optional heat-pump diagnostics."""

    def __init__(self):
        self._balance = deque(maxlen=64)
        self._balance_identity = None
        self._balance_last = None
        self._compressor_identity = None
        self._compressor_previous = None
        self._compressor_starts = 0
        self._water_identity = None
        self._water_previous = None
        self._water_episodes = 0

    def observe(self, now, states, *, balance_kind=None, configured=()):
        """Observe HA states keyed by signal role; return JSON-safe diagnostics."""
        reports = {key: _report(state, now) for key, state in states.items()}
        temperatures = {}
        for key, (minimum, maximum) in _TEMPERATURES.items():
            state = states.get(key)
            attrs = getattr(state, "attributes", {}) or {}
            temperatures[key + "_c"] = (
                safe_float(getattr(state, "state", None), minimum, maximum)
                if reports.get(key) is not None
                and attrs.get("unit_of_measurement") == "°C"
                else None
            )
        supply, target, return_temp = (
            temperatures["supply_c"],
            temperatures["supply_target_c"],
            temperatures["return_c"],
        )

        def aligned(a, b):
            return (
                reports.get(a) is not None
                and reports.get(b) is not None
                and abs(reports[a] - reports[b]) <= timedelta(minutes=5)
            )

        temperatures["supply_difference_c"] = (
            round(supply - target, 3)
            if supply is not None
            and target is not None
            and aligned("supply", "supply_target")
            else None
        )
        temperatures["supply_return_difference_c"] = (
            round(supply - return_temp, 3)
            if supply is not None
            and return_temp is not None
            and aligned("supply", "return")
            else None
        )
        binary = {key: _binary(states.get(key), reports.get(key)) for key in _BINARIES}
        compressor = states.get("compressor")
        compressor_identity = getattr(compressor, "entity_id", None)
        if compressor_identity != self._compressor_identity:
            self._compressor_identity = compressor_identity
            self._compressor_previous = None
        self._compressor_previous, started = _transition(
            self._compressor_previous, reports.get("compressor"), binary["compressor"]
        )
        self._compressor_starts += int(started)
        water = states.get("hot_water")
        water_identity = getattr(water, "entity_id", None)
        if water_identity != self._water_identity:
            self._water_identity = water_identity
            self._water_previous = None
        self._water_previous, water_started = _transition(
            self._water_previous, reports.get("hot_water"), binary["hot_water"]
        )
        self._water_episodes += int(water_started)

        balance = states.get("balance")
        attrs = getattr(balance, "attributes", {}) or {}
        kind = (
            balance_kind
            if balance_kind in ("raw", "degree_minutes", "integral")
            else None
        )
        identity = (
            getattr(balance, "entity_id", None),
            kind,
            attrs.get("unit_of_measurement"),
        )
        if identity != self._balance_identity:
            self._balance_identity = identity
            self._balance.clear()
            self._balance_last = None
        balance_report = reports.get("balance")
        raw = (
            safe_float(getattr(balance, "state", None), -1e9, 1e9)
            if balance_report
            else None
        )
        if raw is None:
            self._balance.clear()
            self._balance_last = None
        elif self._balance_last and (
            balance_report < self._balance_last[0]
            or (
                balance_report == self._balance_last[0] and raw != self._balance_last[1]
            )
        ):
            self._balance.clear()
            self._balance_last = None
            raw = None
        else:
            if (
                self._balance_last
                and balance_report - self._balance_last[0] > _MAX_BALANCE_GAP
            ):
                self._balance.clear()
            if not self._balance_last or balance_report != self._balance_last[0]:
                if not self._balance or balance_report - self._balance[-1][
                    0
                ] >= timedelta(minutes=1):
                    self._balance.append((balance_report, raw))
            self._balance_last = (balance_report, raw)
            while self._balance and balance_report - self._balance[0][0] > _WINDOW:
                self._balance.popleft()
        trend = None
        if len(self._balance) >= 3 and self._balance[-1][0] - self._balance[0][
            0
        ] >= timedelta(minutes=30):
            xs = [
                (row[0] - self._balance[0][0]).total_seconds() / 3600
                for row in self._balance
            ]
            ys = [row[1] for row in self._balance]
            xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
            xx = sum((x - xmean) ** 2 for x in xs)
            if xx:
                trend = round(
                    sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys)) / xx, 3
                )
        brine_configured = (
            "brine_pump" in configured or states.get("brine_pump") is not None
        )
        brine_observable = binary["brine_pump"] is not None and aligned(
            "compressor", "brine_pump"
        )
        conflict = (
            brine_observable
            and binary["compressor"] is True
            and binary["brine_pump"] is False
        )
        missing_context = brine_configured and not brine_observable
        hot_water_configured = (
            "hot_water" in configured or states.get("hot_water") is not None
        )
        hot_water_observable = binary["hot_water"] is not None and aligned(
            "compressor", "hot_water"
        )
        if conflict or missing_context or binary["compressor"] is None:
            operation = "unknown"
        elif binary["compressor"] is False:
            operation = "compressor_off"
        elif hot_water_configured and not hot_water_observable:
            operation = "unknown"
        elif binary["hot_water"] is True and hot_water_observable:
            operation = "hot_water_indicated"
        else:
            operation = "compressor_running"
        consistency = (
            "conflicting"
            if conflict
            else "insufficient_data"
            if missing_context
            or binary["compressor"] is None
            or (hot_water_configured and not hot_water_observable)
            else "no_conflict_observed"
        )
        return {
            "balance": {
                "kind": kind,
                "value": raw,
                "unit": attrs.get("unit_of_measurement") if raw is not None else None,
                "last_reported": balance_report.isoformat() if balance_report else None,
                "trend_per_hour": trend,
                "interpretation": "unverified",
            },
            "temperatures": temperatures,
            "binary": binary,
            "operation": operation,
            "signal_consistency": consistency,
            "compressor_starts_observed": self._compressor_starts,
            "compressor_start_count_scope": "since_monitor_start",
            "hot_water_episodes_observed": self._water_episodes,
            "hot_water_episode_count_scope": "since_monitor_start",
            "signal_last_reported": {
                key: report.isoformat() if report else None
                for key, report in reports.items()
            },
            "learning_qualified": False,
            "physical_heat_delivery": "unverified",
            "physical_measurement_time": "unknown",
        }
