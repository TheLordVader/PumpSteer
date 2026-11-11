# Passive space-heating evidence review

Base: a239245692b4c00baafda5cb646430b9e201a157, with the pinned RoomMind
adapter. This branch adds regression contracts and this review only. No production
configuration, active control, thresholds or model was changed.

## What the available signals establish

| Signal | Establishes | Does not establish |
| --- | --- | --- |
| Calculated/acknowledged virtual outdoor command | Request and transport status | Compressor, circulation, room heat |
| Configured compressor binary | Source's reported operating label | Fresh physical sampling, room heating, actual electrical/thermal production |
| Electrical W/kW | Reported electrical input for the configured meter scope | Delivered heat, COP, circuit selection or circulating flow |
| DHW valve/mode | Reported selected circuit, subject to configured polarity/semantics | Actual DHW production or zero residual room heat |
| Fresh aligned supply/return temperatures | Reported temperature gradient | Water flow or delivered thermal power; stationary pipes can retain a gradient |
| Optional circulation binary | Reported pump state | Verified flow; on can still mean a blocked or failed pump |
| Independent heat-active label | Whatever the upstream source actually measures | A heat meter, unless its physical basis is independently documented |
| Validated circuit-specific thermal meter | Measured heat for that circuit within meter quality limits | Available universally without installation-specific evidence |

No new sensor is mandatory. Without suitable optional signals, operation stays
unknown and commands stay commands. HA last_reported is not physical measurement
time; a fresh republication of an old on flag is not stronger evidence of heat.

## Existing code to retain

`HeatOperation.observe` already reuses `PilotPassive._report` for freshness and
`PowerLedger._value` for electrical units. Uncalibrated coincident compressor,
mode, power and gradient can produce `indicated_radiator_heating`, never a
learning-qualified interval. Even 0/50 W plus an on flag and hot pipes must not
qualify learning; the new tests lock that distinction down. The indication label
itself must not be read as a verified production detector.

The `quality=verified` branch requires a configured independent `heat` label.
Here verified means an accepted label, not independently verified thermal energy.
The installer/source contract must establish what that label represents before
using `learning_qualified` as physical evidence. It must not be supplied from a
PumpSteer command, raw compressor flag or a circular inference.

Heat Pump Pilot's existing `IntervalCollector` and `PumpResponseModel` distinguish
request from observed heat and check continuity, coverage, variation and holdout.
They cannot turn an unreliable input label into measured heat. Do not reactivate
PilotPassive's competing learner to solve this provenance problem.

Heating Analytics excludes OFF/DHW and other incompatible modes from appropriate
learning paths and distinguishes missing energy from zero energy. Its heat-source
classifier infers energy/COP curve shape, not circulating room heat. Importing its
complete classifier would add dependencies without solving the missing measurement.

## Gap in the common evidence chain

`ThermalIntelligence.begin_cycle` derives regime from PumpSteer's mode/brake factor.
Its context records sources, GOS and transport choice; it does not automatically
use HeatOperation/DHW/plant status as training exclusions. A valid command after
HA restart can therefore coexist with an unobserved stopped plant. The collector
cannot discover a manually stopped plant solely from its own command history.

Existing `suspend(reason, at)` creates an interruption/segment boundary and clears
command continuity. Known externally documented disturbances must be excluded for
the entire affected interval, including uncertain recovery, not merely marked by
one boundary at the start. Keep raw history and the reason/source of the exclusion.
Unknown operation is not evidence of zero heat or pure passive cooling. A generic
power threshold would misclassify different pumps, standby loads and DHW operation.

For future offline analyses, apply documented disturbance masks before regression
and forecast scoring. The adapter's known heat-off/DHW cases exercise existing
boundaries. Its unannounced heat-off case intentionally remains undetected. The
simulator's ground truth stays separate and is never presented as an HA measurement.
DHW is a space-heating interruption scenario, not a tank model.

## Verification and bounded decision

Added six synthetic regression cases for zero/standby/high electrical input with
compressor on, DHW override, stale on label and a gradient without flow evidence.
Run alongside existing HeatOperation and common-chain tests. Reuse the existing
RoomMind fast-radiator/slow-UFH, gap, restart, sensor and known/unknown outage cases.
No private Recorder rows are committed and no new plant detector is implemented.

The next smallest functional step is to document and verify one optional upstream
operation source's polarity, freshness, circuit scope and physical basis, then pass
only its defensible exclusion labels into the existing chain. Preserve unknown
when that contract is absent. Do not upgrade inferred operation to delivered heat
or model qualification. This requires evidence about a source, not another model.

Results: 39 operation/provenance/common-chain tests passed (0.60s); all 21
external RoomMind adapter tests passed (5.61s). Ruff and whitespace checks passed.
Integration diff against a239245 is empty. The full 1642-test regression belongs
to the previous adapter commit; it was not rerun or represented as a new result.
