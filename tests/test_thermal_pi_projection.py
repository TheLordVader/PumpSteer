"""Reuse of the actual PI algorithm in pure forecast copies, never live mutation."""
from datetime import timedelta
import math
from test_thermal_observer import NOW
from test_thermal_response import trained_model
from custom_components.pumpsteer.thermal_intelligence import PIProjection


def context(mode='normal',factor=0):
    return PIProjection(target=21,integral=1,last_error=0,kp=2.4,ki=.035,kd=0,
       integral_clamp=6,output_clamp=12,mode=mode,brake_factor=factor,brake_delta_c=10,
       extra_demand=0,reported_at=NOW)


def test_pi_preview_corrects_future_temperature_without_mutating_state():
    from dataclasses import replace
    fit=replace(trained_model().response,coefficients=(.8,.04,.04),delta_range=(-30,0),command_range=(-25,20))
    state=context();before=state.integral
    path=((NOW+timedelta(hours=1),5),)
    # At target and balanced demand, feedback and plant stay balanced.
    value=state.project(fit,21,-4,NOW,path,'normal')
    assert abs(value-21)<.01
    assert state.integral==before
    cold=state.project(fit,20,-1.6,NOW,path,'normal')
    held=fit.project(20,-1.6,NOW,path,'normal',outdoor_now=5)
    assert cold>held
    assert state.integral==1


def test_full_brake_preview_does_not_run_free_heating_pi():
    from dataclasses import replace
    fit=replace(trained_model().response,coefficients=(.8,.04,.04),delta_range=(-30,0),command_range=(-25,20),modes=('braking',))
    state=context('braking',1)
    path=((NOW+timedelta(hours=1),5),)
    value=state.project(fit,21,-15,NOW,path,'braking')
    expected=10+(21-10)*math.exp(-.04)
    assert abs(value-expected)<.01


def test_preview_refuses_future_stale_or_incompatible_controller_state():
    from dataclasses import replace
    fit=trained_model().response
    path=((NOW+timedelta(hours=1),7),)
    assert replace(context(),reported_at=NOW+timedelta(seconds=1)).project(fit,21,0,NOW,path,'normal') is None
    assert replace(context(),reported_at=NOW-timedelta(minutes=3)).project(fit,21,0,NOW,path,'normal') is None
    assert context('braking',1).project(fit,21,0,NOW,path,'normal') is None


def test_forecast_uncertainty_uses_only_already_observed_same_revision_errors():
    from dataclasses import replace
    from custom_components.pumpsteer.thermal_intelligence import ThermalDiagnostics,TemperatureMeasurement
    fit=replace(trained_model().response,coefficients=(.8,.04,.04),noise_c_per_hour=.4)
    diagnostic=ThermalDiagnostics();original=None
    for index in range(25):
        at=NOW+timedelta(minutes=30*index)
        p=diagnostic.observe(now=at,indoor=TemperatureMeasurement(21.,at),
             outdoor=TemperatureMeasurement(5.,at),k_per_hour=None,response=fit,
             control_signal=-4.,pi_projection=replace(context(),reported_at=at),
             operating_mode='normal')
        if original is None:original=p[0]
    assert diagnostic.quality(operating_mode='normal')[30].count>=20
    current=next(p for p in diagnostic.predictions if p.horizon_minutes==30)
    assert current.uncertainty_c==.1
    assert current.uncertainty_basis.startswith('observed_same_revision_errors')
    assert original.uncertainty_c>.5 # future data never rewrote the old allowance


def test_pi_snapshot_factory_does_not_convert_invalid_bool_or_nan_to_state():
    attrs=dict(mode='normal',target_temperature=21.,pi_i_term=1.,pi_error_c=0.,brake_factor=0)
    assert PIProjection.from_control({},attrs,NOW) is not None
    for key,value in [('pi_i_term',True),('pi_error_c',float('nan'))]:
        assert PIProjection.from_control({},{**attrs,key:value},NOW) is None
