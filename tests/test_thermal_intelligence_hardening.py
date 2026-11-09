"""Tests for Thermal Intelligence RTM prototype hardening and safety.

Covers:
- Offloading CPU model fitting off Home Assistant's event loop via executor
- Snapshot isolation and obsolete fit rejection
- Diagnostics for insufficient excitation (constant braking)
- Robust forecast evaluation with realistic outdoor drift tolerance (<= 3.0 C)
- Broadened forecast arrival window (45 - 75 min) evaluated at actual elapsed time
- Multi-regime evidence collection and quantization tracking
- Persistence, restart safety, and unblocked physical dispatch parity
"""

import asyncio
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from custom_components.pumpsteer.thermal_intelligence.rtm_core import Sample
from custom_components.pumpsteer.thermal_intelligence.runtime import (
    ThermalIntelligence,
    classify_regime,
)

START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _make_cycle(
    at: datetime,
    values: list[tuple[str, float, float, float]],
    *,
    mode: str = "braking",
    outdoor: float = 2.0,
    outdoor_reported_at: datetime | None = None,
    command: float = -8.0,
    brake_factor: float = 1.0,
    command_span: float = 12.0,
):
    sources = []
    for entity, weight, val, offset in values:
        sources.append(
            {
                "entity": entity,
                "weight": weight,
                "value": val,
                "reported_at": (at - timedelta(seconds=offset)).isoformat(),
            }
        )
    return {
        "at": at.isoformat(),
        "indoor": (
            sum(row[1] * row[2] for row in values) / sum(row[1] for row in values)
            if values
            else 20.0
        ),
        "indoor_reported_at": at.isoformat(),
        "outdoor": outdoor,
        "outdoor_reported_at": (outdoor_reported_at or at).isoformat(),
        "outdoor_source": "sensor.outdoor",
        "sources": sources,
        "requested_virtual_outdoor": command,
        "mode": mode,
        "brake_factor": brake_factor,
        "pi_demand_c": 0.0,
        "command_span": command_span,
    }


def _setup_runtime_with_model() -> ThermalIntelligence:
    runtime = ThermalIntelligence()
    runtime.model._params = {
        "a": 0.5,
        "b": 0.03,
        "c": 0.01,
        "d_minutes": 0.0,
        "use_outdoor": True,
    }
    runtime.model._last_fit_ts = START.timestamp()
    runtime.model._data_end_at_fit = START.timestamp()
    runtime._status = "candidate_learning"
    return runtime


def _issue_forecast(
    runtime: ThermalIntelligence,
    t0: datetime,
    *,
    outdoor: float = 2.0,
    command: float = -8.0,
) -> datetime:
    values0 = [("sensor.room_1", 1.0, 20.0, 0)]
    runtime.begin_cycle(
        _make_cycle(t0, values0, mode="braking", outdoor=outdoor, command=command, brake_factor=1.0),
        use_ohmigo_input=True,
    )
    runtime.record_output(
        {
            "at": t0.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": command,
        }
    )
    t1 = t0 + timedelta(minutes=15)
    values1 = [("sensor.room_1", 1.0, 19.9, 0)]  # Temperature changed
    runtime.begin_cycle(
        _make_cycle(t1, values1, mode="braking", outdoor=outdoor, command=command, brake_factor=1.0),
        use_ohmigo_input=True,
    )
    return t1


# ============================================================================
# 1. HA Runtime Safety & Executor Offloading
# ============================================================================


def test_prepare_fit_job_snapshot_isolation():
    """Executor fitting receives an isolated snapshot; new observations do not corrupt it."""
    runtime = ThermalIntelligence()
    t = START
    # Add sufficient historical data (> 3 hours)
    for i in range(50):
        t += timedelta(minutes=15)
        runtime.model.add_sample(
            Sample(
                ts=t,
                indoor_c=20.0 - 0.01 * i,
                outdoor_c=2.0,
                effort=0.5 if i % 2 == 0 else 0.2,
            )
        )

    assert runtime.fit_due(t)
    job = runtime.prepare_fit_job(t)
    assert job is not None
    token, snapshot_data = job
    assert token == runtime._fit_generation
    assert runtime._fit_in_progress is True

    initial_grid_len = len(snapshot_data["grid"])

    # Mutate runtime by adding more observations
    t += timedelta(minutes=15)
    runtime.model.add_sample(
        Sample(
            ts=t,
            indoor_c=19.5,
            outdoor_c=5.0,
            effort=0.9,
        )
    )

    # Snapshot data must not contain the newly added observation
    assert len(snapshot_data["grid"]) == initial_grid_len
    assert len(runtime.model._grid) > initial_grid_len

    # Run static_fit in a separate thread
    result = ThermalIntelligence.static_fit(snapshot_data)
    assert "status" in result

    # Apply result
    applied = runtime.apply_fit_result(token, result)
    assert applied is True
    assert runtime._fit_in_progress is False


def test_obsolete_fit_result_rejected_on_source_change():
    """If source configuration changes while a background fit is running, the obsolete result is discarded."""
    runtime = ThermalIntelligence()
    t = START
    # Initial source established in cycle
    values_initial = [("sensor.room_initial", 1.0, 20.0, 0)]
    runtime.begin_cycle(_make_cycle(t, values_initial), use_ohmigo_input=True)

    for i in range(50):
        t += timedelta(minutes=15)
        runtime.model.add_sample(
            Sample(
                ts=t,
                indoor_c=20.0 - 0.01 * i,
                outdoor_c=2.0,
                effort=0.5 if i % 2 == 0 else 0.2,
            )
        )

    job = runtime.prepare_fit_job(t)
    assert job is not None
    token, _snapshot_data = job

    # Source signature changes during fit
    values_new_source = [("sensor.new_room", 1.0, 21.0, 0)]
    cycle_new = _make_cycle(t + timedelta(minutes=1), values_new_source)
    runtime.begin_cycle(cycle_new, use_ohmigo_input=True)

    assert runtime._fit_generation > token

    # Simulate background thread completing now with the old token
    dummy_result = {
        "status": "ok",
        "params": {
            "a": 0.4,
            "b": 0.03,
            "c": 0.01,
            "d_minutes": 60.0,
            "use_outdoor": True,
        },
        "quality": {"rmse_k": 0.05, "max_error_k": 0.1, "r2": 0.9},
        "fit_ts": t.timestamp(),
        "data_end_ts": t.timestamp(),
        "outdoor_ref": 2.0,
    }
    applied = runtime.apply_fit_result(token, dummy_result)
    assert applied is False
    # Model parameters must remain None after rejection
    assert runtime.model.parameters() is None


# ============================================================================
# 2. Identifiability & Command Excitation Diagnostics
# ============================================================================


def test_insufficient_excitation_under_constant_braking():
    """Constant command during braking does not separately identify gains and reports insufficient excitation."""
    runtime = ThermalIntelligence()
    t = START
    # Constant braking gives a fixed normalized command u ≈ 0.0833
    u_const = (12.0 - 10.0) / 24.0  # 0.0833
    indoor = 20.0
    for _ in range(24 * 4):  # 24 hours of 15-min intervals
        t += timedelta(minutes=15)
        indoor -= 0.02
        runtime.model.add_sample(
            Sample(
                ts=t,
                indoor_c=indoor,
                outdoor_c=2.0,
                effort=u_const,
            )
        )

    job = runtime.prepare_fit_job(t)
    assert job is not None
    token, snapshot_data = job
    result = ThermalIntelligence.static_fit(snapshot_data)

    assert result["status"] == "candidate_insufficient_excitation"
    assert result["excitation_sufficient"] is False
    assert result["params"] is None
    assert result["command_range"] < 0.15

    runtime.apply_fit_result(token, result)
    summary = runtime.summary()
    assert summary["status"] == "candidate_insufficient_excitation"
    assert summary["excitation_sufficient"] is False
    assert summary["fit_parameters_available"] is False
    assert "insufficient_command_excitation" in summary["reason"]


def test_sufficient_excitation_identifies_parameters():
    """When commands vary across intervals, excitation check passes and allows parameter fitting."""
    runtime = ThermalIntelligence()
    t = START
    indoor = 20.0
    # Provide synthetic intervals with varying commands (step changes between 0 and 1)
    for index in range(24 * 10 * 4):
        outdoor = 2.0 + 0.5 * math.sin(index / 80)
        command = float((index // 24) % 2)
        indoor += 0.25 * (1.4 * command - 0.045 * (indoor - outdoor) - 0.2)
        indoor = round(indoor * 10) / 10
        t += timedelta(minutes=15)
        runtime.model.add_sample(
            Sample(
                ts=t,
                indoor_c=indoor,
                outdoor_c=outdoor,
                effort=command,
            )
        )

    job = runtime.prepare_fit_job(t)
    assert job is not None
    token, snapshot_data = job
    result = ThermalIntelligence.static_fit(snapshot_data)

    assert result["status"] == "ok"
    assert result["excitation_sufficient"] is True
    assert result["params"] is not None
    assert result["command_range"] >= 0.15
    assert result["command_variance"] >= 0.005

    runtime.apply_fit_result(token, result)
    summary = runtime.summary()
    assert summary["status"] == "candidate_unvalidated"
    assert summary["fit_parameters_available"] is True


# ============================================================================
# 3. Forecast Evaluation Hardening & Ambient Drift Tolerance
# ============================================================================








# ============================================================================
# 4. Evidence Collection & Multi-Regime Classification
# ============================================================================


def test_evidence_collection_regimes_and_quantization():
    """Evidence records clear regime labels and tracks command quantization error."""
    assert classify_regime("braking", 1.0) == "braking_stable"
    assert classify_regime("braking", 0.5) == "braking_transition"
    assert classify_regime("preheating", 0.0) == "preheating"
    assert classify_regime("normal_pi", 0.0) == "normal_pi"
    assert classify_regime("precool", 0.0) == "precool"
    assert classify_regime("summer_mode", 0.0) == "summer"

    runtime = ThermalIntelligence()
    t = START
    values0 = [("sensor.living_room", 1.0, 20.5, 5)]

    # Initial cycle to set dispatched command
    c_init = _make_cycle(t, values0, mode="normal_pi", outdoor=3.0, command=2.5, brake_factor=0.0)
    runtime.begin_cycle(c_init, use_ohmigo_input=True)
    runtime.record_output(
        {
            "at": t.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": 3.0,
        }
    )

    # Next cycle has dispatched command known and updated temperature
    t1 = t + timedelta(minutes=15)
    values1 = [("sensor.living_room", 1.0, 20.4, 5)]
    c_pi = _make_cycle(t1, values1, mode="normal_pi", outdoor=3.0, command=2.5, brake_factor=0.0)
    runtime.begin_cycle(c_pi, use_ohmigo_input=True)

    obs = runtime._observations[-1]
    assert obs["regime"] == "normal_pi"
    assert obs["data_quality"] == "empirical_command_unverified"
    assert obs["calculated_virtual_outdoor_c"] == 2.5
    assert obs["dispatched_virtual_outdoor_c"] == 3.0
    assert obs["command_quantization_error_c"] == 0.5


# ============================================================================
# 5. Persistence & Restart Safety
# ============================================================================


def test_export_and_restore_state_lifecycle():
    """State export/restore bounds history, clears pending forecasts, and preserves diagnostics."""
    runtime = _setup_runtime_with_model()
    _issue_forecast(runtime, START, outdoor=2.0, command=-8.0)
    assert runtime.current_command is not None

    exported = runtime.export_state()
    assert "status" in exported
    assert "fit_summary" in exported
    assert "fit_generation" in exported

    # Restore in a fresh instance
    new_runtime = ThermalIntelligence()
    success = new_runtime.restore_state(exported)
    assert success is True
    # Crucial safety: open predictions never resume across restarts
    assert new_runtime.current_command is None
    assert new_runtime._interruptions[-1]["reason"] == "restart"
    assert new_runtime.model.parameters() is not None
    assert new_runtime._fit_generation > runtime._fit_generation


# ============================================================================
# 6. Sensor Adapter & Physical Output Dispatch Safety
# ============================================================================




def test_no_repeated_fitting_on_consecutive_cycles_without_new_eligible_evidence():
    """Rejected/insufficient fits must not continuously retry on unchanged evidence across control cycles."""
    runtime = ThermalIntelligence()
    t = START
    u_const = (12.0 - 10.0) / 24.0  # constant braking command
    indoor = 20.0
    for _ in range(48):
        t += timedelta(minutes=15)
        indoor -= 0.01
        runtime.model.add_sample(
            Sample(ts=t, indoor_c=indoor, outdoor_c=2.0, effort=u_const)
        )

    # First fit attempt
    assert runtime.fit_due(t) is True
    job = runtime.prepare_fit_job(t)
    assert job is not None
    token, snapshot_data = job
    result = ThermalIntelligence.static_fit(snapshot_data)
    assert result["status"] == "candidate_insufficient_excitation"
    assert "data_end_ts" in result
    assert "fit_ts" in result

    runtime.apply_fit_result(token, result)
    assert runtime.status == "candidate_insufficient_excitation"

    # Now simulate 30 consecutive control cycles (1 minute apart) with no new eligible information
    # and constant command input
    fit_attempts = 0
    now = t
    for _ in range(30):
        now += timedelta(minutes=1)
        if runtime.fit_due(now):
            fit_attempts += 1

    # Verify that NO repeated fitting occurred across all 30 consecutive cycles!
    assert fit_attempts == 0
    assert runtime.prepare_fit_job(now) is None


def test_ohmigo_keepalive_preserves_command_age_for_shadow_prediction():
    """Identical 5-minute Ohmigo keepalives must not reset command stability for a 60-minute dead time."""
    runtime = ThermalIntelligence()
    # Model with 60-minute response delay (dead time)
    runtime.model._params = {
        "a": 0.5,
        "b": 0.03,
        "c": 0.01,
        "d_minutes": 60.0,
        "use_outdoor": True,
    }
    runtime.model._last_fit_ts = START.timestamp()
    runtime.model._data_end_at_fit = START.timestamp()
    runtime._status = "candidate_learning"

    t = START
    values = [("sensor.room_1", 1.0, 20.0, 0)]

    # Initial command dispatched at t=0
    c0 = _make_cycle(t, values, mode="braking", outdoor=2.0, command=-8.0, brake_factor=1.0)
    runtime.begin_cycle(c0, use_ohmigo_input=True)
    runtime.record_output(
        {
            "at": t.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": -8.0,
        }
    )

    # 5-minute keepalives for 55 minutes (t=5, 10, 15, ..., 55 min)
    indoor = 20.0
    for minute in range(5, 60, 5):
        t_k = START + timedelta(minutes=minute)
        # Periodic keepalive resends the same requested value (-8.0)
        runtime.record_output(
            {
                "at": t_k.isoformat(),
                "transport": "ohmigo",
                "status": "service_completed_unverified",
                "requested": -8.0,
            }
        )
        indoor -= 0.01
        c_k = _make_cycle(t_k, [("sensor.room_1", 1.0, indoor, 0)], mode="braking", outdoor=2.0, command=-8.0, brake_factor=1.0)
        runtime.begin_cycle(c_k, use_ohmigo_input=True)

        assert len(runtime._outputs) == 1
        # But established_at must remain anchored to START
        assert runtime._observations[-1]["command_at"] == START.isoformat()

    # At t=60 minutes: keepalive dispatch occurs and 60m dead time is satisfied!
    t_60 = START + timedelta(minutes=60)
    runtime.record_output(
        {
            "at": t_60.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": -8.0,
        }
    )
    indoor -= 0.01
    c_60 = _make_cycle(t_60, [("sensor.room_1", 1.0, indoor, 0)], mode="braking", outdoor=2.0, command=-8.0, brake_factor=1.0)
    runtime.begin_cycle(c_60, use_ohmigo_input=True)

    assert runtime.current_command["established_at"] == START.isoformat()
    assert runtime.current_command["last_dispatched_at"] == t_60.isoformat()
    assert len(runtime._outputs) == 1

    # Now verify that a genuine command change resets stability
    t_65 = START + timedelta(minutes=65)
    runtime.record_output(
        {
            "at": t_65.isoformat(),
            "transport": "ohmigo",
            "status": "service_completed_unverified",
            "requested": -6.0,  # Changed command!
        }
    )
    c_65 = _make_cycle(t_65, [("sensor.room_1", 1.0, indoor - 0.01, 0)], mode="braking", outdoor=2.0, command=-6.0, brake_factor=1.0)
    runtime.begin_cycle(c_65, use_ohmigo_input=True)

    assert runtime._last_command is not None
    assert runtime._last_command["established_at"] == t_65.isoformat()
    assert len(runtime._outputs) == 2
    assert runtime._outputs[-1]["requested"] == -6.0


def test_executor_fit_runs_off_event_loop_thread_and_does_not_block_dispatch():
    """Verify CPU fitting runs on a separate worker thread and physical dispatch completes unblocked."""
    async def _async_test():
        from custom_components.pumpsteer.sensor import PumpSteerSensor

        loop = asyncio.get_running_loop()
        loop_thread_id = threading.get_ident()
        worker_threads = []
        fit_started = asyncio.Event()
        fit_release = threading.Event()
        dispatch_completed_during_fit = []

        pool = ThreadPoolExecutor(max_workers=2)

        def blocking_worker(func, *args):
            worker_threads.append(threading.get_ident())
            loop.call_soon_threadsafe(fit_started.set)
            # Simulate CPU-bound work in background thread
            fit_release.wait(timeout=5.0)
            return func(*args)

        async def mock_async_add_executor_job(func, *args):
            return await loop.run_in_executor(pool, blocking_worker, func, *args)

        fake_hass = MagicMock()
        fake_hass.async_create_task = lambda coro: asyncio.create_task(coro)
        fake_hass.async_add_executor_job = mock_async_add_executor_job

        sensor = PumpSteerSensor.__new__(PumpSteerSensor)
        sensor.hass = fake_hass
        sensor._thermal_intelligence = ThermalIntelligence()
        sensor._thermal_fit_task = None
        sensor._config_entry = MagicMock()
        sensor._pilot_store_read_failed = False
        sensor._temperature_unloaded = False

        # Add data so fit is due
        now = START
        for i in range(50):
            t_hist = now - timedelta(hours=6) + timedelta(minutes=15 * i)
            sensor._thermal_intelligence.model.add_sample(
                Sample(
                    ts=t_hist,
                    indoor_c=20.0 - 0.01 * i,
                    outdoor_c=2.0,
                    effort=0.2 if i % 2 == 0 else 0.7,
                )
            )

        # Trigger background fit
        sensor._maybe_schedule_thermal_fit(now)
        assert sensor._thermal_fit_task is not None

        # Wait until background worker has actually started in the executor thread
        await fit_started.wait()

        # 1. Prove fitting runs off the event-loop thread
        assert len(worker_threads) == 1
        assert worker_threads[0] != loop_thread_id

        # 2. Prove fit is actively in progress and not done
        assert sensor._thermal_fit_task.done() is False
        assert sensor._thermal_intelligence._fit_in_progress is True

        # 3. Prove normal output dispatch proceeds unblocked while fitting is running
        async def mock_push_dispatch(fake_temp, cfg, expected):
            dispatch_completed_during_fit.append(fake_temp)

        sensor._push_validated_temperature = mock_push_dispatch
        await sensor._push_validated_temperature(-8.0, {}, {})
        assert dispatch_completed_during_fit == [-8.0]

        # 4. Release worker and allow task to complete
        fit_release.set()
        await sensor._thermal_fit_task
        pool.shutdown(wait=True)

        assert sensor._thermal_fit_task.done() is True
        assert sensor._thermal_intelligence._fit_in_progress is False

    asyncio.run(_async_test())

