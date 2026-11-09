"""Field counters describe changed evidence independently of the retention cap."""
from datetime import timedelta

import pytest
from test_thermal_intelligence_runtime import START, _cycle

from custom_components.pumpsteer.thermal_intelligence.runtime import ThermalIntelligence


def cycle(at=START, value=20):
    return _cycle(at, [('room', 1, value, 0)])


def counts(runtime):
    return runtime.summary()['evidence_diagnostics']['counts']


def test_republishes_do_not_dilute_source_change_rejection_fraction():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    for minute in range(1, 101):
        runtime.begin_cycle(cycle(START + timedelta(minutes=minute)), use_ohmigo_input=False)
    bad = cycle(START + timedelta(minutes=101), 21)
    bad['sources'][0]['reported_at'] = (START + timedelta(minutes=102)).isoformat()
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    c = counts(runtime)
    assert c['source_numeric_changes'] == 3  # indoor/outdoor initial plus changed indoor.
    assert c['source_change_attempts'] == 3
    assert c['source_changes_accepted'] == 2
    assert c['source_changes_rejected'] == c['reject_report_time_future'] == 1
    assert c['identical_source_rechecks'] == 201
    assert runtime.summary()['evidence_diagnostics']['source_rejection_fraction'] == pytest.approx(1 / 3)
    for _ in range(100):
        runtime.begin_cycle(bad, use_ohmigo_input=False)
    assert counts(runtime)['source_change_attempts'] == 3


@pytest.mark.parametrize('reason,reported', [
    ('report_time_missing', None),
    ('report_time_future', START + timedelta(minutes=20)),
    ('report_time_stale', START - timedelta(hours=2)),
    ('report_out_of_order', START - timedelta(minutes=1)),
])
def test_fixed_rejection_reasons_identify_bad_numeric_changes(reason, reported):
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    bad = cycle(START + timedelta(minutes=10), 21)
    bad['sources'][0]['reported_at'] = reported
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    assert counts(runtime)['reject_' + reason] == 1
    assert counts(runtime)['source_changes_rejected'] == 1
    assert counts(runtime)['aggregate_observations'] == 1


def test_quality_retry_is_separate_from_genuine_numeric_change():
    runtime = ThermalIntelligence()
    bad = cycle()
    bad['sources'][0]['reported_at'] = None
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    runtime.begin_cycle(cycle(START + timedelta(minutes=1)), use_ohmigo_input=False)
    c = counts(runtime)
    assert c['source_numeric_changes'] == 2
    assert c['source_change_attempts'] == 3
    assert c['source_changes_accepted'] == 2
    assert c['source_changes_rejected'] == 1
    assert c['accepted_changes_physical_time_unknown'] == 2


def test_counters_survive_event_eviction_and_restart_and_v2_without_counters():
    runtime = ThermalIntelligence()
    for i in range(2200):
        runtime._interrupt('a' if i % 2 else 'b', START + timedelta(seconds=i))
    assert len(runtime.export_state()['events']) == 2048
    assert counts(runtime)['interruptions'] == 2200
    restored = ThermalIntelligence()
    assert restored.restore_state(runtime.export_state())
    assert counts(restored)['interruptions'] == 2201
    assert counts(restored)['restarts'] == 1
    old_v2 = runtime.export_state()
    old_v2.pop('diagnostic_counters')
    old_v2.pop('counter_started_at')
    assert restored.restore_state(old_v2)
    assert counts(restored)['interruptions'] == counts(restored)['restarts'] == 1
    assert restored.summary()['evidence_diagnostics']['scope'] == 'since_counter_start_not_retained_event_window'


def test_command_confirmations_and_changes_and_dispatch_failure_are_distinct():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=True)
    def dispatch(value, status='service_completed_unverified'):
        runtime.record_output({'at': START, 'transport': 'ohmigo', 'status': status, 'requested': value})
    dispatch(-8)
    dispatch(-8)
    dispatch(-6)
    dispatch(-6, 'dispatch_failed')
    dispatch(-6, 'dispatch_failed')
    c = counts(runtime)
    assert c['command_establishments'] == 2
    assert c['command_value_changes'] == 1
    assert c['command_confirmations'] == 1
    assert c['dispatch_failures'] == 2
    assert runtime.summary()['command_semantics'] == 'service_request_unverified_not_thermal_power'


def test_bad_counters_are_rejected_transactionally():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    before = runtime.export_state()
    for bad in (-1, True, float('inf'), '1'):
        state = runtime.export_state()
        state['diagnostic_counters']['source_changes_rejected'] = bad
        assert not runtime.restore_state(state)
        assert runtime.export_state() == before


def test_model_attempt_status_exposes_inflight_and_obsolete_result(monkeypatch):
    runtime = ThermalIntelligence()
    monkeypatch.setattr(runtime.model, 'fit_due', lambda now: True)
    token, _ = runtime.prepare_fit_job(START)
    assert runtime.summary()['fit_in_progress'] is True
    assert counts(runtime)['fit_attempts'] == 1
    runtime.suspend('safe_mode', START)
    assert not runtime.apply_fit_result(token, {'status': 'ok'})
    assert counts(runtime)['fit_results_stale'] == 1
    assert runtime.summary()['fit_in_progress'] is False
    runtime.apply_fit_result(runtime._fit_generation, {'status': 'candidate_insufficient_excitation'})
    assert counts(runtime)['fit_results_applied'] == 1
    assert runtime.summary()['last_fit_status'] == 'candidate_insufficient_excitation'


def test_distinct_raw_change_rejections_remain_counted_when_event_is_deduplicated():
    runtime = ThermalIntelligence()
    runtime.begin_cycle(cycle(), use_ohmigo_input=False)
    bad = cycle(START + timedelta(minutes=1), 21)
    bad['sources'][0]['reported_at'] = START + timedelta(minutes=10)
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    runtime.begin_cycle(cycle(START + timedelta(minutes=2)), use_ohmigo_input=False)
    bad['at'] = START + timedelta(minutes=3)
    runtime.begin_cycle(bad, use_ohmigo_input=False)
    assert counts(runtime)['source_numeric_changes'] == 5
    assert counts(runtime)['source_change_attempts'] == 5
    assert counts(runtime)['reject_report_time_future'] == 2
