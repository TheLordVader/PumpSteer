"""Finite paired replay, hidden heat truth withheld from both observers.

Run with --baseline-dir containing the e1a74402 model/observer source files.
No HA service, database, live data, network, tuning on holdout, or controller model.
"""

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import ha_test_stubs  # noqa: F401 -- offline boundary only
from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer.thermal_observer import ThermalObserver

START = datetime(2026, 10, 18, tzinfo=timezone.utc)


def load_reference(directory):
    classes = []
    for filename, class_name in (
        ("thermal_model.py", "ThermalModel"),
        ("thermal_observer.py", "ThermalObserver"),
    ):
        name = "custom_components.pumpsteer._reference_" + filename[:-3]
        spec = importlib.util.spec_from_file_location(name, directory / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        classes.append(getattr(module, class_name))
    return classes[1](classes[0]())


def record(value, at):
    return SimpleNamespace(
        state=str(value), last_reported=at, attributes={"unit_of_measurement": "°C"}
    )


def heat_at(episode, phase, scenario):
    if scenario == "intermittent_heat" and episode % 3 == 2 and 15 <= phase < 65:
        return 1.2
    if scenario == "after_baseline_heat" and episode % 3 == 2 and 45 <= phase < 85:
        return 1.2
    if scenario == "weak_heat" and episode % 3 == 2 and 15 <= phase < 65:
        return 0.15
    if scenario == "persistent_heat" and phase < 90:
        return 0.35
    return 0.0


def trace(k, scenario, *, brake_minutes=90, recovery_minutes=60):
    """Chronological RK4 one-minute truth; model sees only reports and mode.

    Twenty training episodes precede ten fixed holdout episodes. Default brakes
    last 90 minutes, then recovery 60 minutes. Durations can be varied for
    sparse-report tests without changing the physical integration. Delivered ordinary heat is a simulator
    assumption, not derived from PumpSteer commands or claimed real COP/power.
    """
    tin = 21.5
    for minute in range(30 * (brake_minutes + recovery_minutes)):
        episode, phase = divmod(minute, brake_minutes + recovery_minutes)
        tout = 5 + 0.3 * math.sin(minute / 500)
        braking = phase < brake_minutes
        heat = heat_at(episode, phase, scenario)
        noise = 0.015 * math.sin(minute * 1.31)
        if scenario == "sensor_bias" and episode % 3 == 2 and 15 <= phase < 65:
            noise += 0.5 * min((phase - 15) / 20, 1)
        if scenario == "late_sensor_bias" and episode % 3 == 2 and 45 <= phase < 85:
            noise += 0.5 * min((phase - 45) / 20, 1)
        if scenario == "quantized":
            observed = round(tin, 1)
        else:
            observed = tin + noise
        yield minute, observed, tout, braking, heat
        ordinary = 0 if braking else max(0, k * (21.5 - tout) + 2 * (21.5 - tin))
        supplied = ordinary + heat

        def rate(t):
            return -k * (t - tout) + supplied

        step = 1 / 60
        a = rate(tin)
        b = rate(tin + step * a / 2)
        c = rate(tin + step * b / 2)
        d = rate(tin + step * c)
        tin += step * (a + 2 * b + 2 * c + d) / 6


def replay(k, scenario, baseline_dir):
    observers = {
        "incumbent": load_reference(baseline_dir),
        "candidate": ThermalObserver(ThermalModel()),
    }
    metrics = {
        name: {
            "errors": {"clean": [], "heated": []},
            "persistence_errors": {"clean": [], "heated": []},
            "forecast_pairs": [],
            "detector_windows": [],
            "windows": {
                "tp": 0,
                "fp": 0,
                "tn": 0,
                "fn": 0,
                "abstained_with_heat": 0,
                "abstained_without_heat": 0,
            },
            "training_k": None,
            "final_k": None,
        }
        for name in observers
    }
    seen_windows = set()
    seen_records = {name: set() for name in observers}
    truth = {}
    previous_braking = False
    for minute, tin, tout, braking, heat in trace(k, scenario):
        at = START + timedelta(minutes=minute)
        truth[minute] = heat
        for name, obs in observers.items():
            if previous_braking and not braking:
                obs.model.fit()
            if minute == 20 * 150:
                metrics[name]["training_k"] = (
                    obs.model.k if obs.model.is_valid else None
                )
            kwargs = {} if name == "candidate" else {"external_heat": False}
            obs.observe(
                at, record(tin, at), record(tout, at), braking=braking, **kwargs
            )
            for comparison in obs.diagnostics.evaluation_history:
                p = comparison.prediction
                key = (p.issued_at, p.horizon_minutes)
                if key in seen_records[name]:
                    continue
                seen_records[name].add(key)
                if (
                    p.issued_at < START + timedelta(minutes=3000)
                    or p.horizon_minutes != 30
                ):
                    continue
                if comparison.error_c is not None:
                    lo = int((p.origin_at - START).total_seconds() / 60)
                    hi = int((comparison.actual_at - START).total_seconds() / 60)
                    group = (
                        "heated"
                        if any(truth.get(t, 0) > 0 for t in range(lo, hi + 1))
                        else "clean"
                    )
                    persistence_error = (
                        comparison.actual_temperature_c - p.indoor_temperature_c
                    )
                    metrics[name]["errors"][group].append(comparison.error_c)
                    metrics[name]["persistence_errors"][group].append(persistence_error)
                    metrics[name]["forecast_pairs"].append(
                        {
                            "issued_at": p.issued_at.isoformat(),
                            "actual_at": comparison.actual_at.isoformat(),
                            "horizon_minutes": p.horizon_minutes,
                            "model_version": p.model_version,
                            "k": p.k_per_hour,
                            "group": group,
                            "error_c": comparison.error_c,
                            "persistence_error_c": persistence_error,
                        }
                    )
            if name == "candidate":
                evidence = obs.attributes()["heat_evidence"]
                window = evidence["last_window"]
                if window and window["ended_at"] not in seen_windows:
                    seen_windows.add(window["ended_at"])
                    lo = int(
                        (
                            datetime.fromisoformat(window["started_at"]) - START
                        ).total_seconds()
                        / 60
                    )
                    hi = int(
                        (
                            datetime.fromisoformat(window["ended_at"]) - START
                        ).total_seconds()
                        / 60
                    )
                    if lo >= 3000:
                        actual = any(truth.get(t, 0) > 0 for t in range(lo, hi))
                        indicated = evidence["status"] == "suspected_extra_heat"
                        qualified = (
                            evidence.get("baseline_confirmed", False)
                            and evidence["status"] != "context_changed"
                        )
                        cell = (
                            "tp"
                            if actual and indicated
                            else "fn"
                            if actual
                            else "fp"
                            if indicated
                            else "tn"
                        )
                        if not qualified:
                            cell = (
                                "abstained_with_heat"
                                if actual
                                else "abstained_without_heat"
                            )
                        metrics[name]["windows"][cell] += 1
                        metrics[name]["detector_windows"].append(
                            {
                                "episode": lo // 150,
                                "started_at": window["started_at"],
                                "ended_at": window["ended_at"],
                                "actual_extra_heat": actual,
                                "qualified_reference": qualified,
                                "indicated": indicated,
                                "status": evidence["status"],
                                "confidence": evidence["confidence"],
                                "residual_c": window["residual_c"],
                                "threshold_c": window["threshold_c"],
                            }
                        )
        previous_braking = braking
    for name, obs in observers.items():
        result = metrics[name]
        result["final_k"] = obs.model.k if obs.model.is_valid else None
        result["fit_samples"] = obs.model.sample_count
        for group, errors in result["errors"].items():
            result["errors"][group] = {
                "count": len(errors),
                "mae_c": sum(abs(e) for e in errors) / len(errors) if errors else None,
                "bias_c": sum(errors) / len(errors) if errors else None,
                "persistence_mae_c": sum(
                    abs(e) for e in result["persistence_errors"][group]
                )
                / len(errors)
                if errors
                else None,
            }
    return {
        "true_k": k,
        "scenario": scenario,
        **metrics,
    }


def benchmark(baseline_dir, *, confirmation=False):
    # The second coefficient set is reserved for the final frozen-code replay.
    coefficients = (0.035, 0.075) if confirmation else (0.025, 0.055)
    return [
        replay(k, scenario, baseline_dir)
        for k in coefficients
        for scenario in (
            "clean",
            "intermittent_heat",
            "after_baseline_heat",
            "weak_heat",
            "persistent_heat",
            "sensor_bias",
            "late_sensor_bias",
            "quantized",
        )
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--confirmation", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            benchmark(args.baseline_dir, confirmation=args.confirmation), indent=2
        )
    )
