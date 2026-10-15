"""Finite synthetic, chronological shadow experiment; no runtime integration.

Nine empirical residual offsets, not physical heat-loss/radiator parameters.
Only a fixed supplied reference coefficient is calibrated. All inputs are
synthetic independent episodes; no files, hardware or production state are used.
"""

import math
from datetime import datetime, timedelta, timezone

from custom_components.pumpsteer.thermal_intelligence import (
    HORIZONS_MINUTES,
    TemperatureMeasurement,
    ThermalDiagnostics,
)
from custom_components.pumpsteer.thermal_model import ThermalModel

MODES = ("braking", "normal", "preheating")
REFERENCE_K = 0.05


def simulate_episode(number, mode, *, shifted=False):
    """One independent episode with known heat loss, residual heat and inputs.

    One-minute Euler integration of dTin/dt=-0.04*(Tin-Tout)+heat+residual.
    Store only two past/origin points and the three future targets. Deterministic
    bounded sensor noise is not an IID uncertainty model. Future inputs are
    withheld from every predictor, including the adaptive correction.
    """
    tin = 21.15 + 0.1 * (number % 4)
    readings = {}
    for minute in range(-10, 121):
        tout = 2 + 1.5 * (number % 3)
        tout += (-3 if shifted else 0.7) * minute / 60
        tout += 0.4 * math.sin(minute / 30 + number)
        noise = 0.015 * math.sin(1.7 * minute + 0.9 * number)
        if minute in (-10, 0, 30, 60, 120):
            readings[minute] = (tin + noise, tout)
        if minute < 0:
            heat = 0.65
        elif shifted:
            heat = 0.0
        elif mode == "braking":
            heat = 0.08 if minute < 60 else 0.9
        elif mode == "preheating":
            heat = 1.25 if minute < 60 else 0.45
        else:
            heat = 0.65 if minute < 90 else 0.25
        residual = (
            0
            if shifted
            else (0.35 + 0.08 * (number % 4)) * math.exp(-max(0, minute) / 40)
        )
        tin += (-0.04 * (tin - tout) + heat + residual) / 60
    return {
        "id": number,
        "mode": mode,
        "k": REFERENCE_K,
        "readings": readings,
        "missing": (60,) if number % 7 == 0 else (),
    }


def forecasts(episode):
    """Use real existing linear helper, new reference, and only a past trend."""
    tin, tout = episode["readings"][0]
    now = datetime(2026, 10, 15, tzinfo=timezone.utc) + timedelta(days=episode["id"])
    model = ThermalModel()  # experiment-local object; never an integration model
    model.restore_k(episode["k"])
    references = ThermalDiagnostics().observe(
        now=now,
        indoor=TemperatureMeasurement(tin, now),
        outdoor=TemperatureMeasurement(tout, now),
        k_per_hour=episode["k"],
        operating_mode=episode["mode"],
    )
    recent_change = (tin - episode["readings"][-10][0]) / 10
    return {
        "linear": {h: tin - model.predict_drop(tin, tout, h) for h in HORIZONS_MINUTES},
        "reference": {p.horizon_minutes: p.temperature_c for p in references},
        "recent_trend": {h: tin + recent_change * h for h in HORIZONS_MINUTES},
    }


def fit_bias(episodes):
    """At least two whole episodes per mode/horizon; maximum twelve episodes.

    This learns observational forecast bias, never claims physical identifiability.
    Refuse incompatible reference coefficients rather than silently pooling them.
    """
    if len(episodes) > 12 or len({e["id"] for e in episodes}) != len(episodes):
        raise ValueError("Need at most twelve distinct independent episodes")
    if any(e["k"] != REFERENCE_K for e in episodes):
        raise ValueError("Calibration requires the fixed reference coefficient")
    residuals = {}
    for episode in episodes:
        reference = forecasts(episode)["reference"]
        for h in HORIZONS_MINUTES:
            if h not in episode["missing"]:
                error = episode["readings"][h][0] - reference[h]
                residuals.setdefault((episode["mode"], h), []).append(error)
    return {
        key: sum(values) / len(values)
        for key, values in residuals.items()
        if len(values) >= 2
    }


def evaluate(episodes, correction):
    """Paired resolved targets, one origin per episode; no fitting on test data."""
    groups = {}
    for episode in episodes:
        if episode["k"] != REFERENCE_K:
            raise ValueError(
                "Do not transfer a correction to a revised reference model"
            )
        predictions = forecasts(episode)
        predictions["adaptive_bias"] = {
            h: predictions["reference"][h] + correction[(episode["mode"], h)]
            for h in HORIZONS_MINUTES
            if (episode["mode"], h) in correction
        }
        for method, values in predictions.items():
            for h, predicted in values.items():
                key = (method, episode["mode"], h)
                eligible, errors = groups.setdefault(key, [0, []])
                groups[key][0] = eligible + 1
                if h not in episode["missing"]:
                    errors.append(episode["readings"][h][0] - predicted)
    return [
        {
            "method": method,
            "mode": mode,
            "horizon_minutes": h,
            "eligible_count": eligible,
            "count": len(errors),
            "coverage": len(errors) / eligible,
            "mae_c": sum(abs(e) for e in errors) / len(errors) if errors else None,
            "bias_c": sum(errors) / len(errors) if errors else None,
        }
        for (method, mode, h), (eligible, errors) in sorted(groups.items())
    ]


def benchmark():
    """Train once on days 0–11; freeze nine offsets; test unused days 12–26.

    Regular validation has similar input distributions but independent episodes.
    Shifted validation removes heat/residual input and introduces a cold front.
    Report all conditions/horizons separately; no significance/confidence claims.
    """
    training = [simulate_episode(i, MODES[i % 3]) for i in range(12)]
    correction = fit_bias(training)
    validation = [simulate_episode(i, MODES[i % 3]) for i in range(12, 24)]
    shifted = [simulate_episode(i, MODES[i % 3], shifted=True) for i in range(24, 27)]
    return {
        "reference_k_per_hour": REFERENCE_K,
        "ground_truth_k_per_hour": 0.04,
        "training_ids": [e["id"] for e in training],
        "validation_ids": [e["id"] for e in validation],
        "shifted_validation_ids": [e["id"] for e in shifted],
        "calibration_offsets_c": {
            f"{mode}:{h}": value for (mode, h), value in correction.items()
        },
        "validation": evaluate(validation, correction),
        "shifted_validation": evaluate(shifted, correction),
    }
