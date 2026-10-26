"""Complete shadow records future inputs and unapplied advice, never control writes."""
import json
from datetime import timedelta
from types import SimpleNamespace
from test_thermal_response import trained_model
from test_thermal_shadow_verification import AT,IDS,MODEL,feed
from thermal_observation import Shadow,read_weather_forecast
from thermal_observation_evaluate import summarize_shadow
from custom_components.pumpsteer.forecast import ThermalForecast,WeatherSlot,PriceSlot


def test_complete_shadow_logs_forecast_inputs_recommendation_and_true_outcomes():
    events=[];s=Shadow(MODEL,entities=IDS,emit=events.append,operational=True,execution_basis='offline_simulation')
    s.boundary(AT-timedelta(seconds=1))
    s.observer.model.restore_response(trained_model().response_state())
    s.calibration_origin='test_fixture'
    fc=ThermalForecast(AT,'weather.house','sensor.price',None,
       (WeatherSlot(AT,AT+timedelta(hours=1),5),WeatherSlot(AT+timedelta(hours=1),AT+timedelta(hours=2),3)),
       (PriceSlot(AT,AT+timedelta(hours=1),1),PriceSlot(AT+timedelta(hours=1),AT+timedelta(hours=2),3)),AT,'SEK/kWh')
    assert s.set_forecast(fc,AT)
    latest,admitted={},{}
    for minute in range(31):
        feed(s,latest,admitted,minute,mode='normal',factor=0)
    s.finish(AT+timedelta(minutes=31))
    forecasts=[e for e in events if e['kind']=='forecast']
    assert forecasts and forecasts[0]['prediction']['response_coefficients'] is not None
    assert forecasts[0]['prediction']['forecast_received_at']==AT.isoformat()
    assert forecasts[0]['forecast_snapshot_id']
    assert forecasts[0]['prediction']['price_unit']=='SEK/kWh'
    assert any(e['kind']=='recommendation' for e in events)
    assert all(e['recommendation']['effective'] is False for e in events if e['kind']=='recommendation')
    result=summarize_shadow(events)
    assert result['basis']=='offline_simulation_not_original_live_forecasts'
    assert result['recommendations']['total']>0
    assert result['recommendations']['applied']==0
    assert result['decisions']['normal']['reasons'].get('outside_braking',0)==0
    assert sum(g['evaluated'] for g in result['groups'])>=1
    assert not result['integrity_issues']


def test_weather_route_is_fixed_read_only_forecast_action():
    requests=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'service_response':{'weather.house':{'forecast':[]}}}).encode()
    def opener(request,timeout):
        requests.append(request)
        return Response()
    assert read_weather_forecast('http://ha.invalid','test-only','weather.house',opener=opener)==[]
    r=requests[0]
    assert r.method=='POST' and r.full_url.endswith('/api/services/weather/get_forecasts?return_response')
    assert json.loads(r.data)=={'entity_id':'weather.house','type':'hourly'}


def test_rejected_future_report_does_not_become_new_when_clock_catches_up():
    from thermal_observation import accept_reading
    latest,admitted={},{}
    future=AT+timedelta(minutes=1)
    raw={'entity_id':IDS[0],'state':'21','last_reported':future.isoformat(),'attributes':{'unit_of_measurement':'°C'}}
    assert accept_reading(latest,admitted,raw,AT,entities=IDS)[0]=='unqualified'
    assert accept_reading(latest,admitted,raw,AT+timedelta(minutes=2),entities=IDS)[0]!='report'
    assert latest[IDS[0]]['_quarantined']
    # A genuinely different valid report recovers, without a long lockout.
    raw={**raw,'last_reported':(AT+timedelta(minutes=2)).isoformat()}
    assert accept_reading(latest,admitted,raw,AT+timedelta(minutes=2),entities=IDS)[0]=='report'


def test_pre_start_seed_is_not_counted_as_a_new_indoor_report_decision():
    from thermal_observation import accept_reading
    events=[];s=Shadow(MODEL,entities=IDS,emit=events.append,operational=True)
    s.boundary(AT)
    latest,admitted={},{}
    for entity,value in zip(IDS,(21,5,5)):
        raw=dict(entity_id=entity,state=str(value),last_reported=(AT-timedelta(seconds=30)).isoformat(),
                 attributes=dict(unit_of_measurement='°C',mode='normal',brake_factor=0,
                                 target_temperature=21,aggressiveness=3))
        accept_reading(latest,admitted,raw,AT+timedelta(seconds=1),entities=IDS)
    s.update(latest,AT+timedelta(seconds=1))
    assert not any(e['kind']=='decision' for e in events)
    assert not any(e['kind']=='forecast' for e in events)


def test_malformed_weather_rows_are_a_read_failure_not_an_uncaught_attribute_error():
    import pytest
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'service_response':{'weather.house':{'forecast':['broken']}}}).encode()
    with pytest.raises(ValueError):
        read_weather_forecast('http://ha.invalid','test-only','weather.house',opener=lambda *args,**kw:Response())
