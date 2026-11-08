"""Passive adapter for a bounded, explicitly qualified Heat Pump Pilot plan.

This module is not connected to PumpSteer's output. Inputs are conditional
assumptions, not proof of delivered heat or economic benefit.
"""

import math
from .pilot_vendor.mpc_controller import MpcController


def build_plan(
    *,
    indoor,
    outdoor,
    prices,
    target,
    loss,
    gain,
    comfort_floor=None,
    comfort_ceiling=None,
    assume_calibrated=False,
    response_context=None,
):
    """Calculate a conditional plan; caller-supplied flags are not evidence."""
    if assume_calibrated is not True:
        return None
    try:
        if len(outdoor) != 8 or len(prices) != 8:
            return None
        numbers = [
            indoor,
            target,
            loss,
            gain,
            comfort_floor,
            comfort_ceiling,
            *outdoor,
            *prices,
        ]
        if any(
            isinstance(x, bool)
            or not isinstance(x, (int, float))
            or not math.isfinite(x)
            for x in numbers
        ):
            return None
        if not 0.001 <= loss <= 0.25 or not 0.1 <= gain <= 1.5:
            return None
        if not comfort_floor < target < comfort_ceiling:
            return None
        controller = MpcController(
            target_temperature=target,
            price_comfort_weight=0.5,
            comfort_temperature_tolerance=min(
                target - comfort_floor, comfort_ceiling - target
            ),
            prediction_horizon_hours=2,
            heat_loss_coeff=loss,
            heat_gain_coeff=gain,
        )
        _, result = controller.suggest_control(
            indoor, outdoor, prices, response_context=response_context
        )
        return result if result and result.comfort_status == "within_band" else None
    except (TypeError, ValueError, OverflowError):
        return None
