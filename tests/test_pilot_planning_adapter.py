"""Adapter fails closed unless a heat response and full forecasts are supplied."""

from custom_components.pumpsteer.pilot_planning import build_plan


def test_missing_evidence_and_forecast_gaps_abstain():
    assert (
        build_plan(
            indoor=20, outdoor=[5] * 8, prices=[1] * 8, target=21, loss=None, gain=None
        )
        is None
    )
    assert (
        build_plan(
            indoor=20,
            outdoor=[5] * 7,
            prices=[1] * 8,
            target=21,
            loss=0.02,
            gain=0.5,
            assume_calibrated=True,
        )
        is None
    )
    assert (
        build_plan(
            indoor=20,
            outdoor=[5] * 8,
            prices=[1] * 7 + [float("nan")],
            target=21,
            loss=0.02,
            gain=0.5,
            assume_calibrated=True,
        )
        is None
    )


def test_real_pilot_planner_consumes_matching_future_price_and_weather():
    common = dict(
        indoor=20.8,
        outdoor=[5] * 8,
        target=21,
        loss=0.02,
        gain=0.5,
        assume_calibrated=True,
        comfort_floor=20.5,
        comfort_ceiling=21.5,
    )
    cheap = build_plan(prices=[0.2] * 4 + [3.0] * 4, **common)
    expensive = build_plan(prices=[3.0] * 4 + [0.2] * 4, **common)
    assert cheap is not None and expensive is not None
    assert len(cheap.predicted_temperatures) == 9
    assert cheap.sequence != expensive.sequence


def test_pilot_delayed_response_optimizer_is_available_with_qualified_inputs():
    from custom_components.pumpsteer.pilot_vendor.pump_response import (
        ResponseParameters,
    )
    from custom_components.pumpsteer.pilot_vendor.response_optimizer import (
        VirtualActuator,
    )

    response = ResponseParameters(delay_steps=1, retention=0.5, slope=0.8)
    actuator = VirtualActuator(offset=10, minimum=-15, smoothing=1, initial_virtual=5)
    result = build_plan(
        indoor=20.8,
        outdoor=[5] * 8,
        prices=[0.2] * 4 + [3.0] * 4,
        target=21,
        loss=0.02,
        gain=0.5,
        assume_calibrated=True,
        comfort_floor=20.5,
        comfort_ceiling=21.5,
        response_context=(response, (0.3, (0.0,)), actuator),
    )
    assert result is not None and len(result.duty_sequence) == 8


def test_requires_and_respects_existing_comfort_bounds():
    common = dict(
        indoor=20.8,
        outdoor=[5] * 8,
        prices=[1] * 8,
        target=21,
        loss=0.02,
        gain=0.5,
        assume_calibrated=True,
    )
    assert build_plan(**common, comfort_floor=None, comfort_ceiling=21.7) is None
    assert build_plan(**common, comfort_floor=21, comfort_ceiling=21.7) is None
