# Thermal Intelligence diagnostic foundation

This pure observation API creates conditional passive references and checks them
against later real indoor measurements. It has no integration caller, HA adapter,
entity, Recorder write, worker, persistence, service or control connection. PI,
comfort, price decisions, preheat, braking, ThermalOutlook and outputs are unchanged.

## Physics and limitations

For constant outdoor temperature and zero heat input, use the exact solution of
`dTin/dt = -k*(Tin-Tout)`: `Tout+(Tin-Tout)*exp(-k*t)`, with t in hours.
The existing ThermalModel linear helper and fitted parameters remain unchanged.

Every forecast explicitly warns that heating input is unknown, residual radiator
heat is unmodelled, and outdoor temperature is held constant. Supplied non-fitted
k adds an unvalidated-coefficient warning. Fitted k is not a reliability guarantee:
braking can still supply heat, radiators retain energy, and solar/occupant gains
are unknown. Forecasts are neither comfort guarantees nor control inputs.

## Independent issuance and evaluation

`ThermalDiagnostics.observe` accepts aware `now`, separate immutable indoor and
outdoor values with physical observation times, supplied k/fitted metadata and an
optional bounded operating-mode tag (`unknown`, `braking`, `preheating`, `normal`).
Never substitute polling time or HA `last_changed` for real observation provenance.

New forecasts require both sensors to be valid and at most five minutes old,
not future or reversed, and k in the existing plausible open interval `(0.005,0.5)`.
Booleans, missing/nonfinite temperatures, and temperatures outside [-100,100] are
rejected, never clamped or zero-filled. Zero and negative temperatures are valid.
A strict bound on physical measurement age does not require a five-minute reporting
interval: a new outdoor sample six minutes after the previous sample is usable.
No previous value is turned into a newly measured value.

Issue at most three forecasts per 30 minutes across sensor gaps and model changes.
A repeated indoor origin cannot issue again; a fresh repeated outdoor origin is
allowed. Separate actual issuance from indoor origin; extrapolate any indoor sample
age under the same passive assumption and flag it. Targets are 30/60/120 minutes
from issuance, using UTC arithmetic across both DST transitions.

Evaluation uses only valid, distinct, ordered indoor observations. Outdoor data,
current k, and intermediate sampling continuity are not needed to score a forecast
already issued from valid inputs. Missing/stale/reversed sensor data blocks relevant
issuance or that observation, without destroying unrelated future forecasts.

Match the first accepted real indoor timestamp in `[target,target+120 seconds]`.
No early match, interpolation or invented measurement is permitted. Record actual
sample time, receipt/resolution time and lateness; error is actual minus forecast.
A sample can arrive at most five minutes after its observation. Keep an unmatched
forecast until `target+120s+300s` to allow that delay, unless an accepted newer indoor
sample already proves its matching window was missed. Missed targets have no error,
not zero error. Long outages expire only targets whose windows have closed.

A reversed/invalid wall clock cancels the pending timeline explicitly (`clock_reset`)
and clears time watermarks. Completed evaluations survive. New engine instances
start empty; no forecast or evaluation is resurrected across a restart.

## Immutable evidence and revisions

Every prediction retains issuance/origin/target times, both measured temperatures,
both source times, k, fitted tag, instance-local model revision, horizon, forecast,
initial operating mode, conditional flag and warnings. Resolved records retain the
prediction plus actual measurement/error/lateness when matched, resolution time and
status (`evaluated`, `missed`, `clock_reset`, `evicted`). Operating mode is supplied
metadata, not a measurement of thermal power or proof of constant future operation.

Each changed valid k/fitted pair increments an instance-local integer revision.
Neither pending predictions nor completed records are cleared. Old forecasts are
scored using their immutable original parameters, never recalculated using new k.
`quality()` describes only the last valid supplied revision; `quality(version)`
selects one historical revision, and `quality_history()` lists retained revisions.
There is no pooled cross-version accuracy score. Reverting k still creates a new
revision. Revision IDs restart with the engine and must not be pooled across runs.
Invalid current k blocks issuance but does not prevent older forecast evaluation.

## Bounded accounting

Retain at most 24 pending predictions and 64 resolved records **per horizon across
all revisions**, including misses/cancellations. There is no unlimited version
registry or raw minute history. Keep at most three current forecasts and one current
batch of matched comparisons. Full evidence is available through immutable tuples
`pending_predictions` and `evaluation_history` (the latter grouped by horizon).

For each revision/horizon, count, missed count, bias, MAE, max absolute error and
coverage use the same retained resolved-record window. Eligible count is the number
of valid issued forecasts that have resolved; coverage is matched/eligible. Pending
forecasts are excluded until resolved. Explicit clock cancellations/evictions count
as unresolved-by-measurement misses, conservatively reducing coverage. Empty metrics
are unavailable. Once records age out, the summary is a rolling window, not lifetime
coverage. No error samples or confidence claims are manufactured.

All computation is bounded, synchronous and in memory; nothing is persisted or
written to Recorder. A future presentation must use compact, infrequent summaries.
A future adapter would require a separate design and verified timestamp provenance.
None is included here.

## Evaluation and learning scope

Regression tests cover frequent revisions, retained long horizons, six-minute
outdoor reports, interrupted/irregular observations, delayed/stale/reversed inputs,
restarts, DST, immutable context, cadence, version attribution and bounded retention.
Existing production files and configuration are unchanged. No new dependencies.

See [THERMAL_LEARNING_EVALUATION.md](THERMAL_LEARNING_EVALUATION.md) for the independent,
synthetic shadow experiment and the limits of physical identifiability. Its nine
empirical corrections exist only in test helpers and never change this reference,
production ThermalModel parameters, PI gains or heating decisions.
