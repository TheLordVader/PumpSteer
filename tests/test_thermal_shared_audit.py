"""One passive evidence chain shares actual endpoints across frozen curves."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit

T = datetime(2026, 11, 1, tzinfo=timezone.utc)
IDENTITY = ("inside", "outside")
CANDIDATE = {"loss_per_hour": .03, "command_gain_per_hour": .6, "background_per_hour": .1}


def state(value, at):
    return SimpleNamespace(state=str(value), last_reported=at,
                           attributes={"unit_of_measurement": "°C"})


def issue(audit, at=T, revision="fit-a", candidate=CANDIDATE, command=.5):
    return audit.issue_shared(at, 21, 5, .02, IDENTITY, revision=revision,
                              context_id="segment-a", command=command, mode="normal",
                              candidate=candidate, source_reported=at)


def observe(audit, at, **changes):
    args = dict(command=.5, mode="normal", context_id="segment-a")
    args.update(changes)
    return audit.observe_shared(at, state(20.9, at), IDENTITY, **args)


def test_frozen_curves_score_one_actual_report_at_real_elapsed_time():
    audit = ForecastAudit()
    candidate = dict(CANDIDATE)
    assert issue(audit, candidate=candidate)
    candidate["command_gain_per_hour"] = 20
    at = T + timedelta(minutes=32)
    assert observe(audit, at)
    row = audit.fixed_records[0]
    assert row["observed"] == at.isoformat()
    assert row["model_order"] == ["linear", "bounded", "empirical_command_response", "persistence"]
    assert row["predictions"][0] == pytest.approx(21 - .02 * 16 * 32 / 60)
    assert row["curves"][2]["input"] == pytest.approx(.4)
    assert row["predictions"][-1] == 21
    assert row["physical_measurement_time"] == "unknown"
    assert row["passive_only"] is True
    # Legacy observation hook cannot supply an unqualified shared endpoint.
    audit.observe_fixed(T + timedelta(minutes=60), state(21, T + timedelta(minutes=60)), IDENTITY)
    assert len(audit.fixed_records) == 1


@pytest.mark.parametrize("changes", [{"command": .6}, {"mode": "brake"}, {"context_id": "segment-b"}])
def test_interruption_discards_remaining_horizons_even_if_context_returns(changes):
    audit = ForecastAudit()
    assert issue(audit)
    observe(audit, T + timedelta(minutes=10), **changes)
    assert audit.fixed_pending is None
    assert not observe(audit, T + timedelta(minutes=30))
    assert not audit.fixed_records


def test_unknown_command_can_issue_persistence_before_fit():
    audit = ForecastAudit()
    assert audit.issue_shared(T, 21, 5, None, IDENTITY, revision="unfitted", context_id="a",
                              mode="normal", command=None)
    assert audit.observe_shared(T + timedelta(minutes=30), state(20.9, T + timedelta(minutes=30)),
                                IDENTITY, command=None, mode="normal", context_id="a")
    assert audit.fixed_records[0]["model_order"] == ["persistence"]


def test_summary_never_pools_revisions_or_legacy_qualification():
    audit = ForecastAudit()
    for index, revision in enumerate(("a", "b")):
        at = T + timedelta(days=index)
        assert issue(audit, at, revision)
        observe(audit, at + timedelta(minutes=30))
        audit.fixed_pending = None
    summary = audit.fixed_summary()
    assert summary["30"]["count"] == 0
    assert summary["shared_by_revision"]["a"]["30"]["count"] == 1
    assert summary["shared_by_revision"]["b"]["30"]["count"] == 1
    assert summary["shared_by_revision"]["a"]["30"]["passive_only"]


def test_store_round_trip_completed_only_revalidates_frozen_curve_predictions():
    audit = ForecastAudit()
    assert issue(audit)
    observe(audit, T + timedelta(minutes=31))
    payload = audit.export_plan()
    assert not payload["fixed_v1"]
    restored = ForecastAudit()
    restored.restore_plan(payload, T + timedelta(minutes=40))
    assert restored.fixed_pending is None
    assert restored.fixed_records == audit.fixed_records
    payload["fixed_v2"][0]["predictions"][2] += 1
    restored.restore_plan(payload, T + timedelta(minutes=40))
    assert not restored.fixed_records


def test_missing_stale_and_source_changed_reports_are_not_filled():
    audit = ForecastAudit()
    assert issue(audit)
    audit.observe_shared(T + timedelta(minutes=36), state(20, T), IDENTITY,
                         command=.5, mode="normal", context_id="segment-a")
    assert not audit.fixed_records
    audit.observe_shared(T + timedelta(minutes=60), state(20, T + timedelta(minutes=60)),
                         ("changed", "outside"), command=.5, mode="normal", context_id="segment-a")
    assert audit.fixed_pending is None
