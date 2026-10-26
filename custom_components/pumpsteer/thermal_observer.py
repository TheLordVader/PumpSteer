"""Passive HA-report adapter; never called by control decisions or outputs."""

from datetime import datetime
import math

from .thermal_intelligence import (
    ThermalDiagnostics,
    TemperatureMeasurement,
    _utc,
    _MAX_SAMPLE_AGE_SECONDS,
)
from .thermal_model import ThermalModel, _MAX_QUALIFIED_INTERVAL_SECONDS


class ThermalObserver:
    """Reuse the existing learner and reference evaluator with bounded evidence.

    last_reported is HA reception, NOT a proven physical measurement timestamp.
    Reports must be distinct, post-start, finite, Celsius and not explicitly
    restored. Unknown source-side age remains a limitation of every result.
    """

    def __init__(self, model: ThermalModel) -> None:
        self.model = model
        self.diagnostics = ThermalDiagnostics()
        self._operational = False
        self._operation = {}
        self._response_identity = None
        self._forecast = None
        self._response_fit_at = None
        self._reference_revision = None
        self._reference_hours = self._reference_residual = 0.
        self._response_confirmed = False
        self._after = None
        self._interval_after = None
        self._awaiting_reports = False
        self._sources = None
        self._control_context = None
        self._learning_context = None
        self._seen_indoor_at = self._seen_outdoor_at = None
        self._last_now = None
        self._indoor_at = None
        self._last_indoor_value = None
        self._report_step = 0.0
        self._braking = False
        self._window = None
        self._response_window = None
        self._warm_streak = 0
        self._reset_confirmation()
        self._accepted_windows = self._rejected_windows = 0
        self._evidence = {
            "status": "insufficient_data",
            "confidence": "none",
            "verified": False,
            "last_window": None,
        }
        self._reason = "no_measurement"
        self._last_comparison = {}

    def start(self, now: datetime) -> None:
        """New lifecycle segment; only genuinely fitted parameters survive."""
        self._braking = False
        self._window = None
        self._response_window = None
        self._warm_streak = 0
        self._reset_confirmation()
        self._accepted_windows = self._rejected_windows = 0
        self._evidence = {
            "status": "insufficient_data",
            "confidence": "none",
            "verified": False,
            "last_window": None,
        }
        self._after = _utc(now)
        self._interval_after = self._after
        self._awaiting_reports = True
        self._sources = None
        self._control_context = None
        self._learning_context = None
        self._seen_indoor_at = self._seen_outdoor_at = None
        self._last_now = self._indoor_at = None
        self._last_indoor_value = None
        self._report_step = 0.0
        self.model.clear_history(samples=True)
        self.model.reset_response()
        self._reference_revision = None
        self._reference_hours = self._reference_residual = 0.
        self._response_confirmed = False
        self.diagnostics = ThermalDiagnostics()
        self._last_comparison = {}
        self._reason = "awaiting_post_start_reports"

    def _reading(self, state, now, previous_at=None):
        if state is None or isinstance(state.state, bool):
            return None
        attrs = state.attributes
        if attrs.get("restored") or attrs.get("unit_of_measurement") != "°C":
            return None
        at = _utc(getattr(state, "last_reported", None))
        # A previously observed report can remain an anchor during silence.
        # A newly encountered delayed report is still not a fresh observation.
        limit = (
            _MAX_QUALIFIED_INTERVAL_SECONDS
            if at is not None and at == previous_at
            else _MAX_SAMPLE_AGE_SECONDS
        )
        if at is None or not 0 <= (now - at).total_seconds() <= limit:
            return None
        if self._interval_after is not None and at < self._interval_after:
            return None
        if self._after is not None and at <= self._after:
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        return (
            TemperatureMeasurement(value, at)
            if math.isfinite(value) and -100 <= value <= 100
            else None
        )

    def _break(self, now, reason):
        self._response_window = None
        if self._operational:
            self.model.reset_response(samples=False)
            self._reference_hours = self._reference_residual = 0.
            self._response_confirmed = False
        # Fix the recovery boundary at the interruption, rather than moving it
        # on every callback while asynchronous sources are returning.
        if not self._awaiting_reports or reason in (
            "observation_gap",
            "braking_context_changed",
            "temperature_source_changed",
            "out_of_order_report",
        ):
            self._interval_after = _utc(now)
        self._awaiting_reports = True
        self.model.clear_history()
        self._braking = False
        self._window = None
        self._warm_streak = 0
        if reason != "outside_braking":
            self._reset_confirmation(self._baseline_k, self._baseline_delta)
        self._evidence = {
            "status": "insufficient_data",
            "confidence": "none",
            "verified": False,
            "last_window": None,
        }
        self.diagnostics.cancel_pending(now)
        self._reason = reason

    @staticmethod
    def _source_signature(state):
        """Track identity and validity transitions even during partial recovery."""
        if state is None:
            return None
        attrs = state.attributes
        members = attrs.get("source_temperatures_used")
        try:
            value = float(state.state)
            valid = (
                not isinstance(state.state, bool)
                and math.isfinite(value)
                and -100 <= value <= 100
                and not attrs.get("restored")
                and attrs.get("unit_of_measurement") == "°C"
                and _utc(getattr(state, "last_reported", None)) is not None
            )
        except (TypeError, ValueError):
            valid = False
        return (
            getattr(state, "entity_id", None),
            repr(attrs.get("selected_source")),
            tuple(sorted(members)) if isinstance(members, dict) else None,
            valid,
        )

    def _reset_confirmation(self, k=None, delta=None):
        self._baseline_k = k
        self._baseline_delta = delta
        self._baseline_matches = 0
        self._baseline_confirmed = False
        self._confirmation_expected = self._confirmation_residual = 0.0

    def _learn_window(self, indoor, outdoor, distinct, now, learning_context):
        """One qualified, non-overlapping window, never a physical heat claim."""
        k = self.model.k if self.model.is_valid else None
        delta = indoor.celsius - outdoor.celsius
        if (
            k != self._baseline_k
            or self._baseline_delta is None
            or abs(delta - self._baseline_delta) > 2.0
        ):
            # Confirmation only covers the observed temperature context.
            self._reset_confirmation(k, delta)
        if self._window is None:
            if distinct:
                self._window = (indoor, outdoor, k, outdoor.celsius, outdoor.celsius)
                self.model.clear_history()
                self.model.record_temp(indoor.measured_at, indoor.celsius)
            return
        origin, outside, frozen_k, low, high = self._window
        low, high = min(low, outdoor.celsius), max(high, outdoor.celsius)
        self._window = (origin, outside, frozen_k, low, high)
        seconds = (indoor.measured_at - origin.measured_at).total_seconds()
        if not distinct or seconds < 600:
            return
        hours = seconds / 3600
        observed = indoor.celsius - origin.celsius
        mean_outdoor = (outside.celsius + outdoor.celsius) / 2
        expected = (
            (mean_outdoor - origin.celsius) * (1 - math.exp(-frozen_k * hours))
            if frozen_k is not None
            else None
        )
        residual = observed - expected if expected is not None else None
        tolerance = max(
            0.10, 2 * self._report_step, 3 * (self.model.rate_noise or 0) * hours
        )
        stable = (
            high - low <= 1.0
            and frozen_k == k
            and seconds <= _MAX_QUALIFIED_INTERVAL_SECONDS
        )
        warm = stable and (residual if residual is not None else observed) > tolerance
        disturbed = (
            not stable or warm or (residual is not None and residual < -tolerance)
        )
        if not self._baseline_confirmed:
            if stable and residual is not None and abs(residual) <= tolerance:
                self._baseline_matches += 1
                self._confirmation_expected += expected
                self._confirmation_residual += residual
                self._baseline_confirmed = (
                    self._baseline_matches >= 3
                    and abs(self._confirmation_expected) >= 3 * tolerance
                    and abs(self._confirmation_residual) <= tolerance
                )
            else:
                self._reset_confirmation(k, delta)
        protected_warm = warm and self._baseline_confirmed
        if protected_warm:
            self._warm_streak += 1
        else:
            self._warm_streak = 0
        if not stable:
            status = "context_changed"
        elif frozen_k is not None and not self._baseline_confirmed:
            status = "unconfirmed_model"
        elif protected_warm:
            status = "suspected_extra_heat"
        elif warm:
            status = "warming_deviation"
        elif disturbed:
            status = "cooling_deviation"
        else:
            status = (
                "consistent_with_model" if frozen_k is not None else "bootstrap_cooling"
            )
        # A restored/unconfirmed incumbent may itself be wrong: retain its
        # errors, but let robust cooling batches establish a fresh baseline.
        admitted = stable and not protected_warm
        self._evidence = {
            "status": status,
            "confidence": "moderate"
            if self._warm_streak >= 2
            else "low"
            if self._warm_streak
            else "none",
            "verified": False,
            "baseline_confirmed": self._baseline_confirmed,
            "matching_windows": self._baseline_matches,
            "confirmation_delta_c": self._baseline_delta,
            "last_window": {
                "minutes": seconds / 60,
                "observed_change_c": observed,
                "expected_change_c": expected,
                "residual_c": residual,
                "threshold_c": tolerance,
                "smallest_report_step_c": self._report_step,
                "resolution_c": None,
                "reporting_threshold_c": None,
                "uncertainty_basis": "empirical_changes_not_sensor_specification",
                "outdoor_report_age_seconds": (
                    now - outdoor.measured_at
                ).total_seconds(),
                "outdoor_range_c": high - low,
                "k_per_hour": frozen_k,
                "started_at": origin.measured_at.isoformat(),
                "ended_at": indoor.measured_at.isoformat(),
                "indoor_start_c": origin.celsius,
                "indoor_end_c": indoor.celsius,
                "outdoor_start_c": outside.celsius,
                "outdoor_end_c": outdoor.celsius,
                "qualified_for_learning": admitted and observed < 0,
            },
        }
        self.model.clear_history()
        if admitted:
            # Faster cooling can repair an underestimated incumbent. Minority
            # cold spikes are filtered by the robust batch fit, not a lockout.
            self.model.record_temp(origin.measured_at, origin.celsius)
            self.model.record_temp(
                indoor.measured_at, indoor.celsius, qualified_interval=True
            )
            admitted_sample = self.model.collect_braking_sample(
                (origin.celsius + indoor.celsius) / 2,
                mean_outdoor,
                rate_uncertainty=2 * self._report_step / hours,
                reset_samples=learning_context != self._learning_context,
            )
            if admitted_sample:
                # A short ramp with no admitted window must not discard a
                # comparable batch before the adapter can fit it.
                self._learning_context = learning_context
            self._evidence["last_window"]["qualified_for_learning"] = admitted_sample
            self._accepted_windows += admitted_sample
            self._rejected_windows += not admitted_sample
        else:
            self._rejected_windows += 1
        self.model.clear_history()
        self.model.record_temp(indoor.measured_at, indoor.celsius)
        self._window = (indoor, outdoor, k, outdoor.celsius, outdoor.celsius)

    def observe(
        self, now, indoor_state, outdoor_state, *, braking, control_context=None,
        operation=None, forecast=None,
    ):
        if operation is not None:
            return self._observe_response(now,indoor_state,outdoor_state,operation,forecast)
        clock = _utc(now)
        if clock is None:
            self._break(self._last_now, "invalid_clock")
            return
        if self._last_now is not None and clock < self._last_now:
            self.start(clock)
            self._reason = "clock_reversal"
            return
        if (
            self._last_now is not None
            and (clock - self._last_now).total_seconds() > 300
        ):
            self._break(clock, "observation_gap")
        self._last_now = clock
        # A changed heat-demand context is not evidence of an external source.
        # Direct offline callers may supply an already qualified braking flag;
        # the HA adapter also supplies its existing mode and exact brake factor.
        if control_context is not None:
            valid = (
                isinstance(control_context, tuple)
                and len(control_context) == 2
                and control_context[0] in ("braking", "brake_hold")
                and type(control_context[1]) in (int, float)
                and math.isfinite(control_context[1])
                and 0 < control_context[1] <= 1
            )
            if not valid:
                braking, control_context = False, None
        context = (braking, control_context)
        # Context boundaries matter even while a temperature is unavailable.
        if self._control_context is not None and context != self._control_context:
            self._break(clock, "braking_context_changed")
        self._control_context = context
        sources = (
            self._source_signature(indoor_state),
            self._source_signature(outdoor_state),
        )
        if self._sources is not None and sources != self._sources:
            self._break(clock, "temperature_source_changed")
            self._report_step = 0.0
            self._last_indoor_value = None
        self._sources = sources
        indoor, outdoor = (
            self._reading(indoor_state, clock, self._seen_indoor_at),
            self._reading(outdoor_state, clock, self._seen_outdoor_at),
        )
        # Remember each valid source independently during asynchronous return;
        # retaining its original time does not make it a joint learning endpoint.
        for reading, seen in (
            (indoor, self._seen_indoor_at),
            (outdoor, self._seen_outdoor_at),
        ):
            if reading is not None and seen is not None and reading.measured_at < seen:
                self._break(clock, "out_of_order_report")
                return
        if indoor is not None:
            self._seen_indoor_at = indoor.measured_at
        if outdoor is not None:
            self._seen_outdoor_at = outdoor.measured_at
        if indoor is None or outdoor is None:
            self._break(clock, "unqualified_temperature_report")
            return
        self._awaiting_reports = False
        distinct = indoor.measured_at != self._indoor_at
        if distinct:
            if self._last_indoor_value is not None:
                step = abs(indoor.celsius - self._last_indoor_value)
                if step > 1e-6:
                    self._report_step = min(self._report_step or step, step)
            self._last_indoor_value = indoor.celsius
        self._indoor_at = indoor.measured_at
        if not braking:
            self._break(clock, "outside_braking")
        elif not self._braking:
            self.model.clear_history()
        self._braking = braking
        if braking:
            self._learn_window(
                indoor, outdoor, distinct, clock, (sources, control_context)
            )
        elif distinct:
            self.model.record_temp(indoor.measured_at, indoor.celsius)
        disturbed = self._evidence["status"] in (
            "context_changed",
            "suspected_extra_heat",
            "warming_deviation",
            "cooling_deviation",
        )
        self.diagnostics.observe(
            now=clock,
            indoor=indoor,
            outdoor=outdoor,
            k_per_hour=self.model.k if self.model.is_valid else None,
            model_fitted=self.model.is_valid,
            operating_mode="braking" if braking else "normal",
            issue_forecasts=braking and not disturbed,
        )
        if braking:
            self._reason = (
                self._evidence["status"]
                if disturbed
                else self.diagnostics.reason
                if self.model.is_valid
                else "model_not_fitted"
            )
        for comparison in self.diagnostics.last_comparisons:
            p = comparison.prediction
            self._last_comparison[str(p.horizon_minutes)] = {
                "predicted_change_c": round(
                    p.temperature_c - p.indoor_temperature_c, 4
                ),
                "actual_change_c": round(
                    comparison.actual_temperature_c - p.indoor_temperature_c, 4
                ),
                "error_c": round(comparison.error_c, 4),
                "k_per_hour": p.k_per_hour,
                "model_version": p.model_version,
                "observed_heat_evidence": self._evidence["status"],
                "actual_at": comparison.actual_at.isoformat(),
                "lateness_seconds": comparison.lateness_seconds,
            }

    def restore_response_identity(self, value):
        try:
            if not isinstance(value,(tuple,list)) or len(value)!=2:
                raise ValueError
            identity=[]
            for row in value:
                if (not isinstance(row,(tuple,list)) or len(row)!=3
                    or row[0] is not None and not isinstance(row[0],str)
                    or not isinstance(row[1],str)
                    or row[2] is not None and (not isinstance(row[2],(tuple,list)) or not all(isinstance(k,str) for k in row[2]))):
                    raise ValueError
                identity.append((row[0],row[1],tuple(row[2]) if row[2] is not None else None))
            self._response_identity=tuple(identity)
            return True
        except (TypeError,ValueError):
            self.model.reset_response(discard_fit=True)
            return False

    def _observe_response(self, now, indoor_state, outdoor_state, operation, forecast):
        """Same source/report qualification, explicit known effective control input."""
        self._operational = True
        clock=_utc(now)
        if clock is None:
            self._break(self._last_now,"invalid_clock")
            return
        if self._last_now is not None and clock<self._last_now:
            self.start(clock)
            self._reason="clock_reversal"
            return
        if self._last_now is not None and (clock-self._last_now).total_seconds()>300:
            self._break(clock,"observation_gap")
        self._last_now=clock
        sources=(self._source_signature(indoor_state),self._source_signature(outdoor_state))
        if self._sources is not None and sources!=self._sources:
            self._break(clock,"temperature_source_changed")
        if all(source is not None and source[3] for source in sources):
            identity=tuple(source[:3] for source in sources)
            if self._response_identity is not None and identity!=self._response_identity:
                self.model.reset_response(discard_fit=True)
            self._response_identity=identity
        self._sources=sources
        indoor=self._reading(indoor_state,clock,self._seen_indoor_at)
        outdoor=self._reading(outdoor_state,clock,self._seen_outdoor_at)
        if any(r and seen and r.measured_at<seen for r,seen in
               ((indoor,self._seen_indoor_at),(outdoor,self._seen_outdoor_at))):
            self._break(clock,"out_of_order_report")
            return
        if indoor: self._seen_indoor_at=indoor.measured_at
        if outdoor: self._seen_outdoor_at=outdoor.measured_at
        mode=operation.get('mode')
        fake=operation.get('fake_outdoor_temperature')
        factor=operation.get('brake_factor',0)
        valid=(mode in ('normal','braking','brake_hold','preheating','pre_braking','recovery','holiday')
               and type(fake) in (float,int) and math.isfinite(fake) and -20<=fake<=25
               and type(factor) in (float,int) and math.isfinite(factor) and 0<=factor<=1)
        context=(mode,factor)
        if self._control_context is not None and context!=self._control_context:
            self._break(clock,'control_context_changed')
        self._control_context=context
        self._operation=operation
        self._forecast=forecast
        if not valid or indoor is None or outdoor is None:
            self._break(clock,'unsafe_or_unknown_control' if not valid else 'unqualified_temperature_report')
            return
        self._awaiting_reports=False
        command=-fake
        distinct=indoor.measured_at!=self._indoor_at
        self._indoor_at=indoor.measured_at
        self._response_interval(indoor,outdoor,command,context,distinct,clock)
        fit=self.model.response
        disturbed=self._evidence['status'] in ('warming_deviation','cooling_deviation','suspected_extra_heat')
        self.diagnostics.observe(now=clock,indoor=indoor,outdoor=outdoor,
            k_per_hour=fit.coefficients[1] if fit else None,model_fitted=fit is not None,
            operating_mode=mode,issue_forecasts=fit is not None and not disturbed,
            response=fit,forecast=forecast,control_signal=command,pi_projection=operation.get("pi_projection"))
        self._reason=(self._evidence['status'] if disturbed else
                      self.diagnostics.reason if fit else self.model.response_reason)

    def _response_interval(self, indoor, outdoor, command, context, distinct, clock):
        window=self._response_window
        if window is None:
            # A delayed newly encountered value cannot establish command history
            # before this observer actually saw it. Forecast-age gating is separate.
            if distinct and (clock-indoor.measured_at).total_seconds()<=60:
                self._response_window=dict(indoor=indoor,outdoor=outdoor,command=command,
                    context=context,low=outdoor.celsius,high=outdoor.celsius,
                    command_low=command,command_high=command)
            return
        window['low']=min(window['low'],outdoor.celsius)
        window['high']=max(window['high'],outdoor.celsius)
        window['command_low']=min(window['command_low'],command)
        window['command_high']=max(window['command_high'],command)
        seconds=(indoor.measured_at-window['indoor'].measured_at).total_seconds()
        # Changes to requested heat break comparability even if source reports
        # stay constant. Do not call an ordinary command ramp an external source.
        if window['command_high']-window['command_low']>1 or context!=window['context']:
            self._response_window=None
            return
        if not distinct or seconds<600:
            return
        hours=seconds/3600
        mean_out=(outdoor.celsius+window['outdoor'].celsius)/2
        mean_in=(indoor.celsius+window['indoor'].celsius)/2
        mean_command=(command+window['command'])/2
        observed=indoor.celsius-window['indoor'].celsius
        fit=self.model.response
        if fit is None or fit.revision!=self._reference_revision:
            self._reference_revision=fit.revision if fit else None
            self._reference_hours=self._reference_residual=0.
            self._response_confirmed=False
            self.model.response_matches=0
        comparable=(seconds<=7200 and window['high']-window['low']<=1
                    and (clock-indoor.measured_at).total_seconds()<=60)
        expected=(fit.rate(mean_in,mean_out,mean_command)*hours
                  if fit and fit.supports(mean_in,mean_out,mean_command,context[0]) else None)
        residual=observed-expected if expected is not None else None
        tolerance=fit.uncertainty(hours) if fit else .1
        abnormal=comparable and residual is not None and abs(residual)>tolerance
        if comparable and residual is not None and not abnormal:
            self.model.response_matches+=1
            self._reference_hours+=hours
            self._reference_residual+=residual
            self._response_confirmed=(self.model.response_matches>=3 and self._reference_hours>=2
                and abs(self._reference_residual)<=fit.uncertainty(self._reference_hours))
            if self._response_confirmed:
                self.model.response_reason="qualified_effective_response"
        confirmed=self._response_confirmed
        protected=abnormal and confirmed
        positive=protected and residual>0
        status=('suspected_extra_heat' if positive and self._operation.get('external_heat_sources') is True
                else 'warming_deviation' if positive
                else 'cooling_deviation' if protected
                else 'consistent_with_model' if fit and confirmed
                else 'unconfirmed_model' if fit else 'bootstrap_response')
        admitted=comparable and not protected and self.model.collect_response_sample(
            mean_out-mean_in,mean_command,observed/hours,hours,context[0])
        self._accepted_windows+=bool(admitted)
        self._rejected_windows+=not admitted
        self._evidence=dict(status=status,verified=False,baseline_confirmed=confirmed,
            confidence='low' if status=='suspected_extra_heat' else 'none',
            extra_heat_enabled=self._operation.get('external_heat_sources') is True,
            last_window=dict(started_at=window['indoor'].measured_at.isoformat(),
                ended_at=indoor.measured_at.isoformat(),minutes=seconds/60,
                observed_change_c=observed,expected_change_c=expected,residual_c=residual,
                threshold_c=tolerance,qualified_for_learning=bool(admitted),
                response_revision=fit.revision if fit else None))
        if (admitted and self.model._response_since_fit>=20
            and (self.model.response is None or self._response_fit_at is None
                 or (clock-self._response_fit_at).total_seconds()>=86400)):
            self.model.fit_response()
            self._response_fit_at=clock
        self._response_window=None
        if comparable:
            self._response_window=dict(indoor=indoor,outdoor=outdoor,command=command,
                context=context,low=outdoor.celsius,high=outdoor.celsius,
                command_low=command,command_high=command)

    def attributes(self):
        quality = self.diagnostics.quality(operating_mode=self._operation.get("mode") if self._operational else None)
        evaluated = any(q.count for q in quality.values())
        result = {
            "status": "conditional"
            if self.model.is_valid and self.diagnostics.predictions
            else "insufficient_data",
            "reason": self._reason,
            "measurement_basis": "ha_report; physical_age_unverified",
            "model_basis": "empirical_reduced_heating_response",
            "heat_detection_basis": "residual_evidence_not_calibrated_probability",
            "heat_source_identifiable": False,
            "reliability": "conditional_observed_error"
            if evaluated
            else "insufficient_evaluations",
            "model_version": self.diagnostics.model_version,
            "heat_evidence": self._evidence,
            "learning_windows": {
                "accepted": self._accepted_windows,
                "rejected": self._rejected_windows,
            },
            "predicted_change_c": {
                str(p.horizon_minutes): round(
                    p.temperature_c - p.indoor_temperature_c, 4
                )
                for p in self.diagnostics.predictions
            },
            "forecast_issued_at": self.diagnostics.predictions[0].issued_at.isoformat()
            if self.diagnostics.predictions
            else None,
            "last_comparison": dict(self._last_comparison),
            "quality": {
                str(h): {
                    "count": q.count,
                    "missed_count": q.missed_count,
                    "mae_c": round(q.mae_c, 4) if q.mae_c is not None else None,
                    "persistence_mae_c": round(q.persistence_mae_c, 4)
                    if q.persistence_mae_c is not None
                    else None,
                    "mae_gain_c": round(q.mae_gain_c, 4)
                    if q.mae_gain_c is not None
                    else None,
                    "bias_c": round(q.bias_c, 4) if q.bias_c is not None else None,
                    "max_absolute_error_c": q.max_absolute_error_c,
                    "coverage": round(q.coverage, 4)
                    if q.coverage is not None
                    else None,
                }
                for h, q in quality.items()
            },
            "assumptions": [
                "heating_input_unknown",
                "residual_heat_unmodelled",
                "constant_outdoor_assumption",
            ],
        }

        if self._operational:
            fit=self.model.response
            result['status']='conditional' if fit and self.diagnostics.predictions else 'insufficient_data'
            result['model_basis']='empirical_effective_temperature_response'
            result['response']=dict(reason=self.model.response_reason,
                pending_samples=self.model.response_pending_samples,
                revision=fit.revision if fit else None,
                coefficients=tuple(round(v,6) for v in fit.coefficients) if fit else None,
                sample_count=fit.sample_count if fit else 0,
                comparable_matches=self.model.response_matches,
                confirmed=self._response_confirmed,
                uncertainty_basis='empirical_allowance_not_calibrated_probability')
            result['assumptions']=['held_command_assumption','heating_input_unknown',
                'empirical_association_not_physical_identification','physical_age_unverified']
            result['future_temperature_c']={str(p.horizon_minutes):round(p.temperature_c,1) for p in self.diagnostics.predictions}
            result['uncertainty_c']={str(p.horizon_minutes):round(p.uncertainty_c,2) for p in self.diagnostics.predictions if p.uncertainty_c is not None}
        return result
