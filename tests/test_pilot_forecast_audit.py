"""Only actually issued, causally qualified forecasts may enter error statistics."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit

T = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)


def state(value, at, changed=T, unit="°C"):
    return SimpleNamespace(
        state=str(value),
        last_reported=at,
        last_changed=changed,
        attributes={"unit_of_measurement": unit},
    )


def test_later_real_report_is_paired_with_issued_forecast():
    audit = ForecastAudit()
    audit.issue(T, 20.0, 5.0, 1.0, 0.02, 0.4, 0.1, "revision-a")
    for minute in (5, 10, 15):
        due = T + timedelta(minutes=minute)
        audit.observe(
            due,
            state(20.1 if minute == 15 else 20, due if minute == 15 else T),
            state(5, due),
            state("on", due, unit=None),
        )
    summary = audit.summary("revision-a")
    assert summary["count"] == 1
    assert summary["mae"] == 0.05
    assert summary["persistence_mae"] == 0.1
    assert audit.summary("other")["count"] == 0


def test_changed_or_missing_context_cannot_supply_outcomes():
    audit = ForecastAudit()
    audit.issue(T, 20.0, 5.0, 1.0, 0.02, 0.4, 0.1, "revision-a")
    due = T + timedelta(minutes=15)
    audit.observe(
        due, state(20.1, due), state(4, due, changed=due), state("on", due, unit=None)
    )
    assert audit.summary("revision-a")["count"] == 0
    assert audit.pending is None


def test_drift_only_uses_same_revision_and_paired_outcomes():
    audit = ForecastAudit()
    for index in range(5):
        at = T + timedelta(minutes=20 * index)
        audit.issue(at, 20.0, 5.0, 1.0, 0.02, 0.4, 0.1, "a")
        for minute in (5, 10, 15):
            now = at + timedelta(minutes=minute)
            audit.observe(
                now,
                state(20, now, changed=at),
                state(5, now, changed=at),
                state("on", now, changed=at, unit=None),
            )
    assert audit.summary("a")["status"] == "degraded"
    assert audit.summary("b")["status"] == "insufficient_outcomes"


def test_restore_drops_pending_and_invalid_records():
    audit = ForecastAudit()
    audit.issue(T, 20, 5, 1, 0.02, 0.4, 0.1, "a")
    restored = ForecastAudit()
    restored.restore(
        {"records": [{"revision": "a", "error": float("nan"), "persistence_error": 0}]},
        T,
    )
    assert restored.pending is None
    assert restored.summary("a")["count"] == 0


def test_source_change_at_outcome_never_creates_a_pair():
    audit = ForecastAudit()
    audit.issue(
        T, 20, 5, 1, 0.02, 0.4, 0.1, "a", source_identity=(("sensor.room_a",), None)
    )
    for minute in (5, 10, 15):
        at = T + timedelta(minutes=minute)
        inside = state(20, at if minute == 15 else T)
        inside.attributes["source_temperatures_used"] = {
            ("sensor.room_b" if minute == 15 else "sensor.room_a"): 20
        }
        audit.observe(at, inside, state(5, at), state("on", at, unit=None))
    assert audit.summary("a")["count"] == 0
    assert audit.pending is None


def test_irregular_observation_uses_actual_horizon_without_interpolation():
    audit = ForecastAudit()
    audit.issue(T, 20, 5, 1, 0.02, 0.4, 0.1, "a")
    for minute in (5, 10, 15, 17):
        now = T + timedelta(minutes=minute)
        indoor = state(20.04 if minute == 17 else 20, now if minute == 17 else T)
        audit.observe(now, indoor, state(5, now), state("on", now, unit=None))
    assert audit.summary("a")["count"] == 1
    assert audit.records[0]["horizon_seconds"] == 1020
    assert audit.records[0]["predicted"] == pytest.approx(20 + 0.2 * 17 / 60)


def test_issued_forecast_is_persisted_but_never_reused_after_restart():
    audit = ForecastAudit()
    audit.issue(T, 20, 5, 1, 0.02, 0.4, 0.1, "a")
    saved = audit.export_state()
    assert saved["pending"]["issued"] == T.isoformat()
    assert saved["pending"]["rate"] == pytest.approx(0.2)
    resumed = ForecastAudit()
    resumed.restore(saved, T + timedelta(minutes=5))
    assert resumed.pending is None


def _qualified_record(at):
    audit = ForecastAudit()
    assert audit.issue(at, 20, 5, 1, 0.02, 0.4, 0.1, "a")
    for minute in (5, 10, 15):
        now = at + timedelta(minutes=minute)
        audit.observe(
            now,
            state(20, now, changed=at),
            state(5, now, changed=at),
            state("on", now, changed=at, unit=None),
        )
    return audit.records[0]


def test_restore_never_counts_copied_outcomes_as_independent_evidence():
    record = _qualified_record(T)
    restored = ForecastAudit()
    restored.restore(
        {"records": [dict(record) for _ in range(5)]}, T + timedelta(hours=1)
    )
    assert restored.summary("a")["count"] == 1
    assert restored.summary("a")["status"] == "insufficient_outcomes"


def test_restore_discards_conflicting_outcome_but_keeps_other_pairs():
    record = _qualified_record(T)
    conflict = {**record, "actual": 20.5}
    other = _qualified_record(T + timedelta(minutes=20))
    restored = ForecastAudit()
    restored.restore(
        {"records": [record, conflict, record, other]}, T + timedelta(hours=1)
    )
    assert restored.summary("a")["count"] == 1
    assert restored.records[0]["issued"] == other["issued"]


def test_restore_discards_overlapping_forecasts_and_preserves_ordered_pairs():
    first = _qualified_record(T)
    overlapping = _qualified_record(T + timedelta(minutes=5))
    later = _qualified_record(T + timedelta(minutes=30))
    last = _qualified_record(T + timedelta(minutes=50))
    restored = ForecastAudit()
    restored.restore(
        {"records": [last, first, overlapping, later]}, T + timedelta(hours=2)
    )
    assert [r["issued"] for r in restored.records] == [later["issued"], last["issued"]]


def test_restore_rejects_multiple_outcomes_for_one_forecast():
    first = _qualified_record(T)
    different_outcome = dict(first)
    different_outcome["observed"] = (T + timedelta(minutes=20)).isoformat()
    different_outcome["predicted"] = first["initial"] + first["rate_per_hour"] / 3
    restored = ForecastAudit()
    restored.restore({"records": [first, different_outcome]}, T + timedelta(hours=1))
    assert restored.summary("a")["count"] == 0
