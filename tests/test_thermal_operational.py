"""Operational qualification uses real report intervals and explicit command context."""
from datetime import timedelta
import math
from test_thermal_observer import NOW,report
from custom_components.pumpsteer.thermal_model import ThermalModel
from custom_components.pumpsteer.thermal_observer import ThermalObserver


def operation(fake=0,mode='normal',extra=False):
    return dict(fake_outdoor_temperature=fake,mode=mode,brake_factor=0 if mode=='normal' else 1,
                external_heat_sources=extra)


def test_normal_mode_supplies_qualified_effective_response_intervals():
    obs=ThermalObserver(ThermalModel());obs.start(NOW-timedelta(seconds=1))
    for minute in range(61):
        at=NOW+timedelta(minutes=minute)
        obs.observe(at,report(21+.003*minute,at),report(5,at),braking=False,operation=operation())
    assert obs.model.response_pending_samples==6
    assert not obs.model.is_valid # normal intervals did not fake legacy cooling k
    assert obs.attributes()['response']['pending_samples']==6


def test_operational_safe_restore_and_source_change_break_intervals():
    obs=ThermalObserver(ThermalModel());obs.start(NOW-timedelta(seconds=1))
    for m in range(12):
        at=NOW+timedelta(minutes=m)
        obs.observe(at,report(21,at),report(5,at),braking=False,operation=operation(mode='safe_mode' if m==5 else 'normal'))
    assert obs.model.response_pending_samples==0
    for m in range(12,26):
        at=NOW+timedelta(minutes=m)
        obs.observe(at,report(21,at,restored=m==18),report(5,at),braking=False,operation=operation())
    assert obs.model.response_pending_samples==1
    at=NOW+timedelta(minutes=26)
    obs.observe(at,report(21,at,selected_source='backup'),report(5,at),braking=False,operation=operation())
    assert obs.model.response_pending_samples==0
    assert obs.model.response is None


def test_command_change_inside_interval_cannot_be_misread_as_extra_heat():
    obs=ThermalObserver(ThermalModel())
    for m in range(10):
        at=NOW+timedelta(minutes=m)
        obs.observe(at,report(21,at),report(5,at),braking=False,operation=operation(fake=m))
    assert obs.model.response_pending_samples==0
    assert obs.attributes()['heat_evidence']['status']!='suspected_extra_heat'


def test_yes_labels_suspected_heat_no_labels_deviation_both_protect_learning():
    from test_thermal_response import trained_model
    results=[]
    for enabled in (False,True):
        m=trained_model();m.reset_response();obs=ThermalObserver(m)
        value=21.
        for minute in range(181):
            at=NOW+timedelta(minutes=minute)
            if minute:
                pulse=1.2 if 120<minute<=150 else 0
                equilibrium=7+(.45+pulse)/.04
                value=equilibrium+(value-equilibrium)*math.exp(-.04/60)
            obs.observe(at,report(value,at),report(7,at),braking=False,operation=operation(fake=0,extra=enabled))
            if minute==150:
                e=obs.attributes()['heat_evidence']
                assert e['status']==('suspected_extra_heat' if enabled else 'warming_deviation')
                assert e['verified'] is False
                assert not e['last_window']['qualified_for_learning']
        results.append(m.response_pending_samples)
    assert results[0]==results[1]


def test_future_weather_is_frozen_in_new_operational_predictions():
    from test_thermal_response import trained_model
    from custom_components.pumpsteer.forecast import ThermalForecast,WeatherSlot
    m=trained_model();obs=ThermalObserver(m)
    fc=ThermalForecast(NOW,'weather.house','sensor.price',None,
         (WeatherSlot(NOW,NOW+timedelta(hours=1),7),WeatherSlot(NOW+timedelta(hours=1),NOW+timedelta(hours=2),3)))
    obs.observe(NOW,report(21),report(7),braking=False,operation=operation(fake=7),forecast=fc)
    original=obs.diagnostics.pending_predictions
    assert len(original)==3
    assert original[-1].weather_path[-1][1]==3
    assert original[-1].response_coefficients==m.response.coefficients
    at=NOW+timedelta(minutes=1)
    later=ThermalForecast(at,'weather.house','sensor.price',None,
         (WeatherSlot(at,at+timedelta(hours=1),10),WeatherSlot(at+timedelta(hours=1),at+timedelta(hours=2),12)))
    obs.observe(at,report(21,at),report(7,at),braking=False,operation=operation(fake=7),forecast=later)
    assert obs.diagnostics.pending_predictions==original
    assert original[-1].forecast_received_at==NOW


def test_restore_matches_temperature_definition_not_only_entity_ids():
    from test_thermal_response import trained_model
    saved=trained_model().response_state()
    for selected,retained in [('primary',True),('backup',False)]:
        m=ThermalModel();assert m.restore_response(saved)
        obs=ThermalObserver(m)
        assert obs.restore_response_identity(((None,"'primary'",None),(None,'None',None)))
        obs.start(NOW-timedelta(seconds=1))
        obs.observe(NOW,report(21,selected_source=selected),report(7),braking=False,operation=operation(fake=7))
        assert (m.response is not None) is retained
    m=trained_model();obs=ThermalObserver(m)
    assert not obs.restore_response_identity(None)
    assert m.response is None
