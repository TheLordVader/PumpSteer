"""Original shadow issuance and later real-report matching, never model replay."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import copy
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from thermal_observation import Shadow, accept_reading, Journal, recover_records

AT = datetime(2026, 11, 1, tzinfo=timezone.utc)
IDS = ("sensor.inside", "sensor.outside", "sensor.controller")
MODEL = Path(__file__).parents[1] / "custom_components/pumpsteer"


def feed(shadow, latest, admitted, minute, tin=21, mode="braking", factor=1, **attrs):
    at = AT + timedelta(minutes=minute)
    for entity, value, extra in (
        (IDS[0], tin, attrs),
        (IDS[1], 5, {}),
        (IDS[2], 5, {"mode": mode, "brake_factor": factor}),
    ):
        raw = {
            "entity_id": entity,
            "state": str(value),
            "last_reported": at.isoformat(),
            "attributes": {"unit_of_measurement": "°C", **extra},
        }
        accept_reading(latest, admitted, raw, at, entities=IDS)
    return shadow.update(latest, at)


def shadow():
    events = []
    obs = Shadow(MODEL, entities=IDS, emit=events.append)
    obs.boundary(AT - timedelta(seconds=1))
    # This fit-evidence fixture tests logging, NOT autonomous calibration/accuracy.
    obs.observer.model.restore_k(0.04, 25)
    obs.calibration_origin = "test_restored_fixture"
    return obs, events, {}, {}


def test_original_forecast_and_same_pair_outcome_are_frozen_and_timestamped():
    obs, events, latest, admitted = shadow()
    feed(obs, latest, admitted, 0)
    issued = [e for e in events if e["kind"] == "forecast"]
    assert len(issued) == 3
    frozen = copy.deepcopy(issued)
    p = issued[0]
    assert p["prediction"]["issued_at"] == AT.isoformat()
    assert p["prediction"]["origin_at"] == AT.isoformat()
    assert p["prediction"]["due_at"] == (AT + timedelta(minutes=30)).isoformat()
    assert p["inputs"]["indoor"]["physical_measured_at"] is None
    assert p["inputs"]["indoor"]["ha_last_reported"] == AT.isoformat()
    assert p["inputs"]["indoor"]["received_at"] == AT.isoformat()
    assert p["context"]["mode"] == "braking"
    assert len(p["code_sha256"]) == 64
    # Genuine identical-value HA reports with advancing timestamps are outcomes.
    for m in range(1, 31):
        feed(obs, latest, admitted, m)
    resolutions = [
        e
        for e in events
        if e["kind"] == "resolution" and e["forecast_id"] == p["forecast_id"]
    ]
    assert len(resolutions) == 1
    assert resolutions[0]["status"] == "evaluated"
    assert resolutions[0]["actual_horizon_minutes"] == 30
    assert resolutions[0]["persistence_error_c"] == 0
    assert resolutions[0]["model_error_c"] > 0
    assert issued == frozen
    assert len({e["forecast_id"] for e in events if e["kind"] == "forecast"}) == 6


@pytest.mark.parametrize(
    "mode,factor", [("safe_mode", 0), ("normal", 0), ("braking", 0.5)]
)
def test_context_interruptions_resolve_original_forecasts_without_learning_across_boundary(
    mode, factor
):
    obs, events, latest, admitted = shadow()
    feed(obs, latest, admitted, 0)
    feed(obs, latest, admitted, 1, mode=mode, factor=factor)
    ended = [e for e in events if e["kind"] == "resolution"]
    assert len(ended) == 3
    assert all(e["status"] == "interrupted" for e in ended)
    assert all(e["model_error_c"] is None for e in ended)
    assert obs.observer.model.pending_samples == 0


def test_restart_cancels_old_predictions_and_requires_post_boundary_reports():
    obs, events, latest, admitted = shadow()
    feed(obs, latest, admitted, 0)
    obs.boundary(AT + timedelta(seconds=30), reason="reconnect")
    ended = [e for e in events if e["kind"] == "resolution"]
    assert len(ended) == 3 and {e["status"] for e in ended} == {"reconnect"}
    obs.update(latest, AT + timedelta(seconds=31))
    assert len([e for e in events if e["kind"] == "forecast"]) == 3
    feed(obs, latest, admitted, 1)
    assert len([e for e in events if e["kind"] == "forecast"]) == 6
    assert len({e["forecast_id"] for e in events if e["kind"] == "forecast"}) == 6


def test_tick_duplicates_never_become_decisions_or_fresh_physical_measurements():
    obs, events, latest, admitted = shadow()
    feed(obs, latest, admitted, 0)
    for second in range(1, 31):
        obs.update(latest, AT + timedelta(seconds=second))
    assert len([e for e in events if e["kind"] == "decision"]) == 1
    assert len([e for e in events if e["kind"] == "forecast"]) == 3
    assert latest[IDS[0]]["last_reported"] == AT.isoformat()


def test_unavailable_restore_and_source_change_do_not_score_old_forecasts():
    for extra, value in [
        ({}, "unavailable"),
        ({"restored": True}, 21),
        ({"selected_source": "backup"}, 21),
    ]:
        obs, events, latest, admitted = shadow()
        feed(obs, latest, admitted, 0)
        feed(obs, latest, admitted, 1, tin=value, **extra)
        assert (
            len(
                [
                    e
                    for e in events
                    if e["kind"] == "resolution" and e["status"] == "interrupted"
                ]
            )
            == 3
        )
        assert obs.observer.model.pending_samples == 0


def test_summary_uses_logged_originals_only_with_revision_mode_and_same_pairs(tmp_path):
    from thermal_observation_evaluate import summarize_shadow

    obs, events, latest, admitted = shadow()
    for m in range(31):
        feed(obs, latest, admitted, m)
    obs.finish(AT + timedelta(minutes=31))
    journal = Journal(tmp_path)
    for event in events:
        journal.add(event)
    journal.flush()
    records = [r for f in tmp_path.glob("*.gz") for r in recover_records(f)]
    summary = summarize_shadow(records)
    assert summary["basis"] == "original_live_shadow_forecasts"
    groups = summary["groups"]
    h30 = next(g for g in groups if g["horizon_minutes"] == 30)
    assert h30["issued"] == 2 and h30["evaluated"] == 1
    assert h30["coverage"] == 0.5
    assert h30["persistence_mae_c"] == 0 and h30["model_mae_c"] > 0
    assert h30["model_bias_c"] == pytest.approx(h30["model_mae_c"])
    assert h30["actual_horizon_min_minutes"] == 30
    assert summary["physical_freshness_verified"] is False
    assert summary["decisions"]["braking"]["reports"] == 31
    assert summary["decisions"]["braking"]["without_forecast"] == 29
    # Changing future reports cannot revise an issued original or prior score.
    prefix = summarize_shadow(
        [
            r
            for r in records
            if datetime.fromisoformat(r["received_at"]) < AT + timedelta(minutes=30)
        ]
    )
    assert sum(g["evaluated"] for g in prefix["groups"]) == 0
    assert sum(g["issued"] for g in prefix["groups"]) == 3
    json.dumps(summary, allow_nan=False)


def test_entire_collector_writes_original_forecasts_with_three_reads_only(
    tmp_path, monkeypatch
):
    import thermal_observation as collection
    from types import SimpleNamespace

    clock = [AT]
    calls = []

    class Timeout(Exception):
        pass

    class WS:
        def __init__(self):
            self.replies = iter(
                [{"type": "auth_required"}, {"type": "auth_ok"}, {"success": True}]
            )

        def recv(self):
            try:
                return json.dumps(next(self.replies))
            except StopIteration:
                clock[0] += timedelta(seconds=30)
                raise Timeout()

        def send(self, text):
            calls.append(json.loads(text))

        def settimeout(self, timeout):
            pass

        def close(self):
            pass

    monkeypatch.setitem(
        sys.modules,
        "websocket",
        SimpleNamespace(
            create_connection=lambda *a, **kw: WS(),
            WebSocketException=Timeout,
            WebSocketTimeoutException=Timeout,
        ),
    )
    monkeypatch.setattr(collection, "now", lambda: clock[0])
    monkeypatch.setattr(
        collection.time, "monotonic", lambda: (clock[0] - AT).total_seconds()
    )
    monkeypatch.setattr(collection.signal, "signal", lambda *a: None)
    monkeypatch.setattr(collection, "read_token", lambda *a: "test-only-credential")
    reads = []

    def read(base, token, entity):
        reads.append(entity)
        return dict(
            entity_id=entity,
            state="21" if entity == IDS[0] else "5",
            last_reported=clock[0].isoformat(),
            attributes=dict(unit_of_measurement="°C", mode="braking", brake_factor=1),
        )

    monkeypatch.setattr(collection, "read_state", read)

    def restore(shadow, path):
        shadow.observer.model.restore_k(0.04, 25)
        return True

    monkeypatch.setattr(collection, "restore_checkpoint", restore)
    config = dict(
        data_dir=str(tmp_path),
        model_dir=str(MODEL),
        start=AT.isoformat(),
        end=(AT + timedelta(minutes=31)).isoformat(),
        ha_url="http://ha.invalid",
        entities=dict(zip(("indoor", "outdoor", "control"), IDS)),
    )
    writes = []
    original_write = collection.atomic_json

    def write(path, value):
        writes.append(path.name)
        original_write(path, value)

    monkeypatch.setattr(collection, "atomic_json", write)
    collection.collect(config, "unused", "TEST")
    records = [r for f in (tmp_path / "raw").glob("*.gz") for r in recover_records(f)]
    from thermal_observation_evaluate import summarize_shadow

    result = summarize_shadow(records)
    assert result["original_forecast_count"] >= 3
    assert sum(g["evaluated"] for g in result["groups"]) >= 1
    assert not result["integrity_issues"]
    assert set(reads) == set(IDS)
    assert len(reads) <= 3 * 64
    assert calls[1]["trigger"]["entity_id"] == list(IDS)
    assert {c["type"] for c in calls} == {"auth", "subscribe_trigger"}
    assert records[-1]["kind"] == "end"
    assert writes.count("model-checkpoint.json") == 1
    assert writes.count("status.json") <= 9


def test_summary_rejects_future_inputs_and_unqualified_outcomes():
    from thermal_observation_evaluate import summarize_shadow

    obs, events, latest, admitted = shadow()
    for m in range(31):
        feed(obs, latest, admitted, m)
    altered = copy.deepcopy(events)
    for e in altered:
        if e["kind"] == "resolution" and e["status"] == "evaluated":
            e["actual_input"]["attributes"]["restored"] = True
    r = summarize_shadow(altered)
    assert sum(g["evaluated"] for g in r["groups"]) == 0
    assert r["integrity_issues"]["invalid_outcome"] == 1
    altered = copy.deepcopy(events)
    for e in altered:
        if e["kind"] == "forecast":
            e["inputs"]["outdoor"]["received_at"] = (AT + timedelta(days=1)).isoformat()
    r = summarize_shadow(altered)
    assert r["original_forecast_count"] == 0
    assert r["integrity_issues"]["invalid_forecast"] == 6


def test_original_records_keep_control_modes_and_model_revisions_separate():
    from thermal_observation_evaluate import summarize_shadow

    obs, events, latest, admitted = shadow()
    feed(obs, latest, admitted, 0)
    feed(obs, latest, admitted, 1, mode="safe_mode", factor=0)
    feed(obs, latest, admitted, 2, mode="normal", factor=0)
    obs.observer.model.restore_k(0.06, 30)
    feed(obs, latest, admitted, 30, mode="brake_hold", factor=0.8)
    r = summarize_shadow(events)
    assert len(r["groups"]) == 6
    assert {g["mode"] for g in r["groups"]} == {"braking", "brake_hold"}
    assert len({g["model_version"] for g in r["groups"]}) == 2
    assert r["decisions"]["safe_mode"]["without_forecast"] == 1
    assert r["decisions"]["normal"]["without_forecast"] == 1
    assert all(g["evaluated"] == 0 for g in r["groups"])


def test_shadow_calibrates_without_injected_coefficient_after_brake_hold():
    import math

    obs = Shadow(MODEL, entities=IDS)
    obs.boundary(AT - timedelta(seconds=1))
    latest, admitted = {}, {}
    for minute in range(202):
        feed(
            obs,
            latest,
            admitted,
            minute,
            tin=5 + 16 * math.exp(-0.04 * minute / 60),
            mode="brake_hold",
        )
    assert obs.observer.model.pending_samples >= 20
    feed(obs, latest, admitted, 202, tin=19, mode="normal", factor=0)
    assert obs.observer.model.is_valid
    assert obs.observer.model.k == pytest.approx(0.04, abs=0.001)
    assert obs.calibration_origin == "shadow_fit"


def test_unavailable_control_cannot_reuse_cached_braking_attributes():
    obs, events, latest, admitted = shadow()
    feed(obs, latest, admitted, 0)
    at = AT + timedelta(minutes=1)
    raw = dict(
        entity_id=IDS[2],
        state="unavailable",
        last_reported=at.isoformat(),
        attributes={"mode": "braking", "brake_factor": 1},
    )
    accept_reading(latest, admitted, raw, at, entities=IDS)
    obs.update(latest, at)
    assert obs.observer._braking is False
    assert len([e for e in events if e["kind"] == "resolution"]) == 3


def test_checkpoint_is_scoped_to_code_and_selected_inputs(tmp_path):
    from thermal_observation import restore_checkpoint

    obs, _, _, _ = shadow()
    path = tmp_path / "checkpoint.json"
    saved = dict(
        valid=True,
        k=0.04,
        sample_count=25,
        code_sha256=obs.code_sha256,
        entities=list(IDS),
    )
    path.write_text(json.dumps(saved))
    fresh = Shadow(MODEL, entities=IDS)
    assert restore_checkpoint(fresh, path)
    assert fresh.calibration_origin == "restored_checkpoint"
    assert not restore_checkpoint(
        Shadow(MODEL, entities=("sensor.another", *IDS[1:])), path
    )
    saved["code_sha256"] = "old-model"
    path.write_text(json.dumps(saved))
    assert not restore_checkpoint(Shadow(MODEL, entities=IDS), path)


def test_gzip_reader_memory_does_not_scale_with_entire_trial_file(tmp_path):
    import hashlib
    import tracemalloc

    journal = Journal(tmp_path)
    for n in range(8192):
        # Several MiB compressed, composed of independently complete members.
        text = "".join(
            hashlib.sha256(f"{n}:{j}".encode()).hexdigest() for j in range(8)
        )
        journal.add({"n": n, "value": text})
    journal.flush()
    path = next(tmp_path.glob("*.gz"))
    assert path.stat().st_size > 2_000_000
    tracemalloc.start()
    count = sum(1 for _ in recover_records(path))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 8192
    assert peak < 1_000_000


def test_summary_independently_rejects_stale_original_input_metadata():
    from thermal_observation_evaluate import summarize_shadow

    obs, events, latest, admitted = shadow()
    feed(obs, latest, admitted, 0)
    altered = copy.deepcopy(events)
    for e in altered:
        if e["kind"] == "forecast":
            e["inputs"]["indoor"]["ha_last_reported"] = (
                AT - timedelta(hours=1)
            ).isoformat()
            e["prediction"]["origin_at"] = (AT - timedelta(hours=1)).isoformat()
    assert summarize_shadow(altered)["original_forecast_count"] == 0
