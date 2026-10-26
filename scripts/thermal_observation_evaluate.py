"""One finite offline evaluation of the passive trial; no HA or network access."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType

from thermal_observation import (
    CONTROL,
    ENTITIES,
    TEMPERATURES,
    recover_records,
    stamp,
    atomic_json,
)


def summarize_shadow(records):
    """Score only append-recorded original forecasts, never run a model here.

    Streaming, finite-trial aggregation. Pending evidence is capped at the runtime
    limit24. Outcomes are grouped by code/run/segment/revision/mode/depth/horizon;
    no silently pooled score across model revisions or operating contexts.
    """
    from collections import OrderedDict
    import math

    pending = OrderedDict()
    groups, decisions, freshness = {}, {}, {}
    events, issues, heat = Counter(), Counter(), Counter()
    last_at = None
    snapshots=OrderedDict()
    bases=set()
    source_events={}
    recommendations=dict(total=0,eligible=0,applied=0,actions=Counter(),reasons=Counter(),
                         application_basis="shadow_has_no_actuation_path")

    def key(event):
        p = event["prediction"]
        context = event["context"]
        return (
            event["code_sha256"],
            event.get("calibration_origin", "unknown"),
            event["run_id"],
            event["segment"],
            p["model_version"],
            p["k_per_hour"],
            context["mode"],
            context["brake_factor"],
            p["horizon_minutes"],
        )

    def outcome(event, status):
        group = groups[key(event)]
        group["outcomes"][status] += 1
        return group

    for event in records:
        kind = event.get("kind")
        if kind not in (
            "shadow_boundary", "forecast", "resolution", "decision", "heat_diagnostic",
            "forecast_inputs", "recommendation",
        ):
            if kind in ('report','seed','unqualified','reordered'):
                entity=event.get('value',{}).get('entity_id','unknown')
                source_events.setdefault(entity,Counter())[kind]+=1
            continue
        events[kind] += 1
        bases.add(event.get("execution_basis","live_shadow"))
        at = stamp(event.get("received_at"))
        if at is None or (last_at is not None and at < last_at):
            issues["nonchronological_or_missing_timestamp"] += 1
            continue
        last_at = at
        if event.get("schema") != 1:
            issues["unsupported_schema"] += 1
            continue
        if kind == "forecast_inputs":
            snapshot=event.get('snapshot',{})
            received=stamp(snapshot.get('received_at'))
            origin=stamp(snapshot.get('weather_issued_at')) if snapshot.get('weather_issued_at') else None
            if received is None or received>at or origin is not None and origin>received:
                issues['invalid_forecast_input_snapshot']+=1
                continue
            snapshots[event.get('snapshot_id')]=(snapshot,at)
            if len(snapshots)>32:snapshots.popitem(last=False)
        elif kind == "recommendation":
            rec=event.get('recommendation',{})
            if rec.get('effective') is not False or type(rec.get('delta_fake_c')) not in (int,float) or not math.isfinite(rec['delta_fake_c']) or abs(rec['delta_fake_c'])>1:
                issues['invalid_or_nonpassive_recommendation']+=1
                continue
            recommendations['total']+=1
            recommendations['eligible']+=rec.get('eligible') is True
            recommendations['actions'][rec.get('action','unknown')]+=1
            recommendations['reasons'][rec.get('reason','unknown')]+=1
        elif kind == "shadow_boundary":
            # Includes an ungraceful process restart whose pending records could
            # not be resolved in memory. Never silently drop them from coverage.
            for p in pending.values():
                outcome(p, "unresolved_after_boundary")
            pending.clear()
        elif kind == "forecast":
            try:
                p = event["prediction"]
                issued, due, origin, outdoor = (
                    stamp(p[t])
                    for t in ("issued_at", "due_at", "origin_at", "outdoor_at")
                )
                if not (
                    issued == at
                    and origin <= issued
                    and outdoor <= issued
                    and (due - issued).total_seconds() == 60 * p["horizon_minutes"]
                    and p["horizon_minutes"] in (30, 60, 120)
                    and all(
                        math.isfinite(p[x])
                        for x in (
                            "temperature_c",
                            "indoor_temperature_c",
                            "outdoor_temperature_c",
                            "k_per_hour",
                        )
                    )
                ):
                    raise ValueError("Invalid original forecast")
                for name, source in event["inputs"].items():
                    received, reported = (
                        stamp(source["received_at"]),
                        stamp(source["ha_last_reported"]),
                    )
                    if not (
                        reported <= received <= issued
                        and 0
                        <= (issued - reported).total_seconds()
                        <= (120 if name == "control" else 900)
                        and not source["quarantined"]
                        and not source["attributes"].get("restored")
                    ):
                        raise ValueError("Future/unqualified original input")
                snapshot_id=event.get('forecast_snapshot_id')
                if snapshot_id:
                    saved=snapshots.get(snapshot_id)
                    if saved is None or saved[1]>issued or stamp(p.get('forecast_received_at'))!=stamp(saved[0].get('received_at')):
                        raise ValueError('Missing or future as-of forecast snapshot')
                    for end,temperature in p.get('weather_path',[]):
                        if not issued<stamp(end)<=due or type(temperature) not in (int,float) or not math.isfinite(temperature):
                            raise ValueError('Invalid weather path provenance')
                ident = event["forecast_id"]
                if ident in pending:
                    issues["duplicate_forecast"] += 1
                    continue
                k = key(event)
            except (KeyError, TypeError, ValueError):
                issues["invalid_forecast"] += 1
                continue
            if k not in groups:
                groups[k] = dict(
                    issued=0,
                    evaluated=0,
                    abs_model=0.0,
                    abs_persistence=0.0,
                    signed_model=0.0,
                    signed_persistence=0.0,
                    horizons_min=None,
                    horizons_max=None,
                    outcomes=Counter(),
                )
            groups[k]["issued"] += 1
            if len(pending) >= 24:
                _, old = pending.popitem(last=False)
                outcome(old, "unresolved_overflow")
                issues["pending_overflow"] += 1
            pending[ident] = event
        elif kind == "resolution":
            original = pending.pop(event.get("forecast_id"), None)
            if original is None:
                issues["resolution_without_pending_original"] += 1
                continue
            status = event.get("status")
            if status != "evaluated":
                outcome(original, status or "unknown_resolution")
                continue
            try:
                p = original["prediction"]
                actual_at = stamp(event["actual_at"])
                issued, due = stamp(p["issued_at"]), stamp(p["due_at"])
                actual = event["actual_temperature_c"]
                source = event["actual_input"]
                if not (
                    issued < actual_at <= at
                    and 0 <= (actual_at - due).total_seconds() <= 120
                    and stamp(source["ha_last_reported"]) == actual_at
                    and actual_at <= stamp(source["received_at"]) <= at
                    and source["attributes"].get("unit_of_measurement") == "°C"
                    and not source["attributes"].get("restored")
                    and not source["quarantined"]
                    and math.isfinite(actual)
                    and float(source["value"]) == actual
                ):
                    raise ValueError("Unqualified outcome")
            except (KeyError, TypeError, ValueError):
                outcome(original, "invalid_outcome")
                issues["invalid_outcome"] += 1
                continue
            g = outcome(original, "evaluated")
            model_error = actual - p["temperature_c"]
            persistence_error = actual - p["indoor_temperature_c"]
            g["evaluated"] += 1
            g["abs_model"] += abs(model_error)
            g["abs_persistence"] += abs(persistence_error)
            g["signed_model"] += model_error
            g["signed_persistence"] += persistence_error
            horizon = (actual_at - issued).total_seconds() / 60
            g["horizons_min"] = (
                min(g["horizons_min"], horizon)
                if g["horizons_min"] is not None
                else horizon
            )
            g["horizons_max"] = (
                max(g["horizons_max"], horizon)
                if g["horizons_max"] is not None
                else horizon
            )
        elif kind == "decision":
            mode = event.get("context", {}).get("mode", "unknown")
            d = decisions.setdefault(
                mode, dict(reports=0, without_forecast=0, reasons=Counter())
            )
            d["reports"] += 1
            d["without_forecast"] += not event.get("issued", False)
            d["reasons"][event.get("reason", "unknown")] += 1
            for name, source in event.get("inputs", {}).items():
                f = freshness.setdefault(
                    name,
                    dict(
                        reports=0,
                        ha_age_sum=0.0,
                        ha_age_max_seconds=None,
                        stale_over_15min=0,
                        invalid_age=0,
                        restored=0,
                        quarantined=0,
                    ),
                )
                f["reports"] += 1
                f["restored"] += bool(source.get("attributes", {}).get("restored"))
                f["quarantined"] += bool(source.get("quarantined"))
                age = source.get("ha_report_age_seconds")
                if isinstance(age, (int, float)) and math.isfinite(age) and age >= 0:
                    f["ha_age_sum"] += age
                    f["ha_age_max_seconds"] = max(f["ha_age_max_seconds"] or 0, age)
                    f["stale_over_15min"] += age > 900
                else:
                    f["invalid_age"] += 1
        else:
            heat[event.get("evidence", {}).get("status", "unknown")] += 1
    for original in pending.values():
        outcome(original, "unresolved_at_end")
    result = []
    for k, g in groups.items():
        n = g["evaluated"]
        result.append(
            dict(
                zip(
                    (
                        "code_sha256",
                        "calibration_origin",
                        "run_id",
                        "segment",
                        "model_version",
                        "k_per_hour",
                        "mode",
                        "brake_factor",
                        "horizon_minutes",
                    ),
                    k,
                ),
                issued=g["issued"],
                evaluated=n,
                coverage=n / g["issued"],
                outcomes=dict(g["outcomes"]),
                model_mae_c=g["abs_model"] / n if n else None,
                persistence_mae_c=g["abs_persistence"] / n if n else None,
                model_bias_c=g["signed_model"] / n if n else None,
                persistence_bias_c=g["signed_persistence"] / n if n else None,
                actual_horizon_min_minutes=g["horizons_min"],
                actual_horizon_max_minutes=g["horizons_max"],
            )
        )
    for d in decisions.values():
        d["abstention_fraction"] = d["without_forecast"] / d["reports"]
    for f in freshness.values():
        n = f["reports"] - f["invalid_age"]
        f["mean_ha_report_age_seconds"] = f.pop("ha_age_sum") / n if n else None
    return dict(
        basis=("original_live_shadow_forecasts" if not bases or bases=={'live_shadow'} else
               'offline_simulation_not_original_live_forecasts' if bases=={'offline_simulation'} else
               'causal_offline_replay_not_original_live_forecasts' if bases=={'causal_offline_replay'} else
               'mixed_execution_bases_not_original_live_forecasts'),
        execution_bases=sorted(bases),
        source_events=source_events,
        recommendations=recommendations,
        decision_basis="first_observed_indoor_report_attempt; later_asynchronous_issuance_recorded_separately",
        groups=result,
        decisions=decisions,
        freshness_at_indoor_report_decisions=freshness,
        events=dict(events),
        integrity_issues=dict(issues),
        heat_diagnostic_events=dict(heat),
        physical_freshness_verified=False,
        original_forecast_count=sum(g["issued"] for g in groups.values()),
        ready_for_active_assistance=False,
    )


def to_history(records, start, end):
    """Keep original HA report times; join by actual collector reception times."""
    history = {
        "start": start.timestamp(),
        "end": end.timestamp(),
        "series": {e: [] for e in ENTITIES},
        "runs": [],
    }
    control = None
    pending_cycle = False
    events = Counter()
    latest_received, clock_overlap = start, False
    for index, record in enumerate(records):
        received = stamp(record.get("received_at"))
        if received is None or not start <= received <= end:
            continue
        if received < latest_received:
            events["clock_overlap_discarded"] += 1
            clock_overlap = True
            continue
        if clock_overlap:
            history["runs"].append([index, received.isoformat(), None, 0])
            control, pending_cycle, clock_overlap = None, False, False
        latest_received = received
        kind = record["kind"]
        events[kind] += 1
        at = received.timestamp()
        if kind in ("boundary", "gap"):
            history["runs"].append([index, received.isoformat(), None, 0])
            control = None
            pending_cycle = False
        elif kind in ("report", "seed", "unqualified"):
            value = record["value"]
            entity = value.get("entity_id")
            if entity not in history["series"]:
                continue
            reported = stamp(value.get("last_reported"))
            row = {
                "id": index,
                "state": value.get("state"),
                "updated": at,
                "ha_updated": value.get("last_updated"),
                "reported": reported.timestamp() if reported else None,
                "attributes": value.get("attributes", {}),
                "quarantined": kind == "unqualified",
            }
            if entity == CONTROL:
                control = row
                pending_cycle = kind == "report"
            else:
                history["series"][entity].append(row)
        elif kind == "tick":
            if control is None:
                continue
            attrs = control["attributes"]
            valid = (
                not control["quarantined"]
                and not attrs.get("restored")
                and control["reported"] is not None
                and 0 <= at - control["reported"] <= 120
            )
            history["series"][CONTROL].append(
                {
                    **control,
                    "updated": at,
                    "legacy_cycle": pending_cycle and valid,
                    "attributes": attrs if valid else {"mode": None},
                }
            )
            pending_cycle = False
    return history, dict(events)


def evaluate(config):
    directory = Path(config["data_dir"])
    files = sorted((directory / "raw").glob("*.jsonl.gz"))
    records = (r for file in files for r in recover_records(file))
    start, end = stamp(config["start"]), stamp(config["end"])
    history, events = to_history(records, start, end)
    # Reuse the existing finite replay without importing a live HA integration.
    parent = ModuleType("custom_components")
    parent.__path__ = []
    sys.modules.setdefault("custom_components", parent)
    package = ModuleType("custom_components.pumpsteer")
    package.__path__ = [config["model_dir"]]
    sys.modules["custom_components.pumpsteer"] = package
    sys.path.insert(0, config["analysis_dir"])
    analysis = importlib.import_module("thermal_history_validation")
    result = {
        "source_inventory": {
            entity: analysis.source_summary(rows)
            for entity, rows in history["series"].items()
        },
        "start": config["start"],
        "end": config["end"],
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "events": events,
        "raw_files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files
        },
        "ready_for_active_assistance": False,
        "previous_candidate_identical_model_files": True,
        "external_heat_ground_truth": "unavailable",
        "cases": {},
    }
    if all(history["series"][e] for e in (TEMPERATURES[0], TEMPERATURES[4], CONTROL)):
        result["cases"]["weighted_yes"] = analysis.replay(
            history,
            TEMPERATURES[0],
            TEMPERATURES[4],
            Path(config["reference_dir"]),
            Path(config["installed_model"]),
            True,
        )
    else:
        result["insufficient_inputs"] = True
    (directory / "evaluation.json").write_text(json.dumps(result, indent=2))
    lines = [
        "# Thermal Intelligence: passivt observationsprov",
        "",
        f"Planerat intervall: {config['start']} – {config['end']}.",
        f"Bevarade nya rapporter: {events.get('report', 0)}; seed-läsningar: {events.get('seed', 0)}; luckmarkörer: {events.get('gap', 0)}; segmentgränser: {events.get('boundary', 0)}.",
        "",
        "Inga styrkommandon skickades. Full state_reported-ström saknas i externa API:t; snapshots kan missa mellanliggande rapporter. Rapporttider ersätts aldrig av pollningstider.",
        "",
    ]
    for case in result["cases"].values():
        windows = case["candidate_windows"]
        lines += [
            f"Bromsperioder: {len(case['episodes'])}. Kvalificerade kandidatfönster: {sum(bool(w['qualified_for_learning']) for w in windows)} av {len(windows)}.",
            "",
            "| Modell | Inlärt k /h | Fits | 30 min: n/utfall, MAE, täckning | 60 min | 120 min |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for name, model in case["models"].items():
            cells = []
            for horizon in ("30", "60", "120"):
                q = model["forecasts"][horizon]
                cells.append(
                    f"{q['evaluated']}/{q['resolved']}; {q['mae_c']}; {q['coverage']}"
                )
            lines.append(
                f"| {name} | {model['k']} | {len(model['fits'])} | "
                + " | ".join(cells)
                + " |"
            )
        evidence = Counter(w["evidence_status"] for w in windows)
        lines += [
            "",
            "Avvikelseindikationer per fönster: "
            + json.dumps(evidence, ensure_ascii=False)
            + ".",
        ]
    lines += [
        "",
        "None betyder ej beräkningsbart. Saknade/avbrutna utfall räknas i nämnaren och är inte nollfel. cb642a0 och 4df10d2 har identiska modellfiler; ingen modellskillnad uppfinns. e1 är den tidigare granskningsadaptern; installed är oförändrad legacy ThermalModel med samma tillgängliga temperaturindata. Legacy tränas endast vid observerade nya kontrollrapporter, vid mottagningstid; eventuellt missade mellanliggande kontrollcykler och upp till 30 s transportfördröjning begränsar jämförelsen.",
        "",
        "Avvikelser är inte verifierade externa värmekällor. Facit för värmetillförsel saknas, så falskpositiv-/falsknegativfrekvens kan inte beräknas. Ett prov med för få kvalificerade perioder belägger inte prognosnytta och är inte i sig ett kodfel.",
        "",
        "Ingen aktiv adaptiv assistans är motiverad enbart av detta observationsprov. Prognos- och täckningsmått är underlag för fortsatt offlineprövning; levererad värme och komforteffekten av ändrade kommandon har inte verifierats.",
    ]
    if result.get("insufficient_inputs"):
        lines += ["", "Otillräckligt indata för modelljämförelse."]
    (directory / "report.md").write_text("\n".join(lines) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--shadow-data", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.shadow_data is not None:
        if args.output is None:
            parser.error("--shadow-data requires --output")
        records = (
            r
            for file in sorted((args.shadow_data / "raw").glob("*.jsonl.gz"))
            for r in recover_records(file)
        )
        atomic_json(args.output, summarize_shadow(records))
    elif args.config is not None:
        evaluate(json.loads(args.config.read_text()))
    else:
        parser.error("select --shadow-data or legacy --config")


if __name__ == "__main__":
    main()
