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


def test_late_real_endpoint_scores_at_actual_time_and_restores():
    audit = ForecastAudit()
    assert issue(audit)
    at = T + timedelta(minutes=37.5)
    assert observe(audit, at)
    row = audit.fixed_records[0]
    assert row["horizon"] == 30 and row["elapsed_minutes"] == 37.5
    assert row["predictions"][0] == pytest.approx(21 - 0.02 * 16 * 37.5 / 60)
    restored = ForecastAudit()
    restored.restore_plan(audit.export_plan(), at)
    assert restored.fixed_records == audit.fixed_records


def test_cancel_releases_blackout_but_cannot_reissue_same_report():
    audit = ForecastAudit()
    assert issue(audit)
    observe(audit, T + timedelta(minutes=2), command=0.6)
    assert audit.fixed_pending is None
    assert not audit.issue_shared(
        T + timedelta(minutes=3),
        21,
        5,
        0.02,
        IDENTITY,
        revision="fit-a",
        context_id="segment-a",
        command=0.6,
        mode="normal",
        candidate=CANDIDATE,
        source_reported=T,
    )
    assert issue(audit, T + timedelta(minutes=4))
    assert audit.fixed_summary()["shared_diagnostics"]["counts"]["cancelled"] == 1


def test_one_late_observation_is_not_two_independent_horizons():
    audit = ForecastAudit()
    assert issue(audit)
    at = T + timedelta(minutes=70)
    assert observe(audit, at)
    assert len(audit.fixed_records) == 1 and audit.fixed_records[0]["horizon"] == 60
    assert not observe(audit, at)
    assert (
        audit.fixed_summary()["shared_diagnostics"]["counts"]["unavailable_horizons"]
        == 1
    )
    assert observe(audit, T + timedelta(minutes=123))
    assert [r["horizon"] for r in audit.fixed_records] == [60, 120]


def test_deadline_and_cancellation_diagnostics_are_bounded_and_persisted():
    audit = ForecastAudit()
    for i in range(100):
        t = T + timedelta(minutes=10 * i)
        assert issue(audit, t)
        audit.cancel_shared("safe_mode", t + timedelta(minutes=1))
    payload = audit.export_plan()
    diag = payload["shared_audit"]
    assert len(diag["events"]) <= 32
    assert diag["counts"]["issued"] == 100 and diag["counts"]["cancelled"] == 100
    restored = ForecastAudit()
    restored.restore_plan(payload, T + timedelta(minutes=1000))
    assert restored.fixed_pending is None
    assert restored.fixed_summary()["shared_diagnostics"]["counts"] == diag["counts"]
    assert issue(restored, T + timedelta(minutes=1001))


def test_expired_curve_cannot_score_very_late_report():
    audit = ForecastAudit()
    assert issue(audit)
    assert not observe(audit, T + timedelta(minutes=126))
    assert not audit.fixed_records and audit.fixed_pending is None
    assert audit.fixed_summary()["shared_diagnostics"]["counts"]["expired"] == 1


def test_restore_cancels_pending_and_prevents_reusing_its_report():
    audit = ForecastAudit()
    assert issue(audit)
    payload = audit.export_plan()
    restored = ForecastAudit()
    restored.restore_plan(payload, T + timedelta(minutes=5))
    assert restored.fixed_pending is None
    diag = restored.shared_diagnostics()
    assert diag["counts"]["cancelled"] == 1
    assert diag["events"][-1]["reason"] == "restart_pending_not_resumed"
    assert not restored.issue_shared(
        T + timedelta(minutes=4),
        21,
        5,
        0.02,
        IDENTITY,
        revision="fit-a",
        context_id="segment-a",
        command=0.5,
        mode="normal",
        candidate=CANDIDATE,
        source_reported=T,
    )


def test_equivalent_timezones_do_not_turn_an_old_report_into_a_new_one():
    audit = ForecastAudit()
    assert issue(audit)
    audit.cancel_shared("command_changed", T + timedelta(minutes=1))
    report = T.astimezone(timezone(timedelta(hours=1)))
    assert not audit.issue_shared(
        T + timedelta(minutes=2),
        21,
        5,
        0.02,
        IDENTITY,
        revision="fit-a",
        context_id="segment-a",
        command=0.5,
        mode="normal",
        candidate=CANDIDATE,
        source_reported=report,
    )


def test_large_diagnostics_remain_restorable_and_old_payload_resets_them():
    audit = ForecastAudit()
    identity = ("a" * 2048, "b" * 2048)
    for i in range(40):
        at = T + timedelta(minutes=i * 10)
        assert audit.issue_shared(
            at,
            21,
            5,
            None,
            identity,
            revision="r",
            context_id="c",
            command=None,
            mode="normal",
        )
        audit.cancel_shared("safe_mode", at)
    payload = audit.export_plan()
    restored = ForecastAudit()
    restored.restore_plan(payload, T + timedelta(minutes=500))
    assert restored.shared_diagnostics()["counts"]["issued"] == 40
    restored.restore_plan({}, T + timedelta(minutes=501))
    assert restored.shared_diagnostics()["counts"]["issued"] == 0


def test_restore_rejects_overlapping_issuances_for_the_same_actual_endpoint():
    first = ForecastAudit()
    second = ForecastAudit()
    assert issue(first)
    assert issue(second, T + timedelta(minutes=1))
    end = T + timedelta(minutes=32)
    assert observe(first, end)
    assert observe(second, end)
    payload = first.export_plan()
    payload["fixed_v2"] += second.export_plan()["fixed_v2"]
    restored = ForecastAudit()
    restored.restore_plan(payload, end)
    assert not restored.fixed_records


def test_switching_sources_back_cannot_reissue_an_old_report():
    audit = ForecastAudit()
    assert issue(audit)
    audit.cancel_shared("source_changed", T + timedelta(minutes=1))
    assert audit.issue_shared(
        T + timedelta(minutes=2),
        21,
        5,
        None,
        ("other", "outside"),
        revision="persistence",
        context_id="segment-b",
        command=None,
        mode="normal",
    )
    audit.cancel_shared("source_changed", T + timedelta(minutes=3))
    assert not audit.issue_shared(
        T + timedelta(minutes=4),
        21,
        5,
        0.02,
        IDENTITY,
        revision="fit-a",
        context_id="segment-c",
        command=0.5,
        mode="normal",
        candidate=CANDIDATE,
        source_reported=T,
    )
