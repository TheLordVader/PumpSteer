"""Bounded, diagnostic-only passive references and future measurement checks.

No Home Assistant, control, persistence or I/O hooks are installed here. The caller
must retain the provenance of explicit observation timestamps. HA report times do
not establish physical measurement age. Unknown heat input prevents these references
from certifying comfort or safety.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

HORIZONS_MINUTES: Final = (30, 60, 120)
_MAX_PENDING: Final = 24
_MAX_EVALUATIONS_PER_HORIZON: Final = 64
_MAX_SAMPLE_AGE_SECONDS: Final = 900
_MATCH_TOLERANCE_SECONDS: Final = 120
_ISSUE_INTERVAL_SECONDS: Final = 1800
_OPERATING_MODES: Final = (
    "unknown",
    "braking",
    "preheating",
    "normal",
    "holiday",
    "pre_braking",
    "brake_hold",
    "recovery",
    "precool",
    "safe_mode",
    "sensor_interruption",
)
_WARNINGS: Final = (
    "heating_input_unknown",
    "residual_heat_unmodelled",
    "constant_outdoor_assumption",
)


@dataclass(frozen=True)
class TemperatureMeasurement:
    """A value and its observation time, not a substituted polling timestamp."""

    celsius: float | None
    measured_at: datetime


@dataclass(frozen=True)
class PIProjection:
    """Known PI state copied as input, never a reference to the live controller.

    Future mode, target, brake factor and preheat boost are conditional constants.
    Unknown price/safety mode changes are NOT claimed as known future decisions.
    """
    target: float
    integral: float
    last_error: float
    kp: float
    ki: float
    kd: float
    integral_clamp: float
    output_clamp: float
    mode: str
    brake_factor: float
    brake_delta_c: float
    extra_demand: float
    reported_at: datetime

    @classmethod
    def from_control(cls, config, attributes, reported_at):
        """Only the existing PI parameters and actually reported PI state."""
        if config is None:
            return None
        from . import settings
        try:
            context=cls(target=float(attributes['target_temperature']),
                integral=float(attributes['pi_i_term']),last_error=float(attributes['pi_error_c']),
                kp=float(config.get('pid_kp',settings.PID_KP)),
                ki=float(config.get('pid_ki',settings.PID_KI)),
                kd=float(config.get('pid_kd',settings.PID_KD)),
                integral_clamp=float(config.get('pid_integral_clamp',settings.PID_INTEGRAL_CLAMP)),
                output_clamp=float(config.get('pid_output_clamp',settings.PID_OUTPUT_CLAMP)),
                mode=attributes['mode'],brake_factor=float(attributes.get('brake_factor',0)),
                brake_delta_c=float(config.get('brake_delta_c',settings.BRAKE_DELTA_C)),
                extra_demand=float(attributes['preheat_boost_c']) if attributes.get('mode')=='preheating' else 0.,
                reported_at=reported_at)
            raw=(attributes['target_temperature'],attributes['pi_i_term'],attributes['pi_error_c'],
                 *(config.get(key,0) for key in ('pid_kp','pid_ki','pid_kd','pid_integral_clamp','pid_output_clamp','brake_delta_c')))
            if any(isinstance(v,bool) for v in raw) or any(not math.isfinite(v) for v in (
                context.target,context.integral,context.last_error,*context.signature,context.extra_demand)):
                return None
            return context
        except (KeyError,TypeError,ValueError):
            return None

    @property
    def signature(self):
        return (self.kp,self.ki,self.kd,self.integral_clamp,self.output_clamp,self.brake_delta_c)

    def project(self, response, indoor, command, now, weather_path, mode,
                *, adjustment_fake_c=0., adjustment_minutes=30., bounds=None):
        from .control import PIController
        from .settings import MIN_FAKE_TEMP, MAX_FAKE_TEMP
        clock=_utc(now);reported=_utc(self.reported_at)
        values=(self.target,self.integral,self.last_error,self.kp,self.ki,self.kd,
                self.integral_clamp,self.output_clamp,self.brake_factor,self.brake_delta_c,
                self.extra_demand,adjustment_fake_c,adjustment_minutes)
        if (clock is None or reported is None or not 0<=(clock-reported).total_seconds()<=120
            or mode!=self.mode or any(type(v) not in (int,float) or not math.isfinite(v) for v in values)
            or not 0<=self.kp<=50 or not 0<=self.ki<=1 or not 0<=self.kd<=10
            or not 0<self.integral_clamp<=50 or abs(self.integral)>self.integral_clamp
            or not 0<self.output_clamp<=50 or not 0<=self.brake_factor<=1
            or not 0<=self.brake_delta_c<=30 or not 0<=self.extra_demand<=10
            or abs(adjustment_fake_c)>1 or not 0<=adjustment_minutes<=30):
            return None
        controller=PIController()
        controller._integral=self.integral
        controller._last_error=self.last_error
        controller._last_time=clock
        value=indoor;at=clock
        def clamp(v):return max(MIN_FAKE_TEMP,min(MAX_FAKE_TEMP,v))
        for end,outdoor in weather_path:
            if not at<end or (end-clock).total_seconds()>7200:
                return None
            while at<end:
                step_end=min(end,at+timedelta(minutes=1))
                if at==clock:
                    fake=-command
                else:
                    result=controller.compute(self.target,value,outdoor,1.,at,
                        braking_active=mode in ('braking','brake_hold','pre_braking'),
                        kp=self.kp,ki=self.ki,kd=self.kd,integral_clamp=self.integral_clamp,
                        output_clamp=self.output_clamp,brake_behavior='freeze')
                    base=clamp(outdoor+result.offset)
                    fake=(base+(outdoor+self.brake_delta_c-base)*self.brake_factor
                          if mode in ('braking','brake_hold','pre_braking') else
                          clamp(outdoor+result.offset-self.extra_demand))
                if (at-clock).total_seconds()<60*adjustment_minutes:
                    fake=clamp(fake+adjustment_fake_c)
                value=response.project(value,-clamp(fake),at,((step_end,outdoor),),mode,bounds=bounds)
                if value is None:return None
                at=step_end
        return value


@dataclass(frozen=True)
class ThermalPrediction:
    """Immutable evidence for a conditional no-heat-input reference."""

    horizon_minutes: int
    issued_at: datetime
    origin_at: datetime
    due_at: datetime
    temperature_c: float
    k_per_hour: float
    warnings: tuple[str, ...]
    model_version: int
    model_fitted: bool
    indoor_temperature_c: float
    outdoor_temperature_c: float
    outdoor_at: datetime
    operating_mode: str
    conditional: bool = True
    response_coefficients: tuple[float, float, float] | None = None
    control_signal_c: float | None = None
    uncertainty_c: float | None = None
    forecast_received_at: datetime | None = None
    forecast_origin_at: datetime | None = None
    weather_source: str = "constant_current_outdoor"
    weather_path: tuple = ()
    price_slots: tuple = ()
    price_unit: str | None = None
    pi_projection: PIProjection | None = None
    uncertainty_basis: str = "rate_residual_allowance_unvalidated"


@dataclass(frozen=True)
class ThermalComparison:
    prediction: ThermalPrediction
    actual_at: datetime | None
    actual_temperature_c: float | None
    error_c: float | None  # actual minus reference; never zero-fill missed targets
    lateness_seconds: float | None
    resolved_at: datetime
    status: str  # evaluated, missed, interrupted, clock_reset or evicted


@dataclass(frozen=True)
class ForecastQuality:
    """One version's resolved records in the retained per-horizon window."""

    count: int
    missed_count: int
    mae_c: float | None
    bias_c: float | None
    max_absolute_error_c: float | None
    persistence_mae_c: float | None = None

    @property
    def mae_gain_c(self) -> float | None:
        """Positive means lower MAE than persistence on identical outcomes."""
        if self.mae_c is None or self.persistence_mae_c is None:
            return None
        return self.persistence_mae_c - self.mae_c

    @property
    def eligible_count(self) -> int:
        return self.count + self.missed_count

    @property
    def coverage(self) -> float | None:
        return self.count / self.eligible_count if self.eligible_count else None


def _utc(value: datetime) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _number_between(value: object, low: float, high: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and low <= value <= high
        and math.isfinite(value)
    )


def _reading(
    measurement: TemperatureMeasurement | None, name: str, now: datetime
) -> tuple[float | None, datetime | None, str]:
    if measurement is None:
        return None, None, f"missing_{name}"
    if not _number_between(measurement.celsius, -100.0, 100.0):
        return None, None, f"invalid_{name}"
    at = _utc(measurement.measured_at)
    if at is None:
        return None, None, f"invalid_{name}_timestamp"
    age = (now - at).total_seconds()
    if age < 0:
        return None, None, f"future_{name}"
    if age > _MAX_SAMPLE_AGE_SECONDS:
        return None, None, f"stale_{name}"
    return float(measurement.celsius), at, ""


class ThermalDiagnostics:
    """Issue with fresh inputs; evaluate independently using real indoor samples.

    Revisions preserve pending forecasts and bounded, version-tagged evaluations.
    Sensor gaps block issuance, not unrelated evaluations. Restarts start empty.
    """

    def __init__(self) -> None:
        self.status = "insufficient_data"
        self.reason = "no_measurement"
        self.predictions: tuple[ThermalPrediction, ...] = ()
        self.last_comparisons: tuple[ThermalComparison, ...] = ()
        self._pending: deque[ThermalPrediction] = deque(maxlen=_MAX_PENDING)
        self._history: dict[int, deque[ThermalComparison]] = {
            h: deque(maxlen=_MAX_EVALUATIONS_PER_HORIZON) for h in HORIZONS_MINUTES
        }
        self._model: tuple[float, bool] | None = None
        self._model_version = 0
        self._last_now: datetime | None = None
        self._last_measurement: datetime | None = None
        self._last_outdoor_measurement: datetime | None = None
        self._last_issue: datetime | None = None

    @property
    def model_version(self) -> int:
        """Instance-local revision of the last valid supplied k/fitted pair."""
        return self._model_version

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pending_predictions(self) -> tuple[ThermalPrediction, ...]:
        return tuple(self._pending)

    @property
    def evaluation_history(self) -> tuple[ThermalComparison, ...]:
        """Resolved records grouped by horizon; at most 64 records per horizon."""
        return tuple(record for records in self._history.values() for record in records)

    def _block(self, reason: str) -> None:
        self.predictions = ()
        self.status = "insufficient_data"
        self.reason = reason

    def _finish(
        self,
        prediction: ThermalPrediction,
        clock: datetime,
        status: str,
        tin: float | None = None,
        at: datetime | None = None,
    ) -> ThermalComparison:
        record = ThermalComparison(
            prediction=prediction,
            actual_at=at,
            actual_temperature_c=tin,
            error_c=tin - prediction.temperature_c if tin is not None else None,
            lateness_seconds=(at - prediction.due_at).total_seconds() if at else None,
            resolved_at=clock,
            status=status,
        )
        self._history[prediction.horizon_minutes].append(record)
        return record

    def _evaluate(
        self, clock: datetime, tin: float | None, at: datetime | None
    ) -> None:
        retained: deque[ThermalPrediction] = deque(maxlen=_MAX_PENDING)
        matched = []
        for prediction in self._pending:
            lateness = (at - prediction.due_at).total_seconds() if at else None
            if lateness is not None and 0 <= lateness <= _MATCH_TOLERANCE_SECONDS:
                matched.append(self._finish(prediction, clock, "evaluated", tin, at))
            elif (lateness is not None and lateness > _MATCH_TOLERANCE_SECONDS) or (
                clock - prediction.due_at
            ).total_seconds() > (_MATCH_TOLERANCE_SECONDS + _MAX_SAMPLE_AGE_SECONDS):
                self._finish(prediction, clock, "missed")
            else:
                retained.append(prediction)
        self._pending = retained
        self.last_comparisons = tuple(matched)

    def cancel_pending(self, now: datetime, reason: str = "interrupted") -> None:
        """Do not score a brake forecast across an observed interruption."""
        clock = _utc(now) or self._last_now
        for prediction in self._pending:
            self._finish(prediction, clock, reason)
        self._pending.clear()
        self.predictions = ()

    def observe(
        self,
        *,
        now: datetime,
        indoor: TemperatureMeasurement | None,
        outdoor: TemperatureMeasurement | None,
        k_per_hour: float | None,
        model_fitted: bool = False,
        operating_mode: str = "unknown",
        issue_forecasts: bool = True,
        response=None,
        forecast=None,
        control_signal: float | None = None,
        pi_projection=None,
    ) -> tuple[ThermalPrediction, ...]:
        """Return new references; last_comparisons contains this call's matches."""
        self.last_comparisons = ()
        clock = _utc(now)
        if clock is None or (self._last_now is not None and clock < self._last_now):
            # A clock discontinuity invalidates this instance's pending timeline.
            for prediction in self._pending:
                self._finish(prediction, self._last_now, "clock_reset")
            self._pending.clear()
            self._last_measurement = self._last_outdoor_measurement = None
            self._last_issue = None
            self._last_now = clock
            self._block("invalid_clock" if clock is None else "clock_reversal")
            return ()
        self._last_now = clock

        if response is not None:
            k_per_hour, model_fitted = response.coefficients[1], True
        valid_model = _number_between(k_per_hour, 0.005, 0.5) and k_per_hour not in (
            0.005,
            0.5,
        )
        if valid_model:
            model = (float(k_per_hour), model_fitted is True, response.revision if response is not None else None, forecast.weather_source if forecast else "constant_outdoor", pi_projection.signature if pi_projection else None)
            if model != self._model:
                self._model = model
                self._model_version += 1

        tin, at, reason = _reading(indoor, "indoor", clock)
        duplicate = not reason and at == self._last_measurement
        if (
            not reason
            and self._last_measurement is not None
            and at < self._last_measurement
        ):
            reason = "out_of_order_measurement"
        if not reason and not duplicate:
            self._last_measurement = at
        self._evaluate(
            clock,
            tin if not reason and not duplicate else None,
            at if not reason and not duplicate else None,
        )

        tout, outdoor_at, outdoor_reason = _reading(outdoor, "outdoor", clock)
        if not outdoor_reason:
            if (
                self._last_outdoor_measurement is not None
                and outdoor_at < self._last_outdoor_measurement
            ):
                outdoor_reason = "out_of_order_outdoor_measurement"
            else:
                self._last_outdoor_measurement = outdoor_at
        if (
            reason
            or outdoor_reason
            or not valid_model
            or operating_mode not in _OPERATING_MODES
        ):
            self._block(
                reason
                or outdoor_reason
                or ("invalid_model" if not valid_model else "invalid_context")
            )
            return ()
        self.status = "conditional"
        if not issue_forecasts:
            self.predictions = ()
            self.reason = (
                "reference_not_comparable"
                if operating_mode in ("braking", "brake_hold")
                else "outside_braking"
            )
            return ()
        self.reason = "duplicate_measurement" if duplicate else "cadence_wait"
        if duplicate or (
            self._last_issue is not None
            and (clock - self._last_issue).total_seconds() < _ISSUE_INTERVAL_SECONDS
        ):
            return ()

        assert tin is not None and tout is not None and at is not None
        assert outdoor_at is not None and self._model is not None
        warnings = _WARNINGS + (
            () if self._model[1] else ("coefficient_not_validated",)
        )
        age_hours = (clock - at).total_seconds() / 3600
        if age_hours > 0:
            warnings += ("measurement_age_extrapolated",)
        issued = []
        if response is not None:
            if type(control_signal) not in (int,float) or not math.isfinite(control_signal) or not response.supports(tin,tout,control_signal,operating_mode):
                self._block("outside_response_domain")
                return ()
            warnings = ("empirical_effective_response", "held_PI_correction_assumption", "heating_input_unknown", "physical_measurement_age_unverified")
            if pi_projection is not None:
                warnings += ("same_mode_copied_PI_projection", "fixed_preheat_and_brake_assumption", "future_price_and_safety_switches_not_simulated")
            if forecast is None:
                warnings += ("constant_outdoor_assumption",)
            elif forecast.weather_issued_at is None:
                warnings += ("weather_issue_time_unknown",)
        for horizon in HORIZONS_MINUTES:
            due=clock+timedelta(minutes=horizon)
            path=forecast.weather_path(clock,due,tout) if forecast is not None else ((due,tout),)
            uncertainty = None
            uncertainty_basis="rate_residual_allowance_unvalidated"
            temperature = None
            if response is not None:
                # Extrapolate the original report only as an explicit model
                # assumption; origin_at is never rewritten as physical freshness.
                initial=response.project(tin,control_signal,at,((clock,tout),),operating_mode) if clock>at else tin
                temperature=response.project(initial,control_signal,clock,path,operating_mode,outdoor_now=tout) if initial is not None and path else None
                if temperature is None:
                    continue
                if pi_projection is not None:
                    temperature=pi_projection.project(response,initial,control_signal,clock,path,operating_mode) if initial is not None and path else None
                    if temperature is None:continue
                uncertainty=response.uncertainty(age_hours+horizon/60)
                q=self.quality(operating_mode=operating_mode)[horizon]
                if q.count>=20 and q.max_absolute_error_c is not None:
                    uncertainty=q.max_absolute_error_c+.1
                    uncertainty_basis="observed_same_revision_errors_plus_allowance_not_probability"
            decay = math.exp(-self._model[0] * (age_hours + horizon / 60))
            prediction = ThermalPrediction(
                horizon_minutes=horizon,
                issued_at=clock,
                origin_at=at,
                due_at=clock + timedelta(minutes=horizon),
                temperature_c=temperature if response is not None else tout + (tin - tout) * decay,
                k_per_hour=self._model[0],
                warnings=warnings,
                model_version=self._model_version,
                model_fitted=self._model[1],
                indoor_temperature_c=tin,
                outdoor_temperature_c=tout,
                outdoor_at=outdoor_at,
                operating_mode=operating_mode,
                response_coefficients=response.coefficients if response else None,
                control_signal_c=control_signal if response else None,
                uncertainty_c=uncertainty,
                forecast_received_at=forecast.received_at if forecast else None,
                forecast_origin_at=forecast.weather_issued_at if forecast else None,
                weather_source=forecast.weather_source if forecast else "constant_current_outdoor",
                weather_path=path or (),
                uncertainty_basis=uncertainty_basis,
                pi_projection=pi_projection,
                price_unit=forecast.price_unit if forecast else None,
                price_slots=tuple((p.start,p.end,p.price) for p in (forecast.prices_between(clock,due) or ())) if forecast else (),
            )
            if len(self._pending) == _MAX_PENDING:
                self._finish(self._pending.popleft(), clock, "evicted")
            self._pending.append(prediction)
            issued.append(prediction)
        if not issued:
            self._block("forecast_unavailable_or_outside_domain")
            return ()
        self.predictions = tuple(issued)
        self._last_issue = clock
        self.reason = "forecast_issued"
        return self.predictions

    def quality(self, model_version: int | None = None, operating_mode: str | None = None) -> dict[int, ForecastQuality]:
        """One revision only; both errors and coverage use the same retained window."""
        version = self._model_version if model_version is None else model_version
        result = {}
        for horizon, records in self._history.items():
            selected = [r for r in records if r.prediction.model_version == version
                        and (operating_mode is None or r.prediction.operating_mode == operating_mode)]
            errors = [r.error_c for r in selected if r.error_c is not None]
            persistence_errors = [
                r.actual_temperature_c - r.prediction.indoor_temperature_c
                for r in selected
                if r.error_c is not None
            ]
            count = len(errors)
            result[horizon] = ForecastQuality(
                count,
                len(selected) - count,
                sum(abs(e) for e in errors) / count if count else None,
                sum(errors) / count if count else None,
                max(abs(e) for e in errors) if count else None,
                sum(abs(e) for e in persistence_errors) / count if count else None,
            )
        return result

    def quality_history(self) -> dict[int, dict[int, ForecastQuality]]:
        """Retained historical revisions, never a pooled accuracy score."""
        versions = sorted({r.prediction.model_version for r in self.evaluation_history})
        return {version: self.quality(version) for version in versions}
