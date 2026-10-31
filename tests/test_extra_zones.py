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
        now + timedelta(minutes=16), cfg, {"sensor.garage": state}
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
        now + timedelta(minutes=6), cfg, {"sensor.garage": state}
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
    result, alerts = resumed.observe(
        state.last_reported + timedelta(minutes=6), cfg, {}
    )
    assert [a["event"] for a in alerts] == ["sensor_fault"]
    resumed.mark_sent(1, "sensor_fault")
    state.last_reported += timedelta(minutes=7)
    state.state = "8"
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
    assert [v["event"] for v in alerts] == ["sensor_fault"]


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
