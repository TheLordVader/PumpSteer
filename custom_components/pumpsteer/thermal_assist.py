"""Bounded, explainable shadow advice. No controller/output calls.

Only simulate_assistance applies a numeric delta, and it is used solely by offline
verification. The HA adapter publishes advice after the PI/output cycle completes.
"""
from datetime import timedelta
import math
from .thermal_intelligence import _utc, _ISSUE_INTERVAL_SECONDS
from .settings import MIN_FAKE_TEMP, MAX_FAKE_TEMP


def assess_assistance(requested, diagnostics, *, now=None, forecast=None, indoor=None,
                      outdoor=None, target=None, comfort_floor=None, ceiling=None,
                      mode=None, control_signal=None, response=None, preheat_enabled=False, pi_projection=None):
    quality=diagnostics.get('quality',{}).get('30',{})
    result=dict(requested=requested is True,effective=False,eligible=False,
        experimental=True,action='hold',delta_fake_c=0.,advance_minutes=0,
        valid_for_minutes=_ISSUE_INTERVAL_SECONDS/60,
        reason='control_benefit_not_validated' if requested is True else 'disabled',
        model_version=diagnostics.get('model_version'),
        heat_evidence=diagnostics.get('heat_evidence',{}).get('status'),
        evaluated_30min_forecasts=quality.get('count',0),mae_30min_c=quality.get('mae_c'),
        coverage_30min=quality.get('coverage'),
        economic_basis='price_indicator_not_verified_energy_cost',
        issued_at=now.isoformat() if _utc(now) else None,operating_mode=mode,
        assumptions=['held_command_assumption','unknown_delivered_heat','counterfactual_benefit_unverified'])
    def abstain(reason):
        result['reason']=reason
        return result
    if requested is not True or now is None:
        return result
    clock=_utc(now)
    from datetime import datetime
    try:
        issued=_utc(datetime.fromisoformat(diagnostics.get('forecast_issued_at','')))
    except (TypeError,ValueError):
        issued=None
    if (clock is None or any(type(v) not in (int,float) or not math.isfinite(v)
        for v in (indoor,outdoor,target,comfort_floor,ceiling,control_signal))
        or not comfort_floor<=target<=ceiling
        or mode not in ('normal','preheating','braking','brake_hold','recovery','holiday')):
        return abstain('unsafe_or_unknown_context')
    if response is None or not response.supports(indoor,outdoor,control_signal,mode):
        return abstain('response_not_identified_in_this_context')
    if (diagnostics.get('response',{}).get('comparable_matches',0)<3
        or diagnostics.get('response',{}).get('confirmed') is not True):
        return abstain('response_unconfirmed')
    if result['heat_evidence'] in ('suspected_extra_heat','warming_deviation','cooling_deviation'):
        return abstain('unexplained_response_suppress_advice')
    if forecast is None or forecast.weather_path(clock,clock+timedelta(hours=2),outdoor) is None:
        return abstain('future_weather_unavailable')
    prices=forecast.prices_between(clock,clock+timedelta(hours=2))
    if not prices or not forecast.price_unit:
        return abstain('future_prices_unavailable')
    if issued is None or issued!=clock:
        return abstain('awaiting_current_forecast_issuance')
    values=diagnostics.get('future_temperature_c',{})
    uncertainty=diagnostics.get('uncertainty_c',{})
    def qualified(h):
        q=diagnostics.get('quality',{}).get(str(h),{})
        numbers=(q.get('mae_c'),q.get('persistence_mae_c'),q.get('bias_c'),q.get('coverage'),values.get(str(h)),uncertainty.get(str(h)))
        return (all(type(v) in (int,float) and math.isfinite(v) for v in numbers)
                and type(q.get('count')) is int and q['count']>=20 and q['coverage']>=.5
                and q['mae_c']<q['persistence_mae_c'] and q['mae_c']<=.2
                and abs(q['bias_c'])<=.1 and uncertainty[str(h)]<=.5)
    if not qualified(30):
        return abstain('forecast_not_better_than_persistence')
    if indoor<=comfort_floor or indoor>=ceiling:
        return abstain('existing_comfort_guard_has_priority')
    delta=0.;action='hold';used=30
    if values['30']-uncertainty['30']<comfort_floor+.1:
        delta=-1.;action='release_brake' if mode in ('braking','brake_hold') else 'earlier_recovery'
    elif mode=='preheating' and qualified(60) and values['60']+uncertainty['60']>=ceiling:
        delta=1.;action='reduce_preheat';used=60
    elif preheat_enabled is True and mode in ('normal','holiday','preheating'):
        if not qualified(60):
            return abstain('preheat_horizon_unvalidated')
        if max(p.price for p in prices)>prices[0].price and values['60']+uncertainty['60']<target:
            delta=-1.;action='bounded_preheat';used=60
    elif mode in ('braking','brake_hold') and qualified(60):
        if prices[0].price>min(p.price for p in prices) and values['60']-uncertainty['60']>comfort_floor+.3:
            delta=.5;action='bounded_brake';used=60
    if not delta:
        return abstain('no_supported_adjustment')
    # Reject extrapolation into unobserved control_signal, or a candidate that conflicts
    # with the current model's empirical comfort allowance.
    path=forecast.weather_path(clock,clock+timedelta(minutes=used),outdoor)
    allowance=uncertainty[str(used)]
    bounds=(comfort_floor+allowance,ceiling-allowance)
    if pi_projection is not None:
        candidate=pi_projection.project(response,indoor,control_signal,clock,path,mode,
            adjustment_fake_c=delta,adjustment_minutes=_ISSUE_INTERVAL_SECONDS/60,bounds=bounds) if path else None
    else:
        # A proposal lasts at most the existing issuance cadence. Do not claim
        # the effect of a longer override when the proposal expires sooner.
        cutoff=clock+timedelta(seconds=_ISSUE_INTERVAL_SECONDS)
        value=indoor;point=clock
        candidate=None
        if path:
            for end,out in path:
                first=min(end,cutoff)
                if point<first:
                    value=response.project(value,control_signal-delta,point,((first,out),),mode,
                                           outdoor_now=outdoor,bounds=bounds)
                    point=first
                if value is None:break
                if point<end:
                    value=response.project(value,control_signal,point,((end,out),),mode,
                                           outdoor_now=outdoor,bounds=bounds)
                    point=end
                if value is None:break
            candidate=value
    if candidate is None or not bounds[0]<=candidate<=bounds[1]:
        return abstain('candidate_outside_observed_domain_or_comfort')
    result.update(eligible=True,action=action,delta_fake_c=delta,advance_minutes=0,
                  reason='bounded_conditional_shadow_advice',horizon_minutes=used,
                  price_spread=max(p.price for p in prices)-min(p.price for p in prices),
                  forecast_received_at=forecast.received_at.isoformat(),
                  response_revision=response.revision)
    return result


def simulate_assistance(base_fake, recommendation, *, now, indoor, comfort_floor,
                        ceiling, mode, qualified=False, safe=False, model_version=None):
    """Pure offline application; never called by the integration or collector."""
    try:
        from datetime import datetime
        issued=_utc(datetime.fromisoformat(recommendation['issued_at']))
        clock=_utc(now)
        delta=recommendation['delta_fake_c']
        if (safe or qualified is not True or not recommendation.get('eligible')
            or recommendation.get('operating_mode')!=mode
            or model_version is None or recommendation.get('model_version')!=model_version or clock is None or issued is None
            or not 0<=(clock-issued).total_seconds()<=_ISSUE_INTERVAL_SECONDS
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in (base_fake,delta,indoor,comfort_floor,ceiling))
            or abs(delta)>1 or comfort_floor>=ceiling
            or (delta>0 and indoor<=comfort_floor+.1)
            or (delta<0 and indoor>=ceiling)):
            return base_fake
        return max(MIN_FAKE_TEMP,min(MAX_FAKE_TEMP,base_fake+delta))
    except (KeyError,TypeError,ValueError):
        return base_fake
