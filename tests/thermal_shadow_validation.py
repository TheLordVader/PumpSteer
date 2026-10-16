"""Finite independent-episode shadow validation; no runtime or live integration."""

import math
from datetime import datetime, timedelta, timezone

from custom_components.pumpsteer.thermal_episodes import (
    EpisodeTracker,
    episode_exposure,
)
from custom_components.pumpsteer.thermal_intelligence import (
    TemperatureMeasurement,
    ThermalDiagnostics,
)
from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer.thermal_reliability import (
    ForecastAssumptions,
    assess_forecast,
    make_episode_forecast,
)

MODES = (
    "normal",
    "preheating",
    "pre_braking",
    "braking",
    "brake_hold",
    "recovery",
    "precool",
)
VARIANTS = (
    "regular",
    "irregular",
    "sensor_failure",
    "revision",
    "transitions",
    "restart",
    "stale_origin",
)
HEAT = dict(
    normal=0.65,
    preheating=1.1,
    pre_braking=0.45,
    braking=0.07,
    brake_hold=0.18,
    recovery=0.95,
    precool=0.12,
)
HORIZONS = (30, 60, 120)


def case(number, dataset, index, *, special=None):
    building = ("slow", "medium", "fast")[(index // 7) % 3]
    mode = MODES[index % 7] if special is None else special
    return dict(
        id=number,
        dataset=dataset,
        building=building,
        k_true={"slow": 0.025, "medium": 0.055, "fast": 0.09}[building],
        season="winter" if (index // 21) % 2 == 0 else "shoulder",
        initial_mode=mode,
        variant=special
        or (
            "regular"
            if dataset == "training"
            else VARIANTS[(index // 7 + index % 7) % 7]
        ),
        heating_kind="unknown"
        if dataset != "training" and number % 11 == 0
        else ("known" if number % 3 == 0 else "estimated"),
        shift_kind=(
            "cold_front",
            "loss_change",
            "unobserved_heat_change",
            "solar_gain",
        )[index % 4]
        if dataset == "shift"
        else None,
    )


def mode_at(c, minute):
    if c["variant"] == "sensor_failure" and 59 <= minute <= 64:
        return "safe_mode"
    if c["variant"] == "transitions" and minute >= 20:
        return MODES[(MODES.index(c["initial_mode"]) + minute // 20) % len(MODES)]
    return c["initial_mode"]


def truth(c):
    """RK4 at one-minute steps; delivered heat is independent of outdoor input.

    Known synthetic effective heat capacity is implicit in C/h units. This is
    not a claim that real operating commands or Tout measure delivered power.
    """
    tin = 21.1 + 0.05 * (c["id"] % 4)
    readings = {}
    origin_out = (-8 if c["season"] == "winter" else 20) + 0.2 * math.sin(c["id"])
    for minute in range(-10, 123):
        shift = c["shift_kind"]
        tout = origin_out + (
            0.4 * minute / 60 + math.sin(minute / 60)
            if shift != "cold_front"
            else -5 * minute / 60
        )
        k = 0.18 if shift == "loss_change" else c["k_true"]
        mode = mode_at(c, minute)
        heat = HEAT.get(mode, 0.4)
        residual = (0.35 + 0.05 * (c["id"] % 4)) * math.exp(-max(0, minute) / 45)
        if shift == "unobserved_heat_change":
            heat = residual = 0.0
        elif shift == "solar_gain":
            heat += 3.0
        measured = tin + 0.015 * math.sin(1.9 * minute + c["id"] / 3)
        readings[minute] = (measured, tout)
        dt = 1 / 60

        def rate(value):
            return -k * (value - tout) + heat + residual

        a = rate(tin)
        b = rate(tin + dt * a / 2)
        d = rate(tin + dt * b / 2)
        e = rate(tin + dt * d)
        tin += dt * (a + 2 * b + 2 * d + e) / 6
    return readings


def run_case(c):
    """One original forecast origin only; later references never become samples."""
    readings = truth(c)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=c["id"])
    run_id = f"episode-{c['id']}"
    tracker = EpisodeTracker(run_id)
    engine = ThermalDiagnostics()
    original = ()
    bounds = {}
    rejections = {}
    outcomes = {}
    nominal = {}
    latest_version = 0
    origin_episode = None
    origin_rejection = None
    for minute in range(123):
        if c["variant"] == "irregular":
            if minute in (30, 60, 120):
                continue
            if minute not in (0, 31, 62, 122) and minute % 7 not in (0, 1, 3):
                continue
        now = start + timedelta(minutes=minute)
        if c["variant"] == "restart" and minute == 50:
            for p in original:
                if p.horizon_minutes not in outcomes:
                    outcomes[p.horizon_minutes] = (None, None)
            engine = ThermalDiagnostics()
            tracker = EpisodeTracker(run_id + "-restart")
        mode = mode_at(c, minute)
        sensor_ok = not (
            c["variant"] == "sensor_failure" and 30 <= minute <= 34
        ) and not (c["variant"] == "sensor_interruption" and minute == 0)
        source = "normal" if mode == "recovery" else mode
        kind = c["heating_kind"] if sensor_ok and mode != "safe_mode" else "unknown"
        episode = tracker.observe(
            now,
            source,
            heating_kind=kind,
            sensor_valid=sensor_ok,
            brake_releasing=mode == "recovery",
        )
        sample_minute = -6 if c["variant"] == "stale_origin" and minute == 0 else minute
        indoor = (
            TemperatureMeasurement(
                readings[sample_minute][0], start + timedelta(minutes=sample_minute)
            )
            if sensor_ok
            else None
        )
        outside_minute = minute // 6 * 6
        outdoor = TemperatureMeasurement(
            readings[outside_minute][1], start + timedelta(minutes=outside_minute)
        )
        if c["variant"] == "sensor_failure" and 59 <= minute <= 64:
            outdoor = None
        k = 0.05 + 0.0001 * (minute // 10) if c["variant"] == "revision" else 0.05
        issued = engine.observe(
            now=now,
            indoor=indoor,
            outdoor=outdoor,
            k_per_hour=k,
            model_fitted=True,
            operating_mode=source,
        )
        latest_version = max(latest_version, engine.model_version)
        if minute == 0:
            original = issued
            origin_episode = episode
            origin_rejection = engine.reason if not original else None
            if original:
                tin, tout = readings[0]
                model = ThermalModel()
                model.restore_k(0.05)
                nominal = {
                    "linear": {
                        h: tin - model.predict_drop(tin, tout, h) for h in HORIZONS
                    },
                    "recent_trend": {
                        h: tin + (tin - readings[-10][0]) * h / 10 for h in HORIZONS
                    },
                }
                # Fixed a priori assumptions, never chosen from future test traces.
                assumptions = ForecastAssumptions(
                    (0.02, 0.1),
                    (tout - 3, tout + 3),
                    (0.0, 1.8),
                    0.02,
                    start,
                    start + timedelta(minutes=120),
                    "fixed synthetic priors; not empirical guarantees",
                )
                for p in original:
                    bounds[p.horizon_minutes], rejections[p.horizon_minutes] = (
                        make_episode_forecast(p, episode, assumptions)
                    )
        for result in engine.evaluation_history:
            if (
                any(result.prediction is p for p in original)
                and result.prediction.horizon_minutes not in outcomes
            ):
                target = (
                    tracker.episode_at(result.actual_at)
                    if result.actual_at is not None
                    else None
                )
                outcomes[result.prediction.horizon_minutes] = (result, target)
    return dict(
        case=c,
        readings={h: readings[h] for h in (-10, 0, 30, 60, 120)},
        original={p.horizon_minutes: p for p in original},
        bounds=bounds,
        rejections=rejections,
        outcomes=outcomes,
        nominal=nominal,
        latest_version=latest_version,
        origin_episode=origin_episode,
        origin_rejection=origin_rejection,
    )


def calibrate(training):
    """Six independent episodes/cell; empirical extremes are not probabilities."""
    samples = {}
    for c in training:
        data = run_case(c)
        for h, (observation, _) in data["outcomes"].items():
            if observation is not None and observation.error_c is not None:
                samples.setdefault((c["initial_mode"], h), []).append(
                    observation.error_c
                )
    return {
        key: (sum(errors) / len(errors), min(errors), max(errors), len(errors))
        for key, errors in samples.items()
    }


def records(data, calibration):
    c = data["case"]
    rows = []
    for h in HORIZONS:
        p = data["original"].get(h)
        outcome, target = data["outcomes"].get(h, (None, None))
        actual = (
            outcome.actual_temperature_c
            if outcome is not None and outcome.status == "evaluated"
            else None
        )
        status = (
            outcome.status
            if outcome is not None
            else (
                "restart_censored"
                if p is not None and c["variant"] == "restart"
                else "not_issued"
            )
        )
        f = data["bounds"].get(h)
        assessment = (
            assess_forecast(f, outcome, target)
            if f is not None and outcome is not None
            else None
        )
        exposure = episode_exposure(
            data["origin_episode"], target, outcome.actual_at if outcome else None
        )
        methods = {
            "previous_reference": (p.temperature_c, None, None) if p else None,
            "linear": (data["nominal"]["linear"][h], None, None) if p else None,
            "recent_trend": (data["nominal"]["recent_trend"][h], None, None)
            if p
            else None,
            "bounded_heat_balance": (f.point_c, f.lower_c, f.upper_c) if f else None,
            "empirical_training_band": None,
        }
        cell = calibration.get((c["initial_mode"], h))
        if p is not None and cell is not None and p.k_per_hour == 0.05:
            offset, low, high, _ = cell
            methods["empirical_training_band"] = (
                p.temperature_c + offset,
                p.temperature_c + low,
                p.temperature_c + high,
            )
        for method, values in methods.items():
            point, low, high = values if values else (None, None, None)
            error = (
                actual - point if values is not None and actual is not None else None
            )
            on_target = (
                actual is not None and p is not None and outcome.actual_at == p.due_at
            )
            covered = low <= actual <= high if low is not None and on_target else None
            lower_breach = (
                max(0.0, low - actual) if low is not None and on_target else None
            )
            interval_status = (
                "not_applicable"
                if low is None
                else (
                    "no_observation"
                    if actual is None
                    else ("evaluated" if on_target else "off_target_time")
                )
            )
            if method == "bounded_heat_balance" and assessment is not None:
                covered, lower_breach, interval_status = (
                    assessment.covered,
                    assessment.lower_breach_c,
                    assessment.interval_status,
                )
            rejection = None
            if values is None:
                rejection = (
                    data["origin_rejection"]
                    if p is None
                    else (
                        data["rejections"].get(h)
                        if method == "bounded_heat_balance"
                        else "uncalibrated_regime"
                    )
                )
            rows.append(
                dict(
                    id=c["id"],
                    dataset=c["dataset"],
                    building=c["building"],
                    season=c["season"],
                    initial_mode=c["initial_mode"],
                    variant=c["variant"],
                    shift_kind=c["shift_kind"],
                    heating_kind=c["heating_kind"],
                    horizon=h,
                    method=method,
                    issued=values is not None,
                    status=status if values is not None else "rejected",
                    rejection=rejection,
                    error=error,
                    covered=covered,
                    lower_breach=lower_breach,
                    interval_status=interval_status,
                    width=high - low if low is not None else None,
                    exposure=exposure,
                    model_k=p.k_per_hour if p else None,
                    model_version=p.model_version if p else None,
                    latest_model_version=data["latest_version"],
                    lateness_seconds=outcome.lateness_seconds if outcome else None,
                    run_id=f"episode-{c['id']}",
                )
            )
    return rows


def summarize(rows, by=("dataset", "method", "horizon")):
    groups = {}
    for row in rows:
        groups.setdefault(tuple(row[name] for name in by), []).append(row)
    result = []
    for key, group in sorted(groups.items()):
        errors = [r["error"] for r in group if r["error"] is not None]
        intervals = [r for r in group if r["covered"] is not None]
        widths = [r["width"] for r in group if r["width"] is not None]
        issued = sum(r["issued"] for r in group)
        metric = dict(zip(by, key))
        metric.update(
            attempted=len(group),
            issued=issued,
            evaluated=len(errors),
            admission=issued / len(group),
            evaluation_coverage=len(errors) / issued if issued else None,
            mae_c=sum(abs(e) for e in errors) / len(errors) if errors else None,
            bias_c=sum(errors) / len(errors) if errors else None,
            worst_absolute_error_c=max(abs(e) for e in errors) if errors else None,
            mean_cooling_underestimation_c=sum(max(0.0, -e) for e in errors)
            / len(errors)
            if errors
            else None,
            worst_cooling_underestimation_c=max(max(0.0, -e) for e in errors)
            if errors
            else None,
            uncertainty_evaluated=len(intervals),
            interval_timing_coverage=len(intervals)
            / sum(r["error"] is not None and r["width"] is not None for r in group)
            if any(r["error"] is not None and r["width"] is not None for r in group)
            else None,
            uncertainty_coverage=sum(r["covered"] for r in intervals) / len(intervals)
            if intervals
            else None,
            worst_lower_breach_c=max(r["lower_breach"] for r in intervals)
            if intervals
            else None,
            mean_interval_width_c=sum(widths) / len(widths) if widths else None,
        )
        result.append(metric)
    return result


def benchmark():
    training = [case(i, "training", i) for i in range(42)]
    holdout = [case(i + 42, "holdout", i) for i in range(42)]
    holdout += [
        case(84, "holdout", 0, special="safe_mode"),
        case(85, "holdout", 0, special="sensor_interruption"),
    ]
    shifted = [case(i + 86, "shift", i) for i in range(21)]
    calibration = calibrate(training)
    before = dict(calibration)
    all_rows = [
        row for c in holdout + shifted for row in records(run_case(c), calibration)
    ]
    paired = {
        (r["id"], r["horizon"])
        for r in all_rows
        if r["method"] == "bounded_heat_balance" and r["issued"]
    }
    return dict(
        training_ids=[c["id"] for c in training],
        holdout_ids=[c["id"] for c in holdout],
        shift_ids=[c["id"] for c in shifted],
        calibration_frozen_before_holdout=calibration == before,
        calibration={
            f"{mode}:{h}": values for (mode, h), values in calibration.items()
        },
        summary=summarize(all_rows),
        paired_summary=summarize(
            [r for r in all_rows if (r["id"], r["horizon"]) in paired]
        ),
        conditions=summarize(
            all_rows,
            by=(
                "dataset",
                "method",
                "horizon",
                "initial_mode",
                "heating_kind",
                "variant",
                "building",
                "season",
                "exposure",
            ),
        ),
        records=all_rows,
    )
