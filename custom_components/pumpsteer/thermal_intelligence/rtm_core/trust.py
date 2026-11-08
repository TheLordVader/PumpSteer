"""Fit trust: is a fitted model good enough to publish a preheat estimate?

The fit always returns *something*: with thin or uninformative data a parameter is
projected onto its bound, left at the prior, or the loss rate is not identified at all
(a time constant pinned at 100 h puts the maximum reachable temperature above 100 C).
Publishing minutes from such a fit is worse than publishing nothing, so this module
turns the fit and its quality report into a verdict: ``trusted`` plus machine readable
``reasons``. It is pure and cheap; the coordinator withholds the estimate when the fit
is not trusted (unless the user opted in to publishing anyway).

Checks (reason codes) and thresholds
------------------------------------
``loss_rate_at_bound``
    ``b`` within 5 % of ``B_MAX`` (a time constant under about 0.5 h): the optimum lay
    outside the range, so the data did not identify the loss rate. The slow end is NOT
    flagged: a well insulated house really does lose heat over days (a time constant of
    100 h or more), the loss is then negligible over a preheat horizon of hours, and a fit
    that is pinned there still predicts well when it passes the skill check.
``heating_rate_low``
    ``a`` below 0.5 K/h. Typical radiators give 1 to 4 K/h; this also covers a pinned
    ``A_MIN`` floor. At this rate the estimate is dominated by noise.
``heating_rate_unlearnt``
    ``a`` within 0.1 K/h of the prior (3 K/h) while fewer than ``MIN_HEATING_HOURS``
    of heating were seen: the value is the starting point, not a measurement.
``dead_time_at_bound``
    Dead time 0 (the first candidate: no real radiator and valve respond instantly) or
    within one step of the configured maximum (the search ran off the end).
``max_reachable_implausible``
    The lift above outdoor at full effort, ``(a + c) / b``, exceeds
    ``MAX_LIFT_FACTOR * (target - DESIGN_OUTDOOR_C)``. A heating system is sized to hold
    the target on a design-cold day (about -3 C in the UK); allowing it to be three times
    as strong as that is generous, and anything beyond means the loss rate is
    under-estimated. At a 20.5 C target the limit is 70 K. Skipped when the time constant
    is a day or more (``SLOW_LOSS_PER_H``): ``(a + c) / b`` then says how hot the room would
    get with unlimited heat, which a radiator cannot give, so for a well insulated house
    the ratio is large by construction and tells nothing. Being below the target is not
    flagged: that is the legitimate ``unreachable``.
``too_little_heating``
    Fewer than ``MIN_HEATING_HOURS`` (10 h, half of what maturity counts as full) of
    usable heating at effort >= 0.5.
``not_evaluated``
    No holdout evaluation (not enough data), so there is no evidence of skill.
``no_skill``
    The holdout contained heat-up episodes yet the 60 minute forecast RMSE is not at
    least ``MIN_SKILL`` (3 %) better than persistence. Idle holdouts are not penalised.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fit import A_MIN, B_MAX, Prior

BOUND_MARGIN = 0.05
MIN_HEATING_RATE = 0.5
PRIOR_HEATING_TOLERANCE = 0.1
MIN_HEATING_HOURS = 10.0
DESIGN_OUTDOOR_C = -3.0
MAX_LIFT_FACTOR = 3.0
SLOW_LOSS_PER_H = 1.0 / 24.0  # a time constant of a day or more: a well insulated room
MIN_SKILL = 0.03

REASON_LOSS_RATE_AT_BOUND = "loss_rate_at_bound"
REASON_HEATING_RATE_LOW = "heating_rate_low"
REASON_HEATING_RATE_UNLEARNT = "heating_rate_unlearnt"
REASON_DEAD_TIME_AT_BOUND = "dead_time_at_bound"
REASON_MAX_REACHABLE = "max_reachable_implausible"
REASON_TOO_LITTLE_HEATING = "too_little_heating"
REASON_NOT_EVALUATED = "not_evaluated"
REASON_NO_SKILL = "no_skill"


@dataclass(frozen=True)
class FitFacts:
    """What the verdict is based on (all known without a target)."""

    a: float
    b: float
    c: float
    dead_time_minutes: float
    max_dead_time_minutes: float
    step_minutes: float
    heating_hours: float
    rmse: float | None
    baseline_rmse: float | None
    episodes: int


@dataclass(frozen=True)
class FitTrust:
    trusted: bool
    reasons: tuple[str, ...] = ()


def max_lift_k(target_c: float) -> float:
    """Largest believable full-effort lift above outdoor for a room held at ``target_c``."""
    return MAX_LIFT_FACTOR * max(target_c - DESIGN_OUTDOOR_C, 0.0)


def assess_fit(facts: FitFacts, target_c: float) -> FitTrust:
    """Verdict for one fit at one target (only the max reachable check uses the target)."""
    reasons: list[str] = []
    f = facts
    slow_loss = f.b <= SLOW_LOSS_PER_H  # well insulated: the lift ratio below means nothing
    if f.b >= B_MAX * (1.0 - BOUND_MARGIN):
        reasons.append(REASON_LOSS_RATE_AT_BOUND)
    if f.a < max(MIN_HEATING_RATE, A_MIN):
        reasons.append(REASON_HEATING_RATE_LOW)
    if f.heating_hours < MIN_HEATING_HOURS and abs(f.a - Prior().a) <= PRIOR_HEATING_TOLERANCE:
        reasons.append(REASON_HEATING_RATE_UNLEARNT)
    if f.dead_time_minutes <= 0 or f.dead_time_minutes >= f.max_dead_time_minutes - f.step_minutes:
        reasons.append(REASON_DEAD_TIME_AT_BOUND)
    if not slow_loss and f.b > 0 and (f.a + f.c) / f.b > max_lift_k(target_c):
        reasons.append(REASON_MAX_REACHABLE)
    if f.heating_hours < MIN_HEATING_HOURS:
        reasons.append(REASON_TOO_LITTLE_HEATING)
    if f.rmse is None or f.baseline_rmse is None or f.baseline_rmse <= 0:
        reasons.append(REASON_NOT_EVALUATED)
    elif f.episodes > 0 and f.rmse > (1.0 - MIN_SKILL) * f.baseline_rmse:
        reasons.append(REASON_NO_SKILL)
    return FitTrust(not reasons, tuple(reasons))
