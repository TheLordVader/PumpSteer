"""Passive diagnostics cannot change real offline controller decisions or dispatches."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_adaptive_braking import START, qualified
from test_safe_mode_output import setup_sensor  # noqa: F401
from test_temperature_input_safety import _report, control  # noqa: F401

from custom_components.pumpsteer.sensor import PumpSteerSensor


def _fault(*args, **kwargs):
    raise RuntimeError("synthetic passive fault")


async def _async_fault(*args, **kwargs):
    _fault()


def _pair(
    fixture, monkeypatch, *, adaptive=False, transport="both", price_mode="normal"
):
    base, states, clock = fixture
    options = base._config_entry.options
    options.update(
        thermal_intelligence_enabled=False, adaptive_braking_enabled=adaptive
    )
    if transport == "ohmigo":
        options.pop("modbus_service", None)
    elif transport == "gos":
        options.pop("ohmigo_entity", None)
    hass = SimpleNamespace(
        states=SimpleNamespace(get=states.get),
        services=type(base.hass.services)(),
    )
    entry = SimpleNamespace(
        entry_id="passive-fault-candidate",
        data={},
        options={**deepcopy(options), "thermal_intelligence_enabled": True},
    )
    candidate = PumpSteerSensor(hass, entry)
    for name in ("_get_prices", "_forecast_temps"):
        monkeypatch.setattr(candidate, name, getattr(base, name))
    if price_mode == "braking":

        async def prices(cfg, now):
            return [10.0] * 24, ["expensive"] * 24, 60, 0

        for sensor in (base, candidate):
            sensor._config_entry.options.update(
                aggressiveness=3, preheat_boost_enabled=False
            )
            monkeypatch.setattr(sensor, "_get_prices", prices)
    candidate.async_write_ha_state = lambda: None
    _report(states, clock, unit_of_measurement="°C")
    return base, candidate, states, clock


async def _update_and_drain(sensor):
    await sensor.async_update()
    task = sensor._thermal_fit_task
    if task is not None:
        await task


def _assert_primary_parity(base, candidate):
    assert candidate.hass.services.calls == base.hass.services.calls
    assert base.hass.services.calls
    assert candidate.state == base.state
    assert candidate._attributes["mode"] == base._attributes["mode"]
    assert candidate._pi._integral == base._pi._integral
    assert candidate._brake_ramp == base._brake_ramp


@pytest.mark.parametrize("thermal", [False, True])
@pytest.mark.parametrize("adaptive", [False, True])
def test_only_explicit_legacy_adaptive_flag_can_change_braking(
    request, thermal, adaptive
):
    sensor, _, _ = request.getfixturevalue("control")
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=thermal,
        adaptive_braking_enabled=adaptive,
    )
    cfg = sensor._cfg()
    model = qualified()
    sensor._thermal_model = model
    sensor._adaptive_evidence_model = model
    sensor._adaptive_inputs_fresh = True
    # Even a fitted empirical candidate cannot authorize an active correction.
    sensor._thermal_intelligence.model._params = {
        "a": 100,
        "b": 1,
        "c": 100,
        "d_minutes": 0,
        "use_outdoor": True,
    }
    multiplier = sensor._adaptive_brake_multiplier(
        START + timedelta(hours=5),
        cfg,
        19.9,
        5,
        19.8,
        15,
        15,
        True,
    )
    if adaptive:
        assert 0.75 <= multiplier < 1
        assert sensor._adaptive_braking["reason"] == "qualified"
    else:
        assert multiplier == 1
        assert sensor._adaptive_braking["reason"] == "disabled"
    diagnostics = sensor._base_attrs(19.9, 21, 5, "expensive", 3)[
        "thermal_intelligence"
    ]
    if thermal:
        assert diagnostics["legacy_adaptive_enabled"] is adaptive
        assert diagnostics["passive_field_trial_safe"] is (not adaptive)
        assert diagnostics["fit_trusted"] is False
    else:
        assert diagnostics is None


@pytest.mark.parametrize("adaptive", [False, True])
@pytest.mark.parametrize("transport", ["ohmigo", "gos", "both"])
@pytest.mark.parametrize("price_mode", ["normal", "braking"])
def test_passive_flag_alone_preserves_controller_and_physical_services(
    request,
    monkeypatch,
    adaptive,
    transport,
    price_mode,
):
    base, candidate, _, _ = _pair(
        request.getfixturevalue("control"),
        monkeypatch,
        adaptive=adaptive,
        transport=transport,
        price_mode=price_mode,
    )
    asyncio.run(_update_and_drain(base))
    asyncio.run(_update_and_drain(candidate))
    _assert_primary_parity(base, candidate)
    if price_mode == "braking":
        assert base._attributes["mode"] == "braking"
    assert candidate._cfg()["thermal_intelligence_enabled"] is True
    assert candidate._cfg()["adaptive_braking_enabled"] is adaptive
    assert {
        k: v
        for k, v in candidate._config_entry.options.items()
        if k != "thermal_intelligence_enabled"
    } == {
        k: v
        for k, v in base._config_entry.options.items()
        if k != "thermal_intelligence_enabled"
    }


@pytest.mark.parametrize(
    "fault",
    [
        "collection",
        "output_capture",
        "shared_audit",
        "store_delay",
        "store_export",
        "fit_due",
        "fit_prepare",
        "fit_schedule",
        "executor",
    ],
)
@pytest.mark.parametrize("transport", ["ohmigo", "gos", "both"])
@pytest.mark.parametrize("price_mode", ["normal", "braking"])
def test_passive_fault_cannot_change_control_or_physical_services(
    request,
    monkeypatch,
    fault,
    transport,
    price_mode,
):
    base, candidate, states, clock = _pair(
        request.getfixturevalue("control"),
        monkeypatch,
        transport=transport,
        price_mode=price_mode,
    )
    runtime = candidate._thermal_intelligence
    if fault == "collection":
        monkeypatch.setattr(runtime, "begin_cycle", _fault)
    elif fault == "output_capture":
        monkeypatch.setattr(runtime, "record_output", _fault)
    elif fault == "shared_audit":
        monkeypatch.setattr(candidate, "_update_shared_thermal_audit", _fault)
    elif fault == "store_delay":
        candidate._pilot_store = SimpleNamespace(async_delay_save=_fault)
    elif fault == "store_export":
        candidate._pilot_store = SimpleNamespace(async_delay_save=lambda *args: None)
        monkeypatch.setattr(runtime, "export_state", _fault)
    elif fault == "fit_due":
        monkeypatch.setattr(runtime, "fit_due", _fault)
    else:
        monkeypatch.setattr(runtime, "fit_due", lambda now: True)
        if fault == "fit_prepare":
            monkeypatch.setattr(runtime, "prepare_fit_job", _fault)
        else:

            def prepare(now):
                runtime._fit_generation += 1
                runtime._fit_in_progress = True
                return runtime._fit_generation, {}

            monkeypatch.setattr(runtime, "prepare_fit_job", prepare)
            if fault == "fit_schedule":

                def reject_task(coroutine):
                    coroutine.close()
                    _fault()

                candidate.hass.async_create_task = reject_task
            else:
                candidate.hass.async_add_executor_job = _async_fault
    asyncio.run(_update_and_drain(base))
    asyncio.run(_update_and_drain(candidate))
    _assert_primary_parity(base, candidate)
    if fault in {"fit_due", "fit_prepare", "fit_schedule", "executor"}:
        assert not runtime._fit_in_progress
        assert runtime.summary()["fit_trusted"] is False

    # Persistent diagnostic failures must also preserve the next real dispatch.
    clock[0] += timedelta(minutes=8)
    _report(states, clock, unit_of_measurement="°C")
    asyncio.run(_update_and_drain(base))
    asyncio.run(_update_and_drain(candidate))
    _assert_primary_parity(base, candidate)


@pytest.mark.parametrize("source", ["runtime", "fixed_audit"])
def test_summary_fault_cannot_break_ha_attribute_read_or_physical_control(
    request,
    monkeypatch,
    source,
):
    base, candidate, _, _ = _pair(request.getfixturevalue("control"), monkeypatch)
    if source == "runtime":
        monkeypatch.setattr(candidate._thermal_intelligence, "summary", _fault)
    else:
        monkeypatch.setattr(candidate._plan_audit, "fixed_summary", _fault)
    asyncio.run(_update_and_drain(base))
    asyncio.run(_update_and_drain(candidate))
    _assert_primary_parity(base, candidate)
    attributes = candidate.extra_state_attributes
    assert attributes["mode"] == base.extra_state_attributes["mode"]


@pytest.mark.parametrize("operation", ["save", "flush"])
def test_store_io_failure_does_not_stop_later_physical_control(
    request,
    monkeypatch,
    operation,
):
    base, candidate, states, clock = _pair(
        request.getfixturevalue("control"), monkeypatch
    )
    candidate._pilot_store = SimpleNamespace(
        async_delay_save=_fault,
        async_save=_async_fault,
    )
    asyncio.run(_update_and_drain(base))
    asyncio.run(_update_and_drain(candidate))
    if operation == "flush":
        # Ensure this actually reaches async_save despite preceding save errors.
        candidate._pilot_persisted = {}
        with pytest.raises(RuntimeError):
            asyncio.run(candidate._flush_pilot_store())
    clock[0] += timedelta(minutes=8)
    _report(states, clock, unit_of_measurement="°C")
    asyncio.run(_update_and_drain(base))
    asyncio.run(_update_and_drain(candidate))
    _assert_primary_parity(base, candidate)


def test_fitted_rtm_cannot_substitute_for_unqualified_legacy_adaptive_evidence(request):
    from custom_components.pumpsteer.thermal_model import ThermalModel

    sensor, _, _ = request.getfixturevalue("control")
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        adaptive_braking_enabled=True,
    )
    sensor._thermal_model = ThermalModel()
    sensor._adaptive_evidence_model = sensor._thermal_model
    sensor._adaptive_inputs_fresh = True
    sensor._thermal_intelligence.model._params = {
        "a": 100,
        "b": 1,
        "c": 100,
        "d_minutes": 0,
        "use_outdoor": True,
    }
    assert (
        sensor._adaptive_brake_multiplier(
            START + timedelta(hours=5),
            sensor._cfg(),
            19.9,
            5,
            19.8,
            15,
            15,
            True,
        )
        == 1
    )
    assert sensor._adaptive_braking["reason"] != "qualified"


def test_disabled_adaptive_flag_does_not_label_residual_ramp_as_passive_safe(request):
    sensor, _, _ = request.getfixturevalue("control")
    sensor._config_entry.options.update(
        thermal_intelligence_enabled=True,
        adaptive_braking_enabled=False,
    )
    sensor._control_modifier_state = {"adaptive": (0.1, START)}
    # An old correction can outlive its flag; the initial diagnostic multiplier
    # alone cannot hide the stored return-to-Base ramp.
    before = sensor._base_attrs(19.9, 21, 5, "expensive", 3)["thermal_intelligence"]
    assert before["legacy_adaptive_enabled"] is False
    assert before["passive_field_trial_safe"] is False
    multiplier = sensor._adaptive_brake_multiplier(
        START + timedelta(minutes=1),
        sensor._cfg(),
        19.9,
        5,
        19.8,
        15,
        15,
        True,
    )
    assert 0.9 < multiplier < 1
    assert sensor._adaptive_braking["reason"] == "disabled"
    assert sensor._adaptive_braking["state"] == "returning_to_base"
    returning = sensor._base_attrs(19.9, 21, 5, "expensive", 3)["thermal_intelligence"]
    assert returning["passive_field_trial_safe"] is False
    assert sensor._config_entry.options["adaptive_braking_enabled"] is False

    # A fresh instance has no transient modifier history. No steering flag or
    # unrelated setting is changed to achieve this passive starting point.
    entry = SimpleNamespace(
        entry_id="fresh-passive-start",
        data={},
        options=deepcopy(sensor._config_entry.options),
    )
    fresh = PumpSteerSensor(sensor.hass, entry)
    fresh_multiplier = fresh._adaptive_brake_multiplier(
        START + timedelta(minutes=1),
        fresh._cfg(),
        19.9,
        5,
        19.8,
        15,
        15,
        True,
    )
    assert fresh_multiplier == 1
    assert fresh._adaptive_braking["multiplier"] == 1
    diagnostics = fresh._base_attrs(19.9, 21, 5, "expensive", 3)["thermal_intelligence"]
    assert diagnostics["passive_field_trial_safe"] is True
    assert fresh._config_entry.options == sensor._config_entry.options


def test_unload_catches_real_checkpoint_flush_io_failure(request, monkeypatch):
    _, candidate, _, _ = _pair(request.getfixturevalue("control"), monkeypatch)
    reached = []
    async def failed_save(payload):
        reached.append(payload)
        raise OSError("injected flush error")
    candidate._pilot_persisted = {}
    candidate._pilot_store = SimpleNamespace(async_delay_save=lambda *args: None,
                                             async_save=failed_save)
    asyncio.run(candidate.async_will_remove_from_hass())
    assert len(reached) == 1
    assert candidate._temperature_unloaded
