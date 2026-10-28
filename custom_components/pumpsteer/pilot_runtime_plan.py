"""Strictly passive, explicitly hypothetical use of Heat Pump Pilot's optimizer."""

from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import math
from .pilot_forecast import _timestamp, price_window
from .pilot_vendor.mpc_controller import MpcController
from .pilot_vendor.pump_response import ResponseParameters, virtual_request
from .pilot_vendor.response_optimizer import VirtualActuator
from .settings import PRECOOL_LOOKAHEAD, PRICE_LOOKAHEAD_HOURS


@dataclass(frozen=True)
class _ReplayPriceSnapshot:
    prices: tuple
    categories: tuple
    slot: int
    p30: float
    p80: float


class _ReplayPriceCalendar:
    """Frozen, already-published local price days and the actual live cache."""

    def __init__(self, context, days):
        self.context = context
        self.days = days
        self.cache = {}

    def at(self, instant):
        from .electricity_price import (
            classify_price_list,
            filter_short_peaks,
            price_cache_transition,
        )
        from .settings import PEAK_FILTER_MIN_DURATION_MINUTES
        from .utils import compute_price_slot_index

        local = instant.astimezone(self.context["calendar_tz"])
        day = local.date()
        today = self.days.get(day)
        if today is None:
            return None
        key = (day, local.hour == 0 and local.minute < 15)
        if key not in self.cache:
            c = self.context
            transition = price_cache_transition(
                now=local,
                entity_id=c["price_entity_id"],
                today_prices=today,
                cached_date=c["cached_price_date"],
                cached_entity_id=c["cached_price_entity_id"],
                cached_p30=c["cached_p30"],
                cached_p80=c["cached_p80"],
            )
            tomorrow = self.days.get(day + timedelta(days=1), ())
            prices = tuple(today) + tuple(tomorrow)
            categories = filter_short_peaks(
                classify_price_list(prices, transition.p30, transition.p80),
                c["interval_minutes"],
                PEAK_FILTER_MIN_DURATION_MINUTES,
            )
            self.cache[key] = (
                prices,
                tuple(categories),
                transition.p30,
                transition.p80,
            )
        prices, categories, p30, p80 = self.cache[key]
        return _ReplayPriceSnapshot(
            prices,
            categories,
            compute_price_slot_index(
                local, self.context["interval_minutes"], len(prices)
            ),
            p30,
            p80,
        )


def _build_replay_price_calendar(price_states, context, now):
    """Qualify full original timestamped days, never invent future prices."""
    from .pilot_forecast import price_series_names, source_is_current
    from .sensor import PumpSteerSensor

    try:
        now = _timestamp(now)
        zone = context["calendar_tz"]
        local = now.astimezone(zone)
        interval = context["interval_minutes"]
        if (
            interval not in (15, 30, 60)
            or not isinstance(context["price_entity_id"], str)
            or not context["price_entity_id"]
        ):
            return None
        if any(
            isinstance(context[key], bool)
            or not isinstance(context[key], (int, float))
            or not math.isfinite(context[key])
            for key in ("cached_p30", "cached_p80")
        ):
            return None
        grouped = {}
        unit = None
        for state in price_states:
            if state is None or not source_is_current(state, now):
                continue
            attrs = getattr(state, "attributes", {}) or {}
            current_unit = attrs.get("unit_of_measurement")
            if current_unit not in ("SEK/kWh", "EUR/kWh") or (
                unit and unit != current_unit
            ):
                return None
            unit = current_unit
            for name in price_series_names(attrs):
                entries = attrs.get(name, [])
                if not isinstance(entries, list) or len(entries) > 192:
                    return None
                if entries and all(not isinstance(entry, dict) for entry in entries):
                    continue
                for row in entries:
                    if not isinstance(row, dict):
                        return None
                    start, end = (
                        _timestamp(row.get("start")),
                        _timestamp(row.get("end")),
                    )
                    value = row.get("value", row.get("price"))
                    if start is None or end is None or isinstance(value, bool):
                        return None
                    try:
                        value = float(value)
                    except (TypeError, ValueError, OverflowError):
                        return None
                    if not math.isfinite(value) or end - start != timedelta(
                        minutes=interval
                    ):
                        return None
                    day = start.astimezone(zone).date()
                    if day not in (local.date(), local.date() + timedelta(days=1)):
                        continue
                    entries_for_day = grouped.setdefault(day, {})
                    prior = entries_for_day.get(start)
                    if prior is not None and prior != (end, value):
                        return None
                    entries_for_day[start] = (end, value)
        days = {}
        for day in (local.date(), local.date() + timedelta(days=1)):
            raw = grouped.get(day, {})
            if not raw and day != local.date():
                continue
            rows = [
                {"start": start, "end": end, "value": value}
                for start, (end, value) in sorted(raw.items())
            ]
            anchor = datetime(day.year, day.month, day.day, tzinfo=zone)
            if not PumpSteerSensor._price_day_matches_slots(rows, anchor, interval):
                return None
            days[day] = tuple(row["value"] for row in rows)
        expected = days[local.date()] + days.get(local.date() + timedelta(days=1), ())
        active = tuple(context["active_prices"])
        if len(active) != len(expected) or any(
            not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
            for a, b in zip(active, expected)
        ):
            return None
        calendar = _ReplayPriceCalendar(context, days)
        snapshot = calendar.at(now)
        if (
            snapshot is None
            or snapshot.slot != context["current_slot"]
            or snapshot.categories != tuple(context["categories"])
        ):
            return None
        return calendar
    except (
        KeyError,
        TypeError,
        ValueError,
        AttributeError,
        OverflowError,
        ZeroDivisionError,
    ):
        return None


def _needs_price_calendar(local_start, local_end):
    return (
        local_start.date() != local_end.date()
        or local_start.utcoffset() != local_end.utcoffset()
        or (local_start.hour == 0 and local_start.minute < 15)
    )


def _price_calendar_for_window(states, context, now, horizon):
    try:
        zone = context["calendar_tz"]
        local_start = now.astimezone(zone)
        local_end = (now + timedelta(hours=horizon + PRICE_LOOKAHEAD_HOURS)).astimezone(
            zone
        )
        return (
            _build_replay_price_calendar(states, context, now)
            if _needs_price_calendar(local_start, local_end)
            else None
        )
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return None


def _calendar_covers_lookahead(calendar, end):
    """Unknown following-day prices must not affect short-peak filtering."""
    from .settings import PEAK_FILTER_MIN_DURATION_MINUTES

    if not calendar.days:
        return False
    zone = calendar.context["calendar_tz"]
    last_day = max(calendar.days)
    next_day = last_day + timedelta(days=1)
    midnight = datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone)
    # A missing next day's first expensive slot can affect the last run.
    interval = calendar.context["interval_minutes"]
    min_slots = max(1, math.ceil(PEAK_FILTER_MIN_DURATION_MINUTES / interval))
    safe_end = midnight.astimezone(timezone.utc) - timedelta(
        minutes=interval * (min_slots - 1)
    )
    return _timestamp(end) <= safe_end


def _weather_intervals(rows, now, horizon_hours=2, *, current_outdoor=None):
    """Hold the current observation only until the first future hourly point."""
    now = _timestamp(now)
    if now is None or not isinstance(rows, (list, tuple)) or len(rows) > 100:
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
    if points and now < points[0][0] <= now + timedelta(hours=1):
        if (
            isinstance(current_outdoor, bool)
            or not isinstance(current_outdoor, (int, float))
            or not math.isfinite(current_outdoor)
            or not -60 <= current_outdoor <= 60
        ):
            return None
        # This is a held initial condition, not a new measurement or a
        # forecast issued by the provider. All later points remain genuine.
        segments.append(
            (
                now,
                min(points[0][0], now + timedelta(hours=horizon_hours)),
                current_outdoor,
            )
        )
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


def _shared_window_is_eligible(
    context, now, hours, price_states, weather_rows, target, *,
    price_calendar=None,
    current_outdoor=None,
):
    """Cheap qualification before choosing a shorter shared planning horizon."""
    from .settings import HOLIDAY_TEMP

    try:
        if not context.get("inputs_qualified") or not context.get(
            "holiday_context_known"
        ):
            return False
        end = now + timedelta(hours=hours)
        price_end = end + timedelta(hours=PRICE_LOOKAHEAD_HOURS)
        zone = context["calendar_tz"]
        local_start, local_end = now.astimezone(zone), price_end.astimezone(zone)
        if _needs_price_calendar(local_start, local_end):
            if price_calendar is None or not _calendar_covers_lookahead(
                price_calendar, price_end
            ):
                return False
        segments = price_window(
            price_states, now, timedelta(hours=hours + PRICE_LOOKAHEAD_HOURS)
        )
        if segments is None or not _matching_pi_prices(
            context, now, local_start, segments
        ):
            return False
        interval = context["interval_minutes"]
        if not isinstance(interval, int) or interval <= 0:
            return False
        into = (
            (local_start.hour * 60 + local_start.minute) % interval
        ) * 60 + local_start.second
        last_slot = context["current_slot"] + math.floor(
            (into + (price_end - now).total_seconds()) / (interval * 60)
        )
        if context["current_slot"] < 0 or last_slot >= len(context["categories"]):
            return False
        start, stop = (
            _timestamp(context.get("holiday_start")),
            _timestamp(context.get("holiday_end")),
        )
        valid = start is not None and stop is not None and stop > start
        if valid and any(now < change <= end for change in (start, stop)):
            return False
        holiday = start <= now < stop if valid else bool(context["holiday_boolean_on"])
        if not math.isclose(
            target,
            HOLIDAY_TEMP if holiday else context["configured_target"],
            abs_tol=1e-9,
        ):
            return False
        return (
            _weather_intervals(
                weather_rows,
                now,
                hours + PRECOOL_LOOKAHEAD,
                current_outdoor=current_outdoor,
            ) is not None
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        AttributeError,
        ZeroDivisionError,
    ):
        return False


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
    replay_context=None,
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
    if weather_unit != "°C" or _weather_intervals(
        weather_rows, now, current_outdoor=outdoor
    ) is None:
        return {**unavailable, "reason": "weather_forecast_unavailable"}
    current_domain_invalid = (
        house_context is not None
        and not house_context["outdoor_min"] <= outdoor <= house_context["outdoor_max"]
    ) or (
        response_context is not None
        and response_context[0].outdoor_gain
        and not response_context[0].outdoor_min
        <= outdoor
        <= response_context[0].outdoor_max
    )
    if current_domain_invalid:
        # A forecast inside the learned range cannot make an already observed
        # out-of-range starting condition into a validated model state.
        response_context = None
    # Bounded beam search: at most 48 quarter-hour steps per planning run.
    max_steps = 48
    horizons = list(range(max_steps // 4, 1, -1))
    price_calendar = None
    if (
        replay_context is not None
        and not current_domain_invalid
        and replay_context.get("inputs_qualified")
        and replay_context.get("holiday_context_known")
    ):
        price_calendar = _price_calendar_for_window(
            price_states, replay_context, now, max_steps // 4
        )
        # Prefer the longest horizon for which both alternatives can be
        # evaluated using the additional forecasts the real controller uses.
        shared_horizon = next(
            (
                hours
                for hours in horizons
                if _shared_window_is_eligible(
                    replay_context,
                    now,
                    hours,
                    price_states,
                    weather_rows,
                    target,
                    price_calendar=price_calendar,
                    current_outdoor=outdoor,
                )
            ),
            None,
        )
        if shared_horizon is not None:
            horizons = [shared_horizon]
    for horizon in horizons:
        prices = price_window(price_states, now, timedelta(hours=horizon))
        weather = _weather_intervals(weather_rows, now, horizon, current_outdoor=outdoor)
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
        "weather_initial_assumption": (
            "current_outdoor_held_until_first_forecast"
            if weather_rows
            and all(_timestamp(row.get("datetime")) > now for row in weather_rows)
            else "provider_forecast"
        ),
        "initial_context": {
            "indoor_temperature": indoor,
            "outdoor_temperature": outdoor,
            "target_temperature": target,
            "comfort_floor": floor,
            "comfort_ceiling": ceiling,
            "virtual_temperature_command": command,
            "physical_measurement_time": "unknown",
        },
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
    if isinstance(replay_context, dict):
        from .control import PIController
        from .control_replay import ControlReplayState

        state = replay_context.get("state")
        if isinstance(state, ControlReplayState) and isinstance(state.pi, PIController):

            def timestamp(value):
                return (
                    value.isoformat()
                    if isinstance(value, datetime) and _timestamp(value) is not None
                    else None
                )

            def number(value):
                return (
                    value
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    else None
                )

            result["initial_context"]["controller"] = {
                "pi_integral": number(state.pi._integral),
                "pi_last_error": number(state.pi._last_error),
                "pi_last_time": timestamp(state.pi._last_time),
                "brake_ramp": number(state.brake_ramp),
                "brake_last_time": timestamp(state.brake_last_t),
                "brake_last_expensive_time": timestamp(state.brake_last_expensive_t),
                "preheat_ramp": number(state.preheat_ramp),
                "preheat_last_time": timestamp(state.preheat_last_t),
                "pi_precool_paused": state.pi_precool_paused,
                "previous_aggressiveness": state.prev_aggressiveness,
                "was_braking_last_cycle": state.was_braking_last_cycle,
                "bridge_short_dip_active": state.bridge_short_dip_active,
            }
    reference = None
    if replay_context is not None and not current_domain_invalid:
        reference = _shared_control_reference(
            context=replay_context,
            now=now,
            indoor=indoor,
            actual_outdoor=outdoor,
            command=command,
            target=target,
            floor=floor,
            ceiling=ceiling,
            outdoor_segments=weather,
            price_segments=prices,
            control_price_segments=price_window(
                price_states, now, timedelta(hours=horizon + PRICE_LOOKAHEAD_HOURS)
            ),
            prices=price_values,
            weather_rows=weather_rows,
            pilot_virtuals=plan.planned_virtual_outdoor,
            controller=controller,
            parameters=parameters,
            initial=initial,
            offset=output_clamp,
            house_scope=house_context if validated else None,
            response_scope=response_scope if validated else None,
            price_calendar=price_calendar,
        )
        if reference is None:
            longer_horizon = next(
                (
                    hours
                    for hours in range(max_steps // 4, horizon, -1)
                    if price_window(price_states, now, timedelta(hours=hours))
                    is not None
                    and _weather_intervals(
                        weather_rows, now, hours, current_outdoor=outdoor
                    ) is not None
                ),
                None,
            )
            if longer_horizon is not None:
                # Restore the original longer Pilot plan, at most once. The
                # normal replay path has a single optimizer run; this bounded
                # retry is used only when the shared simulation itself failed.
                fallback_reference = pi_reference
                if fallback_reference is not None and _known_fallback_conflict(
                    replay_context,
                    now,
                    longer_horizon,
                    weather_rows,
                    fallback_reference.get("summer_threshold"),
                ):
                    fallback_reference = None
                fallback = hypothetical_plan(
                    now=now,
                    indoor=indoor,
                    outdoor=outdoor,
                    command=command,
                    target=target,
                    floor=floor,
                    ceiling=ceiling,
                    price_states=price_states,
                    weather_rows=weather_rows,
                    weather_unit=weather_unit,
                    output_clamp=output_clamp,
                    entity_min=entity_min,
                    entity_max=entity_max,
                    response_context=response_context,
                    house_context=house_context,
                    response_scope=response_scope,
                    pi_reference=fallback_reference,
                )
                if fallback.get("planning_status") != "unavailable":
                    if "comparison" in fallback:
                        fallback["comparison"]["shared_control_status"] = "unavailable"
                        fallback["comparison"]["shared_control_reason"] = (
                            "context_or_model_domain_unavailable"
                        )
                    return fallback
                # Longer forecasts can leave the model's qualified domain.
                # Preserve the already valid shorter plan in that case.
            result["comparison"]["baseline_reason"] = (
                "shared_control_context_or_coverage_unavailable"
            )
            result["comparison"]["shared_control_status"] = "unavailable"
            result["comparison"]["shared_control_reason"] = (
                "context_or_forecast_coverage_unavailable"
            )
            try:
                zone = replay_context["calendar_tz"]
                local_start = now.astimezone(zone)
                local_end = (
                    now + timedelta(hours=horizon + PRICE_LOOKAHEAD_HOURS)
                ).astimezone(zone)
                if _needs_price_calendar(local_start, local_end):
                    result["comparison"]["shared_control_reason"] = (
                        "published_price_day_or_cache_unavailable"
                        if price_calendar is None
                        else "price_lookahead_not_fully_qualified"
                        if not _calendar_covers_lookahead(
                            price_calendar,
                            now + timedelta(hours=horizon + PRICE_LOOKAHEAD_HOURS),
                        )
                        else result["comparison"]["shared_control_reason"]
                    )
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        else:
            if price_calendar is not None:
                reference["price_publication_assumption"] = (
                    "frozen_intervals_available_as_today_not_verified_future_ha"
                )
            result["comparison"].update(reference)
            result["comparison"]["shared_control_status"] = "available"
    elif replay_context is not None:
        result["comparison"]["baseline_reason"] = (
            "current_outdoor_outside_validated_domain"
        )
        result["comparison"]["shared_control_status"] = "unavailable"
        result["comparison"]["shared_control_reason"] = (
            "current_outdoor_outside_validated_domain"
        )
    if (
        reference is None
        and pi_reference is not None
        and not current_domain_invalid
        and not _known_fallback_conflict(
            replay_context,
            now,
            horizon,
            weather_rows,
            pi_reference.get("summer_threshold"),
        )
    ):
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
        if result["comparison"].get("baseline") in {
            "conditional_pi_only",
            "shared_control_passive",
        }:
            result["comparison"]["baseline_below_floor_degree_hours"] = None
            result["comparison"]["baseline_above_ceiling_degree_hours"] = None
        if result["comparison"].get("baseline") == "shared_control_passive":
            result["comparison"]["pilot_below_floor_degree_hours"] = None
            result["comparison"]["pilot_above_ceiling_degree_hours"] = None
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
        pi.emit_log = False
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


def _known_fallback_conflict(context, now, horizon, weather_rows, summer_threshold):
    """Never use PI-only fallback when known future controls contradict it."""
    from .settings import PRECOOL_LOOKAHEAD, PRECOOL_MARGIN

    if (
        isinstance(summer_threshold, bool)
        or not isinstance(summer_threshold, (int, float))
        or not math.isfinite(summer_threshold)
    ):
        return True
    end = now + timedelta(hours=horizon)
    if context is not None and context.get("holiday_context_known"):
        start = _timestamp(context.get("holiday_start"))
        stop = _timestamp(context.get("holiday_end"))
        if start is not None and stop is not None and stop > start:
            if any(now < change <= end for change in (start, stop)):
                return True
    for row in weather_rows or ():
        if not isinstance(row, dict):
            continue
        at = _timestamp(row.get("datetime"))
        try:
            temp = float(row.get("temperature"))
        except (TypeError, ValueError, OverflowError):
            continue
        if (
            at is not None
            and now.replace(minute=0, second=0, microsecond=0)
            <= at
            <= end + timedelta(hours=PRECOOL_LOOKAHEAD)
            and math.isfinite(temp)
            and temp >= summer_threshold + PRECOOL_MARGIN
        ):
            return True
    return False


def _economic_intervals_aligned(rows, now, count):
    """Compare actual instants, including repeated local hours."""
    try:
        if now.tzinfo is None or not isinstance(rows, list) or len(rows) != count:
            return False
        start = _timestamp(now)
        return all(
            _timestamp(row["issued_at"]) <= start < _timestamp(row["valid_until"])
            and _timestamp(row["start"]) == start + timedelta(minutes=15 * i)
            and _timestamp(row["end"]) == start + timedelta(minutes=15 * (i + 1))
            for i, row in enumerate(rows)
        )
    except (KeyError, TypeError, AttributeError, OverflowError):
        return False


def _shared_control_reference(
    *,
    context,
    now,
    indoor,
    actual_outdoor,
    command,
    target,
    floor,
    ceiling,
    outdoor_segments,
    price_segments,
    control_price_segments,
    prices,
    weather_rows,
    pilot_virtuals,
    controller,
    parameters,
    initial,
    offset,
    house_scope=None,
    response_scope=None,
    price_calendar=None,
):
    """Replay the shared controller each minute and the Pilot model each quarter.

    The room temperature is held between model steps; no measured or inferred
    physical heat output is asserted. Both paths keep the already-issued
    command for the first minute.
    """
    from .control_replay import ControlReplayInput, replay_control_step
    from .forecast import ForecastPoint, analyze_thermal_outlook
    from .settings import HOLIDAY_TEMP, PRECOOL_LOOKAHEAD, PRICE_LOOKAHEAD_HOURS

    try:
        if (
            not context.get("holiday_context_known")
            or not context.get("inputs_qualified")
            or not prices
            or len(prices) > 48
        ):
            return None
        if len(pilot_virtuals) != len(prices):
            return None
        zone = context["calendar_tz"]
        local_start = now.astimezone(zone)
        end = now + timedelta(minutes=15 * len(prices))
        price_end = end + timedelta(hours=PRICE_LOOKAHEAD_HOURS)
        holiday_start, holiday_end = (
            _timestamp(context.get("holiday_start")),
            _timestamp(context.get("holiday_end")),
        )
        valid_holiday_dates = (
            holiday_start is not None
            and holiday_end is not None
            and holiday_end > holiday_start
        )
        if valid_holiday_dates and any(
            now < change <= end for change in (holiday_start, holiday_end)
        ):
            # The Pilot optimizer has one target, so a changing holiday target
            # cannot be compared against the same comfort assumptions.
            return None
        current_holiday = (
            holiday_start <= now < holiday_end
            if valid_holiday_dates
            else bool(context["holiday_boolean_on"])
        )
        expected_target = (
            HOLIDAY_TEMP if current_holiday else context["configured_target"]
        )
        if not math.isclose(target, expected_target, abs_tol=1e-9):
            return None
        if (
            (
                _needs_price_calendar(local_start, price_end.astimezone(zone))
                and (
                    price_calendar is None
                    or not _calendar_covers_lookahead(price_calendar, price_end)
                )
            )
            or not control_price_segments
            or control_price_segments[0][0] != now
            or control_price_segments[-1][1] != price_end
            or any(
                a[1] != b[0]
                for a, b in zip(control_price_segments, control_price_segments[1:])
            )
            or not _matching_pi_prices(
                context, now, local_start, control_price_segments
            )
        ):
            return None
        interval = context["interval_minutes"]
        if not isinstance(interval, int) or interval <= 0:
            return None
        slot = context["current_slot"]
        seconds_into = (
            local_start.hour * 3600 + local_start.minute * 60 + local_start.second
        ) % (interval * 60) + local_start.microsecond / 1_000_000
        last_slot = slot + math.floor(
            (seconds_into + (price_end - now).total_seconds()) / (interval * 60)
        )
        categories = context["categories"]
        if slot < 0 or last_slot >= len(categories):
            return None
        # The live outlook and precooling examine up to 24 future hours.
        weather = _weather_intervals(
            weather_rows,
            now,
            len(prices) / 4 + PRECOOL_LOOKAHEAD,
            current_outdoor=actual_outdoor,
        )
        if weather is None:
            return None
        raw_points = {}
        for row in weather_rows:
            at = _timestamp(row.get("datetime"))
            if at is None:
                return None
            hour = at.replace(minute=0, second=0, microsecond=0)

            def number(key):
                try:
                    value = float(row.get(key))
                    return value if math.isfinite(value) else None
                except (TypeError, ValueError, OverflowError):
                    return None

            raw_points[hour] = ForecastPoint(
                timestamp=hour,
                price=None,
                outdoor_temp=number("temperature"),
                wind_speed=number("wind_speed"),
                wind_gust_speed=number("wind_gust_speed"),
                cloud_coverage=number("cloud_coverage"),
                precipitation=number("precipitation"),
                humidity=number("humidity"),
            )
        ordered = sorted(raw_points.items())
        forecast_cache = {}

        def forecast_at(tick):
            hour = tick.replace(minute=0, second=0, microsecond=0)
            if hour not in forecast_cache:
                points = [
                    point
                    for at, point in ordered
                    if hour <= at <= hour + timedelta(hours=PRECOOL_LOOKAHEAD)
                ]
                # Missing hourly observations alter both live precool and outlook.
                if len(points) != PRECOOL_LOOKAHEAD + 1:
                    return None
                outlook = analyze_thermal_outlook(points, context["summer_threshold"])
                forecast_cache[hour] = ([p.outdoor_temp for p in points], outlook)
            return forecast_cache[hour]

        def value_at(segments, at):
            for start, stop, value in segments:
                if start <= at < stop:
                    return value
            return None

        def holiday_at(tick):
            if not valid_holiday_dates:
                return bool(context["holiday_boolean_on"])
            return holiday_start <= tick < holiday_end

        def simulate(pilot):
            state = context["state"]
            value = indoor
            heat, queue = initial
            temperatures = [value]
            heats = []
            requests = []
            virtuals = []
            modes = []
            brake_active = []
            for quarter, price in enumerate(prices):
                requested = 0.0
                average_outdoor = 0.0
                for minute in range(15):
                    elapsed = quarter * 15 + minute
                    tick = now + timedelta(minutes=elapsed)
                    ambient = (
                        actual_outdoor
                        if elapsed == 0
                        else value_at(outdoor_segments, tick)
                    )
                    if ambient is None:
                        return None
                    average_outdoor += ambient / 15
                    if elapsed == 0:
                        fake = command
                        mode = "initial_command"
                    elif pilot:
                        fake = pilot_virtuals[quarter]
                        mode = "pilot_plan"
                    else:
                        forecast = forecast_at(tick)
                        if forecast is None:
                            return None
                        if price_calendar is None:
                            current_slot = slot + math.floor(
                                (seconds_into + elapsed * 60) / (interval * 60)
                            )
                            active_categories = categories
                        else:
                            snapshot = price_calendar.at(tick)
                            if snapshot is None:
                                return None
                            current_slot = snapshot.slot
                            active_categories = snapshot.categories
                        inputs = ControlReplayInput(
                            now=tick.astimezone(zone),
                            cfg=context["cfg"],
                            indoor=value,
                            outdoor=ambient,
                            target=context["configured_target"],
                            summer_threshold=context["summer_threshold"],
                            aggressiveness=context["aggressiveness"],
                            holiday=holiday_at(tick),
                            categories=active_categories,
                            current_slot=current_slot,
                            interval_minutes=interval,
                            house_inertia=context["house_inertia"],
                            forecast_temps=forecast[0],
                            outlook=forecast[1],
                            preheat_enabled=context["preheat_enabled"],
                        )
                        state, decision = replay_control_step(state, inputs)
                        if decision.fake_temp is None or decision.mode in {
                            "safe_mode",
                            "unavailable",
                        }:
                            return None
                        fake, mode = decision.fake_temp, decision.mode
                    request = virtual_request(ambient, fake, offset)
                    if request is None or (
                        response_scope is not None
                        and not response_scope["request_min"]
                        <= request
                        <= response_scope["request_max"]
                    ):
                        return None
                    requested += request / 15
                    virtuals.append(fake)
                    modes.append(mode)
                    brake_active.append(
                        not pilot
                        and (
                            state.brake_ramp > 0
                            or mode in {"braking", "pre_braking", "brake_hold"}
                        )
                    )
                if house_scope is not None and not (
                    house_scope["outdoor_min"]
                    <= average_outdoor
                    <= house_scope["outdoor_max"]
                ):
                    return None
                if getattr(parameters, "outdoor_gain", 0) and not (
                    parameters.outdoor_min <= average_outdoor <= parameters.outdoor_max
                ):
                    return None
                heat, queue = parameters.advance(
                    requested, heat, queue, average_outdoor
                )
                value = controller._predict_temp(value, average_outdoor, heat)
                if house_scope is not None and not (
                    house_scope["indoor_min"] <= value <= house_scope["indoor_max"]
                    and house_scope["heat_min"] <= heat <= house_scope["heat_max"]
                ):
                    return None
                if (
                    response_scope is not None
                    and not response_scope["heat_min"]
                    <= heat
                    <= response_scope["heat_max"]
                ):
                    return None
                temperatures.append(value)
                heats.append(heat)
                requests.append(requested)
            return temperatures, heats, requests, virtuals, modes, brake_active

        baseline, pilot = simulate(False), simulate(True)
        if baseline is None or pilot is None:
            return None
        base_t, base_h, base_r, _, modes, braking = baseline
        pilot_t, pilot_h, pilot_r, _, _, _ = pilot
        transitions = [
            (minute, mode)
            for minute, mode in enumerate(modes)
            if minute == 0 or mode != modes[minute - 1]
        ]
        if len(transitions) > 96:
            return None  # Bound the existing sensor attribute rather than truncate evidence.
        last_brake = max(
            (i for i, active in enumerate(braking) if active), default=None
        )
        release = (
            last_brake + 1
            if last_brake is not None and last_brake < len(braking) - 1
            else None
        )
        recovery = next(
            (
                i * 15 - release
                for i, temp in enumerate(base_t)
                if release is not None and i * 15 >= release and temp >= indoor
            ),
            None,
        )
        # Optional electrical trajectories are supplied independently of the
        # thermal heat fractions; the latter are not electrical kilowatts.
        economic = None
        rows = context.get("economic_forecasts")
        if rows is not None:
            from .pilot_marginal import compare_costs

            if _economic_intervals_aligned(rows, now, len(prices)):
                economic = compare_costs(rows)
            else:
                economic = {"status": "unavailable", "saving_verified": False}
        return {
            "external_economic_scenario": economic,
            "baseline": "shared_control_passive",
            "control_step_seconds": 60,
            "thermal_step_seconds": 900,
            "heat_fraction_kind": "modelled_not_measured_power",
            "baseline_temperatures": [round(v, 2) for v in base_t],
            "baseline_heat_fraction": [round(v, 2) for v in base_h],
            "pilot_temperatures": [round(v, 2) for v in pilot_t],
            "pilot_heat_fraction": [round(v, 2) for v in pilot_h],
            "baseline_request_fraction": [round(v, 2) for v in base_r],
            "pilot_request_fraction": [round(v, 2) for v in pilot_r],
            "request_kind": "virtual_command_derived_not_measured_heat",
            "baseline_mode_changes": transitions,
            "baseline_brake_release_minutes": release,
            "baseline_recovery_to_initial_minutes": recovery,
            "baseline_final_temperature": round(base_t[-1], 2),
            "pilot_final_temperature": round(pilot_t[-1], 2),
            "baseline_below_floor_degree_hours": round(
                sum(max(0, floor - v) * 0.25 for v in base_t[1:]), 3
            ),
            "pilot_below_floor_degree_hours": round(
                sum(max(0, floor - v) * 0.25 for v in pilot_t[1:]), 3
            ),
            "baseline_above_ceiling_degree_hours": round(
                sum(max(0, v - ceiling) * 0.25 for v in base_t[1:]), 3
            ),
            "pilot_above_ceiling_degree_hours": round(
                sum(max(0, v - ceiling) * 0.25 for v in pilot_t[1:]), 3
            ),
            "baseline_heat_price_proxy": round(
                sum(p * h * 0.25 for p, h in zip(prices, base_h)), 4
            ),
            "pilot_heat_price_proxy": round(
                sum(p * h * 0.25 for p, h in zip(prices, pilot_h)), 4
            ),
            "proxy_definition": "price_times_model_heat_fraction_not_currency",
            "recovery": "unknown",
            "heat_storage": "unknown",
            "savings": "unverified",
            "period_complete": "unverified",
            "reference_assumptions": "quarter_hour_room_hold_frozen_forecasts_first_minute_current_command_no_physical_heat_confirmation",
        }
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError):
        return None
