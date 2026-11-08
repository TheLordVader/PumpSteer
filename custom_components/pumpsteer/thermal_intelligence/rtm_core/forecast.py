"""Forecast-averaged outdoor temperature and duration solve.

Trapezoidal averaging and the fixed-point duration solve follow the approach of
Ecronika/ha-preheat (MIT License, Copyright (c) 2025 Ecronika).

- :func:`mean_outdoor`: time-weighted mean of a piecewise-linear outdoor curve over
  ``[start, end]``. The curve goes through the forecast points; outside them it is
  clamped to the first/last value; the current reading (if any) anchors ``start``.
- :func:`solve_duration`: the heating duration depends on the mean outdoor temperature
  over the duration itself. Fixed-point iteration (≤ 3 iterations), then bisection on
  ``g(D) = f(mean_outdoor(D)) − D`` when it has not converged.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime

from .base import ForecastPoint
from .grid import ts_of

MAX_MINUTES = 48 * 60.0


def outdoor_curve(
    points: Sequence[ForecastPoint], start: datetime, current: float | None = None
) -> list[tuple[float, float]]:
    """Sorted (epoch s, °C) breakpoints; ``current`` replaces anything at/before start."""
    t0 = ts_of(start)
    pts = sorted((ts_of(p.ts), float(p.outdoor_c)) for p in points)
    if current is not None:
        pts = [(t0, float(current))] + [(t, v) for t, v in pts if t > t0]
    return pts


def _interp(pts: list[tuple[float, float]], t: float) -> float:
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    lo, hi = 0, len(pts) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if pts[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (ta, va), (tb, vb) = pts[lo], pts[hi]
    if tb == ta:
        return vb
    return va + (vb - va) * (t - ta) / (tb - ta)


def mean_over(pts: list[tuple[float, float]], t_start: float, t_end: float) -> float | None:
    """Trapezoidal time-weighted mean of the clamped piecewise-linear curve."""
    if not pts:
        return None
    if t_end <= t_start:
        return _interp(pts, t_start)
    knots = [t_start] + [t for t, _ in pts if t_start < t < t_end] + [t_end]
    vals = [_interp(pts, t) for t in knots]
    area = 0.0
    for i in range(len(knots) - 1):
        area += 0.5 * (vals[i] + vals[i + 1]) * (knots[i + 1] - knots[i])
    return area / (t_end - t_start)


def mean_outdoor(
    points: Sequence[ForecastPoint],
    start: datetime,
    end: datetime,
    current: float | None = None,
) -> float | None:
    """Mean outdoor temperature over ``[start, end]`` (None without any data)."""
    pts = outdoor_curve(points, start, current)
    return mean_over(pts, ts_of(start), ts_of(end))


def solve_duration(
    minutes_for: Callable[[float], float | None],
    outdoor_for: Callable[[float], float],
    max_iter: int = 3,
    tol_minutes: float = 0.5,
    max_minutes: float = MAX_MINUTES,
) -> tuple[float | None, float, dict[str, object]]:
    """Solve ``D = minutes_for(outdoor_for(D))``.

    ``minutes_for(tout)`` returns the duration at constant outdoor ``tout`` (None when
    unreachable); ``outdoor_for(D)`` the mean outdoor over the next ``D`` minutes.
    Returns (minutes or None, effective outdoor, info).
    """
    tout = outdoor_for(0.0)
    d = minutes_for(tout)
    info: dict[str, object] = {"iterations": 0, "method": "fixed_point"}
    if d is None:
        info["method"] = "unreachable_now"
        return None, tout, info
    for i in range(max_iter):
        tout_n = outdoor_for(d)
        d_n = minutes_for(tout_n)
        info["iterations"] = i + 1
        if d_n is None:
            break
        if abs(d_n - d) < tol_minutes:
            return d_n, tout_n, info
        d = d_n

    # bisection on g(D) = f(mean(D)) − D ; g(0) = f(tout_now) ≥ 0
    def g(x: float) -> float:
        val = minutes_for(outdoor_for(x))
        return float("inf") if val is None else val - x

    info["method"] = "bisection"
    lo = 0.0
    hi = max(d, 60.0)
    while g(hi) > 0:
        lo = hi
        hi *= 2.0
        if hi > max_minutes:
            info["method"] = "unreachable_horizon"
            return None, outdoor_for(max_minutes), info
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if g(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.05:
            break
    d = 0.5 * (lo + hi)
    return d, outdoor_for(d), info
