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


def evaluate_limits(now, cfg, states):
    """Require independently verified measurement time; HA republishing is insufficient."""
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

    def value(key):
        entity = cfg.get(SIGNALS[key])
        state = states.get(entity) if entity else None
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
        ):
            return None
        return state.state

    required = [cfg.get(SIGNALS[k]) for k in ("compressor", "heating", "hot_water")]
    if cfg.get(SIGNALS["brine"]):
        required.append(cfg[SIGNALS["brine"]])
    if len(set(required)) != len(required):
        result.update(
            aux_reason="signal_alias" if enabled_aux else "disabled",
            balance_reason="signal_alias" if enabled_balance else "disabled",
        )
        return result
    heating = value("heating")
    if cfg.get("thermal_heating_mode_inverted") is True and heating in ("on", "off"):
        heating = "off" if heating == "on" else "on"
    context = (
        value("compressor") == "on"
        and heating == "on"
        and value("hot_water") == "off"
        and (not cfg.get(SIGNALS["brine"]) or value("brine") == "on")
    )
    if not context:
        if enabled_aux:
            result["aux_reason"] = "operation_unverified"
        if enabled_balance:
            result["balance_reason"] = "operation_unverified"
        return result
    if enabled_aux:
        aux = (
            value("auxiliary")
            if cfg.get(SIGNALS["auxiliary"]) not in required
            else None
        )
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
        if (
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
