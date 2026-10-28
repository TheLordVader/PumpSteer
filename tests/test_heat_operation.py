"""Operation evidence must remain distinct from measured heat output."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.pumpsteer.heat_operation import HeatOperation

T = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


def reading(value, when, unit=None, *, restored=False, updated=None, changed=None):
    return SimpleNamespace(
        state=str(value),
        last_reported=when,
        last_changed=changed or when,
        last_updated=updated or when,
        attributes={"unit_of_measurement": unit, "restored": restored},
    )


def test_water_and_contradictory_signals_are_not_radiator_heat():
    model = HeatOperation()
    result = model.observe(T, heat=reading("on", T), water=reading("on", T))
    assert result["mode"] == "hot_water"
    assert not result["learning_qualified"]
    result = model.observe(T, heat=reading("on", T), radiator=reading("off", T))
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]


def test_hot_water_valve_is_a_mode_report_not_verified_production():
    model = HeatOperation()
    result = model.observe(
        T,
        water=reading("on", T),
        compressor=reading("off", T),
        power=reading("100", T, "W"),
    )
    assert result["mode"] == "hot_water"
    assert result["quality"] == "probable"
    assert result["reason"] == "hot_water_mode_report_not_production"
    assert not result["learning_qualified"]


def test_direct_heat_signal_and_restored_signal():
    model = HeatOperation()
    result = model.observe(T, heat=reading("on", T), radiator=reading("on", T))
    assert result["mode"] == "space_heating"
    assert result["quality"] == "verified"
    assert result["learning_qualified"]
    result = model.observe(T, heat=reading("on", T, restored=True))
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]


def test_automatic_power_calibration_requires_independent_labels_and_fresh_samples():
    model = HeatOperation()
    for i in range(6):
        at = T + timedelta(minutes=5 * i)
        active = i % 2 == 1
        result = model.observe(
            at,
            heat=reading(
                "on" if active else "off", at, changed=at - timedelta(minutes=2)
            ),
            compressor=reading(
                "on" if active else "off", at, changed=at - timedelta(minutes=2)
            ),
            radiator=reading("on", at, changed=at - timedelta(minutes=2)),
            water=reading("off", at, changed=at - timedelta(minutes=2)),
            power=reading("1200" if active else "100", at, "W"),
            supply=reading("35" if active else "25", at, "°C"),
            return_temp=reading("30" if active else "24", at, "°C"),
        )
        assert result["mode"] == ("space_heating" if active else "idle")
    assert result["calibration"] == "ready"
    at += timedelta(minutes=5)
    result = model.observe(
        at,
        compressor=reading("on", at, changed=at - timedelta(minutes=2)),
        radiator=reading("on", at, changed=at - timedelta(minutes=2)),
        water=reading("off", at, changed=at - timedelta(minutes=2)),
        power=reading("1200", at, "W"),
        supply=reading("35", at, "°C"),
        return_temp=reading("30", at, "°C"),
    )
    assert result["mode"] == "space_heating"
    assert result["quality"] == "probable"
    assert not result["learning_qualified"]
    for kwargs in (
        {"power": reading("100", at, "W")},
        {"supply": reading("25", at, "°C")},
        {"return_temp": reading("35", at, "°C")},
        {"radiator": reading("off", at, changed=at - timedelta(minutes=2))},
    ):
        inputs = {
            "compressor": reading("on", at, changed=at - timedelta(minutes=2)),
            "radiator": reading("on", at, changed=at - timedelta(minutes=2)),
            "water": reading("off", at, changed=at - timedelta(minutes=2)),
            "power": reading("1200", at, "W"),
            "supply": reading("35", at, "°C"),
            "return_temp": reading("30", at, "°C"),
        }
        inputs.update(kwargs)
        assert model.observe(at, **inputs)["mode"] == "unknown"
    result = model.observe(
        at,
        compressor=reading("on", at),
        radiator=reading("on", at),
        water=reading("off", at),
        power=reading("1200", at, "W"),
        supply=reading("35", at, "°C"),
        return_temp=reading("30", at, "°C"),
    )
    assert result["mode"] == "unknown"


def test_no_learning_from_repeated_sample_or_stale_compressor():
    model = HeatOperation()
    for i in range(20):
        at = T + timedelta(seconds=i)
        result = model.observe(
            at,
            heat=reading("off", T),
            compressor=reading("off", T),
            power=reading("100", T, "W"),
        )
    assert result["calibration"] == "insufficient_evidence"
    result = model.observe(
        T + timedelta(minutes=10),
        compressor=reading("on", T),
        power=reading("100", T + timedelta(minutes=10), "W"),
    )
    assert result["mode"] == "unknown"


def test_restored_calibration_requires_new_independent_evidence():
    model = HeatOperation()
    model.samples = {"idle": [0.1] * 3, "heating": [1.2] * 3}
    restored = HeatOperation()
    assert restored.restore(model.export())
    assert restored.observe(T)["calibration"] == "insufficient_evidence"
    assert not HeatOperation().restore({"idle": [float("nan")], "heating": []})


def test_power_report_before_operating_transition_never_labels_calibration():
    model = HeatOperation()
    now = T + timedelta(minutes=4)
    model.observe(
        now,
        heat=reading("on", now),
        compressor=reading("on", now),
        radiator=reading("on", now),
        water=reading("off", now),
        power=reading("100", T, "W"),
    )
    assert model.samples == {"idle": [], "heating": []}


def test_old_heat_report_after_hot_water_ends_cannot_qualify_learning():
    model = HeatOperation()
    now = T + timedelta(minutes=3)
    result = model.observe(
        now,
        heat=reading("on", T),
        radiator=reading("on", now),
        water=reading("off", now),
    )
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]


def test_old_heat_report_after_compressor_restart_is_not_qualified():
    model = HeatOperation()
    now = T + timedelta(minutes=3)
    result = model.observe(now, heat=reading("on", T), compressor=reading("on", now))
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]


def test_repeat_reports_are_observed_but_do_not_make_stale_data_fresh():
    model = HeatOperation()
    at = T + timedelta(minutes=1)
    signal = reading("on", at, updated=T)
    result = model.observe(at, heat=signal)
    assert result["reporting"]["heat"] == "repeated_reports_observed"
    result = model.observe(at + timedelta(minutes=6), heat=signal)
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]


def test_temperature_calibration_does_not_require_electrical_power():
    model = HeatOperation()
    for minute in (2, 7, 12):
        at = T + timedelta(minutes=minute)
        model.observe(
            at,
            heat=reading("on", at, changed=T),
            radiator=reading("on", at, changed=T),
            water=reading("off", at, changed=T),
            supply=reading(35, at, "°C"),
            return_temp=reading(30, at, "°C"),
        )
    assert model.temperatures == {"supply": [35] * 3, "return": [30] * 3}
    assert model.samples == {"idle": [], "heating": []}


def test_temperature_calibration_rejects_configured_unavailable_compressor():
    model = HeatOperation()
    now = T + timedelta(minutes=2)
    model.observe(
        now,
        heat=reading("on", now, changed=T),
        compressor=reading("unavailable", now, changed=T),
        supply=reading(35, now, "°C"),
        return_temp=reading(30, now, "°C"),
    )
    assert model.temperatures == {"supply": [], "return": []}


def test_delta_t_is_aligned_report_evidence_not_thermal_power():
    result = HeatOperation().observe(
        T,
        supply=reading(35, T, "°C"),
        return_temp=reading(30, T - timedelta(seconds=30), "°C"),
    )
    assert result["delta_t_c"] == 5
    assert result["delta_t_status"] == "positive_transfer_indication"
    assert not result["learning_qualified"]


def test_delta_t_rejects_asynchronous_stale_and_restored_reports():
    for state in (
        reading(30, T - timedelta(minutes=2), "°C"),
        reading(30, T - timedelta(minutes=6), "°C"),
        reading(30, T, "°C", restored=True),
        reading(30, T, "W"),
    ):
        result = HeatOperation().observe(
            T, supply=reading(35, T, "°C"), return_temp=state
        )
        assert result["delta_t_c"] is None
        assert not result["learning_qualified"]


def test_delta_t_never_promotes_hot_water_or_residual_heat_to_verified_heating():
    for water, compressor in (("on", "on"), ("off", "off")):
        result = HeatOperation().observe(
            T,
            supply=reading(40, T, "°C"),
            return_temp=reading(30, T, "°C"),
            water=reading(water, T),
            compressor=reading(compressor, T),
            radiator=reading("off" if water == "on" else "on", T),
            power=reading(1000, T, "W"),
        )
        assert result["delta_t_c"] == 10
        assert not result["learning_qualified"]


def test_calibrated_probable_heating_requires_aligned_temperature_reports():
    model = HeatOperation()
    model.samples = {"idle": [0.1] * 3, "heating": [1.2] * 3}
    model.temperatures = {"supply": [35.0] * 3, "return": [30.0] * 3}
    for skew, expected in ((0, "space_heating"), (120, "unknown")):
        changed = T - timedelta(minutes=10)
        result = model.observe(
            T,
            compressor=reading("on", T, changed=changed),
            radiator=reading("on", T, changed=changed),
            water=reading("off", T, changed=changed),
            power=reading(1200, T, "W"),
            supply=reading(35, T, "°C"),
            return_temp=reading(30, T - timedelta(seconds=skew), "°C"),
        )
        assert result["mode"] == expected
        assert not result["learning_qualified"]
