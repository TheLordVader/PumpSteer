"""Synthetic operation evidence; no household data or physical HA calls."""

from datetime import timedelta

import pytest
from test_heat_operation import T, reading

from custom_components.pumpsteer.heat_operation import HeatOperation


@pytest.mark.parametrize("watts", [0, 50, 1200])
def test_compressor_flag_and_hot_pipes_never_prove_delivered_heat(watts):
    old = T - timedelta(minutes=2)
    result = HeatOperation().observe(
        T,
        compressor=reading("on", T, changed=old),
        radiator=reading("on", T, changed=old),
        water=reading("off", T, changed=old),
        power=reading(watts, T, "W"),
        supply=reading(35, T, "°C"),
        return_temp=reading(30, T, "°C"),
    )
    assert not result["learning_qualified"]
    assert result["quality"] != "verified"
    assert result["physical_measurement_time"] == "unknown"


def test_hot_water_selection_excludes_room_learning_even_with_heat_label():
    result = HeatOperation().observe(
        T, heat=reading("on", T), compressor=reading("on", T), water=reading("on", T)
    )
    assert result["mode"] == "hot_water"
    assert not result["learning_qualified"]


def test_unchanged_stale_on_flag_cannot_establish_heat():
    result = HeatOperation().observe(
        T,
        compressor=reading("on", T - timedelta(hours=4)),
        power=reading(50, T, "W"),
        supply=reading(35, T, "°C"),
        return_temp=reading(30, T, "°C"),
    )
    assert "compressor" not in result["signals"]
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]


def test_hot_pipes_without_flow_are_only_gradient_evidence():
    result = HeatOperation().observe(
        T, supply=reading(35, T, "°C"), return_temp=reading(30, T, "°C")
    )
    assert result["delta_t_status"] == "positive_transfer_indication"
    assert result["mode"] == "unknown"
    assert not result["learning_qualified"]


def test_accepted_heat_label_is_not_physical_heat_delivery_verification():
    result = HeatOperation().observe(T, heat=reading("on", T))
    assert result["learning_qualified"]  # existing independent-label contract
    assert result["heat_delivery_verified"] is False


def test_conflicting_operating_reports_have_an_explicit_reason():
    result = HeatOperation().observe(
        T, heat=reading("on", T), compressor=reading("off", T)
    )
    assert result["mode"] == "unknown"
    assert result["reason"] == "contradictory_operating_reports"
    assert not result["learning_qualified"]
