"""Dead time from heat-up responses (cross-check / fallback for ``d``).

Approach borrowed from Ecronika/ha-preheat (MIT License, Copyright (c) 2025 Ecronika),
https://github.com/Ecronika/ha-preheat — re-implemented here on the model grid.

After every off→on heating edge followed by at least 15 valid points, the steepest
10-sample OLS slope is located; its tangent is intersected with the temperature at the
edge. The dead time is the time from the edge to the intersection. Responses with a
max slope below 0.005 K/min are rejected, and results outside 0–480 min are dropped.
The median over edges is returned.
"""

from __future__ import annotations

from dataclasses import dataclass

from .grid import Grid

MIN_POINTS_AFTER_EDGE = 15
SLOPE_WINDOW = 10
MIN_SLOPE_K_PER_MIN = 0.005
MAX_DEAD_TIME_MIN = 480.0
ON_EFFORT = 0.5


@dataclass
class DeadTimeEstimate:
    minutes: float
    edges: int
    values: list[float]


def _slope(ts: list[float], ys: list[float]) -> float:
    n = len(ts)
    mt = sum(ts) / n
    my = sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx <= 0:
        return 0.0
    return sum((t - mt) * (y - my) for t, y in zip(ts, ys, strict=True)) / sxx


def edge_dead_time(grid: Grid, k_edge: int, max_points: int = 60) -> float | None:
    """Dead time (minutes) for the edge at row ``k_edge`` (heating on from that row)."""
    step = grid.step_minutes
    pts_t: list[float] = []
    pts_y: list[float] = []
    for k in range(k_edge, min(len(grid), k_edge + max_points)):
        e = grid.effort[k]
        v = grid.indoor[k]
        if e is None or e < ON_EFFORT or v is None or grid.excluded[k]:
            break
        pts_t.append((k - k_edge) * step)
        pts_y.append(v)
    if len(pts_t) < MIN_POINTS_AFTER_EDGE:
        return None
    best_s = 0.0
    best_i = -1
    for i in range(len(pts_t) - SLOPE_WINDOW + 1):
        s = _slope(pts_t[i : i + SLOPE_WINDOW], pts_y[i : i + SLOPE_WINDOW])
        if s > best_s:
            best_s, best_i = s, i
    if best_i < 0 or best_s < MIN_SLOPE_K_PER_MIN:
        return None
    win_t = pts_t[best_i : best_i + SLOPE_WINDOW]
    win_y = pts_y[best_i : best_i + SLOPE_WINDOW]
    tm = sum(win_t) / SLOPE_WINDOW
    ym = sum(win_y) / SLOPE_WINDOW
    base = pts_y[0]
    t_int = tm - (ym - base) / best_s
    if 0.0 <= t_int <= MAX_DEAD_TIME_MIN:
        return t_int
    if -step < t_int < 0.0:
        return 0.0
    return None


def tangent_dead_time(grid: Grid, min_off_rows: int = 3) -> DeadTimeEstimate | None:
    """Median dead time over all off→on edges (after ≥ ``min_off_rows`` rows off)."""
    vals: list[float] = []
    eff = grid.effort
    for k in range(min_off_rows, len(grid)):
        e = eff[k]
        if e is None or e < ON_EFFORT:
            continue
        prev = eff[k - min_off_rows : k]
        if any(p is None or p >= ON_EFFORT for p in prev):
            continue
        dt = edge_dead_time(grid, k)
        if dt is not None:
            vals.append(dt)
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    med = s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
    return DeadTimeEstimate(med, n, [round(v, 1) for v in vals])
