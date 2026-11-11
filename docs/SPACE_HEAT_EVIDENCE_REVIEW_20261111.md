# Passive space-heating evidence review

Base: a239245692b4c00baafda5cb646430b9e201a157, with the pinned RoomMind
adapter. This branch adds regression contracts, this review and explicit diagnostic provenance.
No production configuration, active control, thresholds or model was changed.

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


## Follow-up: optional source semantics and diagnostics

The configured heating-mode inversion in `_heating_mode_state` flips only on/off
and preserves original HA times. A valve used both as inverted heating selection
and direct DHW selection describes circuit selection, not two independent witnesses
of production. MQTT entity metadata identifies transport, not register semantics.
Binary reports without a source heartbeat/expiry contract cannot be promoted to
current physical state by increasing freshness limits. A pump-labelled entity is
not a flow meter, and retained startup on states need source-level verification.

Bounded read-only production investigation supports this limitation; detailed
household observations remain in a separate private note outside this branch.
The register/payload polarity and reporting contract are still missing, so no
automatic RTM exclusion is wired from these sources. The existing `suspend` and
segment mechanism remains the appropriate path for known interval exclusions.

Small diagnostic change: HeatOperation always exposes `heat_delivery_verified=false`
because it accepts operating labels rather than a circuit-specific thermal meter;
conflicting heat/compressor/radiator labels explicitly report
`contradictory_operating_reports`. Existing learning qualification is unchanged.
New tests first failed on missing keys and passed after the change. Targeted
operation/provenance/common-chain suite: 41 passed (0.61s).

Follow-up full regression with pinned RoomMind enabled: **1650 passed (50.47s)**.
Only runtime diff is the two diagnostic fields/reason in HeatOperation; active
control and all model qualification decisions are unchanged. No production write,
installation or service call; no private data added to git. Local commit only.

## Gateway trace correction: HA state age is not MQTT silence

A subsequent read-only trace found both archived and live gateway republishes,
roughly every 20 minutes. This supersedes any inference above that an old HA
binary-state timestamp establishes missing source heartbeat. These are distinct
clocks: raw MQTT receipt, HA entity write, gateway register read, physical state.

[Husdata settings](https://husdata.se/docs/h60-manual/settings-explanation/)
defines MQTT_PUBALL (minutes, zero means change-only; documented default20).
Observed periodic traffic does not independently establish the local setting.
Discovery inspected for the relevant binaries has payload_on=1/payload_off=0,
without expire_after, availability or force_update. Transport liveliness is not
pump-communication liveliness or physical sampling.

[HA Core2026.10.4 binary_sensor.py](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/mqtt/binary_sensor.py)
tracks is_on/expired attributes. In
[entity.py:_message_callback](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/mqtt/entity.py),
MQTT messages enter debug history but state writes occur only after tracked
attribute changes or force_update. This accounts for old last_reported despite
recent identical raw MQTT messages. Recorder alone cannot recover those receipts.
No age threshold was relaxed and no HA force_update setting was changed.

[Manufacturer C50 register definitions](https://online.husdata.se/h-docs/C50.pdf):
1A01 compressor;1A04 ground-source pump;1A05 internal circulation pump;1A06 radiator
pump;1A07 valve0=radiators,1=DHW. Binaries0=off,1=on. Actual interface/firmware
identity and hydraulic applicability still need checking; a device's friendly
name is not an interface-type certificate. A valve label does not prove production.

Manufacturer source references reviewed:
- [H60-OS at14e047f5a12e9401bb73154bc9c015f2dcb03d4b](https://github.com/peterarandis/H60-OS/tree/14e047f5a12e9401bb73154bc9c015f2dcb03d4b):
  HD_H1comm.h H1_HandleData parses interface-provided XL/XR register lists/values;
  H1_CommInit has a periodic XR reread path. This is H60 starter code, not the
  installed H66/H1 firmware or proof of its physical polling interval.
- [Manufacturer Homey client at5409a901bf03b23b541e977d7606c8d2103b3a91](https://github.com/peterarandis/husdata-h60-homey/tree/5409a901bf03b23b541e977d7606c8d2103b3a91):
  drivers/H60/device.js maps compressor/valve IDX and polls API. Its client timer
  is not the gateway's register freshness contract.

No algorithm defect or safe replacement freshness policy has been established.
The existing HA-report guard can reject a transport-live unchanged binary; simply
holding it longer could also admit a stale gateway cache. Keep unknown until the
source contract is established. This trace adds documentation only; previous test
results are unchanged, not a newly executed regression run. Detailed household
observations and raw debug payloads remain private and are not committed.

## Installed-source follow-up

A further read-only gateway UI inspection now resolves installed controller-family
and firmware identity; the configured republication interval also matches raw MQTT
observations. Details remain in the private audit. This supersedes the earlier
statement that installed family/version and the local publish setting were wholly
unknown. It does **not** resolve per-register bus read time, update schedule,
cache invalidation or physical device state. The read-only pages/log expose no
individual update timestamp for the four operating registers. Detailed logging
was not enabled, API access was not enabled and no command was published.

The gateway web display and its MQTT export are two views of the same source;
agreement is not independent corroboration. Independent pump-display evidence is
still missing. Vendor H60 example code is not the installed H66 firmware. Keep
unknown register freshness distinct from transport heartbeat, retain known external
interval exclusions, and request the exact firmware/interface register-update
contract before changing automatic learning eligibility. No code change or new test
claim is made in this documentation-only follow-up.
