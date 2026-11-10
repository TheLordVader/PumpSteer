"""Lifecycle grace admits unchanged HA reports without renewing their provenance."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_safe_mode_output import setup_sensor  # noqa: F401
from test_temperature_input_safety import _report, control  # noqa: F401

from custom_components.pumpsteer.sensor import MODE_PI, MODE_SAFE
from custom_components.pumpsteer.utils import get_temperature


def _reload(sensor, monkeypatch):
    sensor.hass.is_running = True
    sensor.hass.bus = SimpleNamespace(async_listen_once=lambda *args: lambda: None)
    sensor._config_entry.add_update_listener = lambda callback: lambda: None

    async def no_extra():
        return None

    monkeypatch.setattr(sensor, "async_get_last_extra_data", no_extra, raising=False)
    asyncio.run(sensor.async_added_to_hass())


@pytest.mark.parametrize("age_minutes", [0, 30, 90])
def test_live_reload_accepts_change_only_reports_within_existing_age_limits(
    request,
    monkeypatch,
    age_minutes,
):
    sensor, states, clock = request.getfixturevalue("control")
    _report(states, clock, unit_of_measurement="°C")
    reported = clock[0] - timedelta(minutes=age_minutes)
    for state in (states["sensor.indoor"], states["sensor.outdoor"]):
        state.last_reported = reported
    _reload(sensor, monkeypatch)
    assert sensor.extra_state_attributes["mode"] == MODE_PI
    assert len(sensor.hass.services.calls) == 2
    assert states["sensor.indoor"].last_reported == reported
    assert states["sensor.outdoor"].last_reported == reported


def test_start_grace_releases_only_control_gate_at_exact_event_deadline(request):
    sensor, states, clock = request.getfixturevalue("control")
    _report(states, clock, unit_of_measurement="°C")
    reported = clock[0]
    event = SimpleNamespace(time_fired=clock[0] + timedelta(seconds=30))
    clock[0] = event.time_fired + timedelta(minutes=1)
    asyncio.run(sensor._handle_ha_started(event))
    assert not sensor.hass.services.calls
    clock[0] = event.time_fired + timedelta(minutes=5) - timedelta(seconds=1)
    asyncio.run(sensor.async_update())
    assert not sensor.hass.services.calls
    clock[0] += timedelta(seconds=1)
    asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_PI
    assert len(sensor.hass.services.calls) == 2
    # This anchor still gates optional verified signals and adaptive evidence.
    assert sensor._temperature_reported_after == event.time_fired
    assert states["sensor.indoor"].last_reported == reported
    assert states["sensor.outdoor"].last_reported == reported


@pytest.mark.parametrize("lifecycle", ["startup_grace", "reload"])
def test_release_keeps_default_age_limits_and_does_not_refresh_source_reports(
    request,
    monkeypatch,
    lifecycle,
):
    sensor, states, clock = request.getfixturevalue("control")
    _report(states, clock, unit_of_measurement="°C")
    reported = clock[0]
    if lifecycle == "reload":
        _reload(sensor, monkeypatch)
    else:
        asyncio.run(sensor._handle_ha_started(SimpleNamespace(time_fired=clock[0])))
        clock[0] += timedelta(minutes=5)
        asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_PI
    clock[0] = reported + timedelta(minutes=121)
    asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_SAFE
    assert sensor._temperature_state(sensor._cfg(), "real_outdoor_entity") is None
    assert states["sensor.outdoor"].last_reported == reported
    states["sensor.outdoor"].last_reported = clock[0]
    clock[0] = reported + timedelta(minutes=361)
    states["sensor.outdoor"].last_reported = clock[0]
    asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_SAFE
    assert sensor._temperature_state(sensor._cfg(), "indoor_temp_entity") is None
    assert states["sensor.indoor"].last_reported == reported


@pytest.mark.parametrize("lifecycle", ["startup_grace", "reload"])
@pytest.mark.parametrize("invalid", ["restored", "naive", "future", "bad_unit", "nan"])
def test_release_does_not_admit_invalid_temperature_reports(
    request,
    monkeypatch,
    lifecycle,
    invalid,
):
    sensor, states, clock = request.getfixturevalue("control")
    _report(states, clock, unit_of_measurement="°C")
    bad = states["sensor.indoor"]
    if invalid == "restored":
        bad.attributes["restored"] = True
    elif invalid == "naive":
        bad.last_reported = clock[0].replace(tzinfo=None)
    elif invalid == "future":
        bad.last_reported = clock[0] + timedelta(hours=1)
    elif invalid == "bad_unit":
        bad.attributes["unit_of_measurement"] = "°F"
    else:
        bad.state = "nan"
    if lifecycle == "reload":
        _reload(sensor, monkeypatch)
    else:
        asyncio.run(sensor._handle_ha_started(SimpleNamespace(time_fired=clock[0])))
        clock[0] += timedelta(minutes=5)
        asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_SAFE
    assert sensor._temperature_state(sensor._cfg(), "indoor_temp_entity") is None


def test_grace_does_not_override_stricter_configured_age(request):
    sensor, states, clock = request.getfixturevalue("control")
    sensor._config_entry.options["temperature_report_max_age_minutes"] = 1
    _report(states, clock)
    asyncio.run(sensor._handle_ha_started(SimpleNamespace(time_fired=clock[0])))
    clock[0] += timedelta(minutes=5)
    asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_SAFE
    assert not sensor.hass.services.calls


def test_no_cutoff_still_rejects_future_report_and_recovers_when_clock_catches_up(
    request,
):
    sensor, states, clock = request.getfixturevalue("control")
    report_time = clock[0] + timedelta(minutes=1)
    states["sensor.indoor"].last_reported = report_time
    assert get_temperature(sensor.hass, "sensor.indoor") is None
    clock[0] = report_time
    assert get_temperature(sensor.hass, "sensor.indoor") == 21


def test_unchanged_reports_admitted_for_control_do_not_become_new_adaptive_evidence(
    request,
):
    sensor, states, clock = request.getfixturevalue("control")
    sensor._config_entry.options["adaptive_braking_enabled"] = True
    _report(states, clock, unit_of_measurement="°C")
    reported = clock[0]
    asyncio.run(sensor._handle_ha_started(SimpleNamespace(time_fired=clock[0])))
    clock[0] += timedelta(minutes=5)
    asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_PI
    assert not sensor._adaptive_new_report
    assert not sensor._thermal_model._temp_history
    for _ in range(3):
        clock[0] += timedelta(minutes=1)
        asyncio.run(sensor.async_update())
        assert not sensor._adaptive_new_report
        assert not sensor._thermal_model._temp_history
    assert states["sensor.indoor"].last_reported == reported


def test_no_cutoff_does_not_accept_naive_report_metadata(request):
    sensor, states, clock = request.getfixturevalue("control")
    states["sensor.indoor"].last_reported = clock[0].replace(tzinfo=None)
    assert get_temperature(sensor.hass, "sensor.indoor") is None


@pytest.mark.parametrize("lifecycle", ["startup_grace", "reload"])
@pytest.mark.parametrize("invalid", ["unavailable", "future", "restored"])
def test_change_only_grace_keeps_final_per_output_revalidation(
    request,
    monkeypatch,
    lifecycle,
    invalid,
):
    sensor, states, clock = request.getfixturevalue("control")
    _report(states, clock, unit_of_measurement="°C")
    original = sensor.hass.services.async_call

    async def change_during_first_output(domain, service, data, **kwargs):
        await original(domain, service, data, **kwargs)
        if domain == "test_output":
            if invalid == "unavailable":
                states["sensor.indoor"].state = "unavailable"
            elif invalid == "future":
                states["sensor.indoor"].last_reported = clock[0] + timedelta(minutes=1)
            else:
                states["sensor.indoor"].attributes["restored"] = True

    monkeypatch.setattr(sensor.hass.services, "async_call", change_during_first_output)
    if lifecycle == "reload":
        _reload(sensor, monkeypatch)
    else:
        asyncio.run(sensor._handle_ha_started(SimpleNamespace(time_fired=clock[0])))
        clock[0] += timedelta(minutes=5)
        asyncio.run(sensor.async_update())
    assert [(call[0], call[1]) for call in sensor.hass.services.calls] == [
        ("test_output", "set_value"),
    ]
    assert sensor._ohmigo_last_push is None
    assert sensor.extra_state_attributes["receiver_state"] == "unverified"


@pytest.mark.parametrize(
    "start_utc", ["2026-03-29T00:58:00+00:00", "2026-10-25T00:58:00+00:00"]
)
def test_startup_grace_uses_elapsed_time_across_dst(request, start_utc):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    sensor, states, clock = request.getfixturevalue("control")
    zone = ZoneInfo("Europe/Stockholm")
    start = datetime.fromisoformat(start_utc).astimezone(zone)
    clock[0] = start
    _report(states, clock, unit_of_measurement="°C")
    asyncio.run(sensor._handle_ha_started(SimpleNamespace(time_fired=start)))
    clock[0] = datetime.fromtimestamp(start.timestamp() + 299, tz=zone)
    asyncio.run(sensor.async_update())
    assert not sensor.hass.services.calls
    clock[0] = datetime.fromtimestamp(start.timestamp() + 300, tz=zone)
    asyncio.run(sensor.async_update())
    assert sensor.extra_state_attributes["mode"] == MODE_PI
    assert len(sensor.hass.services.calls) == 2
