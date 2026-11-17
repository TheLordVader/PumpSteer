# Thermal Intelligence: source continuity

Local review and fix against `19e642fe08eb0ff8cc9348e928e1c8e83359e5c9`.
No installation, production setting change or field trial is part of this work.

## Fixed input contract

`PumpSteerSensor._thermal_cycle()` previously used the currently available
components of PumpSteer's weighted Base aggregate as TI's source identity. A
temporary startup loss could therefore replace the restored RTM twice: once on
losing components and once on recovering them. The chronological event ledger
survived, but the model parameters and grid did not.

The adapter now reports the configured unique, positively weighted components,
including missing ones. It reuses the existing temperature qualification helper
for restored states, units, numeric range and the startup report cutoff. Missing
values remain missing. The existing TI runtime interrupts the segment; it retains
the completed grid and candidate model. Its interruption hook now also clears the
held command and active capture cycle. Previously these could survive a live
temperature interruption unless another caller explicitly cleared them. A genuine
source, weight, outdoor source or output receiver change still invalidates RTM.

A single externally supplied aggregate retains its declared underlying source
provenance. This fix does not redefine that aggregate's source membership.
Unchanged recovery reports do not become fresh temperature changes. A genuinely
changed temperature after recovery can be collected in the new segment, without
inheriting a pre-interruption command.

No changes were made to RTM physics, model fit/qualification, ForecastAudit,
Store format/retention, Base aggregation, PI, braking, preheat, SAFE, output
dispatch or Relay Guard. In particular, the Base fallback to a partial aggregate
is preserved; it is not accepted as a full-source TI training observation.

## Verification

Twelve synthetic regression cases exercise restored model/grid retention for
unavailable, restored, foreign-unit, stale and missing-time inputs; unchanged
recovery; genuine recovery in a new segment with a previously established command;
independent output capture during missing-source cycles; true configuration changes;
disabled/duplicate sources; and an external aggregate's provenance.
An earlier test that explicitly expected model loss on temporary unavailability
has been corrected. Existing source-change and control/output checks remain.

Full offline regression: **1,676 passed**. The new cases are labelled synthetic.
An additional private offline replay exercised this adapter with archived reports
and native HA Core **2026.11.1** `State` objects, without starting HA, inventing
missing measurements or fitting a model. Completed evidence and fitted parameters
survived; the missing-source cycle did not yield an aggregate observation.
Private archives and operator scripts are excluded from this repository.

## Remaining limits

Retaining a candidate fit creates opportunities for later comparisons; it does
not qualify that fit or prove predictive benefit. ForecastAudit still requires
real later observations in a continuous compatible segment. Sparse, quantized,
asynchronous reports and changed commands/modes limit usable outcomes. Multiple
horizons from one issuance are correlated, not independent validation episodes.
Changed-value-only outcomes do not represent all operating time.

Optional supply/return temperatures and their time-aligned difference can describe
water-circuit behaviour. Without verified source freshness, flow and operating
mode they cannot establish delivered room heat. Degree-minutes describe a
controller balance/demand quantity, not thermal energy. Electrical power and
virtual outdoor requests are not delivered heat either. Existing `HeatOperation`
and `HeatPumpTelemetry` are sufficient places to inspect these optional signals;
no new RTM input, vendor threshold, mandatory sensor or second model is added.

Known outages, circulation stops, domestic hot water and external heat must be
considered when interpreting evidence. Unknown status remains unknown; an offline
annotation must not manufacture an exact physical measurement or disturbance
interval. Existing raw records must be preserved.

The private operator correction separately addresses deadline handling and
bounded confirmation. No software can promise exact physical shutdown during a
host/network/HA outage: the stop request, saved flags and runtime confirmation
must be reported separately. A new trial requires its own exact-version package,
verified stop/retry routine and explicit owner GO. Adaptive control remains
unqualified and must remain disabled.
