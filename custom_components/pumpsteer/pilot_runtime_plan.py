"""Strictly passive, explicitly hypothetical use of Heat Pump Pilot's optimizer."""

from datetime import timedelta
import math
from .pilot_forecast import _timestamp, price_window
from .pilot_vendor.mpc_controller import MpcController
from .pilot_vendor.pump_response import ResponseParameters
from .pilot_vendor.response_optimizer import VirtualActuator


def _weather_intervals(rows, now):
    """Hourly provider points are held to the next actual provider timestamp."""
    if not isinstance(rows, (list, tuple)) or len(rows) > 100:
        return None
    points = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        at = _timestamp(row.get("datetime"))
        raw = row.get("temperature")
        if at is None or isinstance(raw, bool):
            return None
        try:
            value = float(raw)
        except (ValueError, TypeError, OverflowError):
            return None
        if not math.isfinite(value) or not -60 <= value <= 60:
            return None
        points.append((at, value))
    points.sort()
    if any(points[i][0] >= points[i + 1][0] for i in range(len(points) - 1)):
        return None
    segments = []
    for (start, value), (end, _) in zip(points, points[1:]):
        if end - start != timedelta(hours=1):
            continue
        if end > now and start < now + timedelta(hours=2):
            segments.append(
                (max(start, now), min(end, now + timedelta(hours=2)), value)
            )
    if (
        not segments
        or segments[0][0] != now
        or segments[-1][1] != now + timedelta(hours=2)
    ):
        return None
    if any(a[1] != b[0] for a, b in zip(segments, segments[1:])):
        return None
    return segments


def _quarters(segments, now):
    result = []
    for i in range(8):
        start = now + timedelta(minutes=15 * i)
        end = start + timedelta(minutes=15)
        weighted = 0.0
        covered = 0.0
        for a, b, value in segments:
            dt = max(0.0, (min(end, b) - max(start, a)).total_seconds())
            weighted += value * dt
            covered += dt
        if abs(covered - 900) > 1e-6:
            return None
        result.append(weighted / 900)
    return result


def hypothetical_plan(
    *,
    now,
    indoor,
    outdoor,
    command,
    target,
    floor,
    ceiling,
    price_states,
    weather_rows,
    weather_unit,
    output_clamp=12,
    entity_min=-20,
    entity_max=40,
):
    """No outputs or service calls; all parameters and costs are hypothetical."""
    unavailable = {"planning_status": "unavailable"}
    now = _timestamp(now)
    if now is None:
        return {**unavailable, "reason": "invalid_time"}
    values = (
        indoor,
        outdoor,
        command,
        target,
        floor,
        ceiling,
        output_clamp,
        entity_min,
        entity_max,
    )
    if (
        any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in values
        )
        or not floor < target < ceiling
        or not 0 < output_clamp <= 50
        or not entity_min < entity_max
    ):
        return {**unavailable, "reason": "invalid_temperature_or_comfort"}
    prices = price_window(price_states, now, timedelta(hours=2))
    if prices is None:
        return {**unavailable, "reason": "price_forecast_unavailable"}
    weather = _weather_intervals(weather_rows, now) if weather_unit == "°C" else None
    if weather is None:
        return {**unavailable, "reason": "weather_forecast_unavailable"}
    price_values, outdoor_values = _quarters(prices, now), _quarters(weather, now)
    if price_values is None or outdoor_values is None:
        return {**unavailable, "reason": "forecast_coverage_gap"}
    controller = MpcController(
        target_temperature=target,
        price_comfort_weight=0.5,
        comfort_temperature_tolerance=min(target - floor, ceiling - target),
        prediction_horizon_hours=2,
    )
    actuator = VirtualActuator(
        offset=output_clamp,
        minimum=entity_min,
        entity_min=entity_min,
        entity_max=entity_max,
        smoothing=1,
        initial_virtual=command,
        learned_response=False,
    )
    _, plan = controller.suggest_control(
        indoor,
        outdoor_values,
        price_values,
        response_context=(ResponseParameters(), (0.0, ()), actuator),
    )
    return {
        "planning_status": "hypothetical_unvalidated",
        "reason": "assumed_model_and_heat_state",
        "horizon_minutes": 120,
        "forecast_coverage": "complete_issue_time_unknown",
        "cost_kind": "proxy_not_currency",
        "assumed_model": {"heat_loss": 0.05, "heat_gain": 0.6, "initial_heat": 0.0},
        "duties": [round(x, 2) for x in plan.duty_sequence],
        "temperatures": [round(x, 2) for x in plan.predicted_temperatures],
        "comfort_status": plan.comfort_status,
    }
