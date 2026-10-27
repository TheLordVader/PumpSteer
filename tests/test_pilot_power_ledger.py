"""Reported electrical power is integrated only across qualified HA intervals."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from custom_components.pumpsteer.pilot_power_ledger import PowerLedger

T = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


def state(value, when, unit=None, changed=None):
    return SimpleNamespace(
        state=str(value),
        last_reported=when,
        last_changed=changed or when,
        attributes={"unit_of_measurement": unit},
    )


def prices():
    return SimpleNamespace(
        state="1",
        last_reported=T,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {
                    "start": (T + timedelta(minutes=15 * i)).isoformat(),
                    "end": (T + timedelta(minutes=15 * (i + 1))).isoformat(),
                    "value": 1 if i == 0 else 3,
                }
                for i in range(4)
            ],
        },
    )


def test_reported_power_and_price_integrate_without_claiming_physical_energy():
    ledger = PowerLedger()
    for minute in range(0, 21, 5):
        at = T + timedelta(minutes=minute)
        ledger.observe(
            at, state(1000, at, "W", T), state("on", at, changed=T), None, [prices()]
        )
    result = ledger.summary()
    assert result["energy_kwh_estimate"] == pytest.approx(1 / 3, abs=0.0001)
    assert result["priced_energy_kwh_estimate"] == pytest.approx(1 / 3, abs=0.0001)
    assert result["cost_estimate"] == pytest.approx(0.5, abs=0.0001)
    assert result["radiator_mode_kwh_estimate"] == pytest.approx(1 / 3, abs=0.0001)
    assert result["method"] == "reported_power_held_between_reports"


def test_gap_stale_or_wrong_unit_is_not_invented():
    ledger = PowerLedger()
    ledger.observe(T, state(1, T, "kW"), None, None, [prices()])
    at = T + timedelta(minutes=10)
    ledger.observe(at, state(1, at, "kW"), None, None, [prices()])
    at += timedelta(minutes=5)
    ledger.observe(at, state(1, at, "watts"), None, None, [prices()])
    assert ledger.summary()["energy_kwh_estimate"] == 0
    assert ledger.summary()["gap_count"] >= 1


def test_other_event_does_not_close_a_power_interval_without_a_new_report():
    ledger = PowerLedger()
    power = state(1, T, "kW")
    ledger.observe(T, power, state("on", T), None, [prices()])
    mode_change = T + timedelta(minutes=4)
    ledger.observe(mode_change, power, state("off", mode_change), None, [prices()])
    assert ledger.summary()["energy_kwh_estimate"] == 0
    assert ledger.summary()["qualified_seconds"] == 0
    assert ledger.anchor is None
    for minute in (5, 10):
        at = T + timedelta(minutes=minute)
        ledger.observe(at, state(1, at, "kW", T), None, None, [prices()])
    assert ledger.summary()["energy_kwh_estimate"] == pytest.approx(1 / 12, abs=0.0001)


def test_hot_water_and_ambiguous_modes_are_separate():
    ledger = PowerLedger()
    ledger.observe(T, state(1, T, "kW"), state("off", T), state("on", T), [prices()])
    at = T + timedelta(minutes=5)
    ledger.observe(
        at,
        state(1, at, "kW"),
        state("off", at, changed=T),
        state("on", at, changed=T),
        [prices()],
    )
    result = ledger.summary()
    assert result["hot_water_mode_kwh_estimate"] == pytest.approx(1 / 12, abs=0.0001)
    assert result["radiator_mode_kwh_estimate"] is None


def test_replayed_clock_never_double_counts():
    ledger = PowerLedger()
    at = T + timedelta(minutes=3)
    ledger.observe(at, state(1, at, "kW"), None, None, [prices()])
    ledger.observe(T, state(1, T, "kW"), None, None, [prices()])
    ledger.observe(at, state(1, at, "kW"), None, None, [prices()])
    assert ledger.summary()["energy_kwh_estimate"] == 0


def test_mode_freshness_does_not_attribute_stale_status():
    ledger = PowerLedger()
    ledger.observe(T, state(1, T, "kW"), state("on", T), None, [prices()])
    for minute in (5, 10):
        at = T + timedelta(minutes=minute)
        ledger.observe(at, state(1, at, "kW", T), state("on", T), None, [prices()])
    result = ledger.summary()
    assert result["energy_kwh_estimate"] == pytest.approx(1 / 6, abs=0.0001)
    assert result["radiator_mode_kwh_estimate"] == pytest.approx(1 / 12, abs=0.0001)


def test_configured_unavailable_hot_water_never_counts_as_verified_radiator_mode():
    ledger = PowerLedger()
    for minute in (0, 5):
        at = T + timedelta(minutes=minute)
        ledger.observe(
            at,
            state(1, at, "kW", T),
            state("on", at, changed=T),
            None,
            [prices()],
            hot_water_configured=True,
        )
    assert ledger.summary()["energy_kwh_estimate"] == pytest.approx(1 / 12, abs=0.0001)
    assert ledger.summary()["radiator_mode_kwh_estimate"] is None


def test_mode_cost_tracks_only_known_hot_water_interval():
    ledger = PowerLedger()
    for minute in (0, 5):
        at = T + timedelta(minutes=minute)
        ledger.observe(
            at,
            state(1, at, "kW", T),
            state("off", at, changed=T),
            state("on", at, changed=T),
            [prices()],
        )
    result = ledger.summary()
    assert result["hot_water_cost_estimate"] == pytest.approx(1 / 12, abs=0.0001)
    assert result["radiator_cost_estimate"] is None


def test_mode_cost_is_unknown_when_some_mode_energy_has_no_price():
    ledger = PowerLedger()
    ledger.observe(T, state(1, T, "kW"), state("on", T), state("off", T), [])
    at = T + timedelta(minutes=5)
    ledger.observe(
        at, state(1, at, "kW", T), state("off", at), state("on", at), [prices()]
    )
    at = T + timedelta(minutes=10)
    ledger.observe(
        at,
        state(1, at, "kW", T),
        state("off", at, changed=T + timedelta(minutes=5)),
        state("on", at, changed=T + timedelta(minutes=5)),
        [prices()],
    )
    result = ledger.summary()
    assert result["radiator_mode_kwh_estimate"] > 0
    assert result["radiator_cost_estimate"] is None
    assert result["hot_water_cost_estimate"] == pytest.approx(1 / 12, abs=0.0001)


def test_dst_fold_uses_elapsed_utc_and_timestamped_prices():
    from zoneinfo import ZoneInfo

    utc_start = datetime(2026, 10, 25, 0, 50, tzinfo=timezone.utc)
    price = SimpleNamespace(
        state="1",
        last_reported=utc_start,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {
                    "start": utc_start.isoformat(),
                    "end": (utc_start + timedelta(minutes=10)).isoformat(),
                    "value": 1,
                },
                {
                    "start": (utc_start + timedelta(minutes=10)).isoformat(),
                    "end": (utc_start + timedelta(minutes=30)).isoformat(),
                    "value": 2,
                },
            ],
        },
    )
    ledger = PowerLedger()
    for minute in (0, 5, 10, 15, 20):
        utc = utc_start + timedelta(minutes=minute)
        local = utc.astimezone(ZoneInfo("Europe/Stockholm"))
        ledger.observe(local, state(1, local, "kW", utc_start), None, None, [price])
    assert ledger.summary()["energy_kwh_estimate"] == pytest.approx(1 / 3, abs=0.0001)
    assert ledger.summary()["cost_estimate"] == pytest.approx(0.5, abs=0.0001)


def test_stale_price_is_unpriced_not_permanent_mixed_currency():
    ledger = PowerLedger()
    start = T + timedelta(hours=23, minutes=55)
    price = SimpleNamespace(
        state="1",
        last_reported=T,
        attributes={
            "unit_of_measurement": "SEK/kWh",
            "raw_today": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(minutes=30)).isoformat(),
                    "value": 1,
                }
            ],
        },
    )
    for minute in (0, 5, 6, 7, 8):
        at = start + timedelta(minutes=minute)
        if minute >= 7:
            price.last_reported = at
        ledger.observe(at, state(1, at, "kW", start), None, None, [price])
    assert ledger.summary()["currency"] == "SEK"
    assert ledger.summary()["cost_estimate"] is not None
    assert (
        ledger.summary()["priced_energy_kwh_estimate"]
        < ledger.summary()["energy_kwh_estimate"]
    )


def test_unconfigured_modes_are_unknown_not_zero():
    ledger = PowerLedger()
    ledger.observe(T, state(1, T, "kW"), None, None, [prices()])
    at = T + timedelta(minutes=5)
    ledger.observe(at, state(1, at, "kW", T), None, None, [prices()])
    result = ledger.summary()
    assert result["unclassified_kwh_estimate"] == pytest.approx(1 / 12, abs=0.0001)
    assert result["radiator_mode_kwh_estimate"] is None
    assert result["hot_water_mode_kwh_estimate"] is None
    assert result["radiator_cost_estimate"] is None


def test_invalid_unit_clears_anchor_instead_of_bridging_data():
    ledger = PowerLedger()
    ledger.observe(T, state(1, T, "kW"), None, None, [prices()])
    at = T + timedelta(minutes=2)
    ledger.observe(at, state(1, at, []), None, None, [prices()])
    at = T + timedelta(minutes=4)
    ledger.observe(at, state(0.5, at, "kW"), None, None, [prices()])
    assert ledger.summary()["energy_kwh_estimate"] == 0
