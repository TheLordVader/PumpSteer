# Thermal Intelligence Phase 2: episodes and conditional reliability

Technical discussion report, 2026-10-16. Local branch
`diagnostics/thermal-episodes-reliability`, based on
`0a5241b23f8fd62a40315e36ded4ae969ea79f79`. Earlier branches, reports and
experiments are preserved. This phase adds diagnostic interfaces and finite
synthetic validation; it does not activate adaptive heat control.

## Scope and architecture

- `thermal_episodes.py`: immutable observed episode context, a current snapshot
  and at most 32 closed episodes, explicit run/clock-epoch identity.
- `thermal_reliability.py`: pure conditional heat-balance bounds and assessment.
  It owns no history, worker, registry, I/O or scheduling.
- `thermal_intelligence.py`: only its accepted diagnostic mode list is extended.
  Existing forecast creation/evaluation and bounded retention are unchanged.
- `tests/thermal_shadow_validation.py`: finite offline scenarios, calibration and
  holdout metrics, with one original forecast origin per independent episode.

There is no production caller. Sensor/control/ThermalModel/forecast/Ohmigo/Relay
Guard files, PI gains, comfort protection, pricing and hardware paths are unchanged.
No HA adapter/entity, Recorder write, persistence, dependency, configuration option,
cloud integration or deployment is introduced. Earlier experiments remain unchanged.

## Episode contract

Supply authentic aware timestamps for observed operating context. Classify normal
PI operation, preheating, pre-braking, price braking, brake hold, precooling and safe
mode using actual source names. Normal with explicit `brake_releasing=True` is labelled
release/recovery context. A brake-to-normal transition alone does not establish
recovery duration or actual residual heat. Recovery ends when that supplied flag ends;
no physical decay duration is invented. Unknown source states remain unknown.
Holiday/summer/error modes are outside this classification and must not be relabelled
normal by a future adapter.

Heating contribution metadata is explicitly `known`, `estimated` or `unknown`.
It is supplied evidence, never inferred from mode, virtual outdoor output or weather.
A caller's known/estimated label does not itself verify delivered thermal power.
The numeric future heat bounds require separate source/provenance and remain assumptions.
Delivered watts cannot become degrees/hour without effective thermal-capacity evidence.

Mode/evidence changes and explicit sensor interruptions start distinct episodes.
A context gap longer than five minutes creates an unobserved interruption; this is
not a requirement that an outdoor sensor report every five minutes. Invalid metadata,
naive/conflicting timestamps and reversed clocks break continuity. Clock reversal
starts a new epoch; restart needs a new caller-supplied run identity. Context lookup
never extends beyond the last observation and cannot resurrect evicted history.

Episode endpoints are conservative: a long gap begins at the last trusted context
time. A later lookup at that exact boundary can return interruption, although the
original immutable source snapshot remains retained. This may exclude a historical
boundary from same-episode training; it does not erase its measured temperature or
change forecast error. It is a documented minor limitation, not a reconstruction of
the exact physical outage time.

`episode_exposure` distinguishes observed same episode, transitioned, and unknown
context independently of candidate admission. Different runs/epochs stay unknown.
Observed same episode is not proof of constant heat input between context samples.
Model coefficient updates do not themselves define physical episode boundaries.

## Conditional physics and rejection

The balance is `dTin/dt=-k*(Tin-Tout)+q`, with time in hours. q is total effective
nonnegative supplied, residual, solar and other heat in C/hour. It is not electrical
power, a brake factor or a function inferred from simulated outdoor temperature.
Active net cooling is outside this model; negative heat bounds are rejected.

`ForecastAssumptions` requires immutable k/outdoor/heat ranges, indoor measurement
error bound, validity times and bounded source text. Ranges must cover the entire
source-to-target interval, including source age. Source k/outdoor values must lie
inside them. No default heat, weather envelope or uncertainty is invented. Unknown
heat, unsafe/interrupted/unknown episode, stale episode provenance, invalid numeric
bounds or insufficient time coverage reject the episode forecast explicitly.
Raw legacy passive references remain diagnostic references with their old warnings;
they are not promoted to trusted forecasts when the episode candidate abstains.

For a constant input, the exact response is
`Tin+(Tout-Tin)*(1-exp(-k*t))+q*(1-exp(-k*t))/k`.
The point uses the original nominal k/outdoor and the **lower assumed heat**. Using
an unobserved midpoint heating power increased cooling underestimation in the first
experiment, so that policy was rejected with a regression test. The point is not a
physical lower temperature bound: k/weather can still produce faster cooling.

The lower comparison ODE uses minimum outdoor/heat and maximum k while Tin is above
outdoor, minimum k while below. The upper comparison reverses those k choices with
maximum outdoor/heat. With nonnegative heat, an extremal path crosses its outdoor
level upward at most once, so the crossing and both segments have exact solutions.
This handles shoulder-season warming correctly. Bounds also allow time-varying
inputs inside the declared ranges; the simulator does not supply future values to
choose them. Indoor error is included at origin and target. Outdoor measurement
error must already be covered by the supplied outdoor range.

Input sanity guards are k in (0.005,0.5)/hour, outdoor [-100,100] C, nonnegative
heat up to 10 C/hour, indoor error 0..5 C. These are diagnostic acceptance guards,
not empirically established ranges for all homes. The interval label is
`conditional_assumption_bounds`, never a probability, certified comfort margin or
validated confidence score. Width must be reported alongside coverage.

## Immutable evaluation evidence and timing

An `EpisodeForecast` retains the original reference object (including revision,
source values/times), episode snapshot, UTC-normalized frozen assumptions, point,
lower/upper values and model-family label. Later k updates cannot modify it.
Assessment requires that same in-memory original object, so structurally identical
references from different engine instances cannot be silently joined. Serialization
or cross-restart reconstruction is deliberately unsupported.

The existing engine matches real indoor observations at target..target+120 seconds.
Point error and cooling understatement retain that accepted observation and lateness.
The interval describes the **exact target**, so an off-target actual has unavailable
interval coverage/lower-breach and `interval_status=off_target_time`. Assumptions are
not silently extended, and the frozen prediction is not revised after observing data.
A future time-window envelope would need a separate design covering the whole window.

Error is actual minus point. Cooling underestimation is `max(0,-error)`, and lower
breach is `max(0,lower-actual)`. Missing/restart-censored targets never become zero
errors. Report attempts, admission, matched/issued coverage, exact-time interval
count, interval timing coverage, containment and width separately. A perfect containment
percentage on a small selected subset is not overall forecast reliability.

## Independent synthetic validation

Forty-two chronological training episodes cover seven operating contexts, three
building coefficients (.025/.055/.09 per hour) and winter/shoulder weather. Freeze
21 empirical mean/min/max residual cells (six independent episodes per mode/horizon)
before 44 later holdout episodes and 21 later shift episodes. No minute samples or
three correlated horizons are counted as independent episodes. No holdout is retuned.
The empirical baseline uses fixed nominal k=.05 and is not transferable to new k.

Ground truth uses independent RK4 integration, changing outdoor temperature,
nonnegative independently specified heating, decaying radiator heat and bounded
deterministic sensor noise. Its effective C/hour inputs are synthetic assumptions,
not measured real-house values. New intervals use fixed a priori k=[.02,.1], outdoor
origin +/- 3 C, heat=[0,1.8] C/hour and indoor error 0.02 C. These ranges are not learned or
validated for real installations. Reference, existing linear helper, recent trend,
frozen empirical bands and conditional heat balance receive the same origin data.

Holdout cases include irregular 31/62/122-minute target observations, absent/stale
sensors, six-minute outdoor reports, repeated k revisions, frequent mode transitions,
safe mode and restart. One original origin is evaluated per case; later issued
forecasts never add correlated training samples. All refusals and censored outcomes
remain visible. Full records retain building/season/context/exposure/method/revision.

### Point errors on a shared admitted cohort

| Dataset | Horizon | MAE C | Bias C | Worst absolute C | Worst cooling underestimate C |
| --- | --- | --- | --- | --- | --- |
| holdout | 30 | 0.4363 | +0.3702 | 1.0802 | 0.3868 |
| holdout | 60 | 0.7657 | +0.6581 | 2.0950 | 0.7516 |
| holdout | 120 | 1.3598 | +1.1741 | 3.9257 | 1.3961 |
| shift | 30 | 1.1475 | +0.3221 | 2.0372 | 1.5088 |
| shift | 60 | 1.9328 | +0.7735 | 3.9797 | 2.5112 |
| shift | 120 | 3.6424 | +1.4384 | 7.6954 | 4.4945 |

The conditional point equals the previous passive reference on this shared cohort
because the declared lower heat is zero. No point-accuracy improvement is claimed.
These target-tolerance point errors include the disclosed late-observation offsets.

### Conditional interval results and denominator visibility

| Dataset | Horizon | Matched / issued | Exact-time intervals / matched | Interval coverage | Mean width C | Worst lower breach C |
| --- | --- | --- | --- | --- | --- | --- |
| holdout | 30 | 28/33 | 22/28 | 100.00% | 1.7479 | 0.0000 |
| holdout | 60 | 28/33 | 22/28 | 100.00% | 3.3471 | 0.0000 |
| holdout | 120 | 28/33 | 22/28 | 100.00% | 6.3930 | 0.0000 |
| shift | 30 | 14/16 | 11/14 | 36.36% | 2.2776 | 0.6406 |
| shift | 60 | 13/16 | 10/13 | 60.00% | 4.3928 | 0.8696 |
| shift | 120 | 13/16 | 10/13 | 60.00% | 8.3906 | 1.4681 |

Holdout admission was 33/44 (75%) per horizon; shift admission 16/21 (76.19%).
Holdout interval timing coverage was 22/28 matched observations (78.57%); shift was
11/14 (78.57%) at 30 minutes and 10/13 (76.92%) at 60/120 minutes. Late observations are retained
in point diagnostics but not presented as exact-time interval validations. Widths
are means over issued intervals, including those with missing or late outcomes.

### Comparison with the previous prototype and empirical calibration

| Dataset | Horizon | Previous MAE C | Empirical MAE C | Empirical interval coverage | Conditional interval coverage |
| --- | --- | --- | --- | --- | --- |
| holdout | 30 | 0.4363 | 0.2093 | 72.73% | 100.00% |
| holdout | 60 | 0.7657 | 0.4023 | 72.73% | 100.00% |
| holdout | 120 | 1.3598 | 0.7410 | 72.73% | 100.00% |
| shift | 30 | 1.1475 | 1.1281 | 9.09% | 36.36% |
| shift | 60 | 1.9328 | 1.8117 | 30.00% | 60.00% |
| shift | 120 | 3.6424 | 3.4741 | 30.00% | 60.00% |

The empirical candidate improves average error in some synthetic conditions but
can worsen cooling underestimation. At 120 minutes its worst cooling understatement
was 1.8916 C on holdout versus 1.3961 C for the reference, and 5.8796 C under shift versus
4.4945 C. Lower MAE does not justify adopting that correction for heating decisions.

## Distribution-shift failures and remaining limitations

Shift scenarios introduce a cold front beyond the outdoor envelope, loss coefficient
.18 outside the k prior, hidden heat removal, or additional gains beyond the heat cap.
Conditional containment falls to 36.36%/60%/60%, with up to 1.4681 C lower-bound breach.
An estimated envelope cannot detect all future violations at issuance. Removing heat
within the declared 0..1.8 range is different from violating that range. Individual
case records retain shift type so these causes are not hidden in an average.

Even the holdout 100% containment is only 22 exact-time targets per horizon and requires
mean width up to 6.3930 C. That may be too broad to support any useful decision. It is
neither real-world accuracy nor proof that future homes obey the priors. No statistical
confidence claim is justified by this deterministic finite experiment. In particular,
underestimated cooling remains possible outside the assumptions. No active control
adaptation is warranted by these results.

## Verification and next phase

TDD reproduced missing mode support, invalid-context continuity, cross-instance joins,
late interval scoring, unjustified midpoint heat and benchmark accounting losses.
Old tests/experiments were not weakened or rewritten. Focused regressions include
both DST changes, restart/run/epoch boundaries, memory bounds, uncertainty rejection,
warm-outdoor crossing and time-varying inputs. Full final test/static results and
independent review are saved in the local audit beside the English completion report.

A next phase should specify trustworthy observation episodes and data-quality checks
before any adapter or adaptive controller is considered. Needed real-world evidence:
authentic sample/transition times, source precision/location, actual requested mode/
brake factor, compressor/circulation status, optional supply/return/radiator readings,
and thermal-energy or flow/delta measurements if already available. Electrical power
alone needs heat-capacity/COP qualification; indoor/outdoor temperatures cannot uniquely
separate envelope loss, unknown heating, residual storage and other gains.

Keep a frozen reference, log compact episode-level diagnostics only after a separate
adapter design, validate chronological whole episodes and refuse unsupported regimes.
Investigate a time-window envelope and tighter empirically defensible priors before
making reliability claims. Keep PI primary and comfort protection authoritative.
Do not add persistence or a multi-state radiator model without evidence constraining
its additional parameters. No hardware purchase is required to continue diagnosis;
if extra signals are absent, preserve unknowns and abstain rather than invent them.

No upstream writes, PR publication, deployment, live HA/PVE/MQTT/Ohmigo/Relay Guard
interaction or unrelated-project changes were performed in this phase.
