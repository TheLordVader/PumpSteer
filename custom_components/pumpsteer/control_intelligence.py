"""Optional bounded modifiers, never a controller or an output transport."""

from datetime import datetime
import math

SIGNALS = {
    "compressor": "thermal_compressor_entity",
    "heating": "thermal_heating_mode_entity",
    "hot_water": "thermal_hot_water_mode_entity",
    "brine": "thermal_brine_pump_entity",
    "auxiliary": "thermal_aux_heat_entity",
    "balance": "thermal_balance_entity",
}


def _number(value):
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return value.timestamp() if value.utcoffset() is not None else None
    return _number(value)


def verified_signal_value(
    now, state, *, attribute="measurement_time", measured_after=None
):
    """Existing active-use timestamp contract: never promote a HA republish."""
    instant = _timestamp(now)
    cutoff = _timestamp(measured_after) if measured_after is not None else None
    if (
        instant is None
        or (measured_after is not None and cutoff is None)
        or not isinstance(attribute, str)
        or not attribute.strip()
    ):
        return None
    attrs = getattr(state, "attributes", {}) or {}
    report = _timestamp(getattr(state, "last_reported", None))
    measured = _timestamp(attrs.get(attribute))
    if (
        state is None
        or attrs.get("restored")
        or report is None
        or measured is None
        or not 0 <= instant - report <= 300
        or not 0 <= instant - measured <= 300
        or measured > report
        or (cutoff is not None and measured <= cutoff)
    ):
        return None
    return state.state


def external_heat_status(now, cfg, states, *, measured_after=None):
    entity = cfg.get("thermal_external_heat_entity")
    if not entity:
        return (
            "possible_unverified"
            if cfg.get("external_heat_sources") is True
            else "not_configured"
        )
    value = verified_signal_value(
        now,
        states.get(entity),
        attribute=cfg.get("control_measurement_time_attribute", "measurement_time"),
        measured_after=measured_after,
    )
    return (
        "verified_active"
        if value == "on"
        else "verified_inactive"
        if value == "off"
        else "unknown"
    )


def evaluate_limits(now, cfg, states, *, measured_after=None):
    """Require a verified measurement or source-message time, not a HA republish."""
    enabled_aux = cfg.get("control_aux_preheat_limit") is True
    enabled_balance = cfg.get("control_balance_brake_limit") is True
    result = {
        "preheat_multiplier": 1.0,
        "brake_multiplier": 1.0,
        "aux_reason": "disabled",
        "balance_reason": "disabled",
        "active": False,
    }
    if not enabled_aux and not enabled_balance:
        return result
    result.update(
        aux_reason="unverified" if enabled_aux else "disabled",
        balance_reason="unverified" if enabled_balance else "disabled",
    )
    if cfg.get("control_signals_verified") is not True:
        return result
    attribute = cfg.get("control_measurement_time_attribute", "measurement_time")
    if not isinstance(attribute, str) or not attribute.strip():
        return result
    instant = _timestamp(now)
    if instant is None:
        return result
    cutoff = _timestamp(measured_after) if measured_after is not None else None
    if measured_after is not None and cutoff is None:
        return result

    def value(key):
        entity = cfg.get(SIGNALS[key])
        return verified_signal_value(
            now,
            states.get(entity) if entity else None,
            attribute=attribute,
            measured_after=measured_after,
        )

    def invalid_alias(roles):
        """One verified changeover valve can represent both complementary modes."""
        used = {}
        for role in roles:
            entity = cfg.get(SIGNALS[role])
            if not entity:
                continue
            previous = used.get(entity)
            if previous is not None and not (
                {previous, role} == {"heating", "hot_water"}
                and cfg.get("thermal_heating_mode_inverted") is True
            ):
                return True
            used[entity] = role
        return False

    heating = value("heating")
    if cfg.get("thermal_heating_mode_inverted") is True and heating in ("on", "off"):
        heating = "off" if heating == "on" else "on"
    radiator_mode = heating == "on" and value("hot_water") == "off"
    if enabled_aux:
        roles = ("compressor", "heating", "hot_water", "brine", "auxiliary")
        if invalid_alias(roles):
            result["aux_reason"] = "signal_alias"
        elif not (
            radiator_mode
            and value("compressor") == "on"
            and (not cfg.get(SIGNALS["brine"]) or value("brine") == "on")
        ):
            result["aux_reason"] = "operation_unverified"
        else:
            aux = value("auxiliary")
            result["aux_reason"] = (
                "auxiliary_active"
                if aux == "on"
                else "auxiliary_inactive"
                if aux == "off"
                else "signal_unavailable"
            )
            if aux == "on":
                result["preheat_multiplier"] = 0.0
    if enabled_balance:
        scale = _number(cfg.get("control_balance_scale"))
        offset = _number(cfg.get("control_balance_offset"))
        threshold = _number(cfg.get("control_balance_deficit_threshold"))
        full = _number(cfg.get("control_balance_deficit_full"))
        if invalid_alias(("heating", "hot_water", "balance")):
            result["balance_reason"] = "signal_alias"
        elif not radiator_mode:
            result["balance_reason"] = "operation_unverified"
        elif (
            cfg.get("control_balance_verified") is True
            and scale is not None
            and scale != 0
            and offset is not None
            and threshold is not None
            and full is not None
            and 0 < threshold < full
        ):
            raw = _number(value("balance"))
            if raw is None:
                result["balance_reason"] = "signal_unavailable"
            else:
                deficit = raw * scale + offset
                if math.isfinite(deficit):
                    result["deficit"] = deficit
                    reduction = min(
                        0.5, max(0.0, 0.5 * (deficit - threshold) / (full - threshold))
                    )
                    result["brake_multiplier"] = 1.0 - reduction
                    result["balance_reason"] = (
                        "deficit_limit" if reduction else "below_threshold"
                    )
    result["active"] = (
        result["preheat_multiplier"] < 1 or result["brake_multiplier"] < 1
    )
    return result
