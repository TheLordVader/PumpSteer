"""Bounded, read-only extraction of original timestamped price intervals.

A Home Assistant report time proves receipt, not the upstream issue time. These
intervals are diagnostics only; this module does not qualify a control plan.
"""

from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
import math


def _timestamp(value):
    try:
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(value, datetime) or value.utcoffset() is None:
            return None
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def source_is_current(state, now):
    """HA recency is not upstream issue time or physical measurement time."""
    if state is None or getattr(state, "state", None) in ("unknown", "unavailable"):
        return False
    attrs = getattr(state, "attributes", {}) or {}
    reported, now = _timestamp(getattr(state, "last_reported", None)), _timestamp(now)
    return bool(
        isinstance(attrs, Mapping)
        and not attrs.get("restored")
        and reported is not None
        and now is not None
        and timedelta(0) <= now - reported <= timedelta(hours=24)
    )


def price_series_names(attrs):
    """Choose one coherent series, keeping raw quarter hours ahead of summaries."""
    if attrs.get("raw_today") or attrs.get("raw_tomorrow"):
        return ("raw_today", "raw_tomorrow")
    return ("prices", "forecast")


def price_window(states, now, horizon):
    """Return genuine contiguous clipped intervals, or None for any ambiguity."""
    now = _timestamp(now)
    if (
        now is None
        or not isinstance(horizon, timedelta)
        or not timedelta(0) < horizon <= timedelta(hours=24)
    ):
        return None
    found = {}
    unit = None
    for state in states:
        if state is None:
            continue
        attrs = getattr(state, "attributes", {}) or {}
        if not isinstance(attrs, Mapping):
            return None
        if not any(
            attrs.get(name)
            for name in ("raw_today", "raw_tomorrow", "prices", "forecast")
        ):
            continue
        if not source_is_current(state, now):
            continue
        current_unit = attrs.get("unit_of_measurement")
        if current_unit not in {"SEK/kWh", "EUR/kWh"} or (
            unit and unit != current_unit
        ):
            return None
        unit = current_unit
        # A raw series is one coherent source; a parallel hourly summary may
        # omit end times or overlap its quarters and must not corrupt it.
        for name in price_series_names(attrs):
            entries = attrs.get(name, [])
            if not isinstance(entries, list) or len(entries) > 192:
                return None
            if entries and all(not isinstance(entry, dict) for entry in entries):
                continue  # Undated lists cannot contribute but may coexist.
            for entry in entries:
                if not isinstance(entry, dict):
                    return None
                start, end = (
                    _timestamp(entry.get("start")),
                    _timestamp(entry.get("end")),
                )
                value = entry.get("value", entry.get("price"))
                if (
                    start is None
                    or end is None
                    or end <= start
                    or isinstance(value, bool)
                ):
                    return None
                try:
                    value = float(value)
                except (TypeError, ValueError, OverflowError):
                    return None
                if not math.isfinite(value):
                    return None
                if end <= now or start >= now + horizon:
                    continue
                key = (start, end)
                if key in found and found[key] != value:
                    return None
                found[key] = value
    if not unit:
        return None
    cursor, result = now, []
    for (start, end), value in sorted(found.items()):
        if start > cursor or (start < cursor and result):
            return None
        if end <= cursor:
            continue
        stop = min(end, now + horizon)
        result.append((cursor, stop, value))
        cursor = stop
        if cursor == now + horizon:
            return tuple(result)
    return None
