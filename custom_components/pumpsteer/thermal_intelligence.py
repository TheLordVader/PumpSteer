"""Bounded, diagnostic-only passive references and future measurement checks.

No Home Assistant, control, persistence or I/O hooks are installed here. Supplied
temperatures must be real measurements with explicit physical observation times.
Unknown heat input prevents these references from certifying comfort or safety.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

HORIZONS_MINUTES: Final = (30, 60, 120)
_MAX_PENDING: Final = 24
_MAX_ERRORS_PER_HORIZON: Final = 64
_MAX_SAMPLE_AGE_SECONDS: Final = 300
_MAX_GAP_SECONDS: Final = 300
_MATCH_TOLERANCE_SECONDS: Final = 120
_ISSUE_INTERVAL_SECONDS: Final = 1800
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
class ThermalPrediction:
    """Conditional no-heat-input reference, with explicit assumptions."""

    horizon_minutes: int
    issued_at: datetime
    origin_at: datetime
    due_at: datetime
    temperature_c: float
    k_per_hour: float
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ThermalComparison:
    prediction: ThermalPrediction
    actual_at: datetime
    actual_temperature_c: float
    error_c: float  # actual minus reference
    lateness_seconds: float


@dataclass(frozen=True)
class ForecastQuality:
    """Descriptive errors for the current model cohort, not confidence scores."""

    count: int
    missed_count: int
    mae_c: float | None
    bias_c: float | None
    max_absolute_error_c: float | None


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
    """Observe explicit snapshots; never feed results back to the controller.

    All state is in memory and bounded. Restarts intentionally start empty.
    A gap invalidates pending comparisons but preserves completed quality data.
    A changed coefficient/fitted tag starts a new quality cohort.
    """

    def __init__(self) -> None:
        self.status = "insufficient_data"
        self.reason = "no_measurement"
        self.predictions: tuple[ThermalPrediction, ...] = ()
        self.last_comparisons: tuple[ThermalComparison, ...] = ()
        self._pending: deque[ThermalPrediction] = deque(maxlen=_MAX_PENDING)
        self._errors: dict[int, deque[float]] = {
            h: deque(maxlen=_MAX_ERRORS_PER_HORIZON) for h in HORIZONS_MINUTES
        }
        self._missed = dict.fromkeys(HORIZONS_MINUTES, 0)
        self._model: tuple[float, bool] | None = None
        self._last_now: datetime | None = None
        self._last_measurement: datetime | None = None
        self._last_outdoor_measurement: datetime | None = None
        self._last_issue: datetime | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _invalidate(self, reason: str) -> None:
        for prediction in self._pending:
            self._missed[prediction.horizon_minutes] += 1
        self._pending.clear()
        self.predictions = ()
        self.last_comparisons = ()
        self._last_measurement = None
        self._last_outdoor_measurement = None
        self._last_issue = None
        self.status = "insufficient_data"
        self.reason = reason

    def observe(
        self,
        *,
        now: datetime,
        indoor: TemperatureMeasurement | None,
        outdoor: TemperatureMeasurement | None,
        k_per_hour: float | None,
        model_fitted: bool = False,
    ) -> tuple[ThermalPrediction, ...]:
        """Return newly issued references; score only distinct future readings."""
        self.last_comparisons = ()
        clock = _utc(now)
        if clock is None:
            self._last_now = None
            self._invalidate("invalid_clock")
            return ()
        if self._last_now is not None and clock < self._last_now:
            self._last_now = clock
            self._invalidate("clock_reversal")
            return ()
        self._last_now = clock

        tin, at, reason = _reading(indoor, "indoor", clock)
        tout, outdoor_at, outdoor_reason = _reading(outdoor, "outdoor", clock)
        if reason or outdoor_reason:
            self._invalidate(reason or outdoor_reason)
            return ()
        if not _number_between(k_per_hour, 0.005, 0.5) or k_per_hour in (0.005, 0.5):
            self._invalidate("invalid_model")
            return ()

        # Validated inputs above are non-optional from here onward.
        assert tin is not None and tout is not None and at is not None
        assert outdoor_at is not None and k_per_hour is not None
        model = (float(k_per_hour), model_fitted is True)
        if model != self._model:
            self._invalidate("model_changed")
            self._model = model
            for errors in self._errors.values():
                errors.clear()
            self._missed = dict.fromkeys(HORIZONS_MINUTES, 0)

        gap = False
        for observed, previous in (
            (at, self._last_measurement),
            (outdoor_at, self._last_outdoor_measurement),
        ):
            if previous is None:
                continue
            elapsed = (observed - previous).total_seconds()
            if elapsed < 0:
                self._invalidate("out_of_order_measurement")
                return ()
            if elapsed > _MAX_GAP_SECONDS:
                gap = True
        if gap:
            self._invalidate("measurement_gap")
        elif at == self._last_measurement:
            # A duplicate indoor reading can still carry a new outdoor sample.
            self._last_outdoor_measurement = outdoor_at
            self.reason = "duplicate_measurement"
            return ()
        self._last_measurement = at
        self._last_outdoor_measurement = outdoor_at

        comparisons = []
        retained: deque[ThermalPrediction] = deque(maxlen=_MAX_PENDING)
        for prediction in self._pending:
            lateness = (at - prediction.due_at).total_seconds()
            if lateness < 0:
                retained.append(prediction)
            elif lateness <= _MATCH_TOLERANCE_SECONDS:
                error = tin - prediction.temperature_c
                self._errors[prediction.horizon_minutes].append(error)
                comparisons.append(
                    ThermalComparison(prediction, at, tin, error, lateness)
                )
            else:
                self._missed[prediction.horizon_minutes] += 1
        self._pending = retained
        self.last_comparisons = tuple(comparisons)
        self.status = "conditional"
        self.reason = "measurement_gap" if gap else "cadence_wait"

        if (
            self._last_issue is not None
            and (clock - self._last_issue).total_seconds() < _ISSUE_INTERVAL_SECONDS
        ):
            return ()

        warnings = _WARNINGS + (() if model[1] else ("coefficient_not_validated",))
        age_hours = (clock - at).total_seconds() / 3600
        if age_hours > 0:
            warnings += ("measurement_age_extrapolated",)
        issued = []
        for horizon in HORIZONS_MINUTES:
            decay = math.exp(-model[0] * (age_hours + horizon / 60))
            temperature = tout + (tin - tout) * decay
            prediction = ThermalPrediction(
                horizon_minutes=horizon,
                issued_at=clock,
                origin_at=at,
                due_at=clock + timedelta(minutes=horizon),
                temperature_c=temperature,
                k_per_hour=model[0],
                warnings=warnings,
            )
            if len(self._pending) == _MAX_PENDING:
                dropped = self._pending.popleft()
                self._missed[dropped.horizon_minutes] += 1
            self._pending.append(prediction)
            issued.append(prediction)
        self.predictions = tuple(issued)
        self._last_issue = clock
        if not gap:
            self.reason = "forecast_issued"
        return self.predictions

    def quality(self) -> dict[int, ForecastQuality]:
        """Return a bounded rolling summary; no samples means unknown error."""
        result = {}
        for horizon, errors in self._errors.items():
            count = len(errors)
            result[horizon] = ForecastQuality(
                count,
                self._missed[horizon],
                sum(abs(e) for e in errors) / count if count else None,
                sum(errors) / count if count else None,
                max(abs(e) for e in errors) if count else None,
            )
        return result
