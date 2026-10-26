"""Fail-closed advisory path, never a runtime actuation hook."""
from datetime import timedelta
from test_thermal_observer import NOW
from test_thermal_response import trained_model
from custom_components.pumpsteer.forecast import ThermalForecast, WeatherSlot, PriceSlot
from custom_components.pumpsteer.thermal_assist import assess_assistance, simulate_assistance


def planning_context(mode='normal'):
    forecast=ThermalForecast(NOW,'weather.house','sensor.price',None,
        (WeatherSlot(NOW,NOW+timedelta(hours=1),7),WeatherSlot(NOW+timedelta(hours=1),NOW+timedelta(hours=2),5)),
        (PriceSlot(NOW,NOW+timedelta(hours=1),1),PriceSlot(NOW+timedelta(hours=1),NOW+timedelta(hours=2),4)),NOW,price_unit='SEK/kWh')
    return dict(now=NOW,forecast=forecast,indoor=21.,outdoor=7.,target=21.,comfort_floor=20.,
                ceiling=21.7,mode=mode,control_signal=0.,response=trained_model().response,
                preheat_enabled=True)


def diagnostics(mode='normal'):
    return dict(status='conditional',reason='forecast_issued',model_version=1,forecast_issued_at=NOW.isoformat(),
                response={'comparable_matches':3,'confirmed':True},heat_evidence={'status':'consistent_with_model'},
                quality={str(h):dict(count=24,coverage=.8,mae_c=.05,persistence_mae_c=.2,bias_c=0.) for h in (30,60,120)},
                future_temperature_c={'30':20.8,'60':20.5,'120':20.2},uncertainty_c={'30':.12,'60':.15,'120':.2})


def test_economic_preheat_advisory_is_bounded_not_claimed_as_savings():
    d=diagnostics();r=assess_assistance(True,d,**planning_context())
    assert r['eligible'] and r['action']=='bounded_preheat'
    assert -1<=r['delta_fake_c']<0
    assert r['effective'] is False
    assert r['economic_basis']=='price_indicator_not_verified_energy_cost'
    assert r['advance_minutes']<=15


def test_brake_release_requires_validated_relevant_horizon_and_comfort_risk():
    d=diagnostics();d['future_temperature_c']['30']=20.05
    ctx=planning_context('braking')
    # Fixture explicitly supplies fit support; this test is a guard test, not measured efficacy.
    from dataclasses import replace
    ctx['response']=replace(ctx['response'],modes=('braking',))
    r=assess_assistance(True,d,**ctx)
    assert r['action']=='release_brake' and r['delta_fake_c']<0
    d['quality']['30']['persistence_mae_c']=.01
    assert assess_assistance(True,d,**ctx)['delta_fake_c']==0


def test_missing_forecast_prices_or_bad_quality_returns_exactly_zero():
    for change in ('weather','prices','quality','heat','unconfirmed'):
        d=diagnostics();ctx=planning_context()
        if change=='weather':ctx['forecast']=None
        if change=='prices':
            from dataclasses import replace
            ctx['forecast']=replace(ctx['forecast'],prices=())
        if change=='quality':d['quality']['60']['count']=0
        if change=='heat':d['heat_evidence']['status']='warming_deviation'
        if change=='unconfirmed':d['response']['comparable_matches']=0
        assert assess_assistance(True,d,**ctx)['delta_fake_c']==0


def test_pure_simulation_cannot_override_comfort_context_or_stale_advice():
    r=assess_assistance(True,diagnostics(),**planning_context())
    kwargs=dict(now=NOW,indoor=21.,comfort_floor=20.,ceiling=21.7,mode='normal',qualified=True,model_version=1)
    assert simulate_assistance(7.,r,**kwargs)==7.+r['delta_fake_c']
    assert simulate_assistance(7.,r,**{**kwargs,'safe':True})==7.
    assert simulate_assistance(7.,r,**{**kwargs,'qualified':False})==7.
    assert simulate_assistance(7.,r,**{**kwargs,'mode':'braking'})==7.
    assert simulate_assistance(7.,r,**{**kwargs,'now':NOW+timedelta(minutes=31)})==7.
    assert simulate_assistance(7.,r,**{**kwargs,'indoor':21.8})==7.


def test_mixed_or_unknown_price_units_do_not_support_an_economic_recommendation():
    from dataclasses import replace
    ctx=planning_context()
    ctx['forecast']=replace(ctx['forecast'],price_unit=None)
    assert assess_assistance(True,diagnostics(),**ctx)['delta_fake_c']==0


def test_recommendation_cannot_relabel_an_old_forecast_as_a_new_horizon():
    d=diagnostics();ctx=planning_context()
    d['forecast_issued_at']=(NOW-timedelta(minutes=29)).isoformat()
    assert assess_assistance(True,d,**ctx)['delta_fake_c']==0
    d['forecast_issued_at']=(NOW+timedelta(seconds=1)).isoformat()
    assert assess_assistance(True,d,**ctx)['delta_fake_c']==0


def test_abstention_names_the_missing_model_before_waiting_for_a_forecast():
    ctx=planning_context();ctx['response']=None
    d=diagnostics();d['forecast_issued_at']=None
    assert assess_assistance(True,d,**ctx)['reason']=='response_not_identified_in_this_context'
