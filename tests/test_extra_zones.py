import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from custom_components.pumpsteer.extra_zones import ExtraZones


def test_alarm_requires_continuous_reports_and_hysteresis():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    config = [
        {"entity": "sensor.garage", "name": "Garage", "threshold": 5, "enabled": True}
    ]
    state = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    for minute in range(16):
        now = start + timedelta(minutes=minute)
        state.last_reported = now
        result, alerts = monitor.observe(now, config, {"sensor.garage": state})
        assert bool(alerts) == (minute == 15)
        if alerts:
            monitor.mark_sent(1)
    assert result["1"]["status"] == "alarm"
    state.state = "5.2"
    state.last_reported = now + timedelta(minutes=1)
    assert (
        monitor.observe(state.last_reported, config, {"sensor.garage": state})[0]["1"][
            "status"
        ]
        == "alarm"
    )
    state.state = "5.5"
    assert (
        monitor.observe(state.last_reported, config, {"sensor.garage": state})[0]["1"][
            "status"
        ]
        == "normal"
    )


def test_missing_reports_do_not_prove_low_temperature():
    monitor = ExtraZones()
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.garage", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    monitor.observe(now, cfg, {"sensor.garage": state})
    result, alerts = monitor.observe(
        now + timedelta(minutes=61), cfg, {"sensor.garage": state}
    )
    assert result["1"]["status"] == "unavailable"
    assert [a["event"] for a in alerts] == ["sensor_fault"]
    assert not result["1"]["alarm_active"]


def test_restart_keeps_dispatched_alarm_but_restarts_unqualified_interval():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.garage", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    monitor = ExtraZones()
    for minute in range(16):
        state.last_reported = now + timedelta(minutes=minute)
        result, alerts = monitor.observe(
            state.last_reported, cfg, {"sensor.garage": state}
        )
    assert alerts
    monitor.mark_sent(1)
    saved, _ = monitor.observe(state.last_reported, cfg, {"sensor.garage": state})
    resumed = ExtraZones(saved)
    state.last_reported += timedelta(minutes=1)
    result, alerts = resumed.observe(state.last_reported, cfg, {"sensor.garage": state})
    assert result["1"]["status"] == "alarm"
    assert not alerts
    pending = ExtraZones()
    pending.observe(
        now,
        cfg,
        {
            "sensor.garage": SimpleNamespace(
                state="4", attributes={"unit_of_measurement": "°C"}, last_reported=now
            )
        },
    )
    restarted = ExtraZones()
    state.last_reported = now + timedelta(minutes=15)
    assert not restarted.observe(state.last_reported, cfg, {"sensor.garage": state})[1]


def test_trend_needs_real_reports_across_half_an_hour_and_drops_on_gap():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.garage", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(
        state="10", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    for minute in range(31):
        now = start + timedelta(minutes=minute)
        state.last_reported = now
        state.state = str(10 - minute / 30)
        result, _ = monitor.observe(now, cfg, {"sensor.garage": state})
        if minute < 30:
            assert result["1"]["trend_quality"] == "insufficient_data"
    assert result["1"]["trend_c_per_hour"] == pytest.approx(-2)
    assert result["1"]["estimated_hours_to_threshold"] == pytest.approx(2)
    result, _ = monitor.observe(
        now + timedelta(minutes=36), cfg, {"sensor.garage": state}
    )
    assert result["1"]["trend_quality"] == "insufficient_data"


def test_profile_change_resets_evidence_and_optional_climate_is_passive():
    monitor = ExtraZones()
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.garage",
            "threshold": 5,
            "comfort_threshold": 18,
            "profile": "maintenance",
            "enabled": True,
            "climate": "climate.garage",
        }
    ]
    states = {
        "sensor.garage": SimpleNamespace(
            state="10", attributes={"unit_of_measurement": "°C"}, last_reported=now
        ),
        "climate.garage": SimpleNamespace(
            state="heat",
            attributes={"temperature": 19, "hvac_action": "heating"},
            last_reported=now,
        ),
    }
    first, _ = monitor.observe(now, cfg, states)
    assert first["1"]["threshold"] == 5
    assert first["1"]["climate_action"] == "heating"
    cfg[0]["profile"] = "comfort"
    second, alerts = monitor.observe(now, cfg, states)
    assert second["1"]["threshold"] == 18
    assert second["1"]["status"] == "pending"
    assert not alerts
    assert second["1"]["trend_quality"] == "insufficient_data"
    del states["climate.garage"]
    third, _ = monitor.observe(now, cfg, states)
    assert third["1"]["climate_status"] == "unavailable"
    assert third["1"]["temperature"] == 10


def test_separate_forecast_fault_recovery_and_restart_deduplication():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(
        state="7", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    for minute in range(31):
        state.last_reported = start + timedelta(minutes=minute)
        state.state = str(7 - minute / 30)
        result, alerts = monitor.observe(
            state.last_reported, cfg, {"sensor.zone": state}
        )
    assert result["1"]["events"]["forecast"]
    assert alerts[0]["event"] == "forecast"
    monitor.mark_sent(1, "forecast")
    saved, alerts = monitor.observe(state.last_reported, cfg, {"sensor.zone": state})
    assert not alerts
    resumed = ExtraZones(saved)
    resumed.observe(state.last_reported, cfg, {})
    result, alerts = resumed.observe(
        state.last_reported + timedelta(minutes=61), cfg, {}
    )
    assert [a["event"] for a in alerts] == ["sensor_fault"]
    resumed.mark_sent(1, "sensor_fault")
    state.last_reported += timedelta(minutes=62)
    state.state = "8"
    result, alerts = resumed.observe(state.last_reported, cfg, {"sensor.zone": state})
    assert result["1"]["status"] == "sensor_recovering" and not alerts
    state.state = "8.1"
    state.last_reported += timedelta(minutes=30)
    result, alerts = resumed.observe(state.last_reported, cfg, {"sensor.zone": state})
    assert result["1"]["status"] == "sensor_recovering" and not alerts
    state.state = "8.2"
    state.last_reported += timedelta(minutes=30)
    result, alerts = resumed.observe(state.last_reported, cfg, {"sensor.zone": state})
    assert [a["event"] for a in alerts] == ["recovery"]
    resumed.mark_sent(1, "recovery")
    assert not resumed.observe(state.last_reported, cfg, {"sensor.zone": state})[1]


def test_heat_demand_response_and_outdoor_relative_trend_are_observations():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.zone",
            "threshold": 10,
            "enabled": True,
            "climate": "climate.zone",
        }
    ]
    states = {
        "sensor.zone": SimpleNamespace(
            state="9", attributes={"unit_of_measurement": "°C"}
        ),
        "climate.zone": SimpleNamespace(
            state="heat", attributes={"hvac_action": "heating", "temperature": 12}
        ),
    }
    outdoor = SimpleNamespace(state="0", attributes={"unit_of_measurement": "°C"})
    for minute in range(31):
        now = start + timedelta(minutes=minute)
        for state in [*states.values(), outdoor]:
            state.last_reported = now
        states["sensor.zone"].state = str(9 - minute / 30)
        outdoor.state = str(-minute / 30)
        result, alerts = monitor.observe(now, cfg, states, outdoor=outdoor)
        for alert in alerts:
            monitor.mark_sent(1, alert["event"])
    assert result["1"]["events"]["heat_shortage"]
    assert result["1"]["heat_response"] == "suspected_insufficient"
    assert abs(result["1"]["relative_trend_c_per_hour"]) < 1e-8
    states["climate.zone"].attributes = {"temperature": 12}
    result, _ = monitor.observe(now, cfg, states, outdoor=outdoor)
    assert result["1"]["heat_demand"] is None
    assert result["1"]["events"]["heat_shortage"]
    assert result["1"]["heat_response"] == "insufficient_data"
    assert result["1"]["temperature"] == 8


def test_delay_config_and_disabled_zone_do_not_raise_or_recover():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.zone",
            "threshold": 5,
            "enabled": True,
            "alarm_delay_minutes": 2,
        }
    ]
    monitor = ExtraZones()
    state = SimpleNamespace(state="4", attributes={"unit_of_measurement": "°C"})
    for minute in range(3):
        state.last_reported = start + timedelta(minutes=minute)
        result, alerts = monitor.observe(
            state.last_reported, cfg, {"sensor.zone": state}
        )
    assert result["1"]["events"]["temperature"]
    monitor.mark_sent(1)
    cfg[0]["enabled"] = False
    result, alerts = monitor.observe(state.last_reported, cfg, {})
    assert not alerts
    assert not any(result["1"]["events"].values())


def test_noisy_endpoint_drop_is_not_a_credible_forecast():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    for minute in range(31):
        state.last_reported = start + timedelta(minutes=minute)
        state.state = str(6 if minute == 30 else 10 + (minute % 2))
        result, _ = monitor.observe(state.last_reported, cfg, {"sensor.zone": state})
    assert result["1"]["estimated_hours_to_threshold"] is None
    assert not result["1"]["events"]["forecast"]


def test_forecast_crossing_threshold_does_not_report_recovery_during_alarm_delay():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    for minute in range(62):
        state.last_reported = start + timedelta(minutes=minute)
        state.state = str(7 - minute / 30)
        result, alerts = monitor.observe(
            state.last_reported, cfg, {"sensor.zone": state}
        )
        assert all(a["event"] != "recovery" for a in alerts)
        for alert in alerts:
            monitor.mark_sent(1, alert["event"])
    assert result["1"]["status"] == "pending"


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"temperature": 20}, None),
        ({"valve_position": 60}, True),
        ({"pi_heating_demand": 0}, False),
        ({"valve_position": 200}, None),
        ({"hvac_action": "idle", "valve_position": 50}, False),
    ],
)
def test_optional_climate_uses_only_reported_demand(attrs, expected):
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    states = {
        "sensor.zone": SimpleNamespace(
            state="8", attributes={"unit_of_measurement": "°C"}, last_reported=now
        ),
        "climate.zone": SimpleNamespace(
            state="heat", attributes=attrs, last_reported=now
        ),
    }
    cfg = [
        {
            "entity": "sensor.zone",
            "threshold": 5,
            "enabled": True,
            "climate": "climate.zone",
        }
    ]
    result, alerts = ExtraZones().observe(now, cfg, states)
    assert result["1"]["heat_demand"] is expected
    assert result["1"]["temperature"] == 8
    assert not alerts


def test_independent_zones_rearm_after_recovery_and_reset_after_source_change():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.a",
            "threshold": 5,
            "enabled": True,
            "alarm_delay_minutes": 1,
        },
        {"entity": "sensor.b", "threshold": 5, "enabled": True},
    ]
    a = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    b = SimpleNamespace(
        state="10", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    monitor = ExtraZones()
    for minute in range(2):
        a.last_reported = b.last_reported = now + timedelta(minutes=minute)
        result, alerts = monitor.observe(
            a.last_reported, cfg, {"sensor.a": a, "sensor.b": b}
        )
    assert [(v["slot"], v["event"]) for v in alerts] == [(1, "temperature")]
    monitor.mark_sent(1)
    a.state = "6"
    a.last_reported += timedelta(seconds=1)
    _, alerts = monitor.observe(a.last_reported, cfg, {"sensor.a": a, "sensor.b": b})
    assert [v["event"] for v in alerts] == ["recovery"]
    monitor.mark_sent(1, "recovery")
    a.state = "4"
    for minute in (2, 3):
        a.last_reported = b.last_reported = now + timedelta(minutes=minute)
        _, alerts = monitor.observe(
            a.last_reported, cfg, {"sensor.a": a, "sensor.b": b}
        )
    assert [v["event"] for v in alerts] == ["temperature"]
    cfg[0]["entity"] = "sensor.b"
    result, alerts = monitor.observe(a.last_reported, cfg, {"sensor.b": b})
    assert result["1"]["status"] == "normal"
    assert not alerts


@pytest.mark.parametrize(
    "state,attrs,offset",
    [
        ("nan", {"unit_of_measurement": "°C"}, 0),
        ("5", {"unit_of_measurement": "°F"}, 0),
        ("5", {"unit_of_measurement": "°C", "restored": True}, 0),
        ("5", {"unit_of_measurement": "°C"}, 1),
    ],
)
def test_unqualified_temperature_is_a_fault_not_temperature_evidence(
    state, attrs, offset
):
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    reading = SimpleNamespace(
        state=state, attributes=attrs, last_reported=now + timedelta(seconds=offset)
    )
    result, alerts = ExtraZones().observe(
        now,
        [{"entity": "sensor.zone", "threshold": 10, "enabled": True}],
        {"sensor.zone": reading},
    )
    assert result["1"]["temperature"] is None
    assert not alerts


def test_sparse_climate_reports_do_not_prove_continuous_heat_demand():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.zone",
            "threshold": 10,
            "enabled": True,
            "climate": "climate.zone",
        }
    ]
    room = SimpleNamespace(state="8", attributes={"unit_of_measurement": "°C"})
    climate = SimpleNamespace(
        state="heat", attributes={"hvac_action": "heating"}, last_reported=start
    )
    for minute in range(0, 41, 5):
        now = start + timedelta(minutes=minute)
        room.last_reported = now
        if minute % 10 == 0:
            climate.last_reported = now
        result, _ = monitor.observe(
            now, cfg, {"sensor.zone": room, "climate.zone": climate}
        )
    assert not result["1"]["events"]["heat_shortage"]


def test_failed_recovery_is_cancelled_when_temperature_falls_again():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.zone",
            "threshold": 5,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    room = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    monitor = ExtraZones()
    monitor.observe(now, cfg, {"sensor.zone": room})
    room.last_reported += timedelta(minutes=1)
    monitor.observe(room.last_reported, cfg, {"sensor.zone": room})
    monitor.mark_sent(1)
    room.state = "5.5"
    room.last_reported += timedelta(seconds=1)
    saved, alerts = monitor.observe(room.last_reported, cfg, {"sensor.zone": room})
    assert [a["event"] for a in alerts] == ["recovery"]
    for current in (monitor, ExtraZones(saved)):
        room.state = "4"
        _, alerts = current.observe(room.last_reported, cfg, {"sensor.zone": room})
        assert not alerts


def test_disabling_alarms_keeps_passive_temperature_trend_available():
    monitor = ExtraZones()
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 10, "enabled": False}]
    room = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    for minute in range(31):
        room.state = str(9 - minute / 30)
        room.last_reported = start + timedelta(minutes=minute)
        result, alerts = monitor.observe(room.last_reported, cfg, {"sensor.zone": room})
        assert not alerts
    assert result["1"]["trend_c_per_hour"] == pytest.approx(-2)
    assert not any(result["1"]["events"].values())


@pytest.mark.parametrize("interval", [10, 15, 30])
def test_sparse_reports_confirm_alarm_without_reusing_old_report(interval):
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    monitor.observe(start, cfg, {"sensor.z": state})
    result, alerts = monitor.observe(
        start + timedelta(minutes=interval), cfg, {"sensor.z": state}
    )
    assert not result["1"]["alarm_active"] and not alerts
    state.last_reported = start + timedelta(minutes=interval)
    result, _ = monitor.observe(state.last_reported, cfg, {"sensor.z": state})
    if interval < 15:
        state.last_reported += timedelta(minutes=interval)
        result, _ = monitor.observe(state.last_reported, cfg, {"sensor.z": state})
    assert result["1"]["alarm_active"]


def test_startup_grace_then_sensor_fault():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    assert not monitor.observe(start, cfg, {})[1]
    assert [
        a["event"] for a in monitor.observe(start + timedelta(minutes=61), cfg, {})[1]
    ] == ["sensor_fault"]


def test_forecast_does_not_recover_from_an_old_report_after_gap():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    for minute in range(31):
        state.state = str(7 - minute / 30)
        state.last_reported = start + timedelta(minutes=minute)
        result, alerts = monitor.observe(state.last_reported, cfg, {"sensor.z": state})
    assert result["1"]["events"]["forecast"]
    monitor.mark_sent(1, "forecast")
    result, alerts = monitor.observe(
        state.last_reported + timedelta(minutes=36), cfg, {"sensor.z": state}
    )
    assert not result["1"]["events"]["forecast"]
    assert not alerts


@pytest.mark.parametrize("interval,forecast", [(10, True), (15, True), (30, False)])
def test_forecast_degrades_with_sparse_reports(interval, forecast):
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    for n in range(7):
        state.last_reported = start + timedelta(minutes=n * interval)
        state.state = str(7 - n * interval / 60)
        result, _ = monitor.observe(state.last_reported, cfg, {"sensor.z": state})
    assert result["1"]["events"]["forecast"] is forecast


def test_sample_history_is_bounded_under_rapid_reports():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    state = SimpleNamespace(state="10", attributes={"unit_of_measurement": "°C"})
    for n in range(1000):
        state.last_reported = start + timedelta(seconds=n)
        monitor.observe(state.last_reported, cfg, {"sensor.z": state})
    assert len(monitor._zones[1]["samples"]) <= 128


def test_relative_trend_requires_new_outdoor_reports():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    room = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    outdoor = SimpleNamespace(
        state="0", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    monitor = ExtraZones()
    for minute in range(31):
        room.last_reported = start + timedelta(minutes=minute)
        room.state = str(10 - minute / 30)
        result, _ = monitor.observe(
            room.last_reported,
            cfg,
            {"sensor.z": room},
            outdoor=outdoor,
            outdoor_max_age_minutes=120,
        )
    assert result["1"]["trend_c_per_hour"] == pytest.approx(-2)
    assert result["1"]["relative_trend_c_per_hour"] is None


def test_restart_does_not_clear_alarm_from_pre_restart_report():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.z",
            "threshold": 5,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    state = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    monitor = ExtraZones()
    monitor.observe(start, cfg, {"sensor.z": state})
    state.last_reported += timedelta(minutes=1)
    monitor.observe(state.last_reported, cfg, {"sensor.z": state})
    monitor.mark_sent(1)
    saved, _ = monitor.observe(state.last_reported, cfg, {"sensor.z": state})
    # HA retained this old healthy state through the controller reload.
    state.state = "6"
    resumed = ExtraZones(saved, started_at=state.last_reported + timedelta(minutes=1))
    result, alerts = resumed.observe(
        state.last_reported + timedelta(minutes=1), cfg, {"sensor.z": state}
    )
    assert result["1"]["alarm_active"] and not alerts
    state.last_reported += timedelta(minutes=2)
    result, alerts = resumed.observe(state.last_reported, cfg, {"sensor.z": state})
    assert not result["1"]["alarm_active"]
    assert [a["event"] for a in alerts] == ["recovery"]


def test_rapid_real_reports_preserve_sufficient_trend_span():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    monitor = ExtraZones()
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    room = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    for seconds in range(3601):
        room.last_reported = start + timedelta(seconds=seconds)
        room.state = str(7 - seconds / 3600)
        result, _ = monitor.observe(room.last_reported, cfg, {"sensor.z": room})
    assert result["1"]["trend_c_per_hour"] == pytest.approx(-1)
    assert result["1"]["events"]["forecast"]
    assert len(monitor._zones[1]["samples"]) <= 128


def test_relative_trend_accepts_slightly_asynchronous_observed_reports():
    start = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.z", "threshold": 5, "enabled": True}]
    room = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    outdoor = SimpleNamespace(attributes={"unit_of_measurement": "°C"})
    monitor = ExtraZones()
    for minute in range(31):
        room.last_reported = start + timedelta(minutes=minute)
        room.state = str(10 - minute / 30)
        outdoor.last_reported = room.last_reported + timedelta(seconds=30)
        outdoor.state = str(-minute / 30)
        result, _ = monitor.observe(
            outdoor.last_reported, cfg, {"sensor.z": room}, outdoor=outdoor
        )
    assert result["1"]["relative_trend_c_per_hour"] == pytest.approx(0)


def test_invalid_high_temperature_cannot_clear_alarm():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    cfg = [
        {
            "entity": "sensor.zone",
            "threshold": 5,
            "enabled": True,
            "alarm_delay_minutes": 1,
        }
    ]
    state = SimpleNamespace(
        state="4", attributes={"unit_of_measurement": "°C"}, last_reported=now
    )
    monitor = ExtraZones()
    monitor.observe(now, cfg, {"sensor.zone": state})
    state.last_reported += timedelta(minutes=1)
    monitor.observe(state.last_reported, cfg, {"sensor.zone": state})
    monitor.mark_sent(1)
    state.state = "999"
    state.last_reported += timedelta(minutes=1)
    result, alerts = monitor.observe(state.last_reported, cfg, {"sensor.zone": state})
    assert result["1"]["alarm_active"]
    assert not any(a["event"] == "recovery" for a in alerts)


def test_sensor_fault_is_not_rearmed_by_republish_or_isolated_valid_readings():
    start = datetime(2026, 11, 18, 15, 17, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 10, "enabled": True}]
    monitor = ExtraZones()
    state = SimpleNamespace(attributes={"unit_of_measurement": "°C"})

    def observe(minute, value=None, report_minute=None):
        now = start + timedelta(minutes=minute)
        if value is not None:
            state.state = value
            state.last_reported = start + timedelta(
                minutes=minute if report_minute is None else report_minute
            )
        result, alerts = monitor.observe(now, cfg, {"sensor.zone": state})
        return result["1"], [alert["event"] for alert in alerts]

    observe(0, "12.1")
    assert observe(61)[1] == ["sensor_fault"]
    monitor.mark_sent(1, "sensor_fault")
    # A retained startup value or a repeated numeric value cannot recover.
    assert observe(62, "12.10")[1] == []
    assert observe(123)[1] == []
    # Sparse changed values are real observations but do not establish continuity.
    assert observe(125, "12.2")[1] == []
    assert observe(180, "12.2")[1] == []
    assert observe(188, "12.3")[1] == []
    assert observe(192, "12.7", report_minute=186)[1] == []
    pending, alerts = observe(194)
    assert pending["events"]["sensor_fault"] and alerts == []
    assert observe(200, "12.4")[1] == []
    assert observe(249, "12.5")[1] == ["recovery"]
    monitor.mark_sent(1, "recovery")
    assert observe(250)[1] == []
    assert observe(310)[1] == ["sensor_fault"]


def test_notified_fault_survives_restart_and_cached_value_without_repeat():
    start = datetime(2026, 11, 18, 15, 17, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 10, "enabled": True}]
    monitor = ExtraZones()
    state = SimpleNamespace(
        state="12.1", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    states = {"sensor.zone": state}
    monitor.observe(start, cfg, states)
    assert monitor.observe(start + timedelta(minutes=61), cfg, states)[1]
    monitor.mark_sent(1, "sensor_fault")
    saved, _ = monitor.observe(start + timedelta(minutes=62), cfg, states)
    restart = start + timedelta(minutes=63)
    monitor = ExtraZones(saved, started_at=restart)
    assert not monitor.observe(restart, cfg, {})[1]
    state.last_reported = restart + timedelta(minutes=1)
    result, alerts = monitor.observe(state.last_reported, cfg, states)
    assert result["1"]["events"]["sensor_fault"]
    assert result["1"]["notifications_sent"]["sensor_fault"]
    assert alerts == []
    state.state = "12.2"
    state.last_reported += timedelta(minutes=2)
    assert not monitor.observe(state.last_reported, cfg, states)[1]
    state.state = "12.3"
    state.last_reported += timedelta(minutes=30)
    saved, alerts = monitor.observe(state.last_reported, cfg, states)
    assert not alerts
    restart = state.last_reported + timedelta(minutes=1)
    monitor = ExtraZones(saved, started_at=restart)
    monitor.observe(restart, cfg, {})
    # The pre-restart sample can be republished with a new HA timestamp.
    state.last_reported = restart + timedelta(minutes=1)
    assert not monitor.observe(state.last_reported, cfg, states)[1]
    for value, minutes in (("12.4", 25), ("12.5", 35)):
        state.state = value
        state.last_reported += timedelta(minutes=minutes)
        assert not monitor.observe(state.last_reported, cfg, states)[1]
    state.state = "12.6"
    state.last_reported += timedelta(minutes=25)
    assert [a["event"] for a in monitor.observe(state.last_reported, cfg, states)[1]] == [
        "recovery"
    ]


def test_low_temperature_alarm_is_independent_of_pending_sensor_recovery():
    start = datetime(2026, 11, 18, 15, 17, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 10, "enabled": True}]
    monitor = ExtraZones()
    state = SimpleNamespace(
        state="11", attributes={"unit_of_measurement": "°C"}, last_reported=start
    )
    states = {"sensor.zone": state}
    monitor.observe(start, cfg, states)
    assert monitor.observe(start + timedelta(minutes=61), cfg, states)[1]
    monitor.mark_sent(1, "sensor_fault")
    state.state = "9.5"
    state.last_reported = start + timedelta(minutes=62)
    assert not monitor.observe(state.last_reported, cfg, states)[1]
    state.last_reported += timedelta(minutes=15)
    result, alerts = monitor.observe(state.last_reported, cfg, states)
    assert result["1"]["events"]["sensor_fault"]
    assert result["1"]["alarm_active"]
    assert [a["event"] for a in alerts] == ["temperature"]
    monitor.mark_sent(1, "temperature")
    state.state = "9.4"
    state.last_reported += timedelta(minutes=15)
    result, alerts = monitor.observe(state.last_reported, cfg, states)
    assert result["1"]["events"]["sensor_fault"]
    assert result["1"]["alarm_active"] and not alerts
    state.state = "9.3"
    state.last_reported += timedelta(minutes=30)
    result, alerts = monitor.observe(state.last_reported, cfg, states)
    assert not result["1"]["events"]["sensor_fault"]
    assert result["1"]["alarm_active"] and not alerts


def test_recovery_without_a_saved_temperature_needs_two_value_changes():
    start = datetime(2026, 11, 18, 15, 17, tzinfo=timezone.utc)
    cfg = [{"entity": "sensor.zone", "threshold": 10, "enabled": True}]
    monitor = ExtraZones()
    monitor.observe(start, cfg, {})
    assert monitor.observe(start + timedelta(minutes=61), cfg, {})[1]
    monitor.mark_sent(1, "sensor_fault")
    state = SimpleNamespace(
        state="12.1",
        attributes={"unit_of_measurement": "°C"},
        last_reported=start + timedelta(minutes=62),
    )
    states = {"sensor.zone": state}
    assert not monitor.observe(state.last_reported, cfg, states)[1]
    # This value appeared without a new report and cannot count.
    state.state = "12.2"
    assert not monitor.observe(state.last_reported + timedelta(minutes=1), cfg, states)[1]
    state.state = "12.3"
    state.last_reported += timedelta(minutes=8)
    assert not monitor.observe(state.last_reported, cfg, states)[1]
    state.state = "12.5"
    state.last_reported += timedelta(minutes=15)
    assert not monitor.observe(state.last_reported, cfg, states)[1]
    state.state = "12.7"
    state.last_reported += timedelta(minutes=45)
    assert [a["event"] for a in monitor.observe(state.last_reported, cfg, states)[1]] == [
        "recovery"
    ]
