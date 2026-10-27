"""Passive Pilot learning from explicitly selected measured heat activity.

HA report time is not evidence of physical measurement time. Never writes a
command and never turns inferred electrical power or compressor status into heat.
"""

from datetime import timedelta
import math
from .pilot_vendor.adaptive_model import AdaptiveThermalModel
from .pilot_vendor.learning_manager import LearningManager


class PilotPassive:
    def __init__(self):
        self.manager = LearningManager(AdaptiveThermalModel())
        self.last_indoor_report = None
        self.last_indoor_value = None
        self.house_intervals = 0
        self.source_identity = None
        self._context_reports = None
        self.status = "waiting_for_heat_signal"

    def reset_interval(self):
        self.manager.house.reset()
        self.manager.pump_intervals.reset()
        self.manager.recent.clear()
        self.manager.pump.ready = False
        self._context_reports = None

    @staticmethod
    def _report(
        state,
        now,
        *,
        temperature=False,
        reported_after=None,
        max_age=timedelta(minutes=5),
    ):
        if state is None or (getattr(state, "attributes", {}) or {}).get("restored"):
            return None
        at = getattr(state, "last_reported", None)
        try:
            if (
                at is None
                or at.tzinfo is None
                or now - at < timedelta(0)
                or (max_age is not None and now - at > max_age)
            ):
                return None
            if reported_after is not None and at <= reported_after:
                return None
            if temperature:
                if (getattr(state, "attributes", {}) or {}).get(
                    "unit_of_measurement"
                ) != "°C":
                    return None
                value = float(state.state)
                if not math.isfinite(value) or not -50 <= value <= 60:
                    return None
            else:
                if state.state not in ("on", "off"):
                    return None
                value = 1.0 if state.state == "on" else 0.0
            return at, value
        except (AttributeError, TypeError, ValueError, OverflowError):
            return None

    def observe(self, now, indoor, outdoor, heat, mode, *, reported_after=None):
        if (
            self.manager.house.last is not None
            and now.timestamp() < self.manager.house.last
        ):
            self.reset_interval()
            self.status = "clock_reversal"
            return
        if mode == "safe_mode":
            self.reset_interval()
            self.status = "suspended_safe"
            return
        if mode not in {"normal", "preheating", "pre_braking", "braking", "brake_hold"}:
            self.reset_interval()
            self.status = "control_context_unqualified"
            return
        current_in = self._report(
            indoor, now, temperature=True, reported_after=reported_after, max_age=None
        )
        current_out = self._report(
            outdoor, now, temperature=True, reported_after=reported_after
        )
        current_heat = self._report(heat, now, reported_after=reported_after)
        if current_heat is None:
            self.reset_interval()
            self.status = "heat_signal_unavailable"
            return
        if current_in is None or current_out is None:
            self.reset_interval()
            self.status = "temperature_report_unavailable"
            return
        indoor_attrs = getattr(indoor, "attributes", {}) or {}
        outdoor_attrs = getattr(outdoor, "attributes", {}) or {}
        components = indoor_attrs.get("source_temperatures_used")
        if isinstance(components, dict) and all(isinstance(k, str) for k in components):
            components = tuple(sorted(components))
        else:
            components = None
        selected = outdoor_attrs.get("selected_source")
        selected = selected if isinstance(selected, str) else None
        identity = (components, selected)
        if self.source_identity is not None and identity != self.source_identity:
            self.manager = LearningManager(AdaptiveThermalModel())
            self.house_intervals = 0
            self._context_reports = None
        self.source_identity = identity
        at, temp = current_in
        try:
            for source, report in ((heat, current_heat), (outdoor, current_out)):
                changed = getattr(source, "last_changed", None)
                if changed is None or changed.tzinfo is None or changed > report[0]:
                    raise ValueError
                if (
                    self.last_indoor_report is not None
                    and changed > self.last_indoor_report
                ):
                    self.reset_interval()
        except (TypeError, ValueError):
            self.reset_interval()
            self.status = "source_change_time_unavailable"
            return
        prior_context = self._context_reports
        if prior_context is not None:
            for current, previous in zip((current_out, current_heat), prior_context):
                if current[0] < previous[0] or (
                    current[0] == previous[0] and current[1] != previous[1]
                ):
                    self.reset_interval()
                    self.status = "context_report_reordered"
                    return
        self._context_reports = (current_out, current_heat)
        new_indoor = self.last_indoor_report is None or at > self.last_indoor_report
        house = self.manager.house
        if not new_indoor:
            if at < self.last_indoor_report:
                self.reset_interval()
                self.status = "reordered_indoor_report"
                return
            if at == self.last_indoor_report and temp != self.last_indoor_value:
                self.reset_interval()
                self.status = "inconsistent_indoor_report"
                return
            # These are held states, not new temperature observations. Never
            # admit an interval endpoint unless a real indoor report arrives.
            if house.start is not None:
                tick = now.timestamp()
                if house.last is not None and tick < house.last:
                    self.reset_interval()
                    self.status = "clock_reversal"
                    return
                if tick - house.start < house.seconds:
                    house.observe(
                        tick, temp, current_out[1], current_heat[1], request=None
                    )
                elif house.last is not None and tick - house.last > 300:
                    house.reset()
            self.status = "waiting_for_indoor_report"
            return
        if current_out[0] > at or current_heat[0] > at:
            if prior_context is None:
                self.status = "awaiting_aligned_reports"
                return
            chosen = []
            for source, current, previous in zip(
                (outdoor, heat), (current_out, current_heat), prior_context
            ):
                if current[0] <= at:
                    chosen.append(current)
                elif (
                    previous[0] <= at
                    and at - previous[0] <= timedelta(minutes=5)
                    and previous[1] == current[1]
                    and source.last_changed <= previous[0]
                ):
                    chosen.append(previous)
                else:
                    self.status = "awaiting_aligned_reports"
                    return
            current_out, current_heat = chosen
        if house.last is not None and at.timestamp() <= house.last:
            house.reset()
            self.last_indoor_report = at
            self.last_indoor_value = temp
            self.status = "late_indoor_report"
            return
        self.last_indoor_report = at
        self.last_indoor_value = temp
        sample = house.observe(
            at.timestamp(), temp, current_out[1], current_heat[1], request=None
        )
        self.manager.model.observe_temperature(temp)
        if sample is not None:
            self.manager.model.add_interval(sample)
            self.manager.last_update = at.timestamp()
            self.house_intervals += 1
        self.status = self.manager.model.status
