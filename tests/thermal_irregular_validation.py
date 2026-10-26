"""Finite causal offline checks. No HA instance, network, or archive writes.

Synthetic train/holdout episodes reuse the existing RK4 truth. Raw replay consumes
collector records in reception order, never substitutes snapshots for reports.
"""

import argparse
from collections import Counter
from datetime import timedelta
import gzip
import hashlib
import importlib
import json
import math
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from thermal_self_learning_validation import START, trace, record
from custom_components.pumpsteer.thermal_intelligence import ThermalDiagnostics
from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer.thermal_observer import ThermalObserver
from thermal_history_validation import fit_boundary

HORIZONS = (30, 60, 120)


def load_baseline(directory):
    package = ModuleType(
        "irregular_baseline_"
        + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:12]
    )
    package.__path__ = [str(directory / "custom_components/pumpsteer")]
    sys.modules[package.__name__] = package
    model = importlib.import_module(package.__name__ + ".thermal_model").ThermalModel
    observer = importlib.import_module(
        package.__name__ + ".thermal_observer"
    ).ThermalObserver
    return observer(model())


class Audit:
    """Retain finite run results outside the model's bounded runtime buffers."""

    def __init__(self, obs, evaluate_after):
        self.obs = obs
        self.evaluate_after = evaluate_after
        self.segment = 0
        self.issued = {}
        self.resolved = {}
        self.windows = {}
        self.report_opportunities = Counter()
        self.last_report = None
        self.fits = []
        self.arrivals = None

    def capture(self):
        for p in self.obs.diagnostics.pending_predictions:
            if p.issued_at >= self.evaluate_after:
                self.issued[(self.segment, p.issued_at, p.horizon_minutes)] = p
        for r in self.obs.diagnostics.evaluation_history:
            p = r.prediction
            if p.issued_at >= self.evaluate_after:
                key = (self.segment, p.issued_at, p.horizon_minutes)
                self.issued[key] = p
                self.resolved[key] = r
        w = self.obs.attributes()["heat_evidence"]["last_window"]
        if w:
            evidence = self.obs.attributes()["heat_evidence"]
            self.windows[(self.segment, w["ended_at"])] = dict(
                w,
                heat_status=evidence["status"],
                baseline_confirmed=evidence.get("baseline_confirmed", False),
                confidence=evidence["confidence"],
            )

    def observe(self, at, inside, outside, braking, control_context=None):
        stamp = inside.last_reported if inside else None
        distinct = stamp is not None and stamp != self.last_report
        kwargs = (
            {"control_context": control_context}
            if "control_context" in type(self.obs).observe.__code__.co_varnames
            else {}
        )
        self.obs.observe(at, inside, outside, braking=braking, **kwargs)
        self.capture()
        try:
            actual_report = (
                inside is not None
                and not inside.attributes.get("restored")
                and inside.attributes.get("unit_of_measurement") == "°C"
                and math.isfinite(float(inside.state))
                and stamp is not None
                and stamp <= at
            )
        except (TypeError, ValueError):
            actual_report = False
        if braking and distinct and actual_report and at >= self.evaluate_after:
            issued = any(
                p.issued_at == at for p in self.obs.diagnostics.pending_predictions
            )
            reason = "issued" if issued else self.obs.attributes()["reason"]
            self.report_opportunities[reason] += 1
        self.last_report = stamp

    def boundary(self, at, reason):
        if self.arrivals is not None:
            self.arrivals.boundary(reason)
        self.obs.diagnostics.cancel_pending(at, reason)
        self.capture()
        self.obs.start(at)
        self.segment += 1
        self.last_report = None

    def fit(self, at):
        before = (self.obs.model.k, self.obs.model.sample_count)
        self.obs.model.fit()
        after = (self.obs.model.k, self.obs.model.sample_count)
        if self.obs.model.is_valid and after != before:
            self.fits.append({"at": at.isoformat(), "k": after[0], "samples": after[1]})

    def summary(self):
        forecasts = {}
        for h in HORIZONS:
            keys = [k for k in self.issued if k[-1] == h]
            pairs = [
                self.resolved[k]
                for k in keys
                if k in self.resolved and self.resolved[k].status == "evaluated"
            ]
            model_errors = [r.error_c for r in pairs]
            persistence_errors = [
                r.actual_temperature_c - r.prediction.indoor_temperature_c
                for r in pairs
            ]
            forecasts[str(h)] = {
                "issued": len(keys),
                "evaluated": len(pairs),
                "coverage": len(pairs) / len(keys) if keys else None,
                "model_mae_c": sum(map(abs, model_errors)) / len(pairs)
                if pairs
                else None,
                "persistence_mae_c": sum(map(abs, persistence_errors)) / len(pairs)
                if pairs
                else None,
                "bias_c": sum(model_errors) / len(pairs) if pairs else None,
                "outcomes": dict(
                    Counter(
                        self.resolved[k].status if k in self.resolved else "pending"
                        for k in keys
                    )
                ),
            }
        opportunities = sum(self.report_opportunities.values())
        issued_opportunities = self.report_opportunities["issued"]
        return {
            "model_fitted": self.obs.model.is_valid,
            "k": self.obs.model.k if self.obs.model.is_valid else None,
            "fits": self.fits,
            "completed_windows": len(self.windows),
            "accepted_windows": sum(
                w["qualified_for_learning"] for w in self.windows.values()
            ),
            "forecasts": forecasts,
            "new_report_braking_opportunities": opportunities,
            "no_issue_fraction": (opportunities - issued_opportunities) / opportunities
            if opportunities
            else None,
            "opportunity_reasons": dict(self.report_opportunities),
        }

    def pairs(self):
        return [
            dict(
                issued_at=r.prediction.issued_at.isoformat(),
                due_at=r.prediction.due_at.isoformat(),
                actual_at=r.actual_at.isoformat(),
                horizon_minutes=r.prediction.horizon_minutes,
                model_error_c=r.error_c,
                persistence_error_c=r.actual_temperature_c
                - r.prediction.indoor_temperature_c,
                k=r.prediction.k_per_hour,
                model_version=r.prediction.model_version,
                lateness_seconds=r.lateness_seconds,
            )
            for r in self.resolved.values()
            if r.status == "evaluated"
        ]


def _finite_state(value):
    try:
        return not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class ObservationTimes:
    """Finite audit of frozen existing-model curves, not a new thermal model.

    Primary fitted-k evidence and as-recorded legacy coefficients are never mixed.
    No outcome time/value is known or chosen when a curve is issued.
    """

    def __init__(self, observer):
        self.obs = observer
        self.origins = {"candidate": None, "recorded_coefficient": None}
        self.attempts = []
        self.curves = []
        self.pairs = []
        self._context = self._sources = self._last_now = self._last_report = None

    @staticmethod
    def control_context(control, at):
        if (
            control is None
            or control.attributes.get("restored")
            or control.last_reported is None
            or not 0 <= (at - control.last_reported).total_seconds() <= 120
        ):
            return "unknown", None
        mode = control.attributes.get("mode")
        factor = control.attributes.get("brake_factor")
        if mode == "safe_mode":
            return "SAFE", mode
        if mode in ("braking", "brake_hold"):
            if type(factor) in (int, float) and math.isfinite(factor) and factor > 0:
                return "braking", mode
            return "unknown", mode
        if mode in (
            "normal",
            "preheating",
            "pre_braking",
            "recovery",
            "holiday",
            "summer_mode",
            "precool",
        ):
            return "normal", mode
        return "unknown", mode

    def boundary(self, reason):
        for name, origin in self.origins.items():
            if origin:
                origin[1]["status"] = reason
                self.origins[name] = None

    def context(self, at, inside, outside, control):
        if self._last_now is not None:
            if at < self._last_now:
                self.boundary("clock_reversal")
            elif (at - self._last_now).total_seconds() > 300:
                self.boundary("observation_gap")
        self._last_now = at
        group, mode = self.control_context(control, at)
        context = (
            group,
            mode,
            control.attributes.get("brake_factor")
            if control is not None and group == "braking"
            else None,
        )
        if self._context is not None and context != self._context:
            self.boundary("context_changed")
        self._context = context
        sources = (
            self.obs._source_signature(inside),
            self.obs._source_signature(outside),
        )
        if self._sources is not None and sources != self._sources:
            self.boundary("source_or_validity_changed")
        self._sources = sources

    def report(self, at, inside, outside, control):
        self.context(at, inside, outside, control)
        group, mode = self._context[:2]
        measurement = self.obs._reading(inside, at)
        if measurement is not None:
            if (
                self._last_report is not None
                and measurement.measured_at <= self._last_report
            ):
                self.boundary("non_increasing_report")
                return
            self._last_report = measurement.measured_at
        else:
            self.boundary("unqualified_outcome")
        # Resolve before issuing at this report. The old curve's k and inputs
        # remain frozen even if the outcome's control report has a different k.
        for name, origin in list(self.origins.items()):
            if origin is None or measurement is None:
                continue
            prediction, row = origin
            horizon = (
                measurement.measured_at - prediction.issued_at
            ).total_seconds() / 60
            if not 0 < horizon <= 120:
                row["status"] = (
                    "noncausal_target" if horizon <= 0 else "outside_horizon"
                )
            else:
                # Same exponential curve as ThermalDiagnostics; only its frozen
                # parameters and the later actual report time enter this value.
                hours = (
                    measurement.measured_at - prediction.origin_at
                ).total_seconds() / 3600
                predicted = prediction.outdoor_temperature_c + (
                    prediction.indoor_temperature_c - prediction.outdoor_temperature_c
                ) * math.exp(-prediction.k_per_hour * hours)
                pair = dict(
                    row,
                    actual_at=measurement.measured_at.isoformat(),
                    actual_received_at=at.isoformat(),
                    actual_horizon_minutes=horizon,
                    report_interval_minutes=(
                        measurement.measured_at - prediction.origin_at
                    ).total_seconds()
                    / 60,
                    reception_interval_minutes=(
                        at - prediction.issued_at
                    ).total_seconds()
                    / 60,
                    predicted_c=predicted,
                    actual_c=measurement.celsius,
                    model_error_c=measurement.celsius - predicted,
                    persistence_error_c=measurement.celsius
                    - prediction.indoor_temperature_c,
                    status="evaluated",
                )
                self.pairs.append(pair)
                row["status"] = "evaluated"
            self.origins[name] = None
        outside_measurement = self.obs._reading(outside, at)
        for name in self.origins:
            reason = (
                "safe_context"
                if group == "SAFE"
                else "outside_braking"
                if group == "normal"
                else "unknown_context"
                if group == "unknown"
                else "unqualified_indoor_report"
                if measurement is None
                else "unqualified_outdoor_report"
                if outside_measurement is None
                else None
            )
            k = self.obs.model.k if self.obs.model.is_valid else None
            count = self.obs.model.sample_count
            if not reason and name == "candidate" and k is None:
                reason = "model_not_fitted"
            if name == "recorded_coefficient":
                attrs = control.attributes if control else {}
                k, count = attrs.get("thermal_k"), attrs.get("thermal_k_samples")
                if not reason and not (
                    attrs.get("thermal_k_valid") is True
                    and type(count) is int
                    and count >= 20
                    and type(k) in (int, float)
                    and math.isfinite(k)
                    and 0.005 < k < 0.5
                ):
                    reason = "coefficient_without_fit_evidence"
            self.attempts.append(
                dict(
                    track=name,
                    group=group,
                    mode=mode,
                    received_at=at.isoformat(),
                    reason=reason or "issued",
                    numeric_source_report=(
                        inside is not None
                        and not inside.attributes.get("restored")
                        and inside.attributes.get("unit_of_measurement") == "°C"
                        and _finite_state(inside.state)
                    ),
                )
            )
            if reason:
                continue
            prediction = ThermalDiagnostics().observe(
                now=at,
                indoor=measurement,
                outdoor=outside_measurement,
                k_per_hour=k,
                model_fitted=True,
                operating_mode="braking",
            )[0]
            row = dict(
                track=name,
                group=group,
                mode=mode,
                issued_at=at.isoformat(),
                origin_at=prediction.origin_at.isoformat(),
                indoor_c=prediction.indoor_temperature_c,
                outdoor_c=prediction.outdoor_temperature_c,
                outdoor_at=prediction.outdoor_at.isoformat(),
                k=k,
                samples=count,
                coefficient_reported_at=control.last_reported.isoformat()
                if control
                else None,
                coefficient_received_at=getattr(control, "received_at", at).isoformat(),
                measurement_basis="HA_report_physical_time_unverified",
                status="pending",
            )
            self.curves.append(row)
            self.origins[name] = prediction, row

    def summary(self):
        result = {}
        for name in self.origins:
            result[name] = {}
            for group in ("normal", "braking", "SAFE", "unknown"):
                attempts = [
                    a
                    for a in self.attempts
                    if a["track"] == name and a["group"] == group
                ]
                curves = [
                    c for c in self.curves if c["track"] == name and c["group"] == group
                ]
                pairs = [
                    p for p in self.pairs if p["track"] == name and p["group"] == group
                ]
                result[name][group] = dict(
                    reports=len(attempts),
                    numeric_source_reports=sum(
                        a["numeric_source_report"] for a in attempts
                    ),
                    issued=len(curves),
                    evaluated=len(pairs),
                    abstention_fraction=(len(attempts) - len(curves)) / len(attempts)
                    if attempts
                    else None,
                    coverage=len(pairs) / len(curves) if curves else None,
                    reasons=dict(Counter(a["reason"] for a in attempts)),
                    outcomes=dict(Counter(c["status"] for c in curves)),
                    model_mae_c=sum(abs(p["model_error_c"]) for p in pairs) / len(pairs)
                    if pairs
                    else None,
                    persistence_mae_c=sum(abs(p["persistence_error_c"]) for p in pairs)
                    / len(pairs)
                    if pairs
                    else None,
                )
        return result


def synthetic(
    obs,
    cadence,
    *,
    brake_minutes=90,
    threshold=None,
    quantized=False,
    interruption=False,
    scenario="clean",
):
    cycle = brake_minutes + 60
    audit = Audit(obs, START + timedelta(minutes=20 * cycle))
    obs.start(START - timedelta(seconds=1))
    inside = None
    next_report = 0
    gaps = (7, 21, 43, 16, 60, 30)
    index = 0
    previous_braking = False
    for minute, tin, tout, braking, _ in trace(
        0.04, scenario, brake_minutes=brake_minutes
    ):
        at = START + timedelta(minutes=minute)
        if previous_braking and not braking and minute < 20 * cycle:
            audit.fit(at)
        value = round(tin, 1) if quantized else tin
        emit = (
            inside is None or abs(value - float(inside.state)) >= threshold - 1e-9
            if threshold is not None
            else minute >= next_report
        )
        if emit:
            inside = record(value, at)
            next_report = minute + (
                gaps[index % len(gaps)] if cadence == "irregular" else cadence
            )
            index += 1
        # Missing context is an actual interruption, not merely a silent source.
        known = not interruption or not (45 <= minute % cycle < 55)
        audit.observe(at, inside, record(tout, at), braking and known)
        previous_braking = braking
    obs.diagnostics.cancel_pending(at, "right_censored")
    audit.capture()
    return audit


def parse_stamp(value):
    from datetime import datetime

    return datetime.fromisoformat(value) if value else None


def replay_raw(obs, directory, indoor_id, outdoor_id, control_id, *, at_reports=False):
    files = sorted((directory / "raw").glob("*.jsonl.gz"))
    if not files:
        raise ValueError("No original raw files found")
    states = {}
    control = None
    last = None
    audit = None
    was_braking = False
    events = Counter()
    for path in files:
        with gzip.open(path, "rt") as stream:
            for line in stream:
                row = json.loads(line)
                at = parse_stamp(row.get("received_at"))
                if at is None:
                    continue
                if audit is None:
                    obs.start(at)
                    audit = Audit(obs, at)
                    if at_reports:
                        audit.arrivals = ObservationTimes(obs)
                if last is not None and at < last:
                    events["reordered_reception_discarded"] += 1
                    audit.boundary(last, "reordered_reception")
                    states.clear()
                    control = None
                    was_braking = False
                    continue
                last = at
                kind = row["kind"]
                events[kind] += 1
                if kind in ("boundary", "gap", "reordered"):
                    audit.boundary(at, kind)
                    states.clear()
                    control = None
                    was_braking = False
                elif kind in ("report", "seed", "unqualified"):
                    value = row["value"]
                    entity = value["entity_id"]
                    if entity not in (indoor_id, outdoor_id, control_id):
                        continue
                    attrs = dict(value.get("attributes", {}))
                    if kind != "report":
                        attrs["restored"] = True  # quarantined, never a new observation
                    state = SimpleNamespace(
                        state=value["state"],
                        received_at=at,
                        entity_id=entity,
                        last_reported=parse_stamp(value.get("last_reported")),
                        attributes=attrs,
                    )
                    if entity == control_id:
                        control = state
                    else:
                        states[entity] = state
                    if audit.arrivals is not None:
                        si, so = states.get(indoor_id), states.get(outdoor_id)
                        audit.arrivals.context(at, si, so, control)
                        if entity == indoor_id and kind == "report":
                            group, _ = audit.arrivals.control_context(control, at)
                            audit.observe(
                                at,
                                si,
                                so,
                                group == "braking",
                                control_context=(
                                    control.attributes.get("mode"),
                                    control.attributes.get("brake_factor"),
                                )
                                if control is not None
                                else None,
                            )
                            audit.arrivals.report(at, si, so, control)
                elif kind == "tick":
                    valid = (
                        control is not None
                        and not control.attributes.get("restored")
                        and control.last_reported is not None
                        and 0 <= (at - control.last_reported).total_seconds() <= 120
                    )
                    attrs = control.attributes if valid else {}
                    was_braking, fit = fit_boundary(attrs, was_braking)
                    if fit:
                        audit.fit(at)
                    factor = attrs.get("brake_factor")
                    braking = (
                        attrs.get("mode") in ("braking", "brake_hold")
                        and isinstance(factor, (float, int))
                        and not isinstance(factor, bool)
                        and factor > 0
                    )
                    if audit.arrivals is not None:
                        audit.arrivals.context(
                            at, states.get(indoor_id), states.get(outdoor_id), control
                        )
                    audit.observe(
                        at,
                        states.get(indoor_id),
                        states.get(outdoor_id),
                        braking,
                        control_context=(attrs.get("mode"), attrs.get("brake_factor")),
                    )
    obs.diagnostics.cancel_pending(last, "right_censored")
    if audit.arrivals is not None:
        audit.arrivals.boundary("right_censored")
    audit.capture()
    return (
        audit,
        dict(events),
        {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--indoor-id")
    parser.add_argument("--outdoor-id")
    parser.add_argument("--control-id")
    parser.add_argument("--observation-times-only", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "basis": "new causal offline forecasts, not original operating forecasts",
        "synthetic": {},
        "real": {},
    }
    cases = {str(n): {"cadence": n} for n in (1, 5, 10, 20, 30, 60)}
    cases.update(
        irregular={"cadence": "irregular"},
        quantized={"cadence": 30, "quantized": True},
        change_0_1={"cadence": 1, "quantized": True, "threshold": 0.1},
        change_0_5={"cadence": 1, "quantized": True, "threshold": 0.5},
        interruption={"cadence": 30, "interruption": True},
        hidden_heat={"cadence": 30, "scenario": "intermittent_heat"},
    )
    for brake in () if args.observation_times_only else (90, 240):
        for name, kwargs in cases.items():
            case = f"brake_{brake}min/{name}"
            results["synthetic"][case] = {}
            for label, obs in (
                ("baseline", load_baseline(args.baseline_dir)),
                ("candidate", ThermalObserver(ThermalModel())),
            ):
                audit = synthetic(obs, brake_minutes=brake, **kwargs)
                results["synthetic"][case][label] = audit.summary()
                filename = case.replace("/", "_") + "_" + label + "_pairs.json"
                (args.output_dir / filename).write_text(
                    json.dumps(audit.pairs(), indent=2) + "\n"
                )
    if args.data_dir:
        if not all((args.indoor_id, args.outdoor_id, args.control_id)):
            parser.error("Raw replay requires the three original entity IDs")
        engines = [("candidate", ThermalObserver(ThermalModel()))]
        if not args.observation_times_only:
            engines.insert(0, ("baseline", load_baseline(args.baseline_dir)))
        for label, obs in engines:
            audit, events, hashes = replay_raw(
                obs,
                args.data_dir,
                args.indoor_id,
                args.outdoor_id,
                args.control_id,
                at_reports=args.observation_times_only,
            )
            if audit.arrivals is not None:
                results["observation_times"] = audit.arrivals.summary()
                for filename, rows in (
                    ("observation_pairs.json", audit.arrivals.pairs),
                    ("observation_attempts.json", audit.arrivals.attempts),
                    ("observation_curves.json", audit.arrivals.curves),
                ):
                    (args.output_dir / filename).write_text(
                        json.dumps(rows, indent=2) + "\n"
                    )
            results["real"][label] = audit.summary()
            (args.output_dir / f"real_{label}_pairs.json").write_text(
                json.dumps(audit.pairs(), indent=2) + "\n"
            )
            (args.output_dir / f"real_{label}_windows.json").write_text(
                json.dumps(list(audit.windows.values()), indent=2) + "\n"
            )
        results["real"]["events"] = events
        results["real"]["raw_sha256"] = hashes
    (args.output_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(
        f"Completed {len(results['synthetic'])} synthetic paired cases; raw replay: {bool(args.data_dir)}"
    )


if __name__ == "__main__":
    main()
