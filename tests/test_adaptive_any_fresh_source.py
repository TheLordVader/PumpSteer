"""Asynchronous aggregates require genuine advancement and fresh constituents."""

from types import SimpleNamespace

from test_adaptive_braking import START
from test_brake_provenance import _cycle, _sensor


def setup(monkeypatch):
    sensor, states = _sensor()
    sensor._config_entry.options.update(
        adaptive_braking_enabled=True,
        indoor_temp_entity_2="sensor.two",
        indoor_temp_entity_3="sensor.three",
        indoor_temp_weight_1=70,
        indoor_temp_weight_2=20,
        indoor_temp_weight_3=10,
    )
    for name in ("sensor.two", "sensor.three"):
        states[name] = SimpleNamespace(state="20.5", attributes={}, last_reported=START)
    _cycle(sensor, monkeypatch, 15)
    for name in ("sensor.indoor", "sensor.two", "sensor.three"):
        states[name].state = "20.4"
        states[name].last_reported = START.replace(minute=16)
    _cycle(sensor, monkeypatch, 16)
    assert sensor._adaptive_new_report
    return sensor, states


def test_one_changed_source_with_all_fresh_values_is_one_new_observation(monkeypatch):
    sensor, states = setup(monkeypatch)
    states["sensor.indoor"].state = "20.3"
    _cycle(sensor, monkeypatch, 17)
    assert sensor._adaptive_new_report
    assert sensor.extra_state_attributes["indoor_temperature"] == 20.33
    _cycle(sensor, monkeypatch, 18)
    assert not sensor._adaptive_new_report


def test_unchanged_republishes_do_not_refresh_stale_constituents(monkeypatch):
    sensor, states = setup(monkeypatch)
    from datetime import timedelta

    for name in ("sensor.two", "sensor.three"):
        states[name].last_reported = START + timedelta(minutes=47)
    states["sensor.indoor"].state = "20.3"
    _cycle(sensor, monkeypatch, 47)
    assert not sensor._adaptive_new_report


def test_invalid_constituent_blocks_changed_source(monkeypatch):
    sensor, states = setup(monkeypatch)
    states["sensor.two"].state = "unavailable"
    states["sensor.indoor"].state = "20.3"
    _cycle(sensor, monkeypatch, 17)
    assert not sensor._adaptive_new_report


def test_oldest_constituent_bounds_observation_freshness(monkeypatch):
    sensor, states = setup(monkeypatch)
    states["sensor.indoor"].state = "20.3"
    _cycle(sensor, monkeypatch, 46)
    assert sensor._adaptive_new_report  # Other genuine observations are 30m old.
    states["sensor.indoor"].state = "20.2"
    _cycle(sensor, monkeypatch, 47)
    assert not sensor._adaptive_new_report


def test_cached_reappearance_does_not_resume_observations(monkeypatch):
    sensor, states = setup(monkeypatch)
    states["sensor.two"].state = "unavailable"
    _cycle(sensor, monkeypatch, 17)
    states["sensor.two"].state = "20.4"
    states["sensor.indoor"].state = "20.3"
    _cycle(sensor, monkeypatch, 18)
    assert not sensor._adaptive_new_report
