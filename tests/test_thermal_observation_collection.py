"""Report identity and finite, recoverable collection contracts."""

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from thermal_observation import new_report

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def reading(value, at=NOW):
    return {
        "state": str(value),
        "last_reported": at.isoformat(),
        "attributes": {"unit_of_measurement": "°C"},
    }


def test_identical_value_is_new_only_when_ha_report_time_advances():
    old = reading(21)
    assert not new_report(old, reading(21), NOW + timedelta(seconds=30))
    assert new_report(
        old, reading(21, NOW + timedelta(seconds=20)), NOW + timedelta(seconds=30)
    )
    assert not new_report(old, reading(20), NOW + timedelta(seconds=30))


def test_future_missing_and_reordered_report_times_are_not_promoted():
    old = reading(21)
    assert not new_report(old, reading(20, NOW - timedelta(seconds=1)), NOW)
    assert not new_report(old, reading(20, NOW + timedelta(seconds=1)), NOW)
    assert not new_report(old, {"state": "20"}, NOW)


def test_gzip_recovery_keeps_completed_members_after_process_restart(tmp_path):
    from thermal_observation import Journal, recover_records

    first = Journal(tmp_path)
    first.add({"n": 1})
    first.flush()
    old_file = next(tmp_path.glob("*.gz"))
    old_file.write_bytes(old_file.read_bytes()[:-4])
    restarted = Journal(tmp_path)
    restarted.add({"n": 2})
    restarted.flush()
    recovered = [
        record
        for path in sorted(tmp_path.glob("*.gz"))
        for record in recover_records(path)
    ]
    assert recovered == [{"n": 2}]


def test_storage_limit_preserves_existing_raw_data(tmp_path):
    import pytest
    from thermal_observation import Journal

    journal = Journal(tmp_path, limit=10)
    journal.add({"observation": 1})
    with pytest.raises(RuntimeError, match="storage_limit"):
        journal.flush()
    assert not list(tmp_path.glob("*.gz"))


def test_entity_removal_breaks_availability_without_inventing_report_time():
    from thermal_observation import trigger_reading

    raw = trigger_reading(
        {
            "event": {
                "variables": {
                    "trigger": {"entity_id": "sensor.pumpsteer", "to_state": None}
                }
            }
        }
    )
    assert raw["entity_id"] == "sensor.pumpsteer"
    assert raw["state"] is None
    assert not new_report(None, raw, NOW)


def test_queued_older_ws_report_cannot_replace_newer_rest_observation():
    from thermal_observation import accept_reading, TEMPERATURES

    entity = TEMPERATURES[0]
    latest, admitted = {}, {}
    new = {**reading(21, NOW + timedelta(seconds=30)), "entity_id": entity}
    old = {**reading(20, NOW + timedelta(seconds=20)), "entity_id": entity}
    assert (
        accept_reading(latest, admitted, new, NOW + timedelta(seconds=40))[0]
        == "report"
    )
    assert (
        accept_reading(latest, admitted, old, NOW + timedelta(seconds=40))[0]
        == "reordered"
    )
    assert (
        accept_reading(latest, admitted, new, NOW + timedelta(seconds=50))[0]
        == "duplicate"
    )
    assert latest[entity]["state"] == "21"


def test_rejected_future_temperature_remains_quarantined_after_clock_catches_up():
    from thermal_observation import accept_reading, Shadow, TEMPERATURES, CONTROL

    latest, admitted = {}, {}
    future = {**reading(21, NOW + timedelta(seconds=10)), "entity_id": TEMPERATURES[0]}
    assert accept_reading(latest, admitted, future, NOW)[0] == "unqualified"
    later = NOW + timedelta(seconds=20)
    accept_reading(
        latest, admitted, {**reading(5, later), "entity_id": TEMPERATURES[4]}, later
    )
    accept_reading(
        latest,
        admitted,
        {
            **reading(5, later),
            "entity_id": CONTROL,
            "attributes": {"mode": "braking", "brake_factor": 1},
        },
        later,
    )
    shadow = Shadow(Path(__file__).parents[1] / "custom_components/pumpsteer")
    shadow.boundary(NOW - timedelta(seconds=1))
    assert shadow.update(latest, later)["reason"] == "unqualified_temperature_report"
    assert shadow.observer.model.pending_samples == 0


def test_missing_secondary_entity_is_absence_not_a_global_connection_failure():
    from urllib.error import HTTPError
    from thermal_observation import read_state, TEMPERATURES

    def absent(request, timeout):
        raise HTTPError(request.full_url, 404, "Not found", {}, None)

    raw = read_state(
        "http://ha.invalid", "test-credential", TEMPERATURES[3], opener=absent
    )
    assert raw["entity_id"] == TEMPERATURES[3]
    assert raw["state"] is None
    assert not new_report(None, raw, NOW)


def test_rejected_future_control_cannot_activate_learning_after_clock_catches_up():
    from thermal_observation import accept_reading, Shadow, TEMPERATURES, CONTROL

    latest, admitted = {}, {}
    future = {
        **reading(5, NOW + timedelta(seconds=10)),
        "entity_id": CONTROL,
        "attributes": {"mode": "braking", "brake_factor": 1},
    }
    assert accept_reading(latest, admitted, future, NOW)[0] == "unqualified"
    later = NOW + timedelta(seconds=20)
    for entity, value in ((TEMPERATURES[0], 21), (TEMPERATURES[4], 5)):
        accept_reading(
            latest, admitted, {**reading(value, later), "entity_id": entity}, later
        )
    shadow = Shadow(Path(__file__).parents[1] / "custom_components/pumpsteer")
    shadow.boundary(NOW - timedelta(seconds=1))
    shadow.update(latest, later)
    assert not shadow.observer._braking
    assert not shadow.was_braking


def test_source_signature_changes_for_composition_not_temperature_values():
    from thermal_observation import source_signature, TEMPERATURES

    latest = {
        TEMPERATURES[0]: {
            "attributes": {"source_temperatures_used": {"upstairs": 21, "office": 20}}
        },
        TEMPERATURES[4]: {"attributes": {"selected_source": "vp2"}},
    }
    original = source_signature(latest)
    latest[TEMPERATURES[0]]["attributes"]["source_temperatures_used"]["office"] = 19
    assert source_signature(latest) == original
    latest[TEMPERATURES[4]]["attributes"]["selected_source"] = "switchbot"
    assert source_signature(latest) != original


def test_invalid_checkpoint_cannot_prevent_safe_restart(tmp_path):
    from thermal_observation import restore_checkpoint, Shadow

    shadow = Shadow(Path(__file__).parents[1] / "custom_components/pumpsteer")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"valid": true,')
    assert not restore_checkpoint(shadow, checkpoint)
    assert not shadow.observer.model.is_valid


def test_trial_conversion_uses_availability_time_and_original_report_time():
    from thermal_observation_evaluate import to_history
    from thermal_observation import CONTROL, TEMPERATURES

    records = [
        {"kind": "boundary", "received_at": NOW.isoformat()},
        {
            "kind": "seed",
            "received_at": (NOW + timedelta(seconds=1)).isoformat(),
            "value": {
                **reading(21, NOW - timedelta(minutes=1)),
                "entity_id": TEMPERATURES[0],
            },
        },
        {
            "kind": "report",
            "received_at": (NOW + timedelta(seconds=2)).isoformat(),
            "value": {
                **reading(5, NOW),
                "entity_id": CONTROL,
                "attributes": {"mode": "braking", "brake_factor": 1},
            },
        },
        {"kind": "tick", "received_at": (NOW + timedelta(seconds=3)).isoformat()},
        {
            "kind": "unqualified",
            "received_at": (NOW + timedelta(seconds=4)).isoformat(),
            "value": {"entity_id": TEMPERATURES[4], "state": None, "attributes": {}},
        },
    ]
    data, counts = to_history(records, NOW, NOW + timedelta(minutes=1))
    indoor = data["series"][TEMPERATURES[0]][0]
    assert indoor["updated"] == (NOW + timedelta(seconds=1)).timestamp()
    assert indoor["reported"] == (NOW - timedelta(minutes=1)).timestamp()
    assert data["series"][TEMPERATURES[4]][0]["quarantined"] is True
    assert data["series"][CONTROL][0]["reported"] == NOW.timestamp()
    assert counts["report"] == 1


def test_trial_cleanup_cannot_remove_raw_data_or_other_services(tmp_path):
    from thermal_observation_service import cleanup, COLLECTOR, TIMER, FINISH

    units = tmp_path / "units"
    units.mkdir()
    for name in (COLLECTOR, TIMER, FINISH, "unrelated.service"):
        (units / name).write_text("unit")
    raw = tmp_path / "raw.jsonl.gz"
    raw.write_bytes(b"preserved")
    credential = tmp_path / "credential"
    credential.write_text("test-value")
    calls = []
    cleanup(units, credential, run=lambda argv, **kw: calls.append(argv))
    assert (units / "unrelated.service").exists()
    assert raw.read_bytes() == b"preserved"
    assert not credential.exists()
    assert {p.name for p in units.iterdir()} == {"unrelated.service"}
    assert all("unrelated.service" not in call for call in calls)


def test_trial_units_pin_deadline_and_do_not_restart_after_success():
    from thermal_observation_service import (
        units,
        DATA,
        BUNDLE,
        COLLECTOR,
        TIMER,
        FINISH,
    )

    result = units(BUNDLE, DATA, datetime(2026, 10, 25, 12, tzinfo=timezone.utc))
    assert "Restart=on-failure" in result[COLLECTOR]
    assert "OnCalendar=2026-10-25 12:00:00 UTC" in result[TIMER]
    assert "Persistent=yes" in result[TIMER]
    assert "--cleanup" in result[FINISH]
    assert "PrivateNetwork=yes" in result[FINISH]


def test_trial_legacy_cadence_ignores_extra_temperature_ticks():
    from thermal_observation_evaluate import to_history
    from thermal_observation import CONTROL, TEMPERATURES

    records = [{"kind": "boundary", "received_at": NOW.isoformat()}]
    for second, entity in (
        (1, CONTROL),
        (10, TEMPERATURES[0]),
        (20, TEMPERATURES[0]),
        (30, CONTROL),
    ):
        at = NOW + timedelta(seconds=second)
        records += [
            {
                "kind": "report",
                "received_at": at.isoformat(),
                "value": {
                    **reading(21, at),
                    "entity_id": entity,
                    "attributes": {"mode": "braking", "brake_factor": 1},
                },
            },
            {"kind": "tick", "received_at": at.isoformat()},
        ]
    data, _ = to_history(records, NOW, NOW + timedelta(minutes=1))
    assert [row["legacy_cycle"] for row in data["series"][CONTROL]] == [
        True,
        False,
        False,
        True,
    ]


def test_failed_evaluation_retains_bounded_retry_and_removes_credential(tmp_path):
    from thermal_observation_service import cleanup, COLLECTOR, TIMER, FINISH

    units = tmp_path / "units"
    units.mkdir()
    for name in (COLLECTOR, TIMER, FINISH):
        (units / name).write_text("unit")
    credential = tmp_path / "credential"
    credential.write_text("test-value")
    cleanup(units, credential, run=lambda *args, **kw: None, completed=False)
    assert not credential.exists()
    assert (units / COLLECTOR).exists()
    assert (units / FINISH).exists()
    assert (units / TIMER).exists()


def test_early_finalizer_start_cannot_stop_collection_or_remove_credentials(
    tmp_path, monkeypatch
):
    import thermal_observation_service as service

    calls = []
    monkeypatch.setattr(service, "cleanup", lambda **kw: calls.append(kw))
    config = {"data_dir": str(tmp_path), "end": "2099-01-01T00:00:00+00:00"}
    service.cleanup_result(config, "exec-condition")
    assert calls == []
    assert not list(tmp_path.iterdir())


def test_backward_clock_records_cannot_create_unsorted_replay_inputs():
    from thermal_observation_evaluate import to_history
    from thermal_observation import CONTROL, TEMPERATURES

    records = []
    for second in (20, 10, 15, 30):
        at = NOW + timedelta(seconds=second)
        records += [
            {
                "kind": "report",
                "received_at": at.isoformat(),
                "value": {
                    **reading(21, at),
                    "entity_id": TEMPERATURES[0],
                },
            },
            {
                "kind": "report",
                "received_at": at.isoformat(),
                "value": {
                    **reading(21, at),
                    "entity_id": CONTROL,
                    "attributes": {"mode": "braking", "brake_factor": 1},
                },
            },
            {"kind": "tick", "received_at": at.isoformat()},
        ]
    data, events = to_history(records, NOW, NOW + timedelta(minutes=1))
    assert [r["updated"] for r in data["series"][TEMPERATURES[0]]] == [
        (NOW + timedelta(seconds=n)).timestamp() for n in (20, 30)
    ]
    assert events["clock_overlap_discarded"] == 6
    assert len(data["runs"]) == 1


def test_stop_calendar_never_rounds_deadline_down_before_finalizer_condition():
    from thermal_observation_service import units, BUNDLE, DATA, TIMER

    deadline = datetime(2026, 10, 25, 12, 0, 0, 999999, tzinfo=timezone.utc)
    assert "OnCalendar=2026-10-25 12:00:01 UTC" in units(BUNDLE, DATA, deadline)[TIMER]
