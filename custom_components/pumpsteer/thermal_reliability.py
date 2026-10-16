"""Conditional heat-balance bounds and explicit cooling-risk assessment.

Ranges are supplied assumptions over the whole horizon, not fitted probabilities.
No controller, integration, registry, persistence or service is installed here.
"""

import math
from dataclasses import dataclass
from datetime import datetime

from .thermal_episodes import ThermalEpisode, episode_exposure, utc_time
from .thermal_intelligence import (
    _MATCH_TOLERANCE_SECONDS,
    ThermalComparison,
    ThermalPrediction,
)


@dataclass(frozen=True)
class ForecastAssumptions:
    k_bounds: tuple[float, float]
    outdoor_bounds: tuple[float, float]
    heat_bounds: tuple[
        float, float
    ]  # total supplied/residual/other heat in effective C/h
    sensor_error_c: float
    valid_from: datetime
    valid_until: datetime
    source: str


@dataclass(frozen=True)
class EpisodeForecast:
    reference: ThermalPrediction
    episode: ThermalEpisode
    assumptions: ForecastAssumptions
    point_c: float
    lower_c: float
    upper_c: float
    uncertainty_kind: str = "conditional_assumption_bounds"
    model_family: str = "bounded_heat_balance_v1"


@dataclass(frozen=True)
class ForecastAssessment:
    forecast: EpisodeForecast
    observation: ThermalComparison
    target_episode: ThermalEpisode | None
    exposure: str
    error_c: float | None
    cooling_underestimation_c: float | None
    covered: bool | None
    lower_breach_c: float | None
    interval_status: str
    interval_kind: str | None
    interval_at: datetime | None
    interval_lower_c: float | None
    interval_upper_c: float | None


def _range(value: tuple[float, float], low: float, high: float) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 2
        and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
            for x in value
        )
        and low <= value[0] <= value[1] <= high
    )


def _response(tin: float, tout: float, k: float, heat: float, hours: float) -> float:
    gain = -math.expm1(-k * hours)
    return tin + (tout - tin) * gain + heat * (gain / k)


def _extreme(
    tin: float, tout: float, k_below: float, k_above: float, heat: float, hours: float
) -> float:
    # Comparison ODE: switch extreme k when crossing Tout; nonnegative heat can
    # cross upward only once. This also handles shoulder-season warming, where
    # assuming "larger k is always colder" would produce incorrect bounds.
    if tin >= tout:
        return _response(tin, tout, k_above, heat, hours)
    if heat == 0:
        return _response(tin, tout, k_below, heat, hours)
    crossing = math.log1p(k_below * (tout - tin) / heat) / k_below
    if hours <= crossing:
        return _response(tin, tout, k_below, heat, hours)
    return _response(tout, tout, k_above, heat, hours - crossing)


def _interval_at(
    reference: ThermalPrediction, assumptions: ForecastAssumptions, at: datetime
) -> tuple[float, float]:
    """Only issuance-frozen inputs and target time; never an observed outcome."""
    kl, kh = assumptions.k_bounds
    tl, th = assumptions.outdoor_bounds
    ql, qh = assumptions.heat_bounds
    error = assumptions.sensor_error_c
    hours = (at - reference.origin_at).total_seconds() / 3600
    lower = (
        _extreme(reference.indoor_temperature_c - error, tl, kl, kh, ql, hours) - error
    )
    upper = (
        _extreme(reference.indoor_temperature_c + error, th, kh, kl, qh, hours) + error
    )
    return lower, upper


def make_episode_forecast(
    reference: ThermalPrediction,
    episode: ThermalEpisode | None,
    assumptions: ForecastAssumptions,
) -> tuple[EpisodeForecast | None, str]:
    """Refuse missing provenance/bounds. No zero heat or weather bounds invented."""
    if episode is None or episode.mode in (
        "unknown",
        "safe_mode",
        "sensor_interruption",
    ):
        return None, "unreliable_episode"
    if episode.heating_kind == "unknown":
        return None, "unknown_heating"
    if (
        episode.source_mode != reference.operating_mode
        or not episode.contains(reference.origin_at)
        or not episode.contains(reference.issued_at)
    ):
        return None, "episode_does_not_cover_origin"
    start, end = utc_time(assumptions.valid_from), utc_time(assumptions.valid_until)
    valid_ranges = (
        _range(assumptions.k_bounds, 0.005, 0.5)
        and 0.005 < assumptions.k_bounds[0]
        and assumptions.k_bounds[1] < 0.5
        and _range(assumptions.outdoor_bounds, -100.0, 100.0)
        and _range(assumptions.heat_bounds, 0.0, 10.0)
    )
    error = assumptions.sensor_error_c
    if (
        not valid_ranges
        or not isinstance(error, (int, float))
        or isinstance(error, bool)
        or not math.isfinite(error)
        or not 0 <= error <= 5
    ):
        return None, "invalid_bounds"
    if (
        not isinstance(assumptions.source, str)
        or not assumptions.source
        or len(assumptions.source) > 80
    ):
        return None, "missing_assumption_provenance"
    if (
        start is None
        or end is None
        or start > min(reference.origin_at, reference.outdoor_at)
        or end < reference.due_at
    ):
        return None, "insufficient_time_coverage"
    kl, kh = assumptions.k_bounds
    tl, th = assumptions.outdoor_bounds
    ql, qh = assumptions.heat_bounds
    if (
        not kl <= reference.k_per_hour <= kh
        or not tl <= reference.outdoor_temperature_c <= th
    ):
        return None, "source_outside_assumed_bounds"
    hours = (reference.due_at - reference.origin_at).total_seconds() / 3600
    lower, upper = _interval_at(reference, assumptions, reference.due_at)
    point = _response(
        reference.indoor_temperature_c,
        reference.outdoor_temperature_c,
        reference.k_per_hour,
        ql,
        hours,
    )
    frozen_assumptions = ForecastAssumptions(
        assumptions.k_bounds,
        assumptions.outdoor_bounds,
        assumptions.heat_bounds,
        error,
        start,
        end,
        assumptions.source,
    )
    return EpisodeForecast(
        reference, episode, frozen_assumptions, point, lower, upper
    ), ""


def assess_forecast(
    forecast: EpisodeForecast,
    observation: ThermalComparison,
    target_episode: ThermalEpisode | None = None,
) -> ForecastAssessment:
    """Keep nominal point error; assess bounds separately at authentic sample time.

    Late samples need frozen assumptions covering the full source-to-sample span.
    Exact-target and time-adjusted intervals must be reported separately.
    """
    if observation.prediction is not forecast.reference:
        raise ValueError(
            "Observation belongs to a different original forecast instance"
        )
    actual = (
        observation.actual_temperature_c if observation.status == "evaluated" else None
    )
    exposure = episode_exposure(forecast.episode, target_episode, observation.actual_at)
    error = actual - forecast.point_c if actual is not None else None
    at = utc_time(observation.actual_at)
    interval_kind = interval_at = lower = upper = None
    if actual is None:
        interval_status = "no_observation"
    elif at is None:
        interval_status = "invalid_observation_time"
    elif (
        not 0
        <= (at - forecast.reference.due_at).total_seconds()
        <= _MATCH_TOLERANCE_SECONDS
    ):
        interval_status = "outside_match_tolerance"
    elif forecast.assumptions.valid_until < at:
        interval_status = "insufficient_time_coverage"
    else:
        interval_status = "evaluated"
        interval_at = at
        if at == forecast.reference.due_at:
            interval_kind = "exact_target"
            lower, upper = forecast.lower_c, forecast.upper_c
        else:
            interval_kind = "time_adjusted"
            lower, upper = _interval_at(forecast.reference, forecast.assumptions, at)
    return ForecastAssessment(
        forecast,
        observation,
        target_episode,
        exposure,
        error,
        max(0.0, -error) if error is not None else None,
        lower <= actual <= upper if lower is not None else None,
        max(0.0, lower - actual) if lower is not None else None,
        interval_status,
        interval_kind,
        interval_at,
        lower,
        upper,
    )
