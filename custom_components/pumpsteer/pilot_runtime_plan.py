"""Strictly passive, explicitly hypothetical use of Heat Pump Pilot's optimizer."""

from datetime import timedelta
import math
from .pilot_forecast import _timestamp, price_window
from .pilot_vendor.mpc_controller import MpcController
from .pilot_vendor.pump_response import ResponseParameters, virtual_request
from .pilot_vendor.response_optimizer import VirtualActuator


def _weather_intervals(rows, now, horizon_hours=2):
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
        if end > now and start < now + timedelta(hours=horizon_hours):
            segments.append(
                (max(start, now), min(end, now + timedelta(hours=horizon_hours)), value)
            )
    if (
        not segments
        or segments[0][0] != now
        or segments[-1][1] != now + timedelta(hours=horizon_hours)
    ):
        return None
    if any(a[1] != b[0] for a, b in zip(segments, segments[1:])):
        return None
    return segments


def _quarters(segments, now, horizon_hours=2):
    result = []
    for i in range(horizon_hours * 4):
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
    response_context=None,
    house_context=None,
    response_scope=None,
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
    if price_window(price_states, now, timedelta(hours=2)) is None:
        return {**unavailable, "reason": "price_forecast_unavailable"}
    if weather_unit != "°C" or _weather_intervals(weather_rows, now) is None:
        return {**unavailable, "reason": "weather_forecast_unavailable"}
    # Limit the search to six hours and only use contiguous provider coverage.
    for horizon in range(6, 1, -1):
        prices = price_window(price_states, now, timedelta(hours=horizon))
        weather = _weather_intervals(weather_rows, now, horizon)
        if prices is not None and weather is not None:
            break
    price_values, outdoor_values = (
        _quarters(prices, now, horizon),
        _quarters(weather, now, horizon),
    )
    if price_values is None or outdoor_values is None:
        return {**unavailable, "reason": "forecast_coverage_gap"}
    if (
        response_context is not None
        and response_context[0].outdoor_gain
        and not all(
            response_context[0].outdoor_min <= value <= response_context[0].outdoor_max
            for value in outdoor_values
        )
    ):
        response_context = None
    validated = bool(
        response_context is not None
        and house_context is not None
        and response_scope is not None
        and house_context["indoor_min"] <= indoor <= house_context["indoor_max"]
        and all(
            house_context["outdoor_min"] <= value <= house_context["outdoor_max"]
            for value in outdoor_values
        )
        and (
            not response_context[0].outdoor_gain
            or all(
                response_context[0].outdoor_min
                <= value
                <= response_context[0].outdoor_max
                for value in outdoor_values
            )
        )
    )
    coefficients = (
        {
            "heat_loss_coeff": house_context["loss"],
            "heat_gain_coeff": house_context["gain"],
            "background_gain": house_context["background"],
        }
        if validated
        else {}
    )
    controller = MpcController(
        **coefficients,
        target_temperature=target,
        price_comfort_weight=0.5,
        comfort_temperature_tolerance=min(target - floor, ceiling - target),
        prediction_horizon_hours=horizon,
    )
    actuator = VirtualActuator(
        offset=output_clamp,
        minimum=entity_min,
        entity_min=entity_min,
        entity_max=entity_max,
        smoothing=1,
        initial_virtual=command,
        learned_response=response_context is not None,
    )
    parameters, initial = (
        response_context
        if response_context is not None
        else (ResponseParameters(), (0.0, ()))
    )
    _, plan = controller.suggest_control(
        indoor,
        outdoor_values,
        price_values,
        response_context=(parameters, initial, actuator),
    )
    if validated and any(
        not house_context["indoor_min"] <= value <= house_context["indoor_max"]
        for value in plan.predicted_temperatures
    ):
        return {**unavailable, "reason": "prediction_outside_validated_indoor_domain"}
    if validated and any(
        not house_context["heat_min"] <= value <= house_context["heat_max"]
        for value in plan.predicted_heating
    ):
        return {**unavailable, "reason": "prediction_outside_validated_heat_domain"}
    if validated:
        requests = [
            virtual_request(outdoor, virtual, output_clamp)
            for outdoor, virtual in zip(outdoor_values, plan.planned_virtual_outdoor)
        ]
        if any(
            value is None
            or not response_scope["request_min"]
            <= value
            <= response_scope["request_max"]
            for value in requests
        ) or any(
            not response_scope["heat_min"] <= value <= response_scope["heat_max"]
            for value in [initial[0], *plan.predicted_heating]
        ):
            return {**unavailable, "reason": "plan_outside_observed_response_domain"}
    result = {
        "planning_status": "validated_passive"
        if validated
        else "hypothetical_unvalidated",
        "reason": "validated_model_conditional_not_control"
        if validated
        else "assumed_model_and_heat_state",
        "horizon_minutes": 60 * horizon,
        "forecast_coverage": "complete_issue_time_unknown",
        "cost_kind": "proxy_not_currency",
        "response_status": "validated_response_only"
        if response_context is not None
        else "assumed_unvalidated",
        "duties": [round(x, 2) for x in plan.duty_sequence],
        "temperatures": [round(x, 2) for x in plan.predicted_temperatures],
        "comfort_status": plan.comfort_status,
    }
    if validated:
        result["validation"] = {
            key: house_context[key]
            for key in (
                "mae",
                "persistence_mae",
                "samples",
                "outdoor_min",
                "outdoor_max",
                "indoor_min",
                "indoor_max",
                "heat_min",
                "heat_max",
            )
        }
        result["validation"]["scope"] = "separate_one_step_components_only"
        result["validation"]["combined_forecast_and_economy"] = "unverified"
        result["validation"]["response_domain"] = response_scope
    else:
        result["assumed_model"] = {
            "heat_loss": 0.05,
            "heat_gain": 0.6,
            "initial_heat": 0.0 if response_context is None else "observed",
        }
    return result
