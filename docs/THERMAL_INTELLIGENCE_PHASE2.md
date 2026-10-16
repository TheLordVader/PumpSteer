# Thermal Intelligence Phase 2: episodes and conditional reliability

Technical discussion report, updated 2026-10-16 after bounded review corrections
to `cc465f65d36ac6d4beb194f617553c552655eefd`. Intended local branch:
`fix/thermal-evaluation-review`. The sandbox makes `.git` read-only, so branch
creation and commit are blocked; the verified edits remain in the working tree
on `diagnostics/thermal-episodes-reliability`. Earlier reports and experiments
are preserved. This phase adds diagnostic interfaces and finite
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
naive/conflicting timestamps and reversed clocks break continuity. Authentic aware
clock evidence is retained separately from episode continuity, including when metadata
is invalid. Invalid timestamps do not erase that clock evidence. A reversal is rejected
and starts a new epoch; the next valid non-reversed observation can recover. Old-epoch
history cannot be looked up as current. Restart needs a new caller-supplied run
identity. Context lookup never extends beyond the last observation and cannot
resurrect evicted history.

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

The existing engine matches real indoor observations at target..target+120 seconds,
inclusive. The original nominal horizon, due time, point and point-error semantics
remain unchanged. Error is actual minus the original nominal point even for a late
accepted observation; it is not a new point forecast at the later time.

Assessment separately reports `interval_kind=exact_target` or `time_adjusted`,
`interval_at` and the assessed lower/upper values. A time-adjusted interval evaluates
the same analytic conditional model from the original indoor measurement time to the
**authentic actual measurement time**. It uses only the immutable original sources,
k, model identity and issuance-frozen assumptions. No later k, outdoor reading, actual
temperature or retrospective bounds enter that calculation. The observed temperature
is used only in the subsequent containment comparison. The original forecast is never
mutated. The caller must freeze assumptions at issuance; there is no reconstruction
or retrospective assumption-selection facility.

Assumptions must cover every source time through actual measurement time. Their
validity is never extended: `insufficient_time_coverage` means no interval assessment.
Samples before due time or more than 120 seconds after it are outside the contract.
Missing observations and invalid times are also explicit unassessable statuses. The
empirical residual bands were calibrated for nominal horizons only and remain
exact-target-only; no unsupported time-adjustment of calibration is invented.

Nominal horizon, issued/origin/due/actual times, actual lateness, interval type and
both nominal and assessed bounds are retained in benchmark records. Exact and adjusted
containment/width/count/lower-breach metrics are separate, with no pooled accuracy
percentage. Cooling understatement is `max(0,-error)`; interval lower breach uses the
independently calculated bound at the assessed time. Missing/restart-censored targets
never become zero errors. Counts and refusals remain visible even when a candidate
abstains. A perfect containment percentage on a small selected subset is not overall
forecast reliability.

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

### Fair comparisons: common cohort and all attempts

`paired_summary` now means the common **evaluated** cohort: all five methods issued
at the same origin and have the same accepted actual observation. `summary` retains
all attempts, including source refusals, candidate abstentions and censored outcomes.
The original holdout/shift cases and training are unchanged; no new outcomes were
used to tune parameters or calibration. The common holdout cohort is still 28 cases
per horizon, so the reproduced point metrics match the review findings.

Common holdout cohort (28 observations per row; degrees C):

| Method | Horizon min | MAE | Bias | Worst absolute | Worst cooling underestimate |
| --- | --- | --- | --- | --- | --- |
| Linear drop | 30 | 0.4391 | +0.3749 | 1.0894 | 0.3778 |
| Linear drop | 60 | 0.7756 | +0.6756 | 2.1313 | 0.7159 |
| Linear drop | 120 | 1.3986 | +1.2428 | 4.0687 | 1.2557 |
| Passive reference | 30 | 0.4363 | +0.3702 | 1.0802 | 0.3868 |
| Passive reference | 60 | 0.7657 | +0.6581 | 2.0950 | 0.7516 |
| Passive reference | 120 | 1.3598 | +1.1741 | 3.9257 | 1.3961 |
| Recent trend | 30 | 0.0486 | -0.0392 | 0.1550 | 0.1550 |
| Recent trend | 60 | 0.1718 | -0.1266 | 0.7131 | 0.7131 |
| Recent trend | 120 | 0.4393 | -0.3615 | 1.6796 | 1.6796 |
| Experimental empirical | 30 | 0.2093 | +0.0067 | 0.5636 | 0.5636 |
| Experimental empirical | 60 | 0.4023 | -0.0133 | 1.1629 | 1.1629 |
| Experimental empirical | 120 | 0.7410 | -0.0259 | 2.2357 | 1.8916 |
| Experimental conditional | 30 | 0.4363 | +0.3702 | 1.0802 | 0.3868 |
| Experimental conditional | 60 | 0.7657 | +0.6581 | 2.0950 | 0.7516 |
| Experimental conditional | 120 | 1.3598 | +1.1741 | 3.9257 | 1.3961 |

Recent trend has lower MAE here, but its worst 120-minute cooling underestimate is
1.6796 C versus 1.3961 C for conditional/passive. Conditional point equals passive
because the declared lower heat is zero. There is no evidence of point superiority
for the conditional model and no reason to adopt empirical correction for control.

All holdout attempts (44 per row; observed includes outcomes available even when the
candidate rejected; evaluated requires both an accepted forecast and an outcome):

| Method | Horizon | Issued | Observed | Evaluated | MAE | Bias | Worst absolute | Worst cooling underestimate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Linear drop | 30 | 37 | 31 | 31 | 0.4399 | +0.3819 | 1.0894 | 0.3778 |
| Linear drop | 60 | 37 | 31 | 31 | 0.7984 | +0.7081 | 2.1313 | 0.7159 |
| Linear drop | 120 | 37 | 31 | 31 | 1.4485 | +1.3078 | 4.0687 | 1.2557 |
| Passive reference | 30 | 37 | 31 | 31 | 0.4368 | +0.3771 | 1.0802 | 0.3868 |
| Passive reference | 60 | 37 | 31 | 31 | 0.7860 | +0.6888 | 2.0950 | 0.7516 |
| Passive reference | 120 | 37 | 31 | 31 | 1.3998 | +1.2320 | 3.9257 | 1.3961 |
| Recent trend | 30 | 37 | 31 | 31 | 0.0480 | -0.0396 | 0.1550 | 0.1550 |
| Recent trend | 60 | 37 | 31 | 31 | 0.1686 | -0.1278 | 0.7131 | 0.7131 |
| Recent trend | 120 | 37 | 31 | 31 | 0.4342 | -0.3639 | 1.6796 | 1.6796 |
| Experimental empirical | 30 | 36 | 31 | 30 | 0.1963 | +0.0073 | 0.5636 | 0.5636 |
| Experimental empirical | 60 | 36 | 31 | 30 | 0.3997 | +0.0119 | 1.1629 | 1.1629 |
| Experimental empirical | 120 | 36 | 31 | 30 | 0.7396 | +0.0214 | 2.2357 | 1.8916 |
| Experimental conditional | 30 | 33 | 31 | 28 | 0.4363 | +0.3702 | 1.0802 | 0.3868 |
| Experimental conditional | 60 | 33 | 31 | 28 | 0.7657 | +0.6581 | 2.0950 | 0.7516 |
| Experimental conditional | 120 | 33 | 31 | 28 | 1.3598 | +1.1741 | 3.9257 | 1.3961 |

Holdout source refusals per horizon are one missing indoor and six stale indoor.
Conditional additionally refuses three unknown-heat and one unreliable episode;
empirical additionally refuses one uncalibrated regime. Conditional has five missing
outcomes; other methods have six. These are missed 30-minute targets or restart-censored
60/120-minute targets. They are counted, never zero-filled. All-attempt shift counts,
all point metrics and refusal dictionaries are retained in `shadow-results.json`.

### Conditional intervals: exact and adjusted assessments stay separate

These are all-attempt results. Width is averaged over the observations assessed at
that specific time/type, **not** all nominal issued intervals as in the old table.
This deliberate denominator change explains different width means. Point errors and
the nominal issued widths remain unchanged and are also retained in the JSON.

| Dataset | Horizon | Issued / attempted | Matched | Exact n / containment / width C | Adjusted n / containment / width C |
| --- | --- | --- | --- | --- | --- |
| holdout | 30 | 33/44 | 28 | 22 / 100.00% / 1.7622 | 6 / 100.00% / 1.8228 |
| holdout | 60 | 33/44 | 28 | 22 / 100.00% / 3.2837 | 6 / 100.00% / 3.4927 |
| holdout | 120 | 33/44 | 28 | 22 / 100.00% / 6.2716 | 0 / unavailable / unavailable |
| shift | 30 | 16/21 | 14 | 11 / 36.36% / 2.2763 | 3 / 33.33% / 2.3551 |
| shift | 60 | 16/21 | 13 | 10 / 60.00% / 4.3952 | 3 / 33.33% / 4.5420 |
| shift | 120 | 16/21 | 13 | 10 / 60.00% / 8.3951 | 0 / unavailable / unavailable |

At 120 minutes, six late holdout and three late shift observations have insufficient
time coverage: the original assumptions end at minute 120 while actuals arrive at 122.
Their point errors survive and no interval is scored. At 30/60 minutes the same
frozen assumptions cover the late observation, so adjusted assessment is now useful.
The exact-target shift lower breaches remain 0.6406/0.8696/1.4681 C; adjusted breaches
are 0.1559/0.3006 C at 30/60. Empirical bands are exact-only: common-cohort containment
is 72.73% at each holdout horizon and 9.09%/30%/30% under shift; full-cohort counts,
widths and breaches are in the JSON. Point-only methods have no interval.

### New post-origin surprises, separate from the previous series

Three deterministic reference/disturbance pairs use the same ID, noise, prehistory,
origin measurements, original forecast objects by value, assumptions and calibration.
They are identical through minute 40; differences affect the trajectory thereafter.
Ground truth alone reads future disturbance metadata. At minute 40: total heat is
removed, supplied heat increases by 2 C/h, or outdoor cooling gains an extra -5 C/h
slope. The latter two can exceed the unchanged prior bounds. No priors or training
were retuned. These are three examples, not a new accuracy population; the three
reference partners are paired controls, not six independent episodes.

Signed 120-minute point errors, actual minus nominal point (C; one disturbed case
per row). At 30 minutes every pair still has identical outcomes. All 30/60/120-minute
results and paired reference records are retained in `surprise_summary`/`records`.

| Surprise | Linear | Passive | Trend | Empirical | Conditional | Conditional width C / contained |
| --- | --- | --- | --- | --- | --- | --- |
| heat_loss | +0.6023 | +0.4614 | -1.3728 | -1.0038 | +0.4614 | 8.4031 / yes |
| heat_gain | +4.0424 | +3.9031 | +2.3570 | +2.4378 | +3.9031 | 8.3574 / yes |
| cold_front | +1.2725 | +1.1328 | -0.5215 | -0.3325 | +1.1328 | 8.3687 / yes |

Trend underestimates cooling by 1.3728 C after heat removal and 0.5215 C after the
cold front. Empirical underestimates by 1.0038/0.3325 C, respectively, and misses its
upper band after heat gain. Conditional/passive overpredict cooling in these examples;
conditional containment uses very broad 8.36–8.40 C intervals. Even containment outside
a prior in these selected trajectories does not validate the violated assumption.

## Distribution-shift failures and remaining limitations

Shift scenarios introduce a cold front beyond the outdoor envelope, loss coefficient
.18 outside the k prior, hidden heat removal, or additional gains beyond the heat cap.
Exact-target conditional containment falls to 36.36%/60%/60%, with up to 1.4681 C
lower-bound breach.
An estimated envelope cannot detect all future violations at issuance. Removing heat
within the declared 0..1.8 range is different from violating that range. Individual
case records retain shift type so these causes are not hidden in an average.

Even the holdout 100% exact containment is only 22 exact-time targets per horizon and
requires mean assessed width up to 6.2716 C (nominal issued mean 6.3930 C). That may
be too broad to support any useful decision. It is
neither real-world accuracy nor proof that future homes obey the priors. No statistical
confidence claim is justified by this deterministic finite experiment. In particular,
underestimated cooling remains possible outside the assumptions. No active control
adaptation is warranted by these results.

## Verification and next phase

The review correction first reproduced F3 against the unmodified base runtime
(four failing regression cases). F2 tests then failed on unsupported adjusted
assessment and now cover exact/1/59/120/121-second boundaries, frozen validity,
model revisions, missing outdoor input, object immutability and outcome-independent
bounds. Four hours of minute samples with a one-second issue delay produce 17
point comparisons and 17 adjusted intervals when frozen validity covers the samples.
Legacy exact-only expectations changed only where this explicit contract replaces
them; empirical bands still abstain off target. Existing DST, restart, revision,
sensor hygiene and resource-bound regressions remain green.

Final verification used existing system CPython 3.14.8, pytest 8.4.2 and the existing
temporary dependency/tool directories: **468 passed in 0.99 s** (base: 449 passed).
Ruff 0.15.8 lint and format (53 files), Flake8 7.4.1 fatal/import checks, version
validation (2.2.3) and diff whitespace checks pass. No new environment or repeated
four-version matrix was built. A separate read-only reviewer reported no blocking
code findings and independently ran 197 thermal tests. Detailed commands, red/green
logs, simulator records and resource probe are in `.local-audit/20261016-review/`.

The bounded offline resource probe includes both modified modules, nominal issuance,
adjusted assessment, insufficient-coverage refusal, model changes, missing outdoor
input and metadata followed by clock reversal/recovery. Twenty thousand observations
took 0.4307 s (21.54 microseconds/step) without allocation tracing; 1,777 adjusted
assessments and 214 coverage refusals were exercised. Retention ended at 7 pending,
192 resolved records, 32 closed episodes and 7 forecast joins. A separate traced
20,000-step pass had 126,696 live / 144,920 peak traced bytes and 876 bytes growth
from step 5,000 to 20,000. Process peak RSS was 23,204 KiB, including interpreter,
stubs and both passes; that is not the module's incremental memory. These are
standalone measurements, not evidence of Home Assistant load.

Local Git branch creation failed once because `.git/index.lock` cannot be created
on the read-only filesystem. No Git-write retry, restriction bypass or publication
was attempted. Code and audit evidence are preserved locally; a clean correction
branch/commit remains the concrete delivery blocker.

A next phase should specify trustworthy observation episodes and data-quality checks
before any adapter or adaptive controller is considered. Needed real-world evidence:
authentic sample/transition times, source precision/location, actual requested mode/
brake factor, compressor/circulation status, optional supply/return/radiator readings,
and thermal-energy or flow/delta measurements if already available. Electrical power
alone needs heat-capacity/COP qualification; indoor/outdoor temperatures cannot uniquely
separate envelope loss, unknown heating, residual storage and other gains.

Keep a frozen reference, log compact episode-level diagnostics only after a separate
adapter design, validate chronological whole episodes and refuse unsupported regimes.
Obtain empirically defensible priors before making reliability claims; the bounded
time-adjusted assessment above is not a generic time-window envelope. Keep PI primary
and comfort protection authoritative.
Do not add persistence or a multi-state radiator model without evidence constraining
its additional parameters. No hardware purchase is required to continue diagnosis;
if extra signals are absent, preserve unknowns and abstain rather than invent them.

No upstream writes, PR publication, deployment, live HA/PVE/MQTT/Ohmigo/Relay Guard
interaction or unrelated-project changes were performed in this phase.
