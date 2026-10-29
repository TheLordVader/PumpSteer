"""Isolated use of PumpSteer's real control decisions without HA writes.

This module shares the mode branches and helpers used by the live sensor.  It
does not read HA state, issue service calls or emit operational events.
"""

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .control import PIController
from .electricity_price import PRICE_CHEAP, PRICE_NORMAL, PRICE_EXPENSIVE
from .sensor import PumpSteerSensor, MODE_SAFE
from .settings import (
    HOLIDAY_TEMP,
    PRICE_LOOKAHEAD_HOURS,
    RAMP_MIN_MINUTES,
    RAMP_OUT_FACTOR,
    PREHEAT_ON_MISSING_FORECAST,
)


@dataclass
class ControlReplayState:
    pi: PIController = field(default_factory=PIController)
    pi_precool_paused: bool = False
    last_pi_result: Any = None
    brake_ramp: float = 0.0
    preheat_ramp: float = 0.0
    preheat_last_t: datetime | None = None
    brake_last_t: datetime | None = None
    brake_last_expensive_t: datetime | None = None
    was_braking_last_cycle: bool = False
    prev_aggressiveness: int | None = None
    bridge_short_dip_active: bool = False
    thermal_pending_samples: int = 0
    thermal_should_refit: bool | None = None

    @classmethod
    def from_sensor(cls, sensor):
        return cls(
            pi=copy.deepcopy(sensor._pi),
            pi_precool_paused=sensor._pi_precool_paused,
            last_pi_result=copy.deepcopy(sensor._last_pi_result),
            brake_ramp=sensor._brake_ramp,
            preheat_ramp=sensor._preheat_ramp,
            preheat_last_t=sensor._preheat_last_t,
            brake_last_t=sensor._brake_last_t,
            brake_last_expensive_t=sensor._brake_last_expensive_t,
            was_braking_last_cycle=sensor._was_braking_last_cycle,
            prev_aggressiveness=sensor._prev_aggressiveness,
            bridge_short_dip_active=sensor._bridge_short_dip_active,
            thermal_pending_samples=sensor._thermal_model.pending_samples,
            thermal_should_refit=sensor._thermal_model.should_refit(),
        )


@dataclass(frozen=True)
class ControlReplayInput:
    now: datetime
    cfg: dict
    indoor: float | None
    outdoor: float | None
    target: float
    summer_threshold: float
    aggressiveness: int
    holiday: bool
    categories: tuple
    current_slot: int
    interval_minutes: int
    house_inertia: float
    forecast_temps: Any
    outlook: Any
    preheat_enabled: bool
    prices_available: bool = True


@dataclass(frozen=True)
class ControlReplayDecision:
    fake_temp: float | None
    mode: str
    attributes: dict
    intentions: tuple
    reason: str | None = None


class _ThermalIntent:
    def __init__(self, samples, should_refit, intentions):
        self.pending_samples = samples
        self._should_refit = should_refit
        self.intentions = intentions

    def should_refit(self):
        return (
            self._should_refit
            if self._should_refit is not None
            else self.pending_samples >= 20
        )

    def fit(self):
        self.intentions.append(("thermal_fit", {}))


class _Scratch:
    """Only the explicit control state and side-effect-free collaborators."""

    _run_control_logic = PumpSteerSensor._run_control_logic
    _pi_output = PumpSteerSensor._pi_output
    _update_brake_ramp = PumpSteerSensor._update_brake_ramp
    _update_preheat_ramp = PumpSteerSensor._update_preheat_ramp
    _should_precool = PumpSteerSensor._should_precool
    _brake_temp = PumpSteerSensor._brake_temp
    _comfort_floor = PumpSteerSensor._comfort_floor
    _compute_ramp_minutes = PumpSteerSensor._compute_ramp_minutes
    _preheat_headroom = PumpSteerSensor._preheat_headroom
    _preheat_headroom_factor = staticmethod(PumpSteerSensor._preheat_headroom_factor)
    _minutes_until_expensive = PumpSteerSensor._minutes_until_expensive
    _upcoming_expensive = PumpSteerSensor._upcoming_expensive
    _close_short_dip = PumpSteerSensor._close_short_dip

    def __init__(self, state, inputs):
        self._control_passive = True
        self._pi = copy.deepcopy(state.pi)
        self._pi.emit_log = False
        self._pi_precool_paused = state.pi_precool_paused
        self._last_pi_result = copy.deepcopy(state.last_pi_result)
        self._brake_ramp = state.brake_ramp
        self._preheat_ramp = state.preheat_ramp
        self._preheat_last_t = state.preheat_last_t
        self._brake_last_t = state.brake_last_t
        self._brake_last_expensive_t = state.brake_last_expensive_t
        self._was_braking_last_cycle = state.was_braking_last_cycle
        self._prev_aggressiveness = state.prev_aggressiveness
        self._bridge_short_dip_active = state.bridge_short_dip_active
        self._last_outlook = inputs.outlook
        self._preheat_allowed = inputs.preheat_enabled
        self._intentions = []
        self._thermal_model = _ThermalIntent(
            state.thermal_pending_samples, state.thermal_should_refit, self._intentions
        )
        self._decision = None

    def _forecast_is_cold(self, threshold, temps, hours=6):
        # The shared helper only logs for a missing forecast.
        if not temps:
            return PREHEAT_ON_MISSING_FORECAST
        return PumpSteerSensor._forecast_is_cold(self, threshold, temps, hours)

    def _preheat_enabled(self, cfg):
        return self._preheat_allowed

    def _update_thermal_learning(self, **details):
        self._intentions.append(("thermal_learning", details))

    def _control_event(self, event, **details):
        self._intentions.append(("event", {"event": event, **details}))

    def _base_attrs(
        self,
        indoor,
        target,
        outdoor,
        price_category,
        aggressiveness,
        heating_demand_c=0.0,
    ):
        return {
            "indoor_temperature": indoor,
            "target_temperature": target,
            "outdoor_temperature": outdoor,
            "price_category": price_category,
            "aggressiveness": aggressiveness,
            "heating_demand_c": round(heating_demand_c, 2),
        }

    async def _set_state(self, fake_temp, mode, attrs, now):
        self._close_short_dip(mode)
        self._decision = ControlReplayDecision(
            fake_temp, mode, attrs, tuple(self._intentions)
        )

    def state(self):
        return ControlReplayState(
            pi=self._pi,
            pi_precool_paused=self._pi_precool_paused,
            last_pi_result=self._last_pi_result,
            brake_ramp=self._brake_ramp,
            preheat_ramp=self._preheat_ramp,
            preheat_last_t=self._preheat_last_t,
            brake_last_t=self._brake_last_t,
            brake_last_expensive_t=self._brake_last_expensive_t,
            was_braking_last_cycle=self._was_braking_last_cycle,
            prev_aggressiveness=self._prev_aggressiveness,
            bridge_short_dip_active=self._bridge_short_dip_active,
            thermal_pending_samples=self._thermal_model.pending_samples,
            thermal_should_refit=self._thermal_model._should_refit,
        )


def replay_control_step(state, inputs):
    """Return an isolated next state and decision, never mutating caller state."""
    if not isinstance(state, ControlReplayState) or not isinstance(
        inputs, ControlReplayInput
    ):
        raise TypeError("explicit replay state and inputs required")
    now = inputs.now
    if not isinstance(now, datetime) or now.utcoffset() is None:
        return copy.deepcopy(state), ControlReplayDecision(
            None, "unavailable", {}, (), "invalid_control_time"
        )
    if not isinstance(state.pi, PIController) or any(
        value is not None
        and (not isinstance(value, datetime) or value.utcoffset() is None)
        for value in (
            state.preheat_last_t,
            state.brake_last_t,
            state.brake_last_expensive_t,
            getattr(state.pi, "_last_time", None),
        )
    ):
        return copy.deepcopy(state), ControlReplayDecision(
            None, "unavailable", {}, (), "invalid_control_state"
        )
    context = _Scratch(state, inputs)
    elapsed = 60.0
    if context._preheat_last_t is not None:
        elapsed = max(0.0, now.timestamp() - context._preheat_last_t.timestamp())
    if context._preheat_last_t is None or elapsed > 0:
        context._preheat_last_t = now

    def valid_temperature(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    outdoor = inputs.outdoor if valid_temperature(inputs.outdoor) else None
    if (
        not valid_temperature(inputs.indoor)
        or outdoor is None
        or not inputs.prices_available
    ):
        context._pi.reset(now)
        context._pi_precool_paused = False
        context._last_pi_result = None
        context._brake_ramp = context._preheat_ramp = 0.0
        context._preheat_last_t = context._brake_last_t = (
            context._brake_last_expensive_t
        ) = None
        context._was_braking_last_cycle = False
        return context.state(), ControlReplayDecision(
            outdoor,
            MODE_SAFE,
            {},
            (("cancel_thermal_session", {}),),
            "missing_temperature_or_prices",
        )
    if (
        not valid_temperature(inputs.target)
        or not valid_temperature(inputs.summer_threshold)
        or not valid_temperature(inputs.house_inertia)
        or not 0.5 <= inputs.house_inertia <= 10
        or type(inputs.aggressiveness) is not int
        or inputs.aggressiveness not in range(6)
        or type(inputs.holiday) is not bool
        or type(inputs.preheat_enabled) is not bool
        or type(inputs.prices_available) is not bool
        or not isinstance(inputs.cfg, dict)
        or not isinstance(inputs.categories, (tuple, list))
        or not all(
            category in (PRICE_CHEAP, PRICE_NORMAL, PRICE_EXPENSIVE)
            for category in inputs.categories
        )
        or not isinstance(inputs.interval_minutes, int)
        or isinstance(inputs.interval_minutes, bool)
        or inputs.interval_minutes <= 0
        or type(inputs.current_slot) is not int
        or not 0 <= inputs.current_slot < len(inputs.categories)
        or not valid_temperature(state.brake_ramp)
        or not 0 <= state.brake_ramp <= 1
        or not valid_temperature(state.preheat_ramp)
        or not 0 <= state.preheat_ramp <= 1
        or not valid_temperature(state.pi._integral)
        or not valid_temperature(state.pi._last_error)
        or type(state.thermal_pending_samples) is not int
        or state.thermal_pending_samples < 0
        or (
            state.thermal_should_refit is not None
            and type(state.thermal_should_refit) is not bool
        )
        or (
            inputs.forecast_temps is not None
            and (
                not isinstance(inputs.forecast_temps, (list, tuple))
                or len(inputs.forecast_temps) > 100
                or not all(valid_temperature(value) for value in inputs.forecast_temps)
            )
        )
    ):
        return copy.deepcopy(state), ControlReplayDecision(
            None, "unavailable", {}, (), "invalid_control_context"
        )
    target = HOLIDAY_TEMP if inputs.holiday else inputs.target
    lookahead = max(1, math.ceil(PRICE_LOOKAHEAD_HOURS * 60 / inputs.interval_minutes))
    upcoming = context._upcoming_expensive(
        inputs.categories, inputs.current_slot, lookahead
    )
    ramp_in = context._compute_ramp_minutes(inputs.house_inertia)
    ramp_out = max(RAMP_MIN_MINUTES, ramp_in * RAMP_OUT_FACTOR)
    floor = context._comfort_floor(target, inputs.aggressiveness)
    coroutine = context._run_control_logic(
        now=now,
        cfg=inputs.cfg,
        indoor=inputs.indoor,
        outdoor=outdoor,
        target=target,
        summer_threshold=inputs.summer_threshold,
        aggressiveness=inputs.aggressiveness,
        holiday=inputs.holiday,
        categories=inputs.categories,
        current_slot=inputs.current_slot,
        current_cat=inputs.categories[inputs.current_slot],
        upcoming=upcoming,
        ramp_in=ramp_in,
        ramp_out=ramp_out,
        comfort_floor=floor,
        comfort_floor_reached=inputs.indoor < floor,
        forecast_temps=inputs.forecast_temps,
        interval_minutes=inputs.interval_minutes,
        preheat_dt_seconds=elapsed,
    )
    try:
        coroutine.send(None)
    except StopIteration:
        pass
    except (AttributeError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return copy.deepcopy(state), ControlReplayDecision(
            None, "unavailable", {}, (), "invalid_control_context"
        )
    else:
        raise RuntimeError("control replay unexpectedly attempted asynchronous I/O")
    finally:
        coroutine.close()
    if context._decision is None:
        raise RuntimeError("control replay produced no decision")
    return context.state(), context._decision
