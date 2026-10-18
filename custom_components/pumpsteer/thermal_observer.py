"""Passive HA-report adapter; never called by control decisions or outputs."""

from datetime import datetime
import math

from .thermal_intelligence import ThermalDiagnostics, TemperatureMeasurement, _utc
from .thermal_model import ThermalModel


class ThermalObserver:
    """Reuse the existing learner and reference evaluator with bounded evidence.

    last_reported is HA reception, NOT a proven physical measurement timestamp.
    Reports must be distinct, post-start, finite, Celsius and not explicitly
    restored. Unknown source-side age remains a limitation of every result.
    """

    def __init__(self, model: ThermalModel) -> None:
        self.model = model
        self.diagnostics = ThermalDiagnostics()
        self._after = None
        self._last_now = None
        self._indoor_at = None
        self._outdoor_at = None
        self._braking = False
        self._reason = "no_measurement"
        self._last_comparison = {}

    def start(self, now: datetime) -> None:
        """New lifecycle segment; only genuinely fitted parameters survive."""
        self._braking = False
        self._after = _utc(now)
        self._last_now = self._indoor_at = self._outdoor_at = None
        self.model.clear_history(samples=True)
        self.diagnostics = ThermalDiagnostics()
        self._last_comparison = {}
        self._reason = "awaiting_post_start_reports"

    def _reading(self, state, now):
        if state is None or isinstance(state.state, bool):
            return None
        attrs = state.attributes
        if attrs.get("restored") or attrs.get("unit_of_measurement") != "°C":
            return None
        at = _utc(getattr(state, "last_reported", None))
        if at is None or not 0 <= (now - at).total_seconds() <= 300:
            return None
        if self._after is not None and at <= self._after:
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        return (
            TemperatureMeasurement(value, at)
            if math.isfinite(value) and -100 <= value <= 100
            else None
        )

    def _break(self, now, reason):
        self.model.clear_history()
        self._braking = False
        self.diagnostics.cancel_pending(now)
        self._reason = reason

    def observe(self, now, indoor_state, outdoor_state, *, braking):
        clock = _utc(now)
        if clock is None:
            self._break(self._last_now, "invalid_clock")
            return
        if self._last_now is not None and clock < self._last_now:
            self.start(clock)
            self._reason = "clock_reversal"
            return
        if (
            self._last_now is not None
            and (clock - self._last_now).total_seconds() > 300
        ):
            self._break(clock, "observation_gap")
        self._last_now = clock
        indoor, outdoor = (
            self._reading(indoor_state, clock),
            self._reading(outdoor_state, clock),
        )
        if indoor is None or outdoor is None:
            self._break(clock, "unqualified_temperature_report")
            return
        if (self._indoor_at is not None and indoor.measured_at < self._indoor_at) or (
            self._outdoor_at is not None and outdoor.measured_at < self._outdoor_at
        ):
            self._break(clock, "out_of_order_report")
            return
        if (
            self._indoor_at is not None
            and (indoor.measured_at - self._indoor_at).total_seconds() > 300
        ):
            self._break(clock, "measurement_gap")
        distinct = indoor.measured_at != self._indoor_at
        self._indoor_at, self._outdoor_at = indoor.measured_at, outdoor.measured_at
        if not braking:
            self._break(clock, "outside_braking")
        elif not self._braking:
            self.model.clear_history()
        self._braking = braking
        if distinct:
            self.model.record_temp(indoor.measured_at, indoor.celsius)
            if braking:
                self.model.collect_braking_sample(indoor.celsius, outdoor.celsius)
        self.diagnostics.observe(
            now=clock,
            indoor=indoor,
            outdoor=outdoor,
            k_per_hour=self.model.k if self.model.is_valid else None,
            model_fitted=self.model.is_valid,
            operating_mode="braking" if braking else "normal",
            issue_forecasts=braking,
        )
        if braking:
            self._reason = (
                self.diagnostics.reason if self.model.is_valid else "model_not_fitted"
            )
        for comparison in self.diagnostics.last_comparisons:
            p = comparison.prediction
            self._last_comparison[str(p.horizon_minutes)] = {
                "predicted_change_c": round(
                    p.temperature_c - p.indoor_temperature_c, 4
                ),
                "actual_change_c": round(
                    comparison.actual_temperature_c - p.indoor_temperature_c, 4
                ),
                "error_c": round(comparison.error_c, 4),
                "k_per_hour": p.k_per_hour,
                "model_version": p.model_version,
                "actual_at": comparison.actual_at.isoformat(),
                "lateness_seconds": comparison.lateness_seconds,
            }

    def attributes(self):
        quality = self.diagnostics.quality()
        evaluated = any(q.count for q in quality.values())
        return {
            "status": "conditional"
            if self.model.is_valid and self.diagnostics.predictions
            else "insufficient_data",
            "reason": self._reason,
            "measurement_basis": "ha_report; physical_age_unverified",
            "reliability": "conditional_observed_error"
            if evaluated
            else "insufficient_evaluations",
            "model_version": self.diagnostics.model_version,
            "predicted_change_c": {
                str(p.horizon_minutes): round(
                    p.temperature_c - p.indoor_temperature_c, 4
                )
                for p in self.diagnostics.predictions
            },
            "forecast_issued_at": self.diagnostics.predictions[0].issued_at.isoformat()
            if self.diagnostics.predictions
            else None,
            "last_comparison": dict(self._last_comparison),
            "quality": {
                str(h): {
                    "count": q.count,
                    "missed_count": q.missed_count,
                    "mae_c": round(q.mae_c, 4) if q.mae_c is not None else None,
                    "bias_c": round(q.bias_c, 4) if q.bias_c is not None else None,
                    "coverage": round(q.coverage, 4)
                    if q.coverage is not None
                    else None,
                }
                for h, q in quality.items()
            },
            "assumptions": [
                "heating_input_unknown",
                "residual_heat_unmodelled",
                "constant_outdoor_assumption",
            ],
        }
