"""Synthetic 15-minute price/weather cases; no physical outputs or savings claims."""

from datetime import UTC, datetime, timedelta

import pytest
from test_bridge_short_dip import _make_sensor
from test_bridge_short_dip import _run_cycle as run_existing_cycle

from custom_components.pumpsteer.electricity_price import classify_price_list
from custom_components.pumpsteer.forecast import ForecastPoint, analyze_thermal_outlook
from custom_components.pumpsteer.sensor import MODE_BRAKE_HOLD, MODE_PI, MODE_PREHEAT

NOW = datetime(2026, 1, 12, 8, tzinfo=UTC)
NORMAL = "normal"
EXPENSIVE = "expensive"


def _run_cycle(sensor, monkeypatch, now, categories, forecast_temps=None):
    # Synthetic SEK/kWh cheap/normal valley followed by a costly peak.
    prices = [0.8 if c == NORMAL else 2.5 for c in categories]
    if forecast_temps is not None:
        from custom_components.pumpsteer import forecast as forecast_module

        sensor._config_entry.options["weather_entity"] = "weather.synthetic"

        async def build_forecast(*args, **kwargs):
            return [
                ForecastPoint(
                    timestamp=now + timedelta(hours=i),
                    price=None,
                    outdoor_temp=t,
                    wind_speed=0,
                    wind_gust_speed=0,
                )
                for i, t in enumerate(forecast_temps)
            ]

        monkeypatch.setattr(forecast_module, "async_build_forecast", build_forecast)
    assert classify_price_list(prices, 0.4, 1.5) == categories
    return run_existing_cycle(
        sensor,
        monkeypatch,
        now,
        categories,
        forecast_temps=forecast_temps,
        prices=prices,
    )


@pytest.mark.parametrize(
    "inertia,normal_slots,configured,limit,holds",
    [
        (1, 3, 30, 30, False),
        (2, 2, 30, 30, True),
        (2, 3, 30, 30, False),
        (4, 2, 30, 39, True),
        (6, 3, 30, 54, True),
        (6, 4, 30, 54, False),
        (10, 5, 30, 90, True),
        (6, 4, 70, 70, True),
    ],
)
def test_inertia_scaled_bridge(
    inertia, normal_slots, configured, limit, holds, monkeypatch
):
    sensor = _make_sensor(configured)
    sensor._config_entry.options["house_inertia"] = inertia
    sensor._brake_ramp = 0.8
    sensor._brake_last_t = NOW - timedelta(minutes=1)
    result = _run_cycle(sensor, monkeypatch, NOW, [NORMAL] * normal_slots + [EXPENSIVE])
    assert result["extra"]["bridge_limit_minutes"] == limit
    assert result["extra"]["bridge_short_dip"] is holds
    assert result["mode"] == (MODE_BRAKE_HOLD if holds else MODE_PI)
    assert (sensor._brake_ramp == 0.8) is holds


def outlook(temps):
    return analyze_thermal_outlook(
        [
            ForecastPoint(
                timestamp=NOW + timedelta(hours=i),
                price=None,
                outdoor_temp=t,
                wind_speed=0,
                wind_gust_speed=0,
            )
            for i, t in enumerate(temps)
        ],
        18,
    )


@pytest.mark.parametrize("temps", [[-5.0] * 24, [5 - i for i in range(24)]])
def test_price_preheat_survives_steady_cold_and_cold_front(temps, monkeypatch):
    sensor = _make_sensor()
    sensor._config_entry.options["preheat_boost_enabled"] = True
    sensor._was_braking_last_cycle = False
    sensor._last_outlook = outlook(temps)
    result = _run_cycle(
        sensor, monkeypatch, NOW, [NORMAL] * 4 + [EXPENSIVE] * 4, forecast_temps=temps
    )
    assert result["mode"] == MODE_PREHEAT
    assert result["extra"]["preheat_strength"] > 0


@pytest.mark.parametrize("temps", [[-5.0] * 24, [5 - i for i in range(24)]])
def test_weather_without_price_peak_does_not_activate_preheat(temps, monkeypatch):
    sensor = _make_sensor()
    sensor._config_entry.options["preheat_boost_enabled"] = True
    sensor._was_braking_last_cycle = False
    sensor._last_outlook = outlook(temps)
    result = _run_cycle(sensor, monkeypatch, NOW, [NORMAL] * 12, forecast_temps=temps)
    assert result["mode"] == MODE_PI


def test_heavy_house_bridge_prevents_preheat_takeover(monkeypatch):
    sensor = _make_sensor()
    sensor._config_entry.options.update(house_inertia=6, preheat_boost_enabled=True)
    sensor._last_outlook = outlook([-5.0] * 24)
    sensor._brake_ramp = 0.8
    sensor._brake_last_t = NOW - timedelta(minutes=1)
    result = _run_cycle(
        sensor, monkeypatch, NOW, [NORMAL] * 3 + [EXPENSIVE], forecast_temps=[-5.0] * 24
    )
    assert result["mode"] == MODE_BRAKE_HOLD
    assert sensor._brake_ramp == 0.8


def test_heavy_house_bridge_cannot_override_comfort_floor(monkeypatch):
    sensor = _make_sensor()
    sensor._config_entry.options["house_inertia"] = 6
    sensor.hass.states._mapping["sensor.indoor"] = "19.0"
    sensor._brake_ramp = 0.8
    sensor._brake_last_t = NOW - timedelta(minutes=1)
    result = _run_cycle(sensor, monkeypatch, NOW, [NORMAL] * 3 + [EXPENSIVE])
    assert not result["extra"]["bridge_short_dip"]
    assert result["mode"] == MODE_PI
    assert sensor._brake_ramp < 0.8


def test_warming_forecast_still_suppresses_preheat_before_price_peak(monkeypatch):
    sensor = _make_sensor()
    sensor._config_entry.options["preheat_boost_enabled"] = True
    sensor._was_braking_last_cycle = False
    temps = [-5 + i for i in range(24)]
    result = _run_cycle(
        sensor, monkeypatch, NOW, [NORMAL] * 4 + [EXPENSIVE] * 4, forecast_temps=temps
    )
    assert sensor._last_outlook.preheat_worthwhile is False
    assert result["mode"] == MODE_PI


@pytest.mark.parametrize("minute,held", [(6, True), (5, False)])
def test_bridge_window_boundary_54_minutes(minute, held, monkeypatch):
    sensor = _make_sensor()
    sensor._config_entry.options["house_inertia"] = 6
    sensor._brake_ramp = 0.8
    now = NOW.replace(minute=minute)
    sensor._brake_last_t = now - timedelta(minutes=1)
    result = _run_cycle(sensor, monkeypatch, now, [NORMAL] * 4 + [EXPENSIVE])
    assert result["extra"]["bridge_short_dip"] is held


def test_heavy_house_without_prior_braking_can_price_preheat(monkeypatch):
    sensor = _make_sensor()
    sensor._config_entry.options.update(house_inertia=6, preheat_boost_enabled=True)
    sensor._was_braking_last_cycle = False
    result = _run_cycle(
        sensor, monkeypatch, NOW, [NORMAL] * 3 + [EXPENSIVE], forecast_temps=[-5.0] * 24
    )
    assert result["mode"] == MODE_PREHEAT
