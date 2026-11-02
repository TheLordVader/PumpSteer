# One control engine, three optional levels

## Product objective

PumpSteer aims to be a self-learning, autonomous and manufacturer-independent
heating controller that uses available reliable information to improve comfort
and electricity cost. A basic installation must work without optional telemetry;
additional verified information should improve the same control process. Learned
house response must eventually inform bounded control decisions, with automatic
fallback when evidence is insufficient. Predictions and simulated savings must
remain distinct from measured outcomes.

## Control map and existing contracts

The reported installed reference is `76bf06d55f816aa69a612b9a61567b65523e0594`;
verify the actual installed files/settings before any future deployment. Historical
control-parity tests also retain `b273afc` as a verified code reference; it must not
be mistaken for the currently reported installed version. This hardening starts
from `78b9807`.
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
needed. The baseline test executes the historical verified reference's branch method
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

Both features require explicit `control_signals_verified`, valid radiator mode
and no hot-water mode. A single changeover valve may represent both states when
the radiator role is explicitly inverted; the two meanings must be complementary.
Other conflicting aliases are rejected. The auxiliary limiter also needs a
distinct compressor-on signal, active auxiliary heat, and an on brine pump when
one is configured. The balance limiter needs neither a running compressor nor a
brine pump: a verified deficit does not claim that heat is currently delivered.

Every contributing signal needs an aware ISO timestamp or epoch seconds in the
configured `control_measurement_time_attribute` (default `measurement_time`). It
must represent a verified physical observation or a verified new message from the
specific source, bound to this value; the latter is a receipt time, not a physical
measurement time. A gateway online flag or MQTT republish timestamp is insufficient.
Both that time and the HA report must be no more than five real minutes old;
future/restored values and observations at or before integration startup are
rejected. New HA `last_reported` alone cannot refresh an old sample. An installation
lacking qualifying source timestamps stays at Base.

Balance additionally requires `control_balance_verified`, finite nonzero scale,
finite offset and positive ordered thresholds. Invalid or absent calibration,
missing signals, hot-water mode, contradictions and old samples immediately set
the desired correction to zero. Configuration/source changes invalidate UI
verification. The correction ramps in and out using the existing configured house
ramp durations, with at most 60 seconds' progress per control cycle. The previous
correction is transition history, not a claim that an old observation is still
true. Separate brake and preheat transition histories survive a short phase change;
a previous phase's correction is discarded after a full ramp-out interval in a
different phase, and a new preheat phase clears the completed brake correction.
SAFE and summer/precooling have priority. The original brake and preheat
amplitude bounds and comfort floor remain effective.

Dispatch re-evaluates contributing signals during the existing output/Relay Guard
window. A fresh unchanged report is accepted; a genuine transient invalidation
blocks remaining writes and relay activation without clearing PI or ramp state.
The next normal control cycle recalculates; there is no immediate double integration.
If a first output was already accepted before the invalidation, it cannot be unsent.

### Example source contract, not a proven Tallåsen installation

[ebusds HTTP API](https://github.com/john30/ebusd/wiki/3.2.-HTTP-client) returns a
per-message `lastup` and its values in a JSON object; [HA REST](https://www.home-assistant.io/integrations/rest/)
can expose the value and that same message's `lastup` together in one sensor state.
An isolated native HA test demonstrates this transport and verifies that a new
HTTP read or newer **global** `lastup` does not refresh an older message. A binary
role can use a single trigger-based adapter carrying the same value and timestamp;
the same valve entity can serve the two complementary roles. This is a transport
example, not evidence that a specific pump's data is qualified.

For each role, verify the exact device, message, value meaning, time synchronization
and that its message-specific timestamp advances **only on a new valid response**.
ebusd's implementation can update `lastup` before a successful response for some
message types, so `lastup` is not universally sufficient or a physical sample time.
Do not use a global timestamp, generic online status or a fresh REST/MQTT report as
proof. A normal cache-reading GET avoids initiating bus polling; do not add
`required`, `maxage` or `poll` merely to make data appear fresh. At startup a new
qualified source timestamp is required, including when an adapter restores state.

The observed Husdata/H66 signals have no verified per-observation timestamp, and
their balance decoding remains unverified. Neither active limiter is currently
established as usable with those signals; no H66 settings were changed.

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
| Cold forecast, fully ramped preheat, first verified auxiliary report | −4.487°C | −4.220°C | 0.007 unchanged |
| Expensive slots, full brake, first verified deficit | 10.000°C | 9.651°C | 0.000 unchanged |
| After three successive verified minutes, then missing sample | 10.000°C | 8.952°C → 9.301°C | unchanged |
| Missing/stale data with no earlier correction | Base | Exactly Base | unchanged |
| Summer, aggressiveness zero or SAFE | Base | Exactly Base | existing rules |

These are simulated command differences, not measured energy/cost/comfort savings.
There is insufficient simultaneous physical production/energy/price evidence to
claim field savings or fewer compressor starts.

The helper has no buffer, background task, listener, model or database. It reads
only a bounded handful of already available states on the existing control cycle.
Diagnostics are excluded from Recorder; no large per-cycle history is added.

## Adaptive price braking — first closed-loop delivery

The product objective above is implemented first as an optional conservative
adjustment of existing price braking. `adaptive_braking_enabled` defaults to
false, independently of passive Thermal Intelligence and the two verified
telemetry limiters. The existing thermal coefficient describes **observed net
cooling during unmodified braking**, not measured heat loss, power or COP.

Implementation sequence: qualify distinct HA temperature reports; fit the existing
ThermalModel; issue independent frozen prediction trajectories; validate three
later baseline braking sessions; use the qualified response to reduce price braking near the
comfort floor; ramp back to Base as evidence expires or degrades. The existing PI,
price classification, output validation, Ohmigo and Relay Guard remain the only
control/output chain. Preheat and recovery optimization remain future consumers
of this same evidence, and are not implemented in this delivery.

Activation and quality:

- Enable **Adaptiv prisbromsning / Adaptive price braking** under Thermal
  Intelligence after a separate installation GO. No extra heat-pump sensors are
  required. Normal room/outdoor HA reports are observational evidence, not proof
  of physical sampling time or heat delivery. Cached/repeated polling ticks do
  not add learning samples.
- Learning requires distinct indoor reports and indoor/outdoor report coverage
  within eighteen minutes, stable near-full braking, heating-season temperature
  difference, at least 20 accepted rate samples and fit residual <=0.3 C/hour.
  Adaptive rates use at least three distinct observations over at least fifteen
  minutes, with up to 32 reports and at most 90 minutes in the existing small
  history. This reduces 0.1 C quantization noise without inventing samples.
  Ordinary 1/5/10/15-minute reports (including bounded jitter) can qualify when
  sufficient long braking sessions exist. A 30-minute stream is explicitly too
  sparse for this first adaptive policy. Base report-age settings and output
  safety are unchanged.
- Each qualifying session freezes its coefficient, initial room value and outdoor
  value as a prediction trajectory. At issue, the endpoint rule is fixed: use the
  first actual report at/after 30 minutes, no later than 48 minutes, with at least
  three observations including the start. Predict for that actual elapsed time
  using only the frozen starting data; never choose an endpoint by its error,
  interpolate temperatures or use later outdoor measurements.
  Three separate sessions must each have absolute error <=0.2 C. Confidence alone
  cannot activate control. Evidence expires after seven days; current indoor–outdoor
  difference must remain within 5 C of the validation range.
- Only reduce braking, by at most 25%. Compare predicted 30-minute cooling with
  remaining comfort margin after a 0.2 C reserve and 0.2 C error allowance. Ramp
  the correction using existing ramp times (minimum ten minutes, elapsed time
  capped at one minute per update). Never add braking or bypass the comfort floor.
- Modified braking does not train or validate the baseline response. Expired
  evidence returns gradually to Base, allowing new unmodified validation. Sensor
  source/weight, selected outdoor source, brake-depth or aggressiveness changes
  discard old evidence. SAFE resets output correction; restored correction is
  never applied. Known intermittent extra heat disables adaptive use.

`adaptive_braking` reports `collecting` (Samlar erfarenhet), `learned` (Lärt husets
respons), `adapting` (Anpassar styrningen), `returning_to_base` (Återgår) or `base`
(Grundreglering), together with the gate reason, applied multiplier and predicted
30-minute drop. `adaptive_model_state` is a compact versioned RestoreEntity
summary (policy version 2): coefficient association, source context and only three
validation rows. Older qualification is discarded while the fitted coefficient
can still be independently revalidated. For weighted room temperature, a learning
snapshot is accepted only after every contributing indoor source has reported
again. Its timestamp is the newest included HA report; component times remain
separate and it is explicitly a causal asynchronous snapshot, not a simultaneous
physical measurement. An individual report cannot fabricate new observations for
other contributors.
The existing 240-sample fitting buffer stays bounded; the adaptive rate window
is capped at 32 reports/90 minutes. No new listener, polling, database, service,
output path or model training dependency is introduced.

For a later Tallåsen installation GO: verify candidate file hashes, back up HA
and preserve the actually installed `76bf06d55f816aa69a612b9a61567b65523e0594`
integration/settings after verifying that reference, install only candidate
integration files and perform the agreed Core restart. Verify normal SAFE recovery,
PI/prices, ordinary Ohmigo output and Relay Guard before enabling this option.
Then observe sample count, fit residual, three holdouts, gate reasons, brake factor,
virtual outdoor command and room-temperature floor during actual price periods.
Disable this option for immediate normal-operation selection (an existing correction
ramps out); restore only the previous integration files/settings if rollback is
needed. No production actions are part of local development.

Replay changes demonstrate program behavior and bounded comfort protection, not
measured energy, compressor-start, comfort or monetary improvements. Actual benefit
requires later prospective temperature/comfort comparison and, for cost claims,
qualified metering and prices. The electricity accounting engine is untouched.
