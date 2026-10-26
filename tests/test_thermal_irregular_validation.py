"""Check the offline evaluator's causal boundary and matched-pair accounting."""

import gzip
import json
from datetime import timedelta

import pytest

from test_thermal_observer import NOW, observer, report
from thermal_irregular_validation import Audit, replay_raw, synthetic


def test_persistence_and_model_use_identical_real_outcome_pair():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    audit = Audit(obs, NOW)
    for minute in range(31):
        at = NOW + timedelta(minutes=minute)
        inside = report(20.8, at) if minute == 30 else report(21, NOW)
        audit.observe(at, inside, report(5, at), True)
    result = audit.summary()["forecasts"]["30"]
    assert result["issued"] == 2
    assert result["evaluated"] == 1
    assert result["coverage"] == 0.5
    assert result["persistence_mae_c"] == pytest.approx(0.2)
    assert result["model_mae_c"] == pytest.approx(0.116821227, abs=1e-8)
    assert result["outcomes"] == {"evaluated": 1, "pending": 1}


def test_training_forecasts_are_not_counted_as_holdout_evidence():
    obs = observer()
    obs.model.restore_k(0.04, 25)
    audit = Audit(obs, NOW + timedelta(minutes=1))
    audit.observe(NOW, report(21), report(5), True)
    at = NOW + timedelta(minutes=30)
    audit.observe(at, report(20.8, at), report(5, at), True)
    result = audit.summary()["forecasts"]["30"]
    assert result["issued"] == 1
    assert result["evaluated"] == 0
    assert result["model_mae_c"] is None


def write_raw(tmp_path, *, late_change=False, unknown=False, control_first=False):
    rows = [
        {"kind": "boundary", "received_at": (NOW - timedelta(seconds=1)).isoformat()}
    ]
    for minute in range(21):
        at = NOW + timedelta(minutes=minute)
        values = [("sensor.out", "5", {})]
        if minute in (0, 10, 20):
            val = 30 if minute == 20 and late_change else 21 - minute * 0.01
            values.append(("sensor.in", str(val), {}))
        attrs = {
            "mode": "safe_mode" if unknown and minute == 5 else "braking",
            "brake_factor": 1,
        }
        if control_first:
            values.insert(0, ("sensor.control", "5", attrs))
        else:
            values.append(("sensor.control", "5", attrs))
        for entity, state, attrs in values:
            rows.append(
                {
                    "kind": "report",
                    "received_at": at.isoformat(),
                    "value": {
                        "entity_id": entity,
                        "state": state,
                        "last_reported": at.isoformat(),
                        "attributes": {"unit_of_measurement": "°C", **attrs},
                    },
                }
            )
        rows.append({"kind": "tick", "received_at": at.isoformat()})
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    with gzip.open(raw / "sample.jsonl.gz", "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_future_reports_cannot_change_previous_learned_window(tmp_path):
    windows = []
    for change in (False, True):
        write_raw(tmp_path, late_change=change)
        audit, _, _ = replay_raw(
            observer(), tmp_path, "sensor.in", "sensor.out", "sensor.control"
        )
        windows.append(next(iter(audit.windows.values())))
    assert windows[0] == windows[1]
    assert windows[0]["minutes"] == 10


def test_raw_safe_boundary_prevents_bridging_then_recovers(tmp_path):
    write_raw(tmp_path, unknown=True)
    audit, _, _ = replay_raw(
        observer(), tmp_path, "sensor.in", "sensor.out", "sensor.control"
    )
    assert audit.summary()["accepted_windows"] == 1  # only 10–20


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cadence": 1},
        {"cadence": 20},
        {"cadence": 30},
        {"cadence": 60},
        {"cadence": "irregular"},
        {"cadence": 30, "quantized": True},
        {"cadence": 1, "quantized": True, "threshold": 0.5},
        {"cadence": 30, "interruption": True},
    ],
)
def test_known_house_learns_at_different_densities_without_holdout_fits(kwargs):
    audit = synthetic(observer(), brake_minutes=240, **kwargs)
    result = audit.summary()
    assert result["model_fitted"]
    assert result["k"] == pytest.approx(0.04, abs=0.003)
    assert all(f["at"] < audit.evaluate_after.isoformat() for f in result["fits"])
    for metrics in result["forecasts"].values():
        assert sum(metrics["outcomes"].values()) == metrics["issued"]
        if metrics["evaluated"]:
            assert metrics["model_mae_c"] < metrics["persistence_mae_c"]


def test_restored_snapshot_is_not_a_new_report_opportunity():
    audit = Audit(observer(), NOW)
    audit.observe(NOW, report(21, restored=True), report(5), True)
    assert audit.summary()["new_report_braking_opportunities"] == 0
    assert audit.summary()["no_issue_fraction"] is None


def test_arrival_time_replay_keeps_primary_unfitted_and_recorded_k_separate(tmp_path):
    write_raw(tmp_path, control_first=True)
    path = tmp_path / "raw/sample.jsonl.gz"
    with gzip.open(path, "rt") as f:
        rows = [json.loads(line) for line in f]
    for r in rows:
        if r.get("value", {}).get("entity_id") == "sensor.control":
            r["value"]["attributes"].update(
                thermal_k=0.04, thermal_k_valid=True, thermal_k_samples=25
            )
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    audit, _, _ = replay_raw(
        observer(),
        tmp_path,
        "sensor.in",
        "sensor.out",
        "sensor.control",
        at_reports=True,
    )
    result = audit.arrivals.summary()
    assert not audit.obs.model.is_valid
    assert result["candidate"]["braking"]["issued"] == 0
    pairs = audit.arrivals.pairs
    assert len(pairs) == 2
    assert pairs[0]["actual_horizon_minutes"] == 10
    assert pairs[0]["persistence_error_c"] == pytest.approx(-0.1)
    assert pairs[0]["model_error_c"] == pytest.approx(0.0063119, abs=1e-6)


def arrival_audit():
    from thermal_irregular_validation import ObservationTimes

    return ObservationTimes(observer())


def source_context(at, mode="braking", k=0.04):
    return report(
        5,
        at,
        mode=mode,
        brake_factor=1,
        thermal_k=k,
        thermal_k_valid=True,
        thermal_k_samples=25,
    )


def test_arrival_curve_freezes_coefficient_and_uses_actual_horizon():
    audit = arrival_audit()
    audit.report(NOW, report(21), report(5), source_context(NOW))
    for minute in range(1, 40):
        at = NOW + timedelta(minutes=minute)
        audit.context(at, report(21), report(5, at), source_context(at))
    at = NOW + timedelta(minutes=40)
    audit.report(at, report(20.8, at), report(5, at), source_context(at, k=0.1))
    pair = audit.pairs[0]
    assert pair["k"] == 0.04
    assert pair["actual_horizon_minutes"] == 40
    assert pair["persistence_error_c"] == pytest.approx(-0.2)
    assert pair["model_error_c"] == pytest.approx(0.2210275, abs=1e-6)


@pytest.mark.parametrize("mode", ["normal", "safe_mode"])
def test_arrival_curves_do_not_cross_unsupported_modes(mode):
    audit = arrival_audit()
    audit.report(NOW, report(21), report(5), source_context(NOW))
    at = NOW + timedelta(minutes=5)
    audit.context(at, report(21), report(5), source_context(at, mode))
    at = NOW + timedelta(minutes=30)
    audit.report(at, report(20.7, at), report(5, at), source_context(at))
    assert not audit.pairs
    assert audit.summary()["recorded_coefficient"]["braking"]["outcomes"] == {
        "context_changed": 1,
        "pending": 1,
    }


def test_delivered_late_outcome_cannot_be_backdated_before_issue():
    audit = arrival_audit()
    issue = NOW + timedelta(minutes=2)
    audit.report(issue, report(21, NOW), report(5, NOW), source_context(issue))
    received = NOW + timedelta(minutes=3)
    audit.report(
        received,
        report(20.9, NOW + timedelta(minutes=1)),
        report(5, received),
        source_context(received),
    )
    assert not audit.pairs  # target HA report existed before forecast issuance


def test_no_coefficient_fallback_and_no_fake_pairs():
    audit = arrival_audit()
    at = NOW
    control = source_context(at)
    control.attributes["thermal_k_samples"] = 0
    audit.report(at, report(21), report(5), control)
    s = audit.summary()
    assert s["candidate"]["braking"]["reasons"] == {"model_not_fitted": 1}
    assert s["recorded_coefficient"]["braking"]["reasons"] == {
        "coefficient_without_fit_evidence": 1
    }
    assert not audit.pairs


def test_observation_time_cli_runs_only_real_replay(tmp_path, monkeypatch):
    import sys
    from thermal_irregular_validation import main

    write_raw(tmp_path, control_first=True)
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "thermal_irregular_validation.py",
            "--baseline-dir",
            str(tmp_path),
            "--output-dir",
            str(output),
            "--data-dir",
            str(tmp_path),
            "--indoor-id",
            "sensor.in",
            "--outdoor-id",
            "sensor.out",
            "--control-id",
            "sensor.control",
            "--observation-times-only",
        ],
    )
    main()
    result = json.loads((output / "results.json").read_text())
    assert not result["synthetic"]
    assert result["observation_times"]["candidate"]["braking"]["reports"] == 3
    assert (output / "observation_attempts.json").exists()


def test_reception_and_ha_report_intervals_remain_distinct():
    audit = arrival_audit()
    issued = NOW + timedelta(minutes=2)
    audit.report(issued, report(21), report(5), source_context(issued))
    for minute in range(3, 31):
        at = NOW + timedelta(minutes=minute)
        audit.context(at, report(21), report(5, at), source_context(at))
    received = NOW + timedelta(minutes=31)
    audit.report(
        received,
        report(20.8, NOW + timedelta(minutes=30)),
        report(5, received),
        source_context(received),
    )
    pair = audit.pairs[0]
    assert pair["actual_horizon_minutes"] == 28
    assert pair["report_interval_minutes"] == 30
    assert pair["reception_interval_minutes"] == 29
    assert pair["model_error_c"] == pytest.approx(0.116821227, abs=1e-8)
