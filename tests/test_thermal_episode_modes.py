"""Diagnostic mode provenance must support the controller's actual states."""

from datetime import datetime, timezone
import pytest
from custom_components.pumpsteer.thermal_intelligence import (
    TemperatureMeasurement,
    ThermalDiagnostics,
)


@pytest.mark.parametrize(
    "mode",
    [
        "normal",
        "preheating",
        "pre_braking",
        "braking",
        "brake_hold",
        "recovery",
        "precool",
        "safe_mode",
        "sensor_interruption",
    ],
)
def test_actual_diagnostic_modes_are_preserved_without_touching_control(mode):
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    predictions = ThermalDiagnostics().observe(
        now=now,
        indoor=TemperatureMeasurement(21, now),
        outdoor=TemperatureMeasurement(5, now),
        k_per_hour=0.05,
        operating_mode=mode,
    )
    assert len(predictions) == 3
    assert all(p.operating_mode == mode for p in predictions)
