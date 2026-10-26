"""Finite offline Recorder replay; no network, HA instance or control writes.

Input: bounded Recorder export with original updated/reported times, attributes,
controller rows and run boundaries. Raw history stays outside the repository.
"""

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import math
from pathlib import Path
from statistics import median
import sys
from types import SimpleNamespace

import ha_test_stubs  # noqa: F401 -- offline boundary
from custom_components.pumpsteer.thermal_intelligence import (
    ThermalDiagnostics,
)
from custom_components.pumpsteer.thermal_assist import assess_assistance
from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer.thermal_observer import ThermalObserver
from thermal_self_learning_validation import load_reference

WEIGHTED = "sensor.indoor_weighted"
REDUNDANT = "sensor.outdoor_selected"
RAW = "sensor.indoor_primary"
OUTDOOR = "sensor.outdoor_primary"
CONTROL = "sensor.pumpsteer"


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc)


def latest_before(rows, timestamp):
    """Original state known at this instant; never interpolate or backfill."""
    index = bisect_right(rows, timestamp, key=lambda row: row["updated"]) - 1
    return rows[index] if index >= 0 else None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def state(row):
    if row and row.get("quarantined"):
        return None
    return (
        SimpleNamespace(
            state=row["state"],
            last_reported=utc(row["reported"]) if row["reported"] is not None else None,
            attributes=row["attributes"],
        )
        if row
        else None
    )


def summarize_forecasts(records):
    result = {}
    for horizon in (30, 60, 120):
        selected = [r for r in records if r["horizon"] == horizon]
        errors = [r["error_c"] for r in selected if r["error_c"] is not None]
        result[str(horizon)] = {
            "resolved": len(selected),
            "evaluated": len(errors),
            "coverage": len(errors) / len(selected) if selected else None,
            "mae_c": sum(map(abs, errors)) / len(errors) if errors else None,
            "bias_c": sum(errors) / len(errors) if errors else None,
            "outcomes": dict(Counter(r["status"] for r in selected)),
        }
    return result


def source_summary(rows):
    numeric = [r for r in rows if number(r["state"]) is not None]
    gaps = [
        (b["reported"] - a["reported"]) / 60
        for a, b in zip(rows, rows[1:])
        if a["reported"] is not None and b["reported"] is not None
    ]
    gaps = [g for g in gaps if g > 0]
    steps = [
        abs(number(b["state"]) - number(a["state"]))
        for a, b in zip(rows, rows[1:])
        if number(a["state"]) is not None and number(b["state"]) is not None
    ]
    steps = [s for s in steps if s > 1e-6]
    return {
        "rows": len(rows),
        "numeric": len(numeric),
        "first": utc(rows[0]["updated"]).isoformat() if rows else None,
        "last": utc(rows[-1]["updated"]).isoformat() if rows else None,
        "median_recorded_gap_minutes": median(gaps) if gaps else None,
        "gaps_over_15min": sum(g > 15 for g in gaps),
        "smallest_nonzero_recorded_step_c": min(steps) if steps else None,
    }


def episode_summary(rows, start):
    episodes, active, last = [], None, None

    def finish(end, next_mode):
        a = number(active["attributes"].get("indoor_temperature"))
        b = number(last["attributes"].get("indoor_temperature"))
        episodes.append(
            {
                "start": utc(active["updated"]).isoformat(),
                "end": utc(end["updated"]).isoformat(),
                "minutes": (end["updated"] - active["updated"]) / 60,
                "controller_indoor_change_c": b - a
                if a is not None and b is not None
                else None,
                "last_braking_report_at": utc(last["updated"]).isoformat(),
                "next_mode": next_mode,
            }
        )

    for row in rows:
        if row["updated"] < start:
            continue
        attrs = row["attributes"]
        brake = (
            attrs.get("mode") in ("braking", "brake_hold")
            and (number(attrs.get("brake_factor")) or 0) > 0
        )
        if (
            active is not None
            and last is not None
            and row["updated"] - last["updated"] > 300
        ):
            finish(last, "observation_gap")
            active = None
        if brake and active is None:
            active = row
        elif not brake and active is not None:
            finish(row, attrs.get("mode"))
            active = None
        last = row
    if active is not None:
        finish(last, "right_censored")
    return episodes


def load_installed(path):
    name = "_installed_thermal_history_reference"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.ThermalModel()


def capture_records(diagnostics, records, run):
    for comparison in diagnostics.evaluation_history:
        p = comparison.prediction
        records[(run, p.issued_at.isoformat(), p.horizon_minutes)] = {
            "horizon": p.horizon_minutes,
            "error_c": comparison.error_c,
            "status": comparison.status,
            "revision": [run, p.model_version],
            "k": p.k_per_hour,
        }


def finish_segment(diagnostics, records, run, now, reason):
    """Resolve pending targets before discarding a lifecycle segment."""
    diagnostics.cancel_pending(now, reason)
    capture_records(diagnostics, records, run)


def fit_boundary(attributes, was_braking):
    """Replay the existing learner flag, separate from diagnostic qualification.

    The export's control-context check confirms every recorded non-null
    aggressiveness was 3; explicit zero is still handled for other exports.
    Rounded factor can hide a tiny remainder; compare observed fit markers too.
    """
    mode = attributes.get("mode")
    if (
        mode in (None, "safe_mode", "summer_mode")
        or attributes.get("aggressiveness") == 0
    ):
        return False, False
    if mode == "braking":
        return True, False
    if mode in ("normal", "holiday") and attributes.get("brake_factor") == 0:
        return False, was_braking
    return was_braking, False


def replay(data, indoor_id, outdoor_id, reference_dir, installed_path, possible):
    indoor_rows, outdoor_rows = data["series"][indoor_id], data["series"][outdoor_id]
    start = max(indoor_rows[0]["updated"], outdoor_rows[0]["updated"])
    observers = {
        "e1": load_reference(reference_dir),
        "candidate": ThermalObserver(ThermalModel()),
    }
    legacy = load_installed(installed_path)
    legacy_diag = ThermalDiagnostics()
    # Same reports and controller timestamps. Legacy alone intentionally repeats
    # reads for training, matching its installed call pattern, not authentic reports.
    diagnostics = {
        **{name: obs.diagnostics for name, obs in observers.items()},
        "installed": legacy_diag,
    }
    records = {name: {} for name in diagnostics}
    fits = {name: [] for name in diagnostics}
    reasons = {name: Counter() for name in observers}
    windows, seen_windows = [], set()
    was_braking, previous_run = False, None
    legacy_was_braking = False
    run_starts = [
        datetime.fromisoformat(r[1]).replace(tzinfo=timezone.utc).timestamp()
        for r in data["runs"]
    ]
    ticks = 0
    shadow_reasons, effective_ticks = Counter(), 0
    for row in data["series"][CONTROL]:
        stamp = row["updated"]
        if stamp < start:
            continue
        now = utc(stamp)
        run = bisect_right(run_starts, stamp) - 1
        if run != previous_run:
            if previous_run is not None:
                for name, diag in diagnostics.items():
                    finish_segment(diag, records[name], previous_run, now, "restart")
            for name, obs in observers.items():
                obs.start(utc(run_starts[run]) if run >= 0 else now)
                diagnostics[name] = obs.diagnostics
            legacy._temp_history.clear()
            legacy._samples.clear()
            legacy_diag = ThermalDiagnostics()
            diagnostics["installed"] = legacy_diag
            was_braking = False
            legacy_was_braking = False
            previous_run = run
        indoor, outdoor = (
            latest_before(indoor_rows, stamp),
            latest_before(outdoor_rows, stamp),
        )
        si, so = state(indoor), state(outdoor)
        attrs = row["attributes"]
        braking = (
            attrs.get("mode") in ("braking", "brake_hold")
            and (number(attrs.get("brake_factor")) or 0) > 0
        )
        was_braking, should_fit = fit_boundary(attrs, was_braking)
        if should_fit:
            for name, obs in observers.items():
                old = (obs.model.k, obs.model.sample_count)
                if obs.model.pending_samples >= 20:
                    obs.model.fit()
                if obs.model.is_valid and (obs.model.k, obs.model.sample_count) != old:
                    fits[name].append(
                        {
                            "at": now.isoformat(),
                            "k": obs.model.k,
                            "samples": obs.model.sample_count,
                        }
                    )
        tin, tout = (
            number(indoor["state"])
            if indoor and not indoor.get("quarantined")
            else None,
            number(outdoor["state"])
            if outdoor and not outdoor.get("quarantined")
            else None,
        )
        # Real control reports alone represent legacy update cycles. Extra
        # observer ticks must not shorten its ten-point training buffer.
        if attrs.get("mode") is None:
            legacy_was_braking = False
        if row.get("legacy_cycle", True):
            legacy_was_braking, legacy_fit = fit_boundary(attrs, legacy_was_braking)
            cycle_at = now  # Reception time: never backdate later temperature inputs.
            legacy.record_temp(cycle_at, tin)
            if (
                attrs.get("mode") == "braking"
                and braking
                and tin is not None
                and tout is not None
            ):
                legacy.collect_braking_sample(tin, tout)
            if legacy_fit and legacy.pending_samples >= 20:
                legacy.fit()
                if legacy.is_valid:
                    fits["installed"].append(
                        {
                            "at": cycle_at.isoformat(),
                            "k": legacy.k,
                            "samples": legacy.sample_count,
                        }
                    )
        # Reuse qualification solely for a separate diagnostic evaluation of the
        # legacy fitted k; this diagnostic adapter is NOT installed production code.
        qualifier = observers["candidate"]
        qi, qo = qualifier._reading(si, now), qualifier._reading(so, now)
        if not braking or qi is None or qo is None:
            legacy_diag.cancel_pending(now)
        legacy_diag.observe(
            now=now,
            indoor=qi,
            outdoor=qo,
            k_per_hour=legacy.k if legacy.is_valid else None,
            model_fitted=legacy.is_valid,
            operating_mode="braking" if braking else "normal",
            issue_forecasts=braking and qi is not None and qo is not None,
        )
        for name, obs in observers.items():
            kwargs = (
                {"control_context": (attrs.get("mode"), attrs.get("brake_factor"))}
                if name == "candidate"
                else {"external_heat": False}
            )
            obs.observe(now, si, so, braking=braking, **kwargs)
            reasons[name][obs.attributes()["reason"]] += 1
        candidate_attrs = observers["candidate"].attributes()
        shadow = assess_assistance(True, candidate_attrs)
        shadow_reasons[shadow["reason"]] += 1
        effective_ticks += shadow["effective"]
        evidence = candidate_attrs["heat_evidence"]
        window = evidence["last_window"]
        if window and (run, window["ended_at"]) not in seen_windows:
            seen_windows.add((run, window["ended_at"]))
            windows.append(
                {
                    **window,
                    "evidence_status": evidence["status"],
                    "confidence": evidence["confidence"],
                }
            )
        for name, diag in diagnostics.items():
            capture_records(diag, records[name], run)
        ticks += 1
    for name, diag in diagnostics.items():
        finish_segment(
            diag, records[name], previous_run, utc(data["end"]), "right_censored"
        )
    model_results = {}
    for name in diagnostics:
        model = legacy if name == "installed" else observers[name].model
        model_results[name] = {
            "valid": model.is_valid,
            "k": model.k if model.is_valid else None,
            "pending_samples": model.pending_samples,
            "fits": fits[name],
            "forecasts": summarize_forecasts(list(records[name].values())),
        }
    return {
        "indoor_source": indoor_id,
        "outdoor_source": outdoor_id,
        "legacy_source_preference_ignored": possible,
        "start": utc(start).isoformat(),
        "controller_ticks": ticks,
        "shadow": {
            "requested_ticks": ticks,
            "effective_ticks": effective_ticks,
            "reasons": dict(shadow_reasons),
        },
        "episodes": episode_summary(data["series"][CONTROL], start),
        "models": model_results,
        "candidate_windows": windows,
        "observer_reasons": {k: dict(v) for k, v in reasons.items()},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--installed-model", type=Path, required=True)
    parser.add_argument("--control-context", type=Path, required=True)
    args = parser.parse_args()
    context = json.loads(args.control_context.read_text())
    if {
        value
        for value, count in context["aggressiveness_counts"]
        if value is not None and count
    } != {3}:
        parser.error(
            "This bounded replay requires verified aggressiveness=3; export the per-row attribute for other regimes"
        )
    data = json.loads(gzip.decompress(args.history.read_bytes()))
    result = {
        "history_sha256": hashlib.sha256(args.history.read_bytes()).hexdigest(),
        "installed_model_sha256": hashlib.sha256(
            args.installed_model.read_bytes()
        ).hexdigest(),
        "reference_sha256": {
            name: hashlib.sha256((args.reference_dir / name).read_bytes()).hexdigest()
            for name in ("thermal_model.py", "thermal_observer.py")
        },
        "interval": [utc(data[k]).isoformat() for k in ("start", "end")],
        "source_inventory": {
            eid: source_summary(rows) for eid, rows in data["series"].items()
        },
        "cases": {},
    }
    for label, tin, tout, possible in (
        ("weighted_no", WEIGHTED, REDUNDANT, False),
        ("weighted_yes", WEIGHTED, REDUNDANT, True),
        ("older_raw_sensitivity", RAW, OUTDOOR, True),
    ):
        result["cases"][label] = replay(
            data, tin, tout, args.reference_dir, args.installed_model, possible
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
