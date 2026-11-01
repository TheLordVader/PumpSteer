from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from custom_components.pumpsteer.heatpump_telemetry import HeatPumpTelemetry

T = datetime(2026, 11, 1, tzinfo=timezone.utc)


def state(value, minute=0, unit=None, entity="sensor.test"):
    return SimpleNamespace(
        state=str(value),
        entity_id=entity,
        attributes={"unit_of_measurement": unit},
        last_reported=T + timedelta(minutes=minute),
    )


def test_raw_balance_trend_uses_distinct_reports_without_assuming_sign():
    observer = HeatPumpTelemetry()
    s = state(837)
    for minute, value in [(0, 837), (15, 722), (30, 607)]:
        s.state = str(value)
        s.last_reported = T + timedelta(minutes=minute)
        output = observer.observe(
            s.last_reported, {"balance": s}, balance_kind="degree_minutes"
        )
    assert output["balance"]["value"] == 607
    assert output["balance"]["kind"] == "degree_minutes"
    assert output["balance"]["trend_per_hour"] == pytest.approx(-460)
    assert output["balance"]["interpretation"] == "unverified"
    assert output["physical_measurement_time"] == "unknown"
    stale = observer.observe(
        T + timedelta(minutes=91), {"balance": s}, balance_kind="degree_minutes"
    )
    assert stale["balance"]["value"] is None
    assert stale["balance"]["trend_per_hour"] is None


def test_distinct_integral_label_does_not_invert_or_assume_threshold():
    observer = HeatPumpTelemetry()
    data = {}
    for minute, value in [(0, -300), (15, -250), (30, -200)]:
        data = observer.observe(
            T + timedelta(minutes=minute),
            {"balance": state(value, minute)},
            balance_kind="integral",
        )
    assert data["balance"]["kind"] == "integral"
    assert data["balance"]["trend_per_hour"] == pytest.approx(200)
    assert data["balance"]["interpretation"] == "unverified"
    assert "start_threshold" not in data["balance"]


def test_flow_target_return_and_optional_signals_are_observational():
    observer = HeatPumpTelemetry()
    data = observer.observe(
        T,
        {
            "supply_target": state(40, unit="°C"),
            "supply": state(35, unit="°C"),
            "return": state(30, unit="°C"),
            "compressor": state("on"),
            "hot_water": state("on"),
            "brine_pump": state("on"),
            "circulation_pump": state("on"),
            "brine_in": state(2, unit="°C"),
            "auxiliary": state("off"),
        },
    )
    assert data["temperatures"]["supply_difference_c"] == -5
    assert data["temperatures"]["supply_return_difference_c"] == 5
    assert data["operation"] == "hot_water_indicated"
    assert data["learning_qualified"] is False
    assert data["physical_heat_delivery"] == "unverified"
    assert data["binary"]["circulation_pump"] is True


def test_start_count_requires_observed_off_to_on_and_resets_after_gap():
    observer = HeatPumpTelemetry()
    result = observer.observe(T, {"compressor": state("on")})
    assert result["compressor_starts_observed"] == 0
    for minute, value in [(1, "off"), (2, "on"), (3, "on")]:
        result = observer.observe(
            T + timedelta(minutes=minute), {"compressor": state(value, minute)}
        )
    assert result["compressor_starts_observed"] == 1
    observer.observe(T + timedelta(minutes=64), {"compressor": state("off", 64)})
    result = observer.observe(
        T + timedelta(minutes=125), {"compressor": state("on", 125)}
    )
    assert result["compressor_starts_observed"] == 1


def test_conflicting_missing_and_bad_inputs_degrade_without_fabrication():
    observer = HeatPumpTelemetry()
    data = observer.observe(
        T,
        {
            "supply": state("999", unit="°C"),
            "supply_target": state("68", unit="°F"),
            "compressor": state("on"),
            "brine_pump": state("off"),
        },
    )
    assert data["temperatures"]["supply_c"] is None
    assert data["temperatures"]["supply_target_c"] is None
    assert data["operation"] == "unknown"
    assert data["signal_consistency"] == "conflicting"
    assert data["temperatures"]["supply_difference_c"] is None
    data = observer.observe(T + timedelta(hours=2), {"compressor": state("on")})
    assert data["operation"] == "unknown"
    assert data["binary"]["compressor"] is None


def test_temperature_difference_requires_temporally_aligned_reports():
    observer = HeatPumpTelemetry()
    result = observer.observe(
        T + timedelta(minutes=40),
        {
            "supply": state(35, 40, "°C"),
            "supply_target": state(40, 0, "°C"),
            "return": state(30, 0, "°C"),
        },
    )
    assert result["temperatures"]["supply_c"] == 35
    assert result["temperatures"]["supply_target_c"] == 40
    assert result["temperatures"]["supply_difference_c"] is None
    assert result["temperatures"]["supply_return_difference_c"] is None


def test_unavailable_or_misaligned_brine_does_not_verify_operation():
    observer = HeatPumpTelemetry()
    result = observer.observe(
        T + timedelta(minutes=30),
        {"compressor": state("on", 30), "brine_pump": state("off", 0)},
    )
    assert result["operation"] == "unknown"
    assert result["signal_consistency"] == "insufficient_data"
    result = observer.observe(
        T + timedelta(minutes=30),
        {"compressor": state("on", 30), "brine_pump": state("unavailable", 30)},
    )
    assert result["operation"] == "unknown"
    assert result["signal_consistency"] == "insufficient_data"


def test_hot_water_episodes_need_observed_transition():
    observer = HeatPumpTelemetry()
    output = observer.observe(T, {"hot_water": state("on")})
    assert output["hot_water_episodes_observed"] == 0
    for minute, value in [(1, "off"), (2, "on"), (3, "on")]:
        output = observer.observe(
            T + timedelta(minutes=minute), {"hot_water": state(value, minute)}
        )
    assert output["hot_water_episodes_observed"] == 1
    observer.observe(T + timedelta(minutes=65), {"hot_water": state("off", 65)})
    output = observer.observe(
        T + timedelta(minutes=126), {"hot_water": state("on", 126)}
    )
    assert output["hot_water_episodes_observed"] == 1


def test_configured_but_missing_brine_pump_keeps_operation_unknown():
    observer = HeatPumpTelemetry()
    result = observer.observe(
        T,
        {"compressor": state("on"), "brine_pump": None},
        configured={"compressor", "brine_pump"},
    )
    assert result["operation"] == "unknown"
    assert result["signal_consistency"] == "insufficient_data"


def test_heat_pump_temperatures_allow_hot_water_but_reject_invalid_values():
    observer = HeatPumpTelemetry()
    data = observer.observe(
        T,
        {
            "supply": state(65, unit="°C"),
            "supply_target": state(70, unit="°C"),
            "return": state(999, unit="°C"),
            "brine_in": state(65, unit="°C"),
        },
    )
    assert data["temperatures"]["supply_c"] == 65
    assert data["temperatures"]["supply_target_c"] == 70
    assert data["temperatures"]["return_c"] is None
    assert data["temperatures"]["brine_in_c"] is None
    assert data["temperatures"]["supply_difference_c"] == -5


def test_missing_stale_or_misaligned_configured_hot_water_does_not_guess_mode():
    observer = HeatPumpTelemetry()
    for hot_water in (None, state("off", 0), state("on", 30)):
        now = T + timedelta(minutes=100)
        result = observer.observe(
            now,
            {"compressor": state("on", 100), "hot_water": hot_water},
            configured={"compressor", "hot_water"},
        )
        assert result["operation"] == "unknown"
    result = observer.observe(
        T + timedelta(minutes=30),
        {"compressor": state("on", 30), "hot_water": state("off", 0)},
        configured={"compressor", "hot_water"},
    )
    assert result["operation"] == "unknown"
    result = observer.observe(
        T + timedelta(minutes=30),
        {"compressor": state("on", 30), "hot_water": state("off", 30)},
        configured={"compressor", "hot_water"},
    )
    assert result["operation"] == "compressor_running"


def test_raw_kind_and_optional_absent_brine_are_not_errors():
    result = HeatPumpTelemetry().observe(
        T, {"balance": state(123), "compressor": state("on")}, balance_kind="raw"
    )
    assert result["balance"]["kind"] == "raw"
    assert result["balance"]["value"] == 123
    assert result["signal_consistency"] == "no_conflict_observed"
    assert result["operation"] == "compressor_running"
