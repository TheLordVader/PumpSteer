# One control engine, three optional levels

## Control map and existing contracts

The stable baseline is `b273afc`; the development starting point is `540a285`.
The following map describes the actual code, not a replacement controller.

1. `sensor._do_update` obtains qualified indoor temperature (up to three normalized
   weights) and selected outdoor temperature (primary/fallback). Invalid, restored
   or stale contributors cannot form a qualified control temperature. Source
   changes interrupt thermal observation continuity. Missing control inputs or
   prices enter SAFE; a valid real outdoor temperature is the existing fallback.
2. `electricity_price` and `_get_prices` validate quarter-hour/hourly price coverage,
   timestamps, categories and calendar slots. Local calendar time is retained for
   tariffs; elapsed ramps and PI use timestamps. `holiday` selects the existing
   target. Aggressiveness sets comfort floor and price-control strength.
3. `_run_control_logic` orders summer passthrough, precooling, pure PI when
   aggressiveness is zero, expensive-price braking, prebraking, forecast-qualified
   preheat, short-dip brake hold and ordinary PI/ramp-out. Comfort floor releases
   braking; forecast and indoor headroom constrain preheat. `control_ramps` owns
   bounded entry/exit. `_pi_output` calls `control.PIController`: bounded integral,
   output clamp, frozen integral under braking/precool, and existing reset rules.
4. The same branch method is used by `control_replay`. Neither replay nor passive
   planning sends services. Live `_set_state` validates output and dispatches via
   `_push_validated_temperature`, `ohmigo`, Generic Output and `relay_guard`.
   Receiver state, input continuity and Relay Guard remain mandatory. There is no
   new service/output path.
5. `thermal_model` learns bounded braking-response samples; verified/indicated
   observations, physical heat qualification, forecast validation and model audit
   remain in `pilot_passive`, heat-operation detection and the existing Pilot
   components. `pilot_power_ledger` accounts measured electricity with unambiguous
   prices; solar/export opportunity cost and hypothetical plans remain passive.
   No raw balance, supply delta, compressor status or new limiter becomes measured
   heat delivery, COP or verified model-learning evidence.
6. `heatpump_telemetry` retains bounded raw observations and diagnostic starts;
   `heatpump_alarm`/`notify` handle separate fault messages. `extra_zones` monitors
   rooms independently and does not supply PI inputs or limits.

## Selected improvements

### Base

Both switches default off. Choosing any optional entity does not activate them.
No changes to PI, prices, ramps, targets, physical outputs or safety contracts are
needed. The baseline test executes the installed commit's actual branch method
against identical initial states and inputs (54 combinations). It also compares
its control helpers and `control.py`, `control_ramps.py`, `settings.py` against
working-tree code. Existing full golden traces cover price cycles, preheat,
comfort-floor release, SAFE and recovery.

### Extended: auxiliary-heat preheat limiter

`control_aux_preheat_limit` can remove **only the extra price-driven preheat boost**
while auxiliary heat is positively observed during confirmed radiator operation.
PI demand and integration are untouched, so ordinary heating and the pump's own
emergency/auxiliary controls are not disabled. This avoids requesting additional
preheat while known auxiliary heat is operating; it does not prove that PumpSteer
caused that auxiliary use or that removing the request saves a measured quantity.

### Advanced: internal-balance brake limiter

`control_balance_brake_limit` uses `deficit = raw * scale + offset`, only after
explicitly verified sign, scale, meaning and thresholds. Positive deficit means
heat debt **in the user-verified normalized convention**, not a universal vendor
convention. Between `control_balance_deficit_threshold` and
`control_balance_deficit_full`, the effective existing price-brake factor is reduced
linearly by 0–50%; above the upper threshold it is reduced by 50% at most. The
underlying ramp and PI memory are retained. It applies to expensive braking,
prebraking and residual/held price braking; never to summer or precooling.
The existing braking learning session is suspended when this modifier is applied,
so modified output is not mislabeled as an unmodified stable brake experiment.

## Qualification and fallback

Both active features require explicit `control_signals_verified`, distinct
compressor/radiator/hot-water signals with compressor on, radiator mode on and
hot-water mode off. A configured brine pump must also be on; an absent optional
brine pump does not disqualify an air-source system. Aliased logical signals do
not provide independent evidence. Auxiliary heat requires a separate active signal.

Every contributing signal needs an aware ISO timestamp or epoch seconds in the
configured `control_measurement_time_attribute` (default `measurement_time`). It
must represent the actual physical observation, not an MQTT republish timestamp.
Both that time and the HA report must be no more than five real minutes old;
future/restored values are rejected. New HA `last_reported` alone cannot refresh an
old physical sample. An installation lacking these timestamps stays at Base.

Balance additionally requires `control_balance_verified`, finite nonzero scale,
finite offset and positive ordered thresholds. Invalid or absent calibration,
missing signals, hot-water mode, contradictions and old samples all discard the
modifier immediately. Configuration/source changes invalidate UI verification.
Output dispatch revalidates actual contributing signals during the existing
Relay Guard window; it does not issue an alternate command on failure.

Fallback deliberately returns to the existing Base decision. In particular,
removing an auxiliary limiter can restore the Base preheat ramp already accumulated
internally. This can change the request by up to the existing 4°C boost; no claim
of reduced compressor cycling is made. The user can leave this opt-in disabled.

## Rejected/unproven interventions

* Freezing PI simply because hot water runs was rejected: the building still loses
  heat and no measured basis justifies suppressing its integral. Unknown/VV state
  blocks these optional limiters rather than pretending radiator production.
* Compressor-start diagnostics count observations, not inferred events. No active
  cycling limiter is included without coverage and a verified beneficial replay.
* Supply-minus-target and supply-minus-return remain temperature indications, not
  measured heat flow or COP. No model-based output or COP control was introduced.
* Physical Husdata/1250 decoding remains unverified. The generic calibrated path
  exists, but supplied production observations do not authorize its activation.
  No vendor register identifier appears in generic configuration.

## Synthetic replay results

Same initial state, indoor 20.8°C, target 21°C, outdoor 0°C, aggressiveness 3:

| Scenario | Base virtual outdoor | Optional output | PI integral |
|---|---:|---:|---:|
| Cold forecast, future expensive slots, fully ramped preheat, verified auxiliary on | −4.487°C | −0.487°C | 0.007 unchanged |
| Expensive slots, full brake, verified deficit at upper threshold | 10.000°C | 4.760°C | 0.000 unchanged |
| Same cases with missing/stale/republished/unverified or conflicting context | Base | Exactly Base | unchanged |
| Summer, aggressiveness zero or SAFE | Base | Exactly Base | existing rules |

These are simulated command differences, not measured energy/cost/comfort savings.
There is insufficient simultaneous physical production/energy/price evidence to
claim field savings or fewer compressor starts.

The helper has no buffer, background task, listener, model or database. It reads
only a bounded handful of already available states on the existing control cycle.
Diagnostics are excluded from Recorder; no large per-cycle history is added.
