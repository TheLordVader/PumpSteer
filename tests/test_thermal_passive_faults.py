"""Passive diagnostics cannot change real offline controller decisions or dispatches."""

import asyncio
import sys
from copy import deepcopy
from datetime import timedelta
from types import ModuleType, SimpleNamespace

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
        "plan_identity",
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
    elif fault == "plan_identity":
        monkeypatch.setattr(candidate, "_plan_current_identity", _fault)
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


@pytest.mark.parametrize("startup", [False, True])
def test_unresponsive_store_does_not_block_startup_or_following_dispatches(
    request, monkeypatch, startup
):
    base, candidate, states, clock = _pair(
        request.getfixturevalue("control"), monkeypatch
    )
    module = ModuleType("homeassistant.helpers.storage")
    module.Store = object
    monkeypatch.setitem(sys.modules, module.__name__, module)
    candidate.hass.data = {}
    candidate._config_entry.add_update_listener = lambda callback: lambda: None

    async def no_extra():
        return None

    candidate.async_get_last_extra_data = no_extra

    async def observe():
        entered = asyncio.Event()
        retry = asyncio.Event()
        reads = []
        saves = []
        old = candidate._thermal_intelligence
        old.record_output(
            {
                "at": (clock[0] - timedelta(minutes=10)).isoformat(),
                "transport": "ohmigo",
                "status": "service_completed_unverified",
                "requested": 4.0,
            }
        )
        history = old.export_state()
        # The history belongs to a previous process, not the live controller.
        candidate._thermal_intelligence = type(old)(pilot_only=True)

        async def load():
            reads.append(True)
            entered.set()
            await retry.wait()
            return {"version": 1, "thermal_intelligence": history}

        candidate._pilot_store = SimpleNamespace(
            async_load=load,
            async_delay_save=lambda callback, _delay: saves.append(callback()),
        )
        candidate._pilot_store_read_failed = not startup
        try:
            if startup:
                candidate.hass.is_running = True
                await asyncio.wait_for(candidate.async_added_to_hass(), 0.2)
                # Fresh temperatures arrive later than the real startup cutoff.
                clock[0] += timedelta(seconds=1)
                _report(states, clock, unit_of_measurement="°C")
            await base.async_update()
            await asyncio.wait_for(candidate.async_update(), 0.2)
            await asyncio.wait_for(entered.wait(), 0.2)
            _assert_primary_parity(base, candidate)
            for outdoor in ("-6.3", "-8.3"):
                clock[0] += timedelta(minutes=8)
                states["sensor.outdoor"].state = outdoor
                _report(states, clock, unit_of_measurement="°C")
                await base.async_update()
                await asyncio.wait_for(candidate.async_update(), 0.2)
                _assert_primary_parity(base, candidate)
            assert len(reads) == 1
            assert not saves and not candidate._thermal_intelligence.has_evidence
            task = candidate._pilot_store_load_task
            retry.set()
            await asyncio.wait_for(task, 0.2)
            restored = candidate._thermal_intelligence.export_state()["events"]
            assert restored[0] == history["events"][0]
            assert restored[-1]["reason"] == "restart"

            clock[0] += timedelta(minutes=8)
            _report(states, clock, unit_of_measurement="°C")
            await base.async_update()
            await candidate.async_update()
            _assert_primary_parity(base, candidate)
            assert (
                saves[-1]["thermal_intelligence"]["events"][0] == history["events"][0]
            )
        finally:
            retry.set()

    asyncio.run(observe())


def test_disable_schedules_latest_store_checkpoint_without_waiting_for_io(
    request, monkeypatch
):
    _, candidate, states, clock = _pair(request.getfixturevalue("control"), monkeypatch)
    queued = []

    async def blocked_save(_payload):
        await asyncio.Event().wait()

    candidate._pilot_store = SimpleNamespace(
        async_delay_save=lambda callback, delay: queued.append((callback(), delay)),
        async_save=blocked_save,
    )

    async def observe():
        await candidate.async_update()
        clock[0] += timedelta(minutes=8)
        _report(states, clock, unit_of_measurement="°C")
        entry = candidate._config_entry
        entry.options["thermal_intelligence_enabled"] = False
        await asyncio.wait_for(
            candidate.async_options_update_listener(candidate.hass, entry), 0.2
        )
        assert len(candidate.hass.services.calls) == 4
        saved, delay = queued[-1]
        assert delay == 0
        assert saved["thermal_intelligence"]["events"][-1]["reason"] == "disabled"

    asyncio.run(observe())


def test_reload_can_dispatch_while_previous_unload_checkpoint_is_still_writing(
    request, monkeypatch
):
    _, candidate, states, clock = _pair(request.getfixturevalue("control"), monkeypatch)
    candidate.hass.data = {}
    # HA awaits tasks registered on the config entry during unload.
    candidate._config_entry.async_create_background_task = _fault
    module = ModuleType("homeassistant.helpers.storage")
    module.Store = object
    monkeypatch.setitem(sys.modules, module.__name__, module)

    async def observe():
        writing = asyncio.Event()
        release = asyncio.Event()
        written = []
        reads = []

        async def write(payload):
            writing.set()
            await release.wait()
            written.append(payload)

        async def load():
            reads.append(True)
            return written[-1]

        try:
            candidate._pilot_store = SimpleNamespace(
                async_delay_save=lambda *args: None,
                async_save=write,
            )
            candidate._plan_audit.plan_unverifiable = 2
            await candidate.async_update()
            await asyncio.wait_for(candidate.async_will_remove_from_hass(), 0.2)
            await asyncio.wait_for(writing.wait(), 0.2)

            replacement = PumpSteerSensor(candidate.hass, candidate._config_entry)
            replacement._get_prices = candidate._get_prices
            replacement._forecast_temps = candidate._forecast_temps
            replacement._pilot_store = SimpleNamespace(
                async_load=load,
                async_delay_save=lambda *args: None,
            )
            replacement._schedule_pilot_store_load()
            clock[0] += timedelta(minutes=8)
            _report(states, clock, unit_of_measurement="°C")
            await asyncio.wait_for(replacement.async_update(), 0.2)
            assert len(replacement.hass.services.calls) == 4 and not reads
            release.set()
            await asyncio.wait_for(replacement._pilot_store_load_task, 0.2)
            assert len(written) == len(reads) == 1
            assert replacement._plan_audit.plan_unverifiable == 2
            events = replacement._thermal_intelligence.export_state()["events"]
            assert events[:-1] == written[0]["thermal_intelligence"]["events"]
            assert (
                events[-2]["reason"] == "unload" and events[-1]["reason"] == "restart"
            )
        finally:
            release.set()

    asyncio.run(observe())


def test_late_cancelled_store_load_cannot_restore_history_after_disable(
    request, monkeypatch
):
    _, candidate, states, clock = _pair(request.getfixturevalue("control"), monkeypatch)
    module = ModuleType("homeassistant.helpers.storage")
    module.Store = object
    monkeypatch.setitem(sys.modules, module.__name__, module)

    async def observe():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def unresponsive_load():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return {"version": 1, "plan_audit": {"unverifiable": 3}}

        candidate._pilot_store = SimpleNamespace(async_load=unresponsive_load)
        candidate._schedule_pilot_store_load()
        task = candidate._pilot_store_load_task
        try:
            await asyncio.wait_for(entered.wait(), 1)
            entry = candidate._config_entry
            entry.options["thermal_intelligence_enabled"] = False
            await candidate.async_options_update_listener(candidate.hass, entry)
            await asyncio.wait_for(cancelled.wait(), 1)
            entry.options["thermal_intelligence_enabled"] = True
            clock[0] += timedelta(minutes=8)
            _report(states, clock, unit_of_measurement="°C")
            await candidate.async_options_update_listener(candidate.hass, entry)
            assert len(candidate.hass.services.calls) == 4
            assert candidate._pilot_store_load_task is task
            release.set()
            await asyncio.wait_for(task, 1)
            assert not candidate._plan_audit_loaded
            assert candidate._pilot_persisted is None
        finally:
            release.set()

    asyncio.run(observe())
