"""Adversarial regressions for passive capture; no physical outputs."""
from datetime import timedelta

from custom_components.pumpsteer.thermal_intelligence.runtime import ThermalIntelligence
from test_thermal_intelligence_runtime import START, _cycle


def test_rejected_weather_does_not_consume_genuine_temperature_change():
    runtime = ThermalIntelligence()
    cycle = _cycle(START, [('room', 1, 20, 0)])
    cycle['outdoor_reported_at'] = (START - timedelta(hours=4)).isoformat()
    runtime.begin_cycle(cycle, use_ohmigo_input=False)
    assert not runtime._observations
    cycle['outdoor_reported_at'] = START.isoformat()
    runtime.begin_cycle(cycle, use_ohmigo_input=False)
    assert len(runtime._observations) == 1


def test_future_component_cannot_create_observation():
    runtime = ThermalIntelligence()
    cycle = _cycle(START, [('room', 1, 20, -60)])
    runtime.begin_cycle(cycle, use_ohmigo_input=False)
    assert not runtime._observations


def test_old_component_cannot_hide_behind_newest_component():
    runtime = ThermalIntelligence()
    cycle = _cycle(START, [('room_a', .7, 20, 0), ('room_b', .3, 18, 7200)])
    runtime.begin_cycle(cycle, use_ohmigo_input=False)
    assert not runtime._observations


def test_obsolete_fit_result_does_not_unlock_new_fit_job():
    runtime = ThermalIntelligence()
    runtime._fit_generation = 2
    runtime._fit_in_progress = True
    assert not runtime.apply_fit_result(1, {'status': 'fit_failed'})
    assert runtime._fit_in_progress


def test_restart_does_not_restore_open_resampling_window():
    from custom_components.pumpsteer.thermal_intelligence.rtm_core import Sample
    runtime = ThermalIntelligence()
    runtime.model.add_sample(Sample(START, 20, 2, .3))
    restored = ThermalIntelligence()
    assert restored.restore_state(runtime.export_state())
    assert not restored.model._pending


def test_safe_entry_discards_passive_forecast(request):
    import asyncio
    controller, states, clock = request.getfixturevalue("control")
    runtime = controller._thermal_intelligence
    runtime._pending_forecast = {'issued': clock[0].isoformat()}
    asyncio.run(controller._enter_safe_mode('test', 2, clock[0]))
    assert runtime._pending_forecast is None


from test_safe_mode_output import setup_sensor  # noqa: E402,F401
from test_temperature_input_safety import control  # noqa: E402,F401


def test_base_and_physical_service_calls_match_with_passive_capture(request):
    import asyncio
    from custom_components.pumpsteer import ohmigo
    controller, states, clock = request.getfixturevalue("control")
    controller._config_entry.options['thermal_intelligence_enabled'] = False
    asyncio.run(controller.async_update())
    reference = list(controller.hass.services.calls)
    reference_state = controller.state
    reference_mode = controller.extra_state_attributes['mode']
    controller._config_entry.options['thermal_intelligence_enabled'] = True
    controller._ohmigo_last_push = None
    ohmigo._modbus_last_push_by_entry.clear()
    controller.hass.services.calls.clear()
    asyncio.run(controller.async_update())
    assert controller.hass.services.calls == reference
    assert controller.state == reference_state
    assert controller.extra_state_attributes['mode'] == reference_mode


def test_corrupt_stored_scores_are_rejected_before_control_summary():
    runtime = ThermalIntelligence()
    cycle = _cycle(START, [('room', 1, 20, 0)])
    runtime.begin_cycle(cycle, use_ohmigo_input=False)
    state = runtime.export_state()
    state['scores'] = [{'identity': repr(runtime._source_signature),
                        'model_error_c': 'broken', 'persistence_error_c': 0.1}]
    restored = ThermalIntelligence()
    assert not restored.restore_state(state)
    assert restored.summary()['forecast_count'] == 0
