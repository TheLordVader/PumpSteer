"""Effective response coefficients are empirical, qualified and not physical loss."""
import math
from datetime import timedelta
import pytest
from custom_components.pumpsteer.thermal_model import ThermalModel
from test_thermal_observer import NOW


def trained_model():
    model=ThermalModel()
    for i in range(96):
        delta=(-18,-14,-10,-6)[i%4]
        demand=(-8,0,8)[(i//4)%3]
        model.collect_response_sample(delta,demand,.45+.04*delta+.07*demand,
                                      .5,'normal')
    assert model.fit_response()
    return model


def test_independently_excited_observations_identify_effective_response():
    model=trained_model();fit=model.response
    assert all(a==pytest.approx(b,abs=1e-8) for a,b in zip(fit.coefficients,(.45,.04,.07)))
    assert fit.sample_count==96
    assert fit.revision>0
    assert fit.modes==('normal',)
    assert fit.supports(21,7,0,'normal')
    assert not fit.supports(21,7,20,'normal')
    assert not fit.supports(21,7,0,'preheating')


def test_closed_loop_collinearity_cannot_manufacture_identification():
    model=ThermalModel()
    for i in range(40):
        d=-20+i/4
        model.collect_response_sample(d,2*d,.2+.01*d,.5,'normal')
    assert not model.fit_response()
    assert model.response is None
    assert model.response_reason=='insufficient_independent_variation'


def test_response_predicts_piecewise_future_weather_causally():
    fit=trained_model().response
    path=((NOW+timedelta(minutes=30),7.),(NOW+timedelta(hours=1),3.))
    predicted=fit.project(21,0,NOW,path,'normal')
    expected=21
    for tout in (7,3):
        equilibrium=tout+.45/.04
        expected=equilibrium+(expected-equilibrium)*math.exp(-.04*.5)
    assert predicted==pytest.approx(expected)
    # Frozen fit remains unchanged by future training/revisions.
    with pytest.raises(Exception):
        fit.revision=10
    assert fit.project(21,20,NOW,path,'normal') is None


def test_response_restore_preserves_fit_not_fresh_observations():
    model=trained_model();saved=model.response_state()
    fresh=ThermalModel();assert fresh.restore_response(saved)
    assert fresh.response.coefficients==model.response.coefficients
    assert fresh.response_pending_samples==0
    assert fresh.response_matches==0
    assert not fresh.restore_response({'coefficients':[True,.04,.07]})


def test_minority_outliers_do_not_create_an_arbitrary_thermal_coefficient():
    model=ThermalModel()
    for i in range(96):
        delta=(-18,-14,-10,-6)[i%4];demand=(-8,0,8)[(i//4)%3]
        model.collect_response_sample(delta,demand,.45+.04*delta+.07*demand+(1.5 if i%11==0 else 0),.5,'normal')
    assert model.fit_response()
    assert all(a==pytest.approx(b,abs=.02) for a,b in zip(model.response.coefficients,(.45,.04,.07)))
    assert model.response.sample_count<96


@pytest.mark.parametrize('value',[float('nan'),True,99])
def test_invalid_rate_never_becomes_a_response_sample(value):
    m=ThermalModel()
    assert not m.collect_response_sample(-15,5,value,.5,'normal')
    assert m.response_pending_samples==0


def test_nearly_collinear_inputs_do_not_look_like_independent_excitation():
    m=ThermalModel()
    for i in range(80):
        delta=-18+(i%20)/2
        demand=delta*.5+math.sin(i)*.01
        m.collect_response_sample(delta,demand,.45+.04*delta+.07*demand,.5,'normal')
    assert not m.fit_response()


@pytest.mark.parametrize('change',[{'delta_range':[]},{'command_range':[0]},{'modes':[]}, {'noise_c_per_hour':float('nan')}])
def test_malformed_response_restore_is_ignored_without_losing_valid_reference(change):
    m=trained_model();original=m.response
    saved={**m.response_state(),**change}
    assert not m.restore_response(saved)
    assert m.response is original


def test_projection_does_not_end_outside_its_observed_domain():
    fit=trained_model().response
    assert fit.supports(21,2,8,'normal')
    assert fit.project(21,8,NOW,((NOW+timedelta(hours=2),2),),'normal') is None


def test_projection_comfort_bounds_cover_intermediate_weather_endpoints():
    from dataclasses import replace
    fit=replace(trained_model().response,coefficients=(.45,.04,.30),modes=('preheating',))
    path=((NOW+timedelta(minutes=30),5),(NOW+timedelta(hours=1),9))
    final=fit.project(21.6,0,NOW,path,'preheating',outdoor_now=7)
    assert 21.3<final<21.55
    assert fit.project(21.6,0,NOW,path,'preheating',outdoor_now=7,bounds=(20.15,21.55)) is None
