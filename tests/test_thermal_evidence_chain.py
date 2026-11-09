"""The passive collector keeps changes independent of model/transport readiness."""
from datetime import timedelta

from custom_components.pumpsteer.thermal_intelligence.runtime import ThermalIntelligence
from test_thermal_intelligence_runtime import START, _cycle


def cycle(at=START, value=20.0, **kw):
    return _cycle(at, [('room', 1, value, 0)], **kw)


def output(runtime, at, requested=-8, status='service_completed_unverified'):
    runtime.record_output(dict(at=at.isoformat(), transport='ohmigo',
                               status=status, requested=requested))


def events(runtime, kind):
    return [row for row in runtime.export_state()['events'] if row['kind'] == kind]


def test_collects_sources_context_and_output_without_fitted_model():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    output(runtime, START)
    assert len(events(runtime, 'temperature')) == 2
    assert len(events(runtime, 'context')) == 1
    assert len(events(runtime, 'output')) == 1
    assert runtime.latest_observation['command_coordinate'] is None
    assert runtime.model.parameters() is None
    assert all(row['physical_time_known'] is False for row in events(runtime, 'temperature'))
    assert not events(runtime, 'output')[0]['physical_application_verified']


def test_actual_command_change_survives_constant_indoor_value_and_keepalive():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    output(runtime, START)
    for minute in range(1, 10):
        runtime.begin_cycle(cycle(START + timedelta(minutes=minute)), use_ohmigo_input=True)
        output(runtime, START + timedelta(minutes=minute))
        output(runtime, START + timedelta(minutes=minute), status='not_due')
    assert len(events(runtime, 'temperature')) == 2
    assert len(events(runtime, 'output')) == 1
    assert runtime.current_command['established_at'] == START.isoformat()
    output(runtime, START + timedelta(minutes=10), requested=-5)
    assert len(events(runtime, 'output')) == 2
    assert runtime.model._pending[-1][1].indoor_c is None
    assert runtime.model._pending[-1][1].effort != runtime.model._pending[0][1].effort


def test_context_and_interruptions_do_not_depend_on_new_temperature():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    output(runtime, START)
    runtime.begin_cycle(cycle(START + timedelta(minutes=1), mode='normal'), use_ohmigo_input=True)
    assert len(events(runtime, 'context')) == 2
    assert len(events(runtime, 'observation')) == 1
    assert runtime.current_command is None
    assert events(runtime, 'interruption')[-1]['reason'] == 'context_changed'
    assert any(sample.excluded for _, sample in runtime.model._pending)


def test_invalid_future_report_is_stored_but_not_consumed_or_flooded():
    runtime = ThermalIntelligence()
    bad = cycle()
    bad['sources'][0]['reported_at'] = (START + timedelta(minutes=5)).isoformat()
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    count = len(events(runtime, 'temperature'))
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    assert len(events(runtime, 'temperature')) == count
    assert not events(runtime, 'observation')
    assert events(runtime, 'temperature')[0]['timestamp_valid'] is False
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    assert len(events(runtime, 'observation')) == 1


def test_weather_failure_preserves_changed_indoor_for_retry():
    runtime = ThermalIntelligence()
    bad = cycle()
    bad['outdoor_reported_at'] = (START - timedelta(hours=4)).isoformat()
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    assert not events(runtime, 'observation')
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    assert len(events(runtime, 'observation')) == 1


def test_aggregate_timestamp_comes_from_actual_changed_contributor():
    runtime = ThermalIntelligence()
    initial = _cycle(START, [('a', .5, 20, 0), ('b', .5, 20, 0)])
    runtime.begin_cycle(initial, use_ohmigo_input=False)
    later = _cycle(START + timedelta(minutes=10), [('a', .5, 20.1, 300), ('b', .5, 20, 0)])
    runtime.begin_cycle(later, use_ohmigo_input=False)
    assert runtime.latest_observation['indoor_reported_at'] == (START + timedelta(minutes=5)).isoformat()


def test_one_persisted_chain_restores_without_held_command():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    output(runtime, START)
    state = runtime.export_state()
    assert not {'observations', 'outputs', 'scores', 'interruptions'} & state.keys()
    restored = ThermalIntelligence()
    assert restored.restore_state(state)
    assert restored.current_command is None
    assert events(restored, 'interruption')[-1]['reason'] == 'restart'
    restored.begin_cycle(cycle(START + timedelta(minutes=10)), use_ohmigo_input=True)
    assert len(events(restored, 'observation')) == 1
    assert restored._samples == 0


def test_retention_bounds_count_and_age():
    runtime = ThermalIntelligence()
    for minute in range(2200):
        output(runtime, START + timedelta(minutes=minute), requested=-minute)
    assert len(runtime.export_state()['events']) <= 2048
    output(runtime, START + timedelta(days=33), requested=3)
    assert len(runtime.export_state()['events']) == 1


def test_weather_changes_do_not_invent_command_excitation():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    output(runtime, START)
    coordinate = runtime.current_command['coordinate']
    runtime.begin_cycle(cycle(START + timedelta(minutes=10), value=20.1, outdoor=5), use_ohmigo_input=True)
    assert runtime.current_command['coordinate'] == coordinate
    commands = [sample.effort for _, sample in runtime.model._pending if sample.effort is not None]
    assert commands == [coordinate]
    assert runtime.latest_observation['command_coordinate'] == coordinate


def test_source_identity_change_retires_old_fit_but_keeps_chain():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    runtime.model._params = dict(a=1, b=.1, c=0, d_minutes=0, use_outdoor=True)
    replacement = cycle(START + timedelta(minutes=10))
    replacement['sources'][0]['entity'] = 'replacement'
    runtime.begin_cycle(replacement, use_ohmigo_input=True)
    assert runtime.model.parameters() is None
    assert len(events(runtime, 'context')) == 2
    assert len(events(runtime, 'observation')) == 2


def test_many_constant_republishes_have_constant_persisted_size():
    import json
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    output(runtime, START)
    initial_count = len(runtime.export_state()['events'])
    for second in range(1, 10001):
        at = START + timedelta(seconds=second)
        runtime.begin_cycle(cycle(at), use_ohmigo_input=True)
        output(runtime, at, status="dispatch_attempted")
        output(runtime, at)
        runtime.summary()
    assert len(runtime.export_state()['events']) == initial_count
    assert len(json.dumps(runtime.export_state())) < 8000


def test_restore_rejects_invalid_chain_without_mutating_live_state():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    before = runtime.export_state()
    corrupt = runtime.export_state()
    corrupt['events'] = [{'kind': 'temperature', 'at': START.isoformat(), 'sequence': 1,
                          'timestamp_valid': True, 'value': 20}]
    assert not runtime.restore_state(corrupt)
    assert runtime.export_state() == before


def test_confirmed_keepalive_refreshes_command_coverage_without_events():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    output(runtime, START)
    coordinate = runtime.current_command['coordinate']
    for minute in range(5, 181, 5):
        at = START + timedelta(minutes=minute)
        runtime.begin_cycle(cycle(at, value=20 + minute / 1000), use_ohmigo_input=True)
        output(runtime, at, status='dispatch_attempted')
        output(runtime, at)
    assert runtime.model._grid.effort[-1] == coordinate
    assert len(events(runtime, 'output')) == 1
    assert runtime.current_command['established_at'] == START.isoformat()


def test_unchanged_weather_seeds_new_context_without_temperature_event():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    outdoor_before = len([row for row in events(runtime, 'temperature') if row['role'] == 'outdoor'])
    at = START + timedelta(minutes=15)
    runtime.begin_cycle(cycle(at, mode='normal'), use_ohmigo_input=True)
    assert len([row for row in events(runtime, 'temperature') if row['role'] == 'outdoor']) == outdoor_before
    assert any(sample.ts == at and sample.outdoor_c == 2 for _, sample in runtime.model._pending)


def test_consecutive_context_changes_each_break_continuity_with_no_new_indoor():
    runtime = ThermalIntelligence()
    for minute, mode in enumerate(('braking', 'normal', 'preheating')):
        runtime.begin_cycle(cycle(START + timedelta(minutes=minute), mode=mode), use_ohmigo_input=True)
    assert runtime.context_id == 2
    assert len(events(runtime, 'interruption')) == 2


def test_restart_then_source_change_retires_restored_fit():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    runtime.model._params = dict(a=1, b=.1, c=0, d_minutes=0, use_outdoor=True)
    restored = ThermalIntelligence()
    assert restored.restore_state(runtime.export_state())
    replacement = cycle(START + timedelta(minutes=15))
    replacement['sources'][0]['entity'] = 'replacement'
    restored.begin_cycle(replacement, use_ohmigo_input=True)
    assert restored.model.parameters() is None


def test_out_of_order_change_does_not_borrow_previous_timestamp_or_consume_retry():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(START + timedelta(minutes=10), value=20), use_ohmigo_input=False)
    bad = cycle(START + timedelta(minutes=15), value=19)
    bad['sources'][0]['reported_at'] = (START + timedelta(minutes=5)).isoformat()
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    assert len(events(runtime, 'observation')) == 1
    runtime.begin_cycle(cycle(START + timedelta(minutes=20), value=19), use_ohmigo_input=False)
    assert len(events(runtime, 'observation')) == 2
    assert runtime.latest_observation['indoor_reported_at'] == (START + timedelta(minutes=20)).isoformat()
