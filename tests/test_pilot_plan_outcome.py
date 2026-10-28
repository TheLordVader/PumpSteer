"""Only actual later HA reports may be paired with issued passive plans."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.pumpsteer.pilot_forecast_audit import ForecastAudit

START = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


def reading(value, at, *, source="sensor.indoor", restored=False):
    return SimpleNamespace(
        entity_id=source,
        state=str(value),
        last_reported=at,
        last_changed=at,
        attributes={"unit_of_measurement": "°C", "restored": restored},
    )


def issue(
    audit, *, at=START, source="sensor.indoor", revision="model-a", initial_context=None
):
    return audit.issue_plan(
        forecast_start=at,
        issued_at=at + timedelta(seconds=30),
        temperatures=[20.0 + 0.05 * i for i in range(9)],
        revision=revision,
        status="hypothetical_unvalidated",
        source_entity=source,
        source_identity=(None, None),
        source_reported=at,
        initial_context=initial_context,
    )


def test_issued_context_is_frozen_and_preserved_in_store_after_settings_change():
    context = {
        "indoor_temperature": 20.0,
        "outdoor_temperature": -8.0,
        "target_temperature": 21.0,
        "comfort_floor": 20.0,
        "comfort_ceiling": 22.0,
        "virtual_temperature_command": -7.0,
        "physical_measurement_time": "unknown",
        "controller": {
            "pi_integral": 0.1,
            "pi_last_error": -0.2,
            "pi_last_time": START.isoformat(),
            "brake_ramp": 0.5,
            "brake_last_time": START.isoformat(),
            "brake_last_expensive_time": None,
            "preheat_ramp": 0.0,
            "preheat_last_time": None,
            "pi_precool_paused": False,
            "previous_aggressiveness": 2,
            "was_braking_last_cycle": True,
            "bridge_short_dip_active": False,
        },
    }
    audit = ForecastAudit()
    import copy

    future = copy.deepcopy(context)
    future["controller"]["pi_last_time"] = (START + timedelta(seconds=1)).isoformat()
    assert not issue(audit, initial_context=future)
    assert issue(audit, initial_context=context)
    context["target_temperature"] = 25.0
    for minute in range(1, 31):
        now = START + timedelta(minutes=minute)
        audit.observe_plan(now, reading(20, now), "sensor.indoor", (None, None))
    restored = ForecastAudit()
    restored.restore_plan(audit.export_plan(), now)
    saved = restored.plan_records[0]["initial_context"]
    assert saved["target_temperature"] == 21.0
    assert saved["outdoor_temperature"] == -8.0
    assert saved["virtual_temperature_command"] == -7.0
    assert saved["physical_measurement_time"] == "unknown"
    assert saved["controller"]["brake_ramp"] == 0.5
    assert saved["controller"]["pi_last_time"] == START.isoformat()
    assert restored.plan_summary()["latest_pair"]["initial_context"] == saved

    for key, bad in (
        ("brake_ramp", float("nan")),
        ("pi_last_time", "2026-10-11T08:00:00"),
        ("pi_last_time", (START + timedelta(days=1)).isoformat()),
        ("was_braking_last_cycle", "true"),
    ):
        corrupted = copy.deepcopy(audit.export_plan())
        corrupted["records"][0]["initial_context"]["controller"][key] = bad
        restored.restore_plan(corrupted, now)
        assert not restored.plan_records


def test_context_initial_temperature_must_match_published_grid():
    context = {
        "indoor_temperature": 20.006,
        "outdoor_temperature": 5.0,
        "target_temperature": 21.0,
        "comfort_floor": 20.0,
        "comfort_ceiling": 22.0,
        "virtual_temperature_command": 4.0,
    }
    audit = ForecastAudit()
    assert not issue(audit, initial_context=context)
    context["indoor_temperature"] = 20.004
    assert issue(audit, initial_context=context)
    for minute in range(1, 31):
        now = START + timedelta(minutes=minute)
        audit.observe_plan(now, reading(20, now), "sensor.indoor", (None, None))
    corrupted = audit.export_plan()
    corrupted["records"][0]["initial_context"]["indoor_temperature"] = 20.006
    audit.restore_plan(corrupted, now)
    assert not audit.plan_records


def test_real_observation_pairs_using_forecast_time_and_real_issue_time():
    audit = ForecastAudit()
    assert issue(audit)
    # Intermediate cycles do not invent indoor reports.
    for minute in range(1, 30):
        audit.observe_plan(
            START + timedelta(minutes=minute),
            reading(20, START),
            "sensor.indoor",
            (None, None),
        )
    observed = START + timedelta(minutes=30, seconds=15)
    audit.observe_plan(observed, reading(20.3, observed), "sensor.indoor", (None, None))
    row = audit.plan_records[-1]
    assert row["predicted"] == pytest.approx(20.1)
    assert row["observed"] == observed.isoformat()
    assert row["time_error_seconds"] == 15
    assert row["lead_seconds"] == 1785
    assert row["error"] == pytest.approx(-0.2)
    assert row["persistence_error"] == pytest.approx(-0.3)
    assert row["kind"] == "unexecuted_scenario_residual"
    assert audit.plan_summary()["physical_measurement_time"] == "unknown"


def test_missing_or_changed_source_and_gap_invalidate_pending():
    audit = ForecastAudit()
    assert issue(audit)
    now = START + timedelta(minutes=1)
    audit.observe_plan(
        now, reading(20, now, source="sensor.other"), "sensor.other", (None, None)
    )
    assert not audit.plan_pending
    assert audit.plan_summary()["unverifiable"] == 3
    assert issue(audit, at=now)
    audit.observe_plan(
        now + timedelta(minutes=6), reading(20, now), "sensor.indoor", (None, None)
    )
    assert not audit.plan_pending
    assert audit.plan_summary()["unverifiable"] == 6


def test_recovery_does_not_restore_pending_as_fresh():
    audit = ForecastAudit()
    assert issue(audit)
    restored = ForecastAudit()
    restored.restore_plan(audit.export_plan(), START + timedelta(minutes=5))
    assert not restored.plan_pending
    assert restored.plan_summary()["unverifiable"] >= 3


def test_only_current_source_quality_is_shown_before_and_after_restart():
    audit = ForecastAudit()
    assert issue(audit)
    for minute in range(1, 31):
        now = START + timedelta(minutes=minute)
        audit.observe_plan(now, reading(20, now), "sensor.indoor", (None, None))
    old_identity = (None, None, None)
    new_identity = (None, None, "sensor.other_outside")
    assert audit.plan_summary("sensor.indoor", old_identity)["horizons_minutes"]
    assert not audit.plan_summary("sensor.indoor", new_identity)["horizons_minutes"]
    assert not audit.plan_summary("sensor.indoor", None)["horizons_minutes"]
    restored = ForecastAudit()
    restored.restore_plan(audit.export_plan(), now)
    assert restored.plan_summary("sensor.indoor", old_identity)["horizons_minutes"]
    assert not restored.plan_summary("sensor.indoor", new_identity)["horizons_minutes"]


def test_source_change_at_same_temperature_during_forecast_does_not_issue_audit(
    monkeypatch,
):
    from custom_components.pumpsteer import forecast
    from test_brake_provenance import _cycle
    from test_control_replay_cache import _ready_sensor

    sensor, states, _ = _ready_sensor(monkeypatch)
    states["sensor.indoor"].attributes["source_temperatures_used"] = {"sensor.a": 20}
    original = forecast.async_build_forecast

    async def changed_source(*args, **kwargs):
        result = await original(*args, **kwargs)
        states["sensor.indoor"].attributes["source_temperatures_used"] = {
            "sensor.b": 20
        }
        return result

    monkeypatch.setattr(forecast, "async_build_forecast", changed_source)
    _cycle(sensor, monkeypatch, 0)
    assert not sensor._plan_audit.plan_pending
    assert sensor._pilot_plan["plan_outcome_reason"] == "source_changed_during_cycle"


def test_shared_report_scores_each_issued_plan_horizon_once_and_survives_restore():
    audit = ForecastAudit()
    assert issue(audit)
    for minute in range(1, 151):
        now = START + timedelta(minutes=minute)
        report = (
            reading(20.2, now)
            if minute % 30 == 0
            else reading(20, START + timedelta(minutes=(minute // 30) * 30))
        )
        audit.observe_plan(now, report, "sensor.indoor", (None, None))
        if minute % 30 == 0:
            assert issue(audit, at=now)
    at_120 = [
        row
        for row in audit.plan_records
        if row["observed"] == (START + timedelta(minutes=120)).isoformat()
    ]
    assert {row["horizon_minutes"] for row in at_120} == {30, 60, 120}
    summary = audit.plan_summary()
    assert (
        summary["count_semantics"] == "forecast_outcome_pairs_not_independent_reports"
    )
    payload = audit.export_plan()
    payload["records"].append(dict(payload["records"][0]))
    restored = ForecastAudit()
    restored.restore_plan(payload, now)
    assert len(restored.plan_records) == len(audit.plan_records)
    payload["records"][-1] = {**payload["records"][-1], "actual": 21.1}
    restored.restore_plan(payload, now)
    assert len(restored.plan_records) == len(audit.plan_records) - 1


def test_restart_and_next_save_preserve_prior_indoor_source():
    import asyncio
    from test_brake_provenance import _sensor

    audit = ForecastAudit()
    assert issue(audit)
    for minute in range(1, 31):
        now = START + timedelta(minutes=minute)
        audit.observe_plan(now, reading(20, now), "sensor.indoor", (None, None))
    restored = ForecastAudit()
    restored.restore_plan(audit.export_plan(), now)
    assert [row["source_entity"] for row in restored.plan_records] == ["sensor.indoor"]
    assert issue(restored, at=now, source="sensor.new")
    for minute in range(31, 61):
        now = START + timedelta(minutes=minute)
        restored.observe_plan(now, reading(20, now), "sensor.new", (None, None))

    class Store:
        payload = None

        def async_delay_save(self, make, delay):
            self.payload = make()

    sensor, _ = _sensor()
    sensor._pilot_store = Store()
    sensor._plan_audit = restored
    asyncio.run(sensor._save_pilot_store())
    assert {
        row["source_entity"]
        for row in sensor._pilot_store.payload["plan_audit"]["records"]
    } == {"sensor.indoor", "sensor.new"}
    assert (
        restored.plan_summary("sensor.new", (None, None))["horizons_minutes"]["30"][
            "count"
        ]
        == 1
    )


def test_source_identity_is_preserved_and_never_mixed_after_restore():
    audit = ForecastAudit()
    identity = (("sensor.room_a",), "sensor.outdoor_a", "sensor.outdoor")
    assert audit.issue_plan(
        forecast_start=START,
        issued_at=START + timedelta(seconds=30),
        temperatures=[20.0] * 9,
        revision="a",
        status="validated_passive",
        source_entity="sensor.indoor",
        source_identity=identity,
        source_reported=START,
    )
    for minute in range(1, 31):
        now = START + timedelta(minutes=minute)
        report = reading(20.2, now) if minute == 30 else reading(20, START)
        audit.observe_plan(now, report, "sensor.indoor", identity)
    assert audit.plan_records[0]["source_identity"] == identity
    payload = audit.export_plan()
    # JSON round trips tuples into lists.
    payload["records"][0]["source_identity"] = [
        ["sensor.room_a"],
        "sensor.outdoor_a",
        "sensor.outdoor",
    ]
    restored = ForecastAudit()
    restored.restore_plan(payload, START + timedelta(hours=1))
    assert restored.plan_records[0]["source_identity"] == identity
    assert restored.plan_summary()["horizons_minutes"]["30"]["count"] == 1


def test_statistics_do_not_mix_changed_source_identity_or_model_revision():
    audit = ForecastAudit()
    for cycle, identity, revision in (
        (0, (("sensor.a",), None, "sensor.out"), "a"),
        (40, (("sensor.b",), None, "sensor.out"), "a"),
        (80, (("sensor.b",), None, "sensor.out"), "b"),
    ):
        start = START + timedelta(minutes=cycle)
        assert audit.issue_plan(
            forecast_start=start,
            issued_at=start + timedelta(seconds=1),
            temperatures=[20.0] * 9,
            revision=revision,
            status="validated_passive",
            source_entity="sensor.indoor",
            source_identity=identity,
            source_reported=start,
        )
        for minute in range(1, 31):
            now = start + timedelta(minutes=minute)
            report = reading(20.2, now) if minute == 30 else reading(20, start)
            audit.observe_plan(now, report, "sensor.indoor", identity)
    assert len(audit.plan_records) == 3
    summary = audit.plan_summary()
    assert summary["model_revision"] == "b"
    assert summary["horizons_minutes"]["30"]["count"] == 1


def test_safe_cancellation_and_missing_deadline_never_create_numeric_outcome():
    audit = ForecastAudit()
    assert issue(audit)
    audit.cancel_plans("safe_mode")
    assert audit.plan_summary()["unverifiable"] == 3
    assert audit.plan_summary()["last_reason"] == "safe_mode"
    assert not audit.plan_records
    assert issue(audit, at=START + timedelta(minutes=1))
    for minute in range(2, 34):
        now = START + timedelta(minutes=minute)
        audit.observe_plan(now, reading(20, START), "sensor.indoor", (None, None))
    assert audit.plan_summary()["unverifiable"] == 4
    assert (
        audit.plan_summary()["last_reason"] == "no_distinct_report_near_forecast_time"
    )
    assert not audit.plan_records


def test_distinct_dst_fold_instants_use_actual_utc_lead():
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/Stockholm")
    at = datetime(2026, 10, 25, 2, 45, tzinfo=zone, fold=0)
    audit = ForecastAudit()
    assert issue(audit, at=at)
    utc = at.astimezone(timezone.utc)
    for minute in range(1, 31):
        now = (utc + timedelta(minutes=minute)).astimezone(zone)
        report = reading(20.2, now) if minute == 30 else reading(20, at)
        audit.observe_plan(now, report, "sensor.indoor", (None, None))
    row = audit.plan_records[-1]
    assert row["lead_seconds"] == 1770
    assert row["time_error_seconds"] == 0


def test_outdoor_entity_change_cancels_even_without_selected_source_attribute():
    from test_brake_provenance import _sensor

    sensor, states = _sensor()
    first = sensor._plan_source_identity(sensor._cfg())
    assert first == (None, None, "sensor.outdoor")
    assert sensor._plan_audit.issue_plan(
        forecast_start=START,
        issued_at=START + timedelta(seconds=30),
        temperatures=[20.0] * 9,
        revision="a",
        status="validated_passive",
        source_entity="sensor.indoor",
        source_identity=first,
        source_reported=START,
    )
    sensor._config_entry.options["real_outdoor_entity"] = "sensor.other_outdoor"
    states["sensor.other_outdoor"] = SimpleNamespace(state="5", attributes={})
    now = START + timedelta(minutes=1)
    sensor._plan_audit.observe_plan(
        now,
        reading(20, now),
        "sensor.indoor",
        sensor._plan_source_identity(sensor._cfg()),
    )
    assert not sensor._plan_audit.plan_pending
    assert sensor._plan_audit.plan_summary()["unverifiable"] == 3


def test_basic_ha_chain_scores_issued_plan_and_keeps_it_after_history(monkeypatch):
    from custom_components.pumpsteer import forecast
    from test_brake_provenance import _cycle, _sensor

    sensor, states = _sensor()
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        weather_entity="weather.local",
        aggressiveness=0,
    )
    for name in ("sensor.indoor", "sensor.outdoor"):
        states[name].attributes["unit_of_measurement"] = "°C"
        states[name].last_changed = START
    price = states["sensor.price"]
    price.last_reported = START
    price.attributes.update(
        unit_of_measurement="SEK/kWh",
        raw_today=[
            {
                "start": (
                    START.replace(hour=0) + timedelta(minutes=15 * i)
                ).isoformat(),
                "end": (
                    START.replace(hour=0) + timedelta(minutes=15 * (i + 1))
                ).isoformat(),
                "value": 1.0,
            }
            for i in range(96)
        ],
    )
    states["weather.local"] = SimpleNamespace(
        state="cloudy", last_reported=START, attributes={"temperature_unit": "°C"}
    )

    async def get_forecast(*args, **kwargs):
        callback = kwargs.get("raw_weather_callback")
        if callback:
            callback(
                [
                    {
                        "datetime": (START + timedelta(hours=i)).isoformat(),
                        "temperature": 5,
                    }
                    for i in range(4)
                ]
            )
        return []

    monkeypatch.setattr(forecast, "async_build_forecast", get_forecast)

    class MemoryStore:
        def __init__(self):
            self.payload = None

        def async_delay_save(self, make, delay):
            self.payload = make()

    sensor._pilot_store = MemoryStore()
    attrs = _cycle(sensor, monkeypatch, 0)
    assert sensor._pilot is None
    assert attrs["thermal_intelligence_planning_status"] == "hypothetical_unvalidated"
    assert sensor._plan_audit.plan_pending
    issued_context = dict(attrs["thermal_intelligence_plan_detail"]["initial_context"])
    assert sensor._pilot_store.payload["plan_audit"]["pending_count"] > 0
    for minute in range(1, 31):
        attrs = _cycle(sensor, monkeypatch, minute)
    assert sensor._plan_audit.plan_records
    assert sensor._plan_audit.plan_records[0]["horizon_minutes"] == 30
    assert (
        sensor._plan_audit.plan_records[0]["initial_context"]["target_temperature"]
        == issued_context["target_temperature"]
    )
    assert sensor._pilot_store.payload["plan_audit"]["records"]
    assert (
        attrs["thermal_intelligence_plan_outcomes"]["kind"]
        == "unexecuted_scenario_residual"
    )
    restored = ForecastAudit()
    restored.restore_plan(
        sensor._pilot_store.payload["plan_audit"],
        START + timedelta(minutes=31),
    )
    assert restored.plan_records
    assert not restored.plan_pending


@pytest.mark.parametrize("failure", ["audit", "storage"])
def test_audit_or_storage_failure_cannot_prevent_safe_output(monkeypatch, failure):
    from test_brake_provenance import _cycle, _sensor

    sensor, states = _sensor()
    sensor._config_entry.options["thermal_intelligence_enabled"] = True
    states["sensor.indoor"].state = "unavailable"
    sent = []

    async def push(value, cfg, expected):
        sent.append(value)

    async def failed_save():
        raise RuntimeError("synthetic storage failure")

    def failed_audit(*args):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(sensor, "_push_validated_temperature", push)
    if failure == "audit":
        monkeypatch.setattr(sensor._plan_audit, "cancel_plans", failed_audit)
    else:
        monkeypatch.setattr(sensor, "_save_pilot_store", failed_save)
    attrs = _cycle(sensor, monkeypatch, 0)
    assert attrs["mode"] == "safe_mode"
    assert sensor.state == 5.0
    assert sent == [5.0]
