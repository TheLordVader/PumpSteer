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
    pi_reference=None,
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
        or not floor <= target <= ceiling
        or not 0 < output_clamp <= 50
        or not entity_min < entity_max
    ):
        return {**unavailable, "reason": "invalid_temperature_or_comfort"}
    if price_window(price_states, now, timedelta(hours=2)) is None:
        return {**unavailable, "reason": "price_forecast_unavailable"}
    if weather_unit != "°C" or _weather_intervals(weather_rows, now) is None:
        return {**unavailable, "reason": "weather_forecast_unavailable"}
    # Bounded beam search: at most 48 quarter-hour steps per planning run.
    max_steps = 48
    for horizon in range(max_steps // 4, 1, -1):
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
        "comparison": {
            "baseline": "unavailable",
            "baseline_reason": "future_pi_and_heat_delivery_unknown",
            "recovery": "unknown",
            "heat_storage": "unknown",
            "savings": "unverified",
            "final_temperature": round(plan.predicted_temperatures[-1], 2),
            "minimum_temperature": round(min(plan.predicted_temperatures), 2),
            "maximum_temperature": round(max(plan.predicted_temperatures), 2),
            "below_floor_degree_hours": round(
                sum(
                    max(0, floor - temp) * 0.25
                    for temp in plan.predicted_temperatures[1:]
                ),
                3,
            ),
            "above_ceiling_degree_hours": round(
                sum(
                    max(0, temp - ceiling) * 0.25
                    for temp in plan.predicted_temperatures[1:]
                ),
                3,
            ),
            "cost": "unavailable",
            "plan_score": round(plan.cost, 4),
            "plan_score_kind": "pilot_mixed_comfort_price_proxy",
            "period_complete": "unverified",
            "resource_cap_reached": horizon * 4 == max_steps,
        },
    }
    if pi_reference is not None:
        reference = _pure_pi_reference(
            pi_reference,
            now=now,
            indoor=indoor,
            command=command,
            target=target,
            outdoor=outdoor_values,
            prices=price_values,
            price_segments=prices,
            floor=floor,
            ceiling=ceiling,
            controller=controller,
            parameters=parameters,
            initial=initial,
            offset=output_clamp,
            entity_min=entity_min,
            entity_max=entity_max,
            house_scope=house_context if validated else None,
            response_scope=response_scope if validated else None,
        )
        if reference is None:
            result["comparison"]["baseline_reason"] = (
                "pi_reference_outside_domain_or_unsupported"
            )
        if reference is not None:
            result["comparison"].update(reference)
            result["comparison"]["pilot_heat_fraction"] = [
                round(value, 2) for value in plan.predicted_heating
            ]
            result["comparison"]["heat_fraction_kind"] = "modelled_not_measured_power"
            result["comparison"]["pilot_heat_price_proxy"] = round(
                sum(p * h * 0.25 for p, h in zip(price_values, plan.predicted_heating)),
                4,
            )
            result["comparison"]["pilot_heat_fraction_hours"] = round(
                sum(plan.predicted_heating) * 0.25, 3
            )
    if floor == ceiling:
        result["comfort_status"] = "no_configured_band"
        result["comparison"]["below_floor_degree_hours"] = None
        result["comparison"]["above_ceiling_degree_hours"] = None
        result["comparison"]["target_deviation_degree_hours"] = round(
            sum(abs(temp - target) * 0.25 for temp in plan.predicted_temperatures[1:]),
            3,
        )
        if result["comparison"].get("baseline") == "conditional_pi_only":
            result["comparison"]["baseline_below_floor_degree_hours"] = None
            result["comparison"]["baseline_above_ceiling_degree_hours"] = None
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


def _pure_pi_reference(
    context,
    *,
    now,
    indoor,
    command,
    target,
    outdoor,
    prices,
    floor,
    ceiling,
    controller,
    parameters,
    initial,
    offset,
    entity_min,
    entity_max,
    house_scope=None,
    response_scope=None,
    price_segments=None,
):
    """Counterfactual PI-only path, sampled every 15 minutes; not full overlays."""
    import copy

    try:
        if any(value >= context["summer_threshold"] for value in outdoor):
            return None
        categories = context.get("categories")
        if categories is not None:
            interval = context["interval_minutes"]
            start = context["current_slot"]
            lookahead = context["price_lookahead_minutes"]
            if interval <= 0 or start < 0 or lookahead < 0:
                return None
            required = math.ceil((len(outdoor) * 15 + lookahead) / interval) + 1
            if (
                start + required > len(categories)
                or "expensive" in categories[start : start + required]
            ):
                return None
            timezone = context.get("calendar_tz", now.tzinfo)
            local_start = now.astimezone(timezone)
            local_end = (
                now + timedelta(minutes=len(outdoor) * 15 + lookahead)
            ).astimezone(timezone)
            if (
                local_end.date() != local_start.date()
                or local_end.utcoffset() != local_start.utcoffset()
                or not _matching_pi_prices(context, now, local_start, price_segments)
            ):
                return None
        pi = copy.deepcopy(context["controller"])
        heat, queue = initial
        value = indoor
        temperatures = [value]
        heats = []
        for index, ambient in enumerate(outdoor):
            if index == 0:
                fake = command
            else:
                output = pi.compute(
                    target,
                    value,
                    ambient,
                    1.0,
                    now + timedelta(minutes=15 * index),
                    kp=context["kp"],
                    ki=context["ki"],
                    kd=context["kd"],
                    integral_clamp=context["integral_clamp"],
                    output_clamp=offset,
                    min_fake_temp=entity_min,
                    max_fake_temp=entity_max,
                )
                fake = max(entity_min, min(entity_max, ambient + output.offset))
            request = virtual_request(ambient, fake, offset)
            if request is None:
                return None
            heat, queue = parameters.advance(request, heat, queue, ambient)
            value = controller._predict_temp(value, ambient, heat)
            if house_scope is not None and not (
                house_scope["indoor_min"] <= value <= house_scope["indoor_max"]
                and house_scope["heat_min"] <= heat <= house_scope["heat_max"]
            ):
                return None
            if response_scope is not None and not (
                response_scope["request_min"]
                <= request
                <= response_scope["request_max"]
                and response_scope["heat_min"] <= heat <= response_scope["heat_max"]
            ):
                return None
            temperatures.append(value)
            heats.append(heat)
        return {
            "baseline": "conditional_pi_only",
            "baseline_reason": "price_overlays_disabled"
            if categories is None
            else "no_expensive_period_in_known_window",
            "cadence_minutes": 15,
            "baseline_temperatures": [round(v, 2) for v in temperatures],
            "baseline_heat_fraction": [round(v, 2) for v in heats],
            "baseline_final_temperature": round(temperatures[-1], 2),
            "baseline_target_deviation_degree_hours": round(
                sum(abs(v - target) * 0.25 for v in temperatures[1:]), 3
            ),
            "baseline_min_temperature": round(min(temperatures), 2),
            "baseline_max_temperature": round(max(temperatures), 2),
            "baseline_below_floor_degree_hours": round(
                sum(max(0, floor - v) * 0.25 for v in temperatures[1:]), 3
            ),
            "baseline_above_ceiling_degree_hours": round(
                sum(max(0, v - ceiling) * 0.25 for v in temperatures[1:]), 3
            ),
            "baseline_heat_price_proxy": round(
                sum(p * h * 0.25 for p, h in zip(prices, heats)), 4
            ),
            "baseline_heat_fraction_hours": round(sum(heats) * 0.25, 3),
            "proxy_definition": "price_times_model_heat_fraction_not_currency",
            "reference_assumptions": "15_minute_PI_no_future_holiday_or_precool_unverified_heat_delivery",
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _matching_pi_prices(context, now, local_start, segments):
    """Require the PI price slots and timestamped plan prices to agree."""
    if not segments:
        return False
    interval = context["interval_minutes"] * 60
    slot = context["current_slot"]
    active = context["active_prices"]
    seconds_into_slot = (
        local_start.hour * 3600 + local_start.minute * 60 + local_start.second
    ) % interval + local_start.microsecond / 1_000_000
    for start, end, value in segments:
        cursor = start
        while cursor < end:
            elapsed = (cursor - now).total_seconds()
            index = slot + math.floor((seconds_into_slot + elapsed) / interval)
            if (
                index < 0
                or index >= len(active)
                or not math.isclose(value, active[index], rel_tol=1e-9, abs_tol=1e-9)
            ):
                return False
            boundary = now + timedelta(
                seconds=(index - slot + 1) * interval - seconds_into_slot
            )
            if boundary <= cursor:
                return False
            cursor = min(end, boundary)
    return True
