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


def test_economic_evidence_exposes_partial_vs_complete_without_fabricating_rate():
    ledger = PowerLedger()
    ledger.energy = 2.0
    ledger.priced_energy = 1.0
    ledger.cost = 2.0
    ledger.currency = "SEK"
    result = ledger.summary()
    assert result["price_coverage"] == "partial"
    assert result["effective_price_per_kwh"] is None
    ledger.priced_energy = 2.0
    assert ledger.summary()["price_coverage"] == "complete"
    assert ledger.summary()["effective_price_per_kwh"] == 1.0
    ledger.mixed_currency = True
    assert ledger.summary()["price_coverage"] == "unavailable"
    assert ledger.summary()["effective_price_per_kwh"] is None


def test_accumulated_energy_meter_records_wh_delta():
    ledger = PowerLedger()
    ledger.observe_energy(T, {"meter": state(1000, T, "Wh")}, [])
    at = T + timedelta(minutes=5)
    ledger.observe_energy(at, {"meter": state(1250, at, "Wh")}, [])
    assert ledger.summary()["energy_kwh_estimate"] == pytest.approx(0.25)


def test_accumulated_energy_reset_and_gap_are_not_counted():
    ledger = PowerLedger()
    for minute, value in ((0, 10), (1, 11), (2, 1), (10, 5), (11, 6)):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(at, {"meter": state(value, at, "kWh")}, [])
    assert ledger.summary()["energy_kwh_estimate"] == 6
    assert ledger.summary()["gap_count"] == 1


def test_accumulated_energy_does_not_bridge_unavailable_reading():
    ledger = PowerLedger()
    ledger.observe_energy(T, {"meter": state(1, T, "kWh")}, [])
    at = T + timedelta(minutes=1)
    ledger.observe_energy(at, {"meter": state("unavailable", at, "kWh")}, [])
    at += timedelta(minutes=1)
    ledger.observe_energy(at, {"meter": state(2, at, "kWh")}, [])
    assert ledger.summary()["energy_kwh_estimate"] == 0


def test_asynchronous_meters_qualify_independently_and_sum_only_common_time():
    ledger = PowerLedger()

    def report(name, minute, value):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(
            at, {name: state(value, at, "kWh")}, [prices()], meter_ids=("a", "b")
        )

    report("a", 0, 0)
    report("b", 1, 0)
    report("a", 2, 2)
    assert ledger.summary()["qualified_meter_energy_kwh"] == pytest.approx(2)
    assert ledger.summary()["energy_kwh_estimate"] == 0
    report("b", 3, 2)
    result = ledger.summary()
    assert result["qualified_meter_energy_kwh"] == pytest.approx(4)
    assert result["energy_kwh_estimate"] == 0
    assert result["qualified_seconds"] == 0
    assert result["cost_estimate"] is None


def test_energy_meter_price_boundary_is_time_weighted_and_unpriced_if_missing():
    ledger = PowerLedger()
    for minute, value in ((14, 0), (16, 2)):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(at, {"a": state(value, at, "kWh")}, [prices()])
    result = ledger.summary()
    assert result["cost_estimate"] is None
    assert result["priced_energy_kwh_estimate"] == 0
    assert result["method"] == "cumulative_energy_counter_deltas"


def test_still_meter_reports_are_valid_and_duplicate_reports_are_ignored():
    ledger = PowerLedger()
    for minute in (0, 1, 1, 2):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(at, {"a": state(10, at, "kWh", T)}, [])
    assert ledger.summary()["qualified_seconds"] == 120
    assert ledger.summary()["gap_count"] == 0


def test_meter_dropout_keeps_qualified_energy_separate_from_common_coverage():
    ledger = PowerLedger()
    for name, minute, value in (
        ("a", 0, 0),
        ("b", 0, 0),
        ("a", 1, 1),
        ("b", 1, 1),
        ("a", 2, 2),
        ("a", 3, 3),
        ("b", 7, 7),
        ("a", 7, 7),
    ):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(
            at, {name: state(value, at, "kWh")}, [], meter_ids=("a", "b")
        )
    result = ledger.summary()
    assert result["qualified_meter_energy_kwh"] == 14
    assert result["energy_kwh_estimate"] == 14
    assert result["qualified_seconds"] == 420
    assert result["gap_count"] == 0
    assert result["meter_count"] == 2
    assert result["energy_allocation"] == "unknown_within_counter_interval"


def test_price_change_event_invalidates_price_but_not_counter_delta():
    ledger = PowerLedger()
    ledger.observe_energy(T, {"a": state(0, T, "kWh")}, [prices()])
    ledger.invalidate_energy_prices()
    at = T + timedelta(minutes=1)
    ledger.observe_energy(at, {"a": state(1, at, "kWh")}, [prices()])
    result = ledger.summary()
    assert result["energy_kwh_estimate"] == 1
    assert result["priced_energy_kwh_estimate"] == 0
    assert result["cost_estimate"] is None


def test_reset_anchor_discards_pending_intervals_without_resetting_totals():
    ledger = PowerLedger()
    for name in ("a", "b"):
        ledger.observe_energy(T, {name: state(0, T, "kWh")}, [], meter_ids=("a", "b"))
    at = T + timedelta(minutes=1)
    ledger.observe_energy(at, {"a": state(1, at, "kWh")}, [], meter_ids=("a", "b"))
    ledger.reset_anchor()
    ledger.observe_energy(at, {"b": state(1, at, "kWh")}, [], meter_ids=("a", "b"))
    assert ledger.summary()["energy_kwh_estimate"] == 0
    assert ledger.summary()["qualified_meter_energy_kwh"] == 1


def test_energy_prices_require_only_the_actual_report_interval():
    ledger = PowerLedger()
    source = prices()
    source.attributes["raw_today"] = [
        {
            "start": T.isoformat(),
            "end": (T + timedelta(minutes=2)).isoformat(),
            "value": 2,
        }
    ]
    ledger.observe_energy(T, {"a": state(0, T, "kWh")}, [source])
    at = T + timedelta(minutes=2)
    ledger.observe_energy(at, {"a": state(1, at, "kWh")}, [source])
    assert ledger.summary()["cost_estimate"] == 2


def test_invalid_price_unit_does_not_discard_valid_counter_energy():
    ledger = PowerLedger()
    price = prices()
    price.attributes["unit_of_measurement"] = []
    ledger.observe_energy(T, {"a": state(0, T, "kWh")}, [price])
    at = T + timedelta(minutes=1)
    ledger.observe_energy(at, {"a": state(1, at, "kWh")}, [price])
    assert ledger.summary()["energy_kwh_estimate"] == 1
    assert ledger.summary()["cost_estimate"] is None


def test_unchanged_price_forecast_survives_price_entity_report():
    ledger = PowerLedger()
    start = T + timedelta(minutes=14)
    ledger.observe_energy(start, {"a": state(0, start, "kWh")}, [prices()])
    change = T + timedelta(minutes=15)
    ledger.invalidate_energy_prices([prices()], change)
    end = T + timedelta(minutes=16)
    ledger.observe_energy(end, {"a": state(2, end, "kWh")}, [prices()])
    assert ledger.summary()["cost_estimate"] is None


def test_changed_price_forecast_invalidates_started_interval():
    ledger = PowerLedger()
    start = T + timedelta(minutes=14)
    ledger.observe_energy(start, {"a": state(0, start, "kWh")}, [prices()])
    changed = prices()
    changed.attributes["raw_today"][1]["value"] = 4
    ledger.invalidate_energy_prices([changed], T + timedelta(minutes=15))
    end = T + timedelta(minutes=16)
    ledger.observe_energy(end, {"a": state(2, end, "kWh")}, [changed])
    assert ledger.summary()["energy_kwh_estimate"] == 2
    assert ledger.summary()["cost_estimate"] is None


def test_partial_price_coverage_prices_only_known_part_of_meter_interval():
    ledger = PowerLedger()
    source = prices()
    source.attributes["raw_today"] = [
        {
            "start": (T + timedelta(minutes=1)).isoformat(),
            "end": (T + timedelta(minutes=2)).isoformat(),
            "value": 2,
        }
    ]
    ledger.observe_energy(T, {"a": state(0, T, "kWh")}, [source])
    end = T + timedelta(minutes=2)
    ledger.observe_energy(end, {"a": state(2, end, "kWh")}, [source])
    result = ledger.summary()
    assert result["energy_kwh_estimate"] == 2
    assert result["priced_energy_kwh_estimate"] == 0
    assert result["cost_estimate"] is None
    assert result["price_coverage"] == "unavailable"


def test_poll_rejects_attribute_change_that_may_have_a_queued_event():
    ledger = PowerLedger(started_after=T - timedelta(seconds=1))
    original = state(1, T, "kWh")
    original.last_updated = T
    ledger.observe_energy(T, {"a": original}, [])
    at = T + timedelta(minutes=1)
    updated = state(1, at, "kWh", T)
    updated.last_updated = at
    assert not ledger.can_poll_energy("a", updated)
    updated.last_updated = T
    assert ledger.can_poll_energy("a", updated)


def test_reset_anchor_with_energy_gap_reports_gap():
    ledger = PowerLedger()
    ledger.observe_energy(T, {"a": state(1, T, "kWh")}, [])
    ledger.reset_anchor(gap=True)
    assert ledger.summary()["gap_count"] == 1


def test_retroactively_corrected_price_invalidates_interval():
    ledger = PowerLedger()
    start = T + timedelta(minutes=14)
    ledger.observe_energy(start, {"a": state(0, start, "kWh")}, [prices()])
    changed = prices()
    changed.last_reported = T + timedelta(minutes=15, seconds=30)
    changed.attributes["raw_today"][0]["value"] = 5
    ledger.invalidate_energy_prices([changed], changed.last_reported)
    end = T + timedelta(minutes=16)
    ledger.observe_energy(end, {"a": state(2, end, "kWh")}, [changed])
    assert ledger.summary()["energy_kwh_estimate"] == 2
    assert ledger.summary()["cost_estimate"] is None


def test_unchanged_price_report_preserves_pending_async_meter_interval():
    ledger = PowerLedger()
    source = prices()
    ledger.sync_energy_prices([source], T)
    for name in ("a", "b"):
        ledger.observe_energy(
            T, {name: state(0, T, "kWh")}, [source], meter_ids=("a", "b")
        )
    at = T + timedelta(minutes=1)
    ledger.observe_energy(
        at, {"a": state(1, at, "kWh")}, [source], meter_ids=("a", "b")
    )
    newer = prices()
    newer.last_changed = at
    newer.last_updated = at
    newer.last_reported = at
    ledger.sync_energy_prices([newer], at)
    ledger.observe_energy(at, {"b": state(1, at, "kWh")}, [newer], meter_ids=("a", "b"))
    assert ledger.summary()["energy_kwh_estimate"] == 2
    assert ledger.summary()["priced_energy_kwh_estimate"] == 2
    assert ledger.summary()["cost_estimate"] == 2


def test_counter_interval_spanning_price_change_is_not_verified_cost():
    ledger = PowerLedger()
    for minute, value in ((14, 0), (16, 2)):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(at, {"a": state(value, at, "kWh")}, [prices()])
    result = ledger.summary()
    assert result["energy_kwh_estimate"] == 2
    assert result["priced_energy_kwh_estimate"] == 0
    assert result["cost_estimate"] is None


def test_long_counter_interval_preserves_energy_without_price():
    ledger = PowerLedger()
    ledger.observe_energy(T, {"a": state(10, T, "kWh")}, [])
    at = T + timedelta(hours=2)
    ledger.observe_energy(at, {"a": state(12, at, "kWh")}, [])
    assert ledger.summary()["energy_kwh_estimate"] == 2
    assert ledger.summary()["cost_estimate"] is None


def test_async_counter_partial_overlap_is_not_interpolated():
    ledger = PowerLedger()
    for name, minute, value in (("a", 0, 0), ("b", 1, 0), ("a", 2, 1), ("b", 3, 1)):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(
            at, {name: state(value, at, "kWh")}, [], meter_ids=("a", "b")
        )
    assert ledger.summary()["qualified_meter_energy_kwh"] == 2
    assert ledger.summary()["energy_kwh_estimate"] == 0


def test_async_shared_window_equals_total_with_stationary_meter():
    ledger = PowerLedger()
    for name, minute, value in (
        ("a", 0, 0),
        ("b", 0, 10),
        ("a", 10, 1),
        ("a", 20, 2),
        ("b", 20, 10),
    ):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(
            at, {name: state(value, at, "kWh")}, [], meter_ids=("a", "b")
        )
    total = PowerLedger()
    for minute, value in ((0, 10), (20, 12)):
        at = T + timedelta(minutes=minute)
        total.observe_energy(at, {"total": state(value, at, "kWh")}, [])
    assert ledger.energy == total.energy == 2
    assert ledger.seconds == total.seconds == 1200


def test_constant_price_long_interval_and_unit_change():
    ledger = PowerLedger()
    source = prices()
    for entry in source.attributes["raw_today"]:
        entry["value"] = 2
    for minute, value, unit in (
        (0, 10, "kWh"),
        (30, 12, "kWh"),
        (40, 13000, "Wh"),
        (50, 14000, "Wh"),
    ):
        at = T + timedelta(minutes=minute)
        ledger.observe_energy(at, {"a": state(value, at, unit)}, [source])
    assert ledger.energy == 3
    assert ledger.priced_energy == 3
    assert ledger.cost == 6
    assert ledger.gaps == 1


def test_counter_expiry_and_unreasonable_jump_restart_anchor():
    ledger = PowerLedger()
    for hour, value in ((0, 0), (25, 1), (26, 1000), (27, 1001)):
        at = T + timedelta(hours=hour)
        ledger.observe_energy(at, {"a": state(value, at, "kWh")}, [])
    assert ledger.energy == 1
    assert ledger.gaps == 2


def test_long_counter_interval_rechecks_corrected_price_after_first_five_minutes():
    ledger = PowerLedger()
    original = prices()
    for row in original.attributes["raw_today"]:
        row["value"] = 2
    ledger.observe_energy(T, {"a": state(0, T, "kWh")}, [original])
    corrected = prices()
    corrected.attributes["raw_today"][0]["value"] = 2
    corrected.attributes["raw_today"][1]["value"] = 3
    corrected.last_reported = T + timedelta(minutes=20)
    ledger.invalidate_energy_prices([corrected], corrected.last_reported)
    end = T + timedelta(minutes=30)
    ledger.observe_energy(end, {"a": state(1, end, "kWh")}, [corrected])
    assert ledger.energy == 1
    assert ledger.priced_energy == 0
    assert ledger.summary()["cost_estimate"] is None
