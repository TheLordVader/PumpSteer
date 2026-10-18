# Thermal Intelligence: passive braking diagnostics

This candidate observes the existing controller. It does not change PI tuning,
price categories, brake depth, comfort protection, forecasts used by control, or
Ohmigo commands. There are no new entities, services, cloud calls,
polling loops or storage components. It is independent of PRs #279 and #280.

## What is learned and displayed

The existing `ThermalModel` estimates a cooling coefficient `k` using its existing
least-squares fit after completed brake phases. At least 20 qualifying cooling
samples are needed. Its fallback coefficient is not a learned result and never
enables these forecasts. Samples are now collected only from distinct qualified
indoor reports during actual `braking`/`brake_hold` with a positive brake factor.

The existing PumpSteer output sensor gains a `thermal_diagnostics` attribute.
No additional entity ID is introduced. During braking, a fitted model produces
conditional temperature changes for 30, 60 and 120 minutes:

`T(t) = T_out + (T_in - T_out) * exp(-k * t_hours)`

This is the exact solution of the existing model's differential equation; its
older linear prediction helpers remain unchanged. Forecasts are issued at most
once per 30 minutes on the existing sensor update path. There is no extra timer.
The first distinct qualifying indoor report at or after each target, within
120 seconds, supplies the actual outcome. The original coefficient, revision and
starting temperature are frozen with each forecast. There is no interpolation.
Leaving braking, invalid input or a gap interrupts pending comparisons.

Useful fields in `thermal_diagnostics`:

| Field | Meaning |
| --- | --- |
| `status`, `reason` | Conditional forecast or explicit insufficient-data reason |
| `predicted_change_c` | Changes from the observed origin, keyed by horizon |
| `last_comparison` | Last resolved predicted/actual change, signed error (actual minus predicted), coefficient, revision and timing per horizon |
| `quality` | Valid count, missed count, MAE, signed bias and coverage per horizon for the current coefficient revision |
| `reliability` | `insufficient_evaluations` or `conditional_observed_error`; never a probability or a safety guarantee |
| `measurement_basis`, `assumptions` | Data provenance and unmodelled influences |

Absent forecasts are empty; unavailable error statistics are `null`, not zero.
Counts describe retained evidence, not lifetime totals. Comparisons from different
coefficients are not pooled. The last comparison retains its original revision,
even if a subsequent fit changes the coefficient. Longer horizons need an
uninterrupted qualified brake period; short brakes may never produce an outcome.

## Measurement and lifecycle contract

`last_changed` is not a freshness test: identical temperatures can be legitimate.
`last_updated` tracks HA state/attribute changes. `last_reported` tracks HA
reception, including identical reports. This adapter uses distinct, timezone-aware
`last_reported` values after its lifecycle boundary, finite Celsius values,
monotonic report order and no explicit `restored` flag. Reports older than 15
minutes or from the future are rejected. Home Assistant 2024.4 or later is required
for `last_reported`. The startup boundary uses the actual aware startup event time,
with current time as fallback for missing, invalid, naive or future event time. An unchanged value with a new qualifying
report is accepted; repeated polling of the same report does not add samples.

**HA reception is not proof of a new physical measurement.** An integration or
template can re-report a cached value without a restore marker. These inputs do
not provide a verified hardware measurement timestamp, so this cannot be detected
generically. Results explicitly say `physical_age_unverified`. The 15-minute
report/rate-history limit tolerates ordinary sparse (for example ten-minute)
reporting. A gap in PumpSteer observations longer than five minutes still breaks
continuity. These bounds are not a hardware watchdog or claim about
physical freshness. The existing rate estimator also requires three minutes of
history and rejects implausible rates and small indoor/outdoor differences.

The existing sensor's HA `ExtraStoredData` preserves only genuinely fitted `k`
(full precision), validity and fit sample count, including during safe mode.
Legacy attributes require explicit learned/valid status and an integer fit count
of at least 20. Older code could incorrectly mark its fallback valid with count
zero, or lose the count of a genuine fit; such ambiguous legacy state must be
learned again. Legacy attributes cannot override explicitly invalid new-format
extra data. No additional Store or
Recorder-based recovery is used.

Startup, integration reload, template reload, clock reversal and configured
temperature-source changes start a new observation segment. Valid learned
parameters survive; pending raw training points, forecasts and evaluation scores
are not restored as new measurements. Old restored diagnostic attributes are
discarded. Post-boundary reports are required. An outage or invalid input breaks
rate continuity and cancels pending forecasts, without inventing outcomes. Valid
raw cooling samples can accumulate across short completed phases, as in the
existing learner; rate estimation never bridges their gaps. UTC elapsed time
handles DST independently of local wall-clock labels.

## Physical interpretation and cost

This is an empirical **net cooling** model, not a measured building heat-loss
coefficient. Braking does not prove zero heating input. Residual radiator heat,
uncertain heat-pump input, sun, occupancy and changing outdoor temperature can
alter both fitted `k` and prediction error. Radiator heat often delays cooling;
the observer cannot separate that from insulation. No heating input or residual
heat state is invented. The displayed errors are conditional observations, not
evidence that a particular brake depth is safe. No automatic adjustment follows.

Memory is bounded: 10 rate-history points, 240 pending training samples, at most
24 pending forecasts and 64 resolved records per horizon. Each forecast keeps its
original metadata. There is no background worker or additional Recorder row
stream; the existing sensor row gains attributes. A local synthetic 10,000-update
measurement took about 52.7 microseconds per observer update and serialized a
representative diagnostic attribute to 1,278 bytes. These are test measurements,
not production CPU, database growth or SSD measurements. Timestamp attributes can
increase bytes in existing Recorder rows but do not create additional polling.

## Optional external heat marker

In PumpSteer options, `external_heat_entity` selects an existing `binary_sensor`,
`input_boolean` or `switch`. No entity is selected by default; existing behavior
is preserved. `on` means active external heating (for example a fire or air-source
heat pump); `off` allows qualified observations. Other states, a missing entity,
or an explicitly restored marker conservatively pause observation and learning.
A temporarily unavailable marker can remain selected.

The existing update path reads this marker before and after control calculation.
Active or unknown heat discards unfinished training samples, rate history and
pending comparisons. A genuinely fitted coefficient and completed comparisons
survive. After the first observed `off`, both temperature sources must report
again before a new series can start. Changing/removing the selected entity also
starts a new lifecycle segment. No new subscription, polling loop, integration,
automatic fire detection or control dependency is added.

The marker affects only diagnostics. PI, prices, brake depth, comfort protection
and Ohmigo outputs are unchanged. A short on/off pulse entirely between updates
can be missed. Residual radiator or stove heat after `off`, sunlight and unmarked
external heat remain model limitations; `off` is not proof of zero heat input.

## Validation and delivery

See [the validation receipt](thermal-validation-20261018/README.md) for commands,
red/green evidence, native Core probes and environment limitations. No production
system was contacted. The isolated inputs and receiver are synthetic; Core
startup, reloads, SQLite and restore files are real.

Upstream review should precede any installation. Normal installation would use
the official integration update followed by the supported HA lifecycle action,
under separate deployment approval. There is no configuration migration. The external heat marker is optional;
leaving it unselected preserves existing behavior. Rollback restores the prior integration directory/version and
uses the corresponding supported reload/restart, also separately approved.
Older code ignores the added thermal extra-data field. The existing controller,
source entity selections and hardware configuration remain untouched.
