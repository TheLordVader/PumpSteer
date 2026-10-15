# Thermal Intelligence diagnostic foundation

This local prototype addresses the Roadmap's prediction-validation stage. PI,
comfort protection, ThermalOutlook, price logic, outputs and hardware recovery are
unchanged. It adds a pure, explicit observation API, not an active HA entity or
automatic control input. There are no service calls, dependencies or disk writes.

## Physics and interpretation

For a constant real outdoor temperature and no heat input, the existing model's
equation is `dT/dt = -k * (T - Tout)`, with time in hours. Its exact solution is
`T(h) = Tout + (Tin - Tout) * exp(-k*h)`. Use this solution for 30, 60 and 120
minutes. Do not change ThermalModel's existing linear helper or fitted k.

These are **conditional passive references**, not claims that the house receives
no heat. Every prediction names unknown heating input, unmodelled residual heat
and constant outdoor temperature as limitations. A non-fitted supplied k adds a
coefficient warning; missing/invalid k produces no prediction, not an invented
fallback. A fitted k alone does not certify forecast reliability.

Warm radiators and water continue releasing stored energy after the heat pump
reduces output. Compressor operation, solar gains and occupants are also unknown.
Positive actual-minus-reference error can reflect these inputs rather than a bad
heat-loss coefficient. A single indoor sensor cannot identify the separate
envelope, radiator-storage and heating-input terms. Do not fit an arbitrary
residual decay constant, publish probabilistic confidence or tune PI from errors.

forecast.py already performs HA weather service calls and ThermalOutlook affects
preheat. This foundation does not invoke that path, use prices or treat wind chill
as measured building outdoor temperature. Outdoor temperature is held constant
and the assumption remains explicit at every horizon. A later weather adapter
needs coverage, provenance and issuance-time checks before using future points.

## Observation and validation contract

- Call `ThermalDiagnostics.observe` with aware wall-clock `now`, separate real
  indoor/outdoor measurements and their actual observation timestamps, supplied
  `k_per_hour`, and optional `model_fitted` metadata.
- Timestamp-free, missing, nonfinite, boolean or physically implausible readings
  are unavailable. The diagnostic sanity range is -100 to 100 degrees C; values
  are rejected, never clamped. k must lie in the existing plausible open interval
  `(0.005, 0.5)` per hour. Zero and negative temperatures remain valid.
- A measurement may be at most five minutes old and cannot be from the future.
  Never substitute poll time or HA `last_changed` for a physical sample timestamp.
  Any eventual HA adapter must establish timestamp provenance explicitly.
- Normalize all times to UTC before comparisons and horizon arithmetic. DST
  folds and spring jumps represent physical elapsed time. Naive times are rejected.
- Record actual issuance time separately from the indoor observation origin.
  Horizons start at issuance, not a backdated sample. Extrapolate the sample's age
  under the same passive assumption and flag that extra extrapolation explicitly.
- Within a contiguous model cohort, issue at most three references per 30 minutes.
  Duplicate timestamps cannot create predictions or additional validation samples.
- Missing/stale data, a gap over five minutes or reversed clocks cancel pending
  comparisons. A fresh segment can start from the next valid snapshot; completed
  quality measurements survive gaps. Restarts start empty, with no fabricated
  history or resurrected pending forecasts.
- Match the first distinct actual indoor measurement at or after each target,
  within two minutes. Never match an early measurement or interpolate a gap.
  Record the time offset; that tolerance contributes to descriptive forecast error.
  Later or cancelled references are counted as missed, not scored as zero error.
- A changed k or fitted-state tag starts a new quality cohort so results from
  different model snapshots are not silently mixed.

## Bounded diagnostics

Keep at most 24 pending forecasts, three current references, one current batch of
comparisons and 64 completed errors per horizon. Retain no raw temperature history.
Expose sample counts, missed counts, signed bias, MAE and maximum absolute error.
No completed samples means unavailable metrics. Counts are descriptive and neither
an empirical confidence score nor evidence of a validated no-heating model.

The API performs no Recorder, file, cloud or HA writes. No new entity/attribute is
registered and no extra integration update is scheduled. Histories are deliberately
not persisted. A future HA presentation should publish a compact, infrequent
summary, not per-poll histories or growing attribute lists.

## Compatibility and implementation scope

Add only `thermal_intelligence.py`, focused tests and this document. Existing
`sensor.py`, `thermal_model.py`, `forecast.py`, configuration and manifest remain
byte-for-byte unchanged. The component takes explicit immutable snapshots rather
than depending on private model state or pending PR #278's confidence/session API.
This keeps both the current main and that independent learning proposal viable.

TDD covers exact passive trajectories, unknown heat input, absent calibration,
invalid readings, fresh zero/negative temperatures, stale and future measurements,
duplicate/out-of-order timestamps, gaps, late/early comparisons, model changes,
restarts, both DST changes, issuance cadence and bounded long-running histories.
Run the entire existing suite and CI-equivalent checks on Python 3.11 through 3.14.
Perform a read-only independent review before any local completion claim.

Upstream publication, GitHub comments/issues/PRs/reviews/actions and destructive
cleanup remain separate user approval gates. This prototype is local only.
