# thermal_model.py

"""
Empirical thermal model for PumpSteer.

This module estimates the house cooling rate constant k
(°C/h per °C temperature delta) from samples collected during
braking periods, when reduced heating demand makes thermal loss
easier to observe.

In PumpSteer 2.2.x, the model collects cooling samples during braking
and may fit its cooling constant k after a completed brake phase when
enough valid samples have accumulated.

The fitted model is still diagnostic with respect to control decisions.
It does NOT directly control brake/pre-brake/preheat decisions.

The model can estimate expected indoor temperature drop during
reduced-heating periods, but these prediction helpers are not yet part
of the active control path.
"""

from __future__ import annotations

import logging
import math
from statistics import median
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Optional

_LOGGER = logging.getLogger(__name__)

# Minimum braking samples before the model is considered valid.
_MIN_SAMPLES = 20

# Fallback k if insufficient braking history exists.
# 0.05 °C/h per °C delta means a house at 21°C inside and -4°C outside
# (delta = 25°C) cools at about 1.25°C/h. This is conservative but plausible
# for a Swedish wood-frame house.
_FALLBACK_K = 0.05

# Physically plausible range for k.
# < 0.005: unrealistically low heat loss
# > 0.5: unrealistically high heat loss
_K_MIN = 0.005
_K_MAX = 0.5

# Bounded temperature history used to compute dT/dt. The observer supplies
# the endpoints of a qualified, independent observation interval.
_TEMP_BUFFER_SIZE = 10

# Minimum history span before rate is considered reliable.
# A few minutes gives a more stable estimate than a near-instant sample.
_MIN_RATE_WINDOW_MINUTES = 3.0

# Tolerate ordinary polling jitter, but never estimate a continuous cooling
# rate across a long outage or a known invalid observation.
_MAX_TEMP_GAP_MINUTES = 15.0
# Only the passive observer may explicitly qualify a longer interval.
_MAX_QUALIFIED_INTERVAL_SECONDS = 120 * 60

# Plausible bounds for measured indoor temperature rate (°C/h).
# These are intentionally conservative and only reject obvious spikes/noise.
_MIN_RATE_C_PER_HOUR = -5.0
_MAX_RATE_C_PER_HOUR = 1.0


@dataclass
class ThermalSample:
    """One data point collected during a braking period."""

    indoor_temp: float
    outdoor_temp: float
    rate: float  # °C/h, negative when indoor temperature is falling
    rate_uncertainty: float = (
        0.0  # Empirical report-step allowance, not a confidence bound.
    )
    interval_hours: float = 0.0


@dataclass(frozen=True)
class ResponseFit:
    """Local empirical net response, NOT delivered power or physical heat loss."""
    coefficients: tuple[float, float, float]
    delta_range: tuple[float, float]
    command_range: tuple[float, float]
    noise_c_per_hour: float
    sample_count: int
    revision: int
    modes: tuple[str, ...]

    def supports(self, indoor, outdoor, command, mode):
        return (
            mode in self.modes
            and self.delta_range[0]-1 <= outdoor-indoor <= self.delta_range[1]+1
            and self.command_range[0] <= command <= self.command_range[1]
        )

    def rate(self, indoor, outdoor, command):
        a,b,c=self.coefficients
        return a+b*(outdoor-indoor)+c*command

    def project(self, indoor, command, now, weather_path, mode, outdoor_now=None, bounds=None):
        """Exact affine propagation at frozen command through as-of weather slots."""
        a,b,c=self.coefficients
        value=indoor
        for end,outdoor in weather_path:
            hours=(end-now).total_seconds()/3600
            future_command=command if outdoor_now is None else command+outdoor_now-outdoor
            if not 0<hours<=2 or not self.supports(value,outdoor,future_command,mode):
                return None
            equilibrium=outdoor+(a+c*future_command)/b
            value=equilibrium+(value-equilibrium)*math.exp(-b*hours)
            if (not self.supports(value,outdoor,future_command,mode)
                or (bounds is not None and not bounds[0]<=value<=bounds[1])):
                return None
            now=end
        return value

    def uncertainty(self, hours):
        # Empirical allowance, not a calibrated probability or physical guarantee.
        return .1+max(.05,3*self.noise_c_per_hour)*hours


def _response_regression(samples):
    """Three-parameter weighted least squares, fail closed on dependent inputs."""
    means=[sum(s[j]*s[3]**2 for s in samples)/sum(s[3]**2 for s in samples) for j in (0,1)]
    scales=[max(s[j] for s in samples)-min(s[j] for s in samples) for j in (0,1)]
    if min(scales)<2:
        return None
    matrix=[[0.]*4 for _ in range(3)]
    for delta,command,rate,hours,_ in samples:
        x=(1.,(delta-means[0])/scales[0],(command-means[1])/scales[1])
        for i in range(3):
            for j in range(3):
                matrix[i][j]+=hours**2*x[i]*x[j]
            matrix[i][3]+=hours**2*x[i]*rate
    # Reject nearly collinear closed-loop observations too: a formal nonzero
    # determinant alone does not identify independent weather/command response.
    if matrix[1][1]*matrix[2][2]<=0 or 1-matrix[1][2]**2/(matrix[1][1]*matrix[2][2])<.05:
        return None
    for col in range(3):
        pivot=max(range(col,3),key=lambda i:abs(matrix[i][col]))
        matrix[col],matrix[pivot]=matrix[pivot],matrix[col]
        if abs(matrix[col][col])<1e-6:
            return None
        divisor=matrix[col][col]
        matrix[col]=[v/divisor for v in matrix[col]]
        for row in range(3):
            if row!=col:
                amount=matrix[row][col]
                matrix[row]=[a-amount*b for a,b in zip(matrix[row],matrix[col])]
    b,c=matrix[1][3]/scales[0],matrix[2][3]/scales[1]
    return matrix[0][3]-b*means[0]-c*means[1],b,c


class ThermalModel:
    """
    Estimate house cooling rate from braking-period samples.

    Simplified cooling model:
        dT_indoor/dt = -k * (indoor - outdoor)

    k is estimated by linear regression over samples collected
    during braking periods.

    In PumpSteer 2.2.x, fitting is active after qualifying brake phases:
    - fitted values and sample counts are exposed for diagnostics
    - fitted k may be restored across Home Assistant restarts
    - it does NOT directly drive the state machine

    Persistence:
    k can be restored from saved sensor state so the model survives
    Home Assistant restarts without depending on Recorder-based history.
    """

    def __init__(self) -> None:
        self._response_samples = deque(maxlen=240)
        self.response = None
        self._response_revision = self._response_since_fit = self.response_matches = 0
        self.response_reason = "insufficient_response_intervals"
        self._k: float = _FALLBACK_K
        self._valid: bool = False
        self._sample_count: int = 0
        self.rate_noise: Optional[float] = None

        # Braking samples accumulated since the last fit cycle.
        self._samples: Deque[ThermalSample] = deque(maxlen=240)

        # Ring buffer of (timestamp, indoor_temp) used to estimate cooling rate.
        self._temp_history: Deque[tuple[datetime, float]] = deque(
            maxlen=_TEMP_BUFFER_SIZE
        )

    # ── Public properties ──────────────────────────────────────────────────────

    @property
    def k(self) -> float:
        """Estimated cooling rate constant in °C/h per °C delta."""
        return self._k

    @property
    def is_valid(self) -> bool:
        """True if k was fitted from collected data instead of fallback."""
        return self._valid

    @property
    def sample_count(self) -> int:
        """Number of samples used in the most recent successful fit."""
        return self._sample_count

    @property
    def pending_samples(self) -> int:
        """Number of collected samples waiting for the next fit."""
        return len(self._samples)

    # ── Persistence ────────────────────────────────────────────────────────────

    def restore_k(self, k: float, sample_count: int = 0) -> None:
        """
        Restore k from previously saved state.

        Intended to be called once during startup before the first
        update cycle.
        """
        if (
            isinstance(k, (int, float))
            and not isinstance(k, bool)
            and math.isfinite(k)
            and _K_MIN < k < _K_MAX
        ):
            self._k = float(k)
            self.rate_noise = (
                None  # Restored parameters are not fresh calibration evidence.
            )
            self._valid = True
            self._sample_count = (
                sample_count if type(sample_count) is int and sample_count >= 0 else 0
            )
            _LOGGER.debug("ThermalModel: restored k=%.4f from state", k)
        else:
            _LOGGER.debug("ThermalModel: invalid restored coefficient ignored")

    def clear_history(self, *, samples: bool = False) -> None:
        """Break observation continuity without discarding a valid fitted k."""
        self._temp_history.clear()
        if samples:
            self._samples.clear()

    # ── Sample collection ──────────────────────────────────────────────────────

    def record_temp(
        self,
        now: datetime,
        indoor_temp: Optional[float],
        *,
        qualified_interval: bool = False,
    ) -> None:
        """
        Record indoor temperature for later cooling-rate estimation.

        The observer supplies qualified window endpoints. A missing reading
        breaks the rate history instead of inventing a temperature.
        """
        if indoor_temp is None or not math.isfinite(indoor_temp):
            self._temp_history.clear()
            return
        if self._temp_history:
            elapsed_seconds = now.timestamp() - self._temp_history[-1][0].timestamp()
            limit = (
                _MAX_QUALIFIED_INTERVAL_SECONDS
                if qualified_interval
                else _MAX_TEMP_GAP_MINUTES * 60
            )
            if elapsed_seconds <= 0.0 or elapsed_seconds > limit:
                self._temp_history.clear()
        self._temp_history.append((now, indoor_temp))

    def collect_braking_sample(
        self,
        indoor_temp: float,
        outdoor_temp: float,
        *,
        rate_uncertainty: float = 0.0,
        reset_samples: bool = False,
    ) -> bool:
        """
        Collect one sample during braking periods.

        Uses the recent indoor temperature history to estimate current
        cooling rate and stores the result for later fitting.

        Samples are skipped when:
        - the history window is too short
        - the indoor/outdoor temperature delta is too small to be meaningful
        - the measured rate exceeds the empirical report-step allowance
        - the measured rate is clearly implausible
        """
        rate = self._compute_rate()
        if rate is None:
            return False

        # Keep quantized flat/slightly rising windows only within the observed
        # report-step allowance, so coarse reporting does not bias the fit.
        if rate >= 0.0 and (rate_uncertainty <= 0 or rate > rate_uncertainty):
            return False

        # Reject obvious spikes/noise before they contaminate the fit.
        if rate < _MIN_RATE_C_PER_HOUR or rate > _MAX_RATE_C_PER_HOUR:
            return False

        delta_t = indoor_temp - outdoor_temp
        if abs(delta_t) < 2.0:
            # Too small a delta — measurement noise dominates.
            return False

        # Change the calibration cohort only when this window is admitted.
        # A rejected/unfinished ramp must not erase earlier qualified samples.
        if reset_samples:
            self._samples.clear()
        self._samples.append(
            ThermalSample(
                indoor_temp=indoor_temp,
                outdoor_temp=outdoor_temp,
                rate=rate,
                interval_hours=(
                    self._temp_history[-1][0] - self._temp_history[0][0]
                ).total_seconds()
                / 3600,
                rate_uncertainty=rate_uncertainty,
            )
        )

        return True

    def _compute_rate(self) -> Optional[float]:
        """
        Compute indoor temperature rate of change in °C/h.

        Returns None if:
        - there are fewer than two samples
        - the history span is too short to be reliable
        """
        if len(self._temp_history) < 2:
            return None

        t_old, temp_old = self._temp_history[0]
        t_now, temp_now = self._temp_history[-1]

        dt_hours = (t_now.timestamp() - t_old.timestamp()) / 3600.0
        if dt_hours < (_MIN_RATE_WINDOW_MINUTES / 60.0):
            # Too little history — the estimate becomes noisy and unstable.
            return None

        return (temp_now - temp_old) / dt_hours

    # ── Model fitting ──────────────────────────────────────────────────────────

    def fit(self) -> None:
        """
        Fit k from accumulated braking samples.

        Uses ordinary least squares on the simplified model:
            rate = -k * delta_T
            k = -sum(rate * delta_T) / sum(delta_T²)

        In PumpSteer 2.2.x, fitting improves diagnostics and observability.
        It does not by itself activate any control behavior.
        """
        if len(self._samples) < _MIN_SAMPLES:
            _LOGGER.debug(
                "ThermalModel: only %d braking samples, keeping k=%.4f",
                len(self._samples),
                self._k,
            )
            return

        # Robust admission before the existing OLS fit. This limits minority
        # heat/noise contamination; it cannot identify a uniformly heated batch.
        candidates = [
            (sample, -sample.rate / (sample.indoor_temp - sample.outdoor_temp))
            for sample in self._samples
            if sample.indoor_temp - sample.outdoor_temp >= 2.0
        ]
        candidates = [
            (s, k)
            for s, k in candidates
            if k + s.rate_uncertainty / (s.indoor_temp - s.outdoor_temp) > _K_MIN
            and k - s.rate_uncertainty / (s.indoor_temp - s.outdoor_temp) < _K_MAX
        ]
        if len(candidates) < _MIN_SAMPLES:
            return
        center = median(k for _, k in candidates)
        mad = median(abs(k - center) for _, k in candidates)
        tolerance = max(0.003, 0.15 * center, 3 * 1.4826 * mad)
        retained = [
            sample
            for sample, k in candidates
            if abs(k - center)
            <= max(
                tolerance,
                sample.rate_uncertainty / (sample.indoor_temp - sample.outdoor_temp),
            )
        ]
        if len(retained) < _MIN_SAMPLES:
            return

        sum_xy = 0.0
        sum_xx = 0.0
        for sample in retained:
            delta_t = sample.indoor_temp - sample.outdoor_temp
            sum_xy += sample.rate * delta_t
            sum_xx += delta_t**2

        if sum_xx < 1e-6:
            _LOGGER.debug("ThermalModel: degenerate data, skipping fit")
            return

        k = -sum_xy / sum_xx

        if _K_MIN < k < _K_MAX:
            self._k = k
            self._valid = True
            self._sample_count = len(retained)
            residuals = [
                s.rate + k * (s.indoor_temp - s.outdoor_temp) for s in retained
            ]
            residual_center = median(residuals)
            self.rate_noise = 1.4826 * median(
                abs(r - residual_center) for r in residuals
            )
            _LOGGER.debug(
                "ThermalModel: fitted k=%.4f from %d samples",
                k,
                self._sample_count,
            )
        else:
            _LOGGER.warning(
                "ThermalModel: fitted k=%.4f outside [%.3f, %.3f], keeping k=%.4f",
                k,
                _K_MIN,
                _K_MAX,
                self._k,
            )

        # Clear samples after fit so a new learning window can begin.
        self._samples.clear()

    @property
    def response_pending_samples(self):
        return len(self._response_samples)

    def reset_response(self, *, samples=True, discard_fit=False):
        self.response_matches=0
        if samples:
            self._response_samples.clear()
            self._response_since_fit=0
        if discard_fit:
            self.response=None
            self.response_reason="temperature_definition_changed"

    def collect_response_sample(self, delta, command, rate, hours, mode):
        if (any(type(v) not in (float,int) or not math.isfinite(v) for v in (delta,command,rate,hours))
            or not -5<=rate<=3 or not 1/6<=hours<=2 or not -60<=command<=60
            or not -100<=delta<=100 or mode not in ("normal","braking","brake_hold","preheating","recovery","pre_braking","holiday")):
            return False
        self._response_samples.append((delta,command,rate,hours,mode))
        self._response_since_fit+=1
        return True

    def fit_response(self):
        self._response_since_fit=0
        samples=list(self._response_samples)
        if len(samples)<20:
            self.response_reason="insufficient_response_intervals"
            return False
        coefficients=_response_regression(samples)
        if coefficients is None:
            self.response_reason="insufficient_independent_variation"
            return False
        residuals=[s[2]-sum(a*b for a,b in zip(coefficients,(1,s[0],s[1]))) for s in samples]
        center=median(residuals)
        spread=max(.10,3*1.4826*median(abs(v-center) for v in residuals))
        retained=[s for s,e in zip(samples,residuals) if abs(e-center)<=max(spread,.1/s[3])]
        coefficients=_response_regression(retained) if len(retained)>=20 else None
        if coefficients is None or not (.005<coefficients[1]<.5 and 0<=coefficients[2]<=1 and abs(coefficients[0])<=5):
            self.response_reason="unidentified_or_implausible_response"
            return False
        errors=[s[2]-sum(a*b for a,b in zip(coefficients,(1,s[0],s[1]))) for s in retained]
        noise=math.sqrt(sum(e*e for e in errors)/len(errors))
        if noise>.5:
            self.response_reason="unstable_response"
            return False
        self._response_revision+=1
        self.response=ResponseFit(coefficients,
            (min(s[0] for s in retained),max(s[0] for s in retained)),
            (min(s[1] for s in retained),max(s[1] for s in retained)),noise,len(retained),
            self._response_revision,tuple(sorted({s[4] for s in retained if sum(t[4]==s[4] for t in retained)>=5})))
        self.response_matches=0
        self._response_since_fit=0
        self.response_reason="unconfirmed_response"
        return True

    def response_state(self):
        from dataclasses import asdict
        return asdict(self.response) if self.response else None

    def restore_response(self, saved):
        try:
            if not isinstance(saved,dict):
                return False
            if (len(saved.get('coefficients',()))!=3 or len(saved.get('delta_range',()))!=2
                or len(saved.get('command_range',()))!=2):
                return False
            a,b,c=saved['coefficients']
            values=(*saved['coefficients'],*saved['delta_range'],*saved['command_range'],saved['noise_c_per_hour'])
            if (any(type(v) not in (int,float) or not math.isfinite(v) for v in values)
                or not (.005<b<.5 and 0<=c<=1 and abs(a)<=5 and 0<=saved['noise_c_per_hour']<=.5)
                or type(saved['sample_count']) is not int or saved['sample_count']<20
                or type(saved['revision']) is not int or saved['revision']<1
                or any(saved[r][0]>saved[r][1] for r in ('delta_range','command_range'))
                or not saved['modes'] or any(m not in ('normal','braking','brake_hold','preheating','recovery','pre_braking','holiday') for m in saved['modes'])):
                return False
            self.response=ResponseFit(tuple(saved['coefficients']),tuple(saved['delta_range']),
                tuple(saved['command_range']),saved['noise_c_per_hour'],saved['sample_count'],saved['revision'],tuple(saved['modes']))
            self._response_revision=self.response.revision
            self.reset_response()
            self.response_reason="restored_response_unconfirmed"
            return True
        except (KeyError,TypeError,ValueError):
            return False

    # ── Prediction helpers (diagnostic / future-facing) ───────────────────────

    def predict_drop(
        self,
        indoor: float,
        outdoor: float,
        duration_minutes: float,
    ) -> float:
        """
        Estimate indoor temperature drop over a reduced-heating period.

        Returns the predicted drop in degrees Celsius as a positive number.

        This remains a diagnostic/future-facing helper and is not part of
        the active state-machine decision path.
        """
        delta_t = max(0.0, indoor - outdoor)
        return self._k * delta_t * (duration_minutes / 60.0)

    def brake_is_safe(
        self,
        indoor: float,
        outdoor: float,
        brake_duration_minutes: float,
        comfort_floor: float,
    ) -> bool:
        """
        Estimate whether indoor temperature would remain above comfort_floor.

        This helper uses predict_drop() and returns True when the predicted
        end temperature remains at or above the comfort floor.

        This remains diagnostic/future-facing only and does not directly
        gate brake/pre-brake decisions.
        """
        drop = self.predict_drop(indoor, outdoor, brake_duration_minutes)
        predicted = indoor - drop
        safe = predicted >= comfort_floor
        _LOGGER.debug(
            "ThermalModel: safe=%s drop=%.2f°C end=%.2f°C floor=%.2f°C k=%.4f",
            safe,
            drop,
            predicted,
            comfort_floor,
            self._k,
        )
        return safe
