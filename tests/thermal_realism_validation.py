"""Finite maturity audit: hidden mass, continuing heat and sensor effects.

No new runtime model. No production/network/HA instance. A fixed independent
plant is sampled at declared report times; hidden heat never enters the learner.
The first12 cycles train; the final6 are frozen-fit evaluation, not tuning data.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import time

from thermal_self_learning_validation import START, record
from thermal_irregular_validation import Audit, load_baseline
from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer.thermal_observer import ThermalObserver

CYCLE = 480
TRAIN = 12 * CYCLE
END = 18 * CYCLE
# Fixed before seeing results. Equivalent C/hour heat input, not delivered watts
# or an estimate of any real house. Two-state plant is deliberately not learner truth.
CASES = [
    dict(name=f"{house}_{cadence}", house=house, cadence=cadence, ordinary="off")
    for house in ("slow", "fast")
    for cadence in (5, 15, 30, 60)
] + [
    dict(name="continued_heat", house="fast", cadence=15, ordinary="continued"),
    dict(name="varying_ordinary", house="fast", cadence=60, ordinary="varying"),
    dict(name="extra_slow", house="slow", cadence=15, ordinary="off", extra=True),
    dict(name="extra_fast", house="fast", cadence=30, ordinary="continued", extra=True),
    dict(
        name="slow_sensor_drift", house="slow", cadence=15, ordinary="off", drift=True
    ),
    dict(name="change_only", house="fast", cadence="change", ordinary="continued"),
    dict(
        name="irregular_lifecycle",
        house="fast",
        cadence="irregular",
        ordinary="off",
        lifecycle=True,
    ),
    dict(
        name="comfort_limited", house="fast", cadence=15, ordinary="off", comfort=True
    ),
]


def plant(case):
    """RK4 air/mass balance and lagged, cycling delivered ordinary heat.

    Known scripted command context is stable while physical delivery is not.
    Normal heating is a bounded thermostat surrogate, not PumpSteer's PI.
    """
    slow = case["house"] == "slow"
    loss, coupling, mass_rate, tau = (
        (0.018, 0.12, 0.025, 0.8) if slow else (0.092, 0.08, 0.045, 0.35)
    )
    air = mass = 21.0
    delivered = 0.0
    for minute in range(END):
        cycle, phase = divmod(minute, CYCLE)
        if phase == 0:
            brake_allowed = True
        if case.get("comfort") and air <= 20.0:
            brake_allowed = False
        braking = phase < 240 and (brake_allowed if case.get("comfort") else True)
        mode = "braking" if braking else "normal"
        factor = 1.0 if braking else 0.0
        tout = (
            7.5
            + 1.4 * math.sin(2 * math.pi * (minute - 150) / 1440)
            + 0.3 * math.sin(minute / 137)
        )
        ordinary = max(0.0, min(4.0, loss * (21 - tout) + 0.7 * (21 - air)))
        fraction = 0.0 if case["ordinary"] == "off" else 0.55
        if case["ordinary"] == "varying":
            fraction = 0.28 if cycle % 2 else 0.82
        target = ordinary * (fraction if braking else 1.0)
        target *= 0.82 + 0.36 * (0.5 + 0.5 * math.sin(minute * 2 * math.pi / 73))
        delivered = target + (delivered - target) * math.exp(-1 / (60 * tau))
        extra = (
            (1.05 if slow else 1.4)
            if case.get("extra")
            and cycle >= 12
            and cycle % 2 == 0
            and 170 <= phase < 225
            else 0.0
        )
        bias = 0.006 * max(0, minute - TRAIN) / 60 if case.get("drift") else 0.0
        observed = round(air + bias + 0.009 * math.sin(minute * 0.837), 1)
        yield minute, observed, tout, mode, factor, extra, air, delivered

        def rates(a, m):
            return -loss * (a - tout) + coupling * (
                m - a
            ) + delivered + extra, mass_rate * (a - m)

        step = 1 / 60
        a, b = rates(air, mass)
        c, d = rates(air + a * step / 2, mass + b * step / 2)
        e, f = rates(air + c * step / 2, mass + d * step / 2)
        g, h = rates(air + e * step, mass + f * step)
        air += step * (a + 2 * c + 2 * e + g) / 6
        mass += step * (b + 2 * d + 2 * f + h) / 6


def run_case(case, obs):
    obs.start(START - timedelta(seconds=1))
    audit = Audit(obs, START + timedelta(minutes=TRAIN))
    inside = outside = None
    next_at = 0
    index = 0
    previous = False
    truth = []
    windows = []
    seen = set()
    diag_changes = eval_callbacks = actual_reports = same_value_reports = 0
    available_reports = 0
    reports_by_mode = Counter()
    last_available = None
    last_attrs = None
    last_value = None
    cpu = 0
    max_samples = max_pending = max_history = 0
    reasons = Counter()
    events = {}
    for minute, value, tout, mode, factor, extra, _, _ in plant(case):
        now = START + timedelta(minutes=minute)
        truth.append(extra)
        braking = mode == "braking"
        if previous and not braking and minute < TRAIN:
            audit.fit(now)
        cadence = case["cadence"]
        emit = (
            (minute >= next_at)
            if cadence != "change"
            else (inside is None or value != last_value or minute >= next_at)
        )
        if emit:
            if minute >= TRAIN:
                actual_reports += 1
                same_value_reports += value == last_value
            inside = record(value, now)
            last_value = value
            next_at = minute + (
                (5, 17, 31, 60)[index % 4]
                if cadence == "irregular"
                else 60
                if cadence == "change"
                else cadence
            )
            index += 1
        if minute % 15 == 0:
            outside = record(tout, now)
        # Distinct lifecycle cases, each followed by genuine reports. No clock
        # rewriting, Core start/reload or synthetic replacement of real history.
        if case.get("lifecycle"):
            if minute in (TRAIN + 100, TRAIN + 3 * CYCLE + 100):
                audit.boundary(now, "reload" if minute == TRAIN + 100 else "startup")
                events[minute] = "reload/startup"
            if TRAIN + CYCLE + 80 <= minute < TRAIN + CYCLE + 110:
                inside = record("unavailable", now)
            if minute == TRAIN + CYCLE + 110:
                inside = record(value, now)
                next_at = minute + 15
            if TRAIN + 2 * CYCLE + 80 <= minute < TRAIN + 2 * CYCLE + 90:
                inside = record(
                    value, START + timedelta(minutes=TRAIN + 2 * CYCLE + 60)
                )
                inside.attributes["restored"] = True
            if minute == TRAIN + 2 * CYCLE + 90:
                inside = record(value, now)
                next_at = minute + 15
            if TRAIN + 4 * CYCLE + 80 <= minute < TRAIN + 4 * CYCLE + 105:
                previous = braking
                continue  # observer process outage, not ordinary sensor silence
            if TRAIN + 5 * CYCLE + 80 <= minute < TRAIN + 5 * CYCLE + 105:
                mode = "safe_mode"
                factor = 0.0
                braking = False
        if (
            minute >= TRAIN
            and inside is not None
            and not inside.attributes.get("restored")
        ):
            try:
                numeric = math.isfinite(float(inside.state))
            except (TypeError, ValueError):
                numeric = False
            if numeric and inside.last_reported != last_available:
                available_reports += 1
                reports_by_mode[mode] += 1
                last_available = inside.last_reported
        t = time.process_time_ns()
        audit.observe(now, inside, outside, braking, control_context=(mode, factor))
        attrs = obs.attributes()
        cpu += time.process_time_ns() - t
        max_samples = max(max_samples, obs.model.pending_samples)
        max_pending = max(max_pending, obs.diagnostics.pending_count)
        max_history = max(max_history, len(obs.diagnostics.evaluation_history))
        if minute >= TRAIN:
            eval_callbacks += 1
            reasons[attrs["reason"]] += 1
            # Upper bound on diagnostic attribute-only changes; this is NOT an
            # actual Recorder transaction count or new write call.
            encoded = json.dumps(attrs, sort_keys=True)
            diag_changes += encoded != last_attrs
            last_attrs = encoded
            w = attrs["heat_evidence"]["last_window"]
            if w and w["ended_at"] not in seen:
                seen.add(w["ended_at"])
                lo = int(
                    (datetime.fromisoformat(w["started_at"]) - START).total_seconds()
                    / 60
                )
                hi = int(
                    (datetime.fromisoformat(w["ended_at"]) - START).total_seconds() / 60
                )
                if lo >= TRAIN:
                    e = attrs["heat_evidence"]
                    actual = any(truth[x] > 0 for x in range(lo, hi))
                    qualified = (
                        e.get("baseline_confirmed", False)
                        and e["status"] != "context_changed"
                    )
                    indicated = e["status"] == "suspected_extra_heat"
                    windows.append(
                        dict(
                            start=lo,
                            end=hi,
                            cycle=lo // CYCLE,
                            actual_heat=actual,
                            qualified=qualified,
                            indicated=indicated,
                            status=e["status"],
                        )
                    )
        previous = braking
    obs.diagnostics.cancel_pending(now, "right_censored")
    audit.capture()
    summary = audit.summary()
    cells = Counter()
    pairs = audit.pairs()
    groups = {}
    for pair in pairs:
        lo = int(
            (datetime.fromisoformat(pair["issued_at"]) - START).total_seconds() / 60
        )
        hi = int(
            (datetime.fromisoformat(pair["actual_at"]) - START).total_seconds() / 60
        )
        pair["extra_heat_during_interval"] = any(
            truth[x] > 0 for x in range(lo, hi + 1)
        )
    for h in (30, 60, 120):
        for heated in (False, True):
            selected = [
                p
                for p in pairs
                if p["horizon_minutes"] == h
                and p["extra_heat_during_interval"] == heated
            ]
            groups[f"{h}_" + ("heated" if heated else "not_heated")] = dict(
                count=len(selected),
                model_mae_c=sum(abs(p["model_error_c"]) for p in selected)
                / len(selected)
                if selected
                else None,
                persistence_mae_c=sum(abs(p["persistence_error_c"]) for p in selected)
                / len(selected)
                if selected
                else None,
            )
    summary["forecast_groups"] = groups
    for w in windows:
        key = (
            ("abstained_heat" if w["actual_heat"] else "abstained_no_heat")
            if not w["qualified"]
            else "tp"
            if w["actual_heat"] and w["indicated"]
            else "fn"
            if w["actual_heat"]
            else "fp"
            if w["indicated"]
            else "tn"
        )
        cells[key] += 1
    event_results = []
    for cycle in range(12, 18):
        if case.get("extra") and cycle % 2 == 0:
            onset = cycle * CYCLE + 170
            end = cycle * CYCLE + 225
            hits = [
                w
                for w in windows
                if w["indicated"] and w["end"] > onset and w["start"] < end
            ]
            event_results.append(
                dict(
                    onset_min=onset,
                    detected=bool(hits),
                    delay_min=min(w["end"] for w in hits) - onset if hits else None,
                )
            )
    summary.update(
        case=case,
        windows=dict(cells),
        detection_events=event_results,
        evidence_windows=windows,
        emitted_source_reports=actual_reports,
        available_numeric_reports=available_reports,
        available_reports_by_mode=dict(reports_by_mode),
        unchanged_reports=same_value_reports,
        eval_callbacks=eval_callbacks,
        callback_reasons=dict(reasons),
        diagnostic_attribute_changes=diag_changes,
        observer_and_audit_cpu_ms=cpu / 1e6,
        max_retained_samples=max_samples,
        max_pending=max_pending,
        max_evaluations=max_history,
        lifecycle_events=events,
    )
    assert not any(
        datetime.fromisoformat(f["at"]) >= audit.evaluate_after for f in audit.fits
    )
    return summary, pairs


def profile(obs):
    """Pure runtime cost and retained buffers; excludes finite analysis storage."""
    import tracemalloc

    tracemalloc.start()
    begin = time.process_time_ns()
    changes = 0
    last = None
    for minute in range(5000):
        now = START + timedelta(minutes=minute)
        if minute and minute % 240 == 0:
            obs.model.fit()
        obs.observe(
            now,
            record(5 + 16 * math.exp(-0.018 * (minute % 240) / 60), now),
            record(5, now),
            braking=True,
            control_context=("braking", 1.0),
        )
        at = obs.attributes()
        encoded = json.dumps(at, sort_keys=True)
        changes += encoded != last
        last = encoded
    elapsed = (time.process_time_ns() - begin) / 1e6
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dict(
        callbacks=5000,
        cpu_ms_with_tracemalloc=elapsed,
        us_per_callback=elapsed / 5,
        traced_current_bytes=current,
        traced_peak_bytes=peak,
        retained_samples=obs.model.pending_samples,
        temp_history=len(obs.model._temp_history),
        pending=obs.diagnostics.pending_count,
        evaluations=len(obs.diagnostics.evaluation_history),
        diagnostic_attribute_changes=changes,
        extra_pollers_or_write_calls=0,
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--baseline-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result = dict(
        basis="new fixed-parameter independent-plant simulated verification, not real measurements",
        train_minutes=TRAIN,
        evaluation_minutes=END - TRAIN,
        cases=[],
        profiles={},
    )
    for case in CASES:
        row = {"case": case}
        for name, obs in (
            ("base", load_baseline(args.baseline_dir)),
            ("candidate", ThermalObserver(ThermalModel())),
        ):
            summary, pairs = run_case(case, obs)
            row[name] = summary
            (args.output_dir / (case["name"] + "_" + name + "_pairs.json")).write_text(
                json.dumps(pairs, indent=2) + "\n"
            )
        result["cases"].append(row)
    for name, obs in (
        ("base", load_baseline(args.baseline_dir)),
        ("candidate", ThermalObserver(ThermalModel())),
    ):
        result["profiles"][name] = profile(obs)
    (args.output_dir / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print("Completed", len(CASES), "paired realism cases; no production access")


if __name__ == "__main__":
    main()
