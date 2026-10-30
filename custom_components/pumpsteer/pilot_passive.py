"""Passive Pilot learning from explicitly selected measured heat activity.

HA report time is not evidence of physical measurement time. Never writes a
command and never turns inferred electrical power or compressor status into heat.
"""

from collections import deque
from datetime import timedelta
import math
from .pilot_vendor.adaptive_model import AdaptiveThermalModel
from .pilot_vendor.learning_interval import IntervalCollector
from .pilot_vendor.learning_manager import LearningManager


class PilotPassive:
    def __init__(self):
        self.manager = LearningManager(AdaptiveThermalModel())
        from .pilot_forecast_audit import ForecastAudit

        self.audit = ForecastAudit()
        self.last_indoor_report = None
        self.last_indoor_value = None
        self.house_intervals = 0
        self.source_identity = None
        self._context_reports = None
        self.status = "waiting_for_heat_signal"
        self.validation_after = None
        # Lower-evidence, diagnostic-only intervals. Never feed these rows to
        # the fitted/validated Pilot model.
        self.indicated_house = IntervalCollector(60)
        self.indicated_observations = deque(maxlen=72)
        self._observation_evidence = None
        self._observation_first_indoor_report = None
        self._observation_reports = None
        self._observation_report_counts = [0, 0]
        self._observation_max_age_seconds = [0.0, 0.0]

    def observe_indicated(
        self, now, indoor, outdoor, operation, *, reported_after=None
    ):
        """Collect temperature intervals with indicated or unknown operation.

        This is not measured heat delivery and is deliberately isolated from
        ``manager.model`` and all validated predictions. Temperatures can be
        held for at most 15 minutes; coverage describes that bounded hold,
        not independent physical samples.
        """
        required = {"compressor", "radiator", "hot_water", "power", "supply", "return"}
        signals = (
            set(operation.get("signals", ())) if isinstance(operation, dict) else set()
        )
        states = (indoor, outdoor)
        reports = tuple(
            self._report(
                source,
                now,
                temperature=True,
                reported_after=reported_after,
                max_age=timedelta(minutes=15),
            )
            for source in states
        )
        stable = bool(
            isinstance(operation, dict)
            and operation.get("mode") == "indicated_radiator_heating"
            and operation.get("evidence_level") == "operating_indication"
            and operation.get("evidence_uncertainty") == "heat_delivery_unverified"
            and required <= signals
        )
        evidence = "operating_indication" if stable else "unknown_heating_mode"
        previous_tick = self.indicated_house.last
        if (
            previous_tick is not None
            and not 0 <= now.timestamp() - previous_tick <= 300
        ):
            self.indicated_house.reset()
            self._observation_first_indoor_report = None
            self._observation_reports = None
            self._observation_report_counts = [0, 0]
            self._observation_max_age_seconds = [0.0, 0.0]
        if evidence != self._observation_evidence:
            self.indicated_house.reset()
            self._observation_first_indoor_report = None
            self._observation_reports = None
            self._observation_report_counts = [0, 0]
            self._observation_max_age_seconds = [0.0, 0.0]
            self._observation_evidence = evidence
        if any(report is None for report in reports):
            self.indicated_house.reset()
            self._observation_first_indoor_report = None
            self._observation_reports = None
            self._observation_report_counts = [0, 0]
            self._observation_max_age_seconds = [0.0, 0.0]
            self.indicated_house.last_status = "temperature_report_unavailable"
            return None
        for source, report in zip(states, reports):
            changed = getattr(source, "last_changed", None)
            if changed is None or changed.tzinfo is None or changed > report[0]:
                self.indicated_house.reset()
                self._observation_first_indoor_report = None
                self._observation_reports = None
                self._observation_report_counts = [0, 0]
                self._observation_max_age_seconds = [0.0, 0.0]
                self.indicated_house.last_status = "temperature_context_transition"
                return None
        previous = self._observation_reports
        if previous is not None and any(
            current[0] < old[0] or (current[0] == old[0] and current[1] != old[1])
            for current, old in zip(reports, previous)
        ):
            self.indicated_house.reset()
            self._observation_first_indoor_report = None
            self._observation_reports = None
            self._observation_report_counts = [0, 0]
            self._observation_max_age_seconds = [0.0, 0.0]
            self.indicated_house.last_status = "temperature_report_inconsistent"
            return None
        for index, report in enumerate(reports):
            if previous is None or report[0] > previous[index][0]:
                self._observation_report_counts[index] += 1
            self._observation_max_age_seconds[index] = max(
                self._observation_max_age_seconds[index],
                (now - report[0]).total_seconds(),
            )
        self._observation_reports = reports
        if self._observation_first_indoor_report is None:
            self._observation_first_indoor_report = reports[0][0]
        self.indicated_house.last_status = "waiting_for_interval"
        # The collector needs a numeric input for its coverage accounting;
        # zero here is a placeholder when the mode is unknown, not zero heat.
        sample = self.indicated_house.observe(
            now.timestamp(), reports[0][1], reports[1][1], 1.0 if stable else 0.0
        )
        if sample is not None and reports[0][0] > self._observation_first_indoor_report:
            row = {
                "evidence_level": evidence,
                "interpretation": "apparent_indoor_response_not_heat_delivery",
                "end": sample.end,
                "hours": sample.hours,
                "start_indoor": sample.start_temp,
                "end_indoor": sample.end_temp,
                "mean_outdoor": sample.outdoor,
                "indicated_fraction": sample.heat if stable else None,
                "coverage": sample.coverage,
                "coverage_basis": "reported_temperature_held_at_most_15_minutes",
                "indoor_report_count": self._observation_report_counts[0],
                "outdoor_report_count": self._observation_report_counts[1],
                "maximum_indoor_report_age_seconds": self._observation_max_age_seconds[
                    0
                ],
                "maximum_outdoor_report_age_seconds": self._observation_max_age_seconds[
                    1
                ],
            }
            self.indicated_observations.append(row)
            self._observation_first_indoor_report = reports[0][0]
            self._observation_report_counts = [1, 1]
            self._observation_max_age_seconds = [
                (now - r[0]).total_seconds() for r in reports
            ]
        elif sample is not None:
            self._observation_first_indoor_report = reports[0][0]
            self._observation_report_counts = [1, 1]
            self._observation_max_age_seconds = [
                (now - r[0]).total_seconds() for r in reports
            ]
            self.indicated_house.last_status = "insufficient_indoor_reports"
            return None
        return sample

    def reset_interval(self, *, invalidate_validation=True, reset_pump=True):
        self.manager.house.reset()
        self.audit.pending = None
        if reset_pump:
            self.manager.pump_intervals.reset()
            self.manager.recent.clear()
            self.manager.pump.ready = False
            self.manager.pump.reason = "interval_interrupted"
        if invalidate_validation:
            self.validation_after = None
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

    def validated_house(self, now):
        """Validate a frozen Pilot house fit on later actual interval endpoints."""
        rows = self.manager.model.history
        if self.validation_after is None or len(rows) < 32:
            return None
        if rows[-1].end > now or now - rows[-1].end > 3 * 3600:
            return None
        training, validation = rows[:-8], rows[-8:]
        if any(
            row.end - row.hours * 3600 < self.validation_after for row in validation
        ):
            return None
        if any(
            row.end - row.hours * 3600 < prior.end - 1
            for prior, row in zip(rows, rows[1:])
        ):
            return None
        model = AdaptiveThermalModel()
        for row in training:
            model.add_interval(row)
        if not model.gain_identified:
            return None
        outdoor_min, outdoor_max = (
            min(row.outdoor for row in training),
            max(row.outdoor for row in training),
        )
        indoor_min, indoor_max = (
            min(row.start_temp for row in training),
            max(row.start_temp for row in training),
        )
        heat_min, heat_max = (
            min(row.heat for row in training),
            max(row.heat for row in training),
        )
        if any(
            not (
                outdoor_min <= row.outdoor <= outdoor_max
                and indoor_min <= row.start_temp <= indoor_max
                and heat_min <= row.heat <= heat_max
            )
            for row in validation
        ):
            return None
        errors, baseline = [], []
        for row in validation:
            prediction = row.start_temp + row.hours * (
                model.heat_loss_coeff * (row.outdoor - row.start_temp)
                + model.heat_gain_coeff * row.heat
                + model.background_gain
            )
            errors.append(abs(prediction - row.end_temp))
            baseline.append(abs(row.start_temp - row.end_temp))
        mae, persistence = sum(errors) / len(errors), sum(baseline) / len(baseline)
        if persistence < 0.02 or mae > persistence * 0.9:
            return None
        return {
            "loss": model.heat_loss_coeff,
            "gain": model.heat_gain_coeff,
            "background": model.background_gain,
            "mae": mae,
            "persistence_mae": persistence,
            "samples": len(validation),
            "outdoor_min": outdoor_min,
            "outdoor_max": outdoor_max,
            "indoor_min": indoor_min,
            "indoor_max": indoor_max,
            "heat_min": heat_min,
            "heat_max": heat_max,
        }

    def observe_response(
        self,
        now,
        outdoor,
        heat,
        command,
        mode,
        *,
        output_clamp,
        reported_after=None,
        defer_fit=False,
    ):
        """Collect actual HA-reported command and heat into Pilot's 15-minute bins.

        The number entity reports HA state, not physical heat-pump acknowledgement.
        """
        from .pilot_vendor.pump_response import virtual_request

        manager = self.manager
        reports = (
            self._report(outdoor, now, temperature=True, reported_after=reported_after),
            self._report(heat, now, reported_after=reported_after),
            self._report(command, now, temperature=True, reported_after=reported_after),
        )
        if mode not in {
            "normal",
            "preheating",
            "pre_braking",
            "braking",
            "brake_hold",
        } or any(value is None for value in reports):
            manager.pump_intervals.reset()
            manager.recent.clear()
            manager.pump.ready = False
            manager.pump.reason = "response_input_unavailable"
            return
        out, heat_report, actual = reports
        request = virtual_request(out[1], actual[1], output_clamp)
        if request is None:
            manager.pump_intervals.reset()
            manager.recent.clear()
            manager.pump.ready = False
            manager.pump.reason = "response_input_unavailable"
            return
        previous = manager.pump_intervals.last
        if previous is not None and (
            now.timestamp() <= previous or now.timestamp() - previous > 300
        ):
            manager.pump_intervals.reset()
            manager.recent.clear()
            manager.pump.ready = False
            manager.pump.reason = "observation_gap"
            previous = None
        if previous is not None:
            for source in (outdoor, heat, command):
                changed = getattr(source, "last_changed", None)
                if (
                    changed is None
                    or changed.tzinfo is None
                    or (previous < changed.timestamp() < now.timestamp())
                ):
                    manager.pump_intervals.reset()
                    manager.recent.clear()
                    manager.pump.ready = False
                    manager.pump.reason = "unobserved_transition"
                    break
        tick = now.timestamp()
        if manager.recent and (
            tick <= manager.recent[-1][0] or tick - manager.recent[-1][0] > 300
        ):
            manager.recent.clear()
        manager.recent.append((tick, request, heat_report[1]))
        while len(manager.recent) > 512 or (
            len(manager.recent) > 1 and manager.recent[1][0] <= tick - 8100
        ):
            manager.recent.popleft()
        sample = manager.pump_intervals.observe(
            tick, None, out[1], heat_report[1], request
        )
        if sample is not None:
            if defer_fit:
                return sample
            manager.pump.add_interval(sample)
        elif manager.pump_intervals.last_status in {
            "observation_gap",
            "insufficient_coverage",
        }:
            manager.pump.ready = False
            manager.pump.reason = manager.pump_intervals.last_status

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
            from .pilot_forecast_audit import ForecastAudit

            self.audit = ForecastAudit()
            self.house_intervals = 0
            self.validation_after = None
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
                    self.reset_interval(invalidate_validation=False, reset_pump=False)
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
        if self.validation_after is None:
            self.validation_after = now.timestamp()
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


class SupplyHeatDetector:
    """Pilot hysteresis/debounce, conditioned on verified radiator mode.

    Derived from Heat Pump Pilot's _update_heating_detected_from_supply,
    Apache-2.0; this is an indicator, not measured thermal power.
    """

    def __init__(self, threshold=30.0):
        self.threshold = threshold
        self.value = None
        self.candidate = None
        self.candidate_since = None
        self.changed_at = None
        self.last_report = None

    def observe(self, now, supply, heating_mode, *, reported_after=None):
        temperature = PilotPassive._report(
            supply, now, temperature=True, reported_after=reported_after
        )
        mode = PilotPassive._report(heating_mode, now, reported_after=reported_after)
        if temperature is None or mode is None or mode[1] != 1.0:
            self.value = self.candidate = self.candidate_since = self.changed_at = (
                self.last_report
            ) = None
            return None
        at, value = temperature
        mode_changed = getattr(heating_mode, "last_changed", None)
        if mode_changed is None or mode_changed.tzinfo is None or mode_changed > at:
            self.value = self.candidate = self.candidate_since = self.changed_at = (
                self.last_report
            ) = None
            return None
        if self.last_report is not None and mode_changed > self.last_report:
            self.value = self.candidate = self.candidate_since = self.changed_at = (
                self.last_report
            ) = None
        if self.last_report is not None and at <= self.last_report:
            return self.value
        self.last_report = at
        upper, lower = self.threshold + 0.5, self.threshold - 0.5
        if self.value is True:
            instantaneous = not value <= lower
        elif self.value is False:
            instantaneous = value >= upper
        else:
            instantaneous = (
                True if value >= upper else False if value <= lower else None
            )
        if self.value is None and instantaneous is not None:
            self.value, self.changed_at = instantaneous, at
            self.candidate = self.candidate_since = None
        elif instantaneous == self.value:
            self.candidate = self.candidate_since = None
        elif self.candidate != instantaneous:
            self.candidate, self.candidate_since = instantaneous, at
        elif (
            self.candidate_since is not None
            and (at - self.candidate_since).total_seconds() >= 60
        ):
            self.value, self.changed_at = instantaneous, at
            self.candidate = self.candidate_since = None
        return self.value
