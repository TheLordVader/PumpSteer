# Thermal evidence: open-source reuse and offline verification

## Decision and scope

Use RoomMind's existing simulation engine through a test-only adapter. Do not
import another learner, controller, ledger, runtime dependency or sensor requirement.
Base is exactly `aff5419119e72fb99c49906953e75f63c94e99a1`; the separate SAFE
commit `e3527d61e9393dbceae5864a77609cc1d891eb17` is not included. Both remote
branch heads were verified before starting. No production connection is used.

Plan: first assert the external-adapter contract (observed failing before implementation),
then wire existing World/SimSensor/emitters to ThermalIntelligence, exercise faults,
run regression and verify zero integration-file changes, then publish isolated branch.
User authorized this bounded choice and implementation without another design round.

## Reviewed upstream revisions and concrete reuse

All repositories were read from actual source checkouts, including licenses and
relevant test contracts. These are snapshots, not claims that a whole project has
been independently validated.

| Project/revision | Component | License/dependencies | Decision and simplification |
| --- | --- | --- | --- |
| [Heat Pump Pilot](https://github.com/andox/heat-pump-pilot/tree/40f3fb603bc794fe2c2c81bb289322376a7d6d93) | `pump_response.py`: `PumpResponseModel._fit_new_model`, `_usable_indices`, `ResponseParameters.advance` | Apache-2.0; standard library/local learning_math | Already byte-identical in pilot_vendor. Separates request from observed heat, delayed response, chronological holdout, variance and baseline checks. Useful reference once a credible activity signal exists; do not feed a virtual outdoor coordinate as measured heat or activate this as a second model. |
| Same | `learning_interval.py:IntervalCollector.observe`; `learning_manager.py:LearningManager.observe` | Same | Both byte-identical locally. Coverage >=95%, missing data, >300s gaps, separate pump/house intervals. Coverage is input continuity, not proof that a sparse repeated temperature was freshly measured. Reusing whole manager would restore two collectors/learners; reject. PilotPassive's report-order and interval-reset handling remains useful reference for the current chain, not a new runtime path. |
| Same | `adaptive_model.py:AdaptiveThermalModel.add_interval`; `learning_math.py:bounded_fit`, `finite`, `solve` | Same | Both byte-identical locally. Constrained fits/persistence already available; no second import and no replacement RTM introduced. House response cannot be identified from assumed heat alone. |
| [RoomMind](https://github.com/snazzybean/roommind/tree/d374baa7ba7d7adc0e6e9beec03ae7e9d3c91119) | `dev/sim/roommind_sim/world.py:World.step`; `physics/zone.py:Zone`, `ZoneParams`; `devices/models.py:Trv`, `Ufh`; `sensors.py:SimSensor.update`; `scenario/loader.py:load_scenario_dict` | MIT; sim declares Python>=3.13, PyYAML (UI optional Playwright, unused) | **Selected.** Import externally at pinned revision, no source copied. Existing wood-frame/passive-house profiles, radiator water/slab states, housing delay, noise/quantization/heartbeat and asynchronous reports replace a home-grown physics engine. No EKF/MPC/controller or HA simulator integration is loaded. |
| [Heating Analytics](https://github.com/thuemah/heating_analytics/tree/eba9e6e712090e674dd99f2bdba94e3339266998) | `observation.py:detect_solar_shutdown_entities`; `const.py:MODES_EXCLUDED_FROM_GLOBAL_LEARNING`; `heat_source.py` source provenance | MIT; local constants/calibration/energy history | Reuse classification **principle**, not module: OFF/DHW/external modes excluded without erasing evidence, absent meter != zero. Heat-source curve cannot determine COP level; flat COP cannot distinguish ground-source from direct electric. Not a universal circulation detector. Existing optional PumpSteer power/valve/compressor/supply/return data may support labels, but their semantics/freshness must first be checked. No manufacturer-specific inference added. |
| [ThermoSmart](https://github.com/Mikasmarthome/ThermoSmart/tree/76f5fe1743f35a9b8010faf5afe36cfe8102cdcd) | `learning/episode_schemas.py:EpisodeReason`, `TrajectoryPoint`; `learning/runtime/prediction_ledger.py:PredictionSnapshotLedger.record` | MIT; dataclasses plus local contracts | Versioned episodes, explicit gaps and DISTURBED/INTERRUPTED/INSUFFICIENT_DATA are sound descriptions. Ledger snapshots are bounded/restart-serializable but `record` overwrites the same key: frozen dataclass alone does not guarantee append-once semantics. Keep existing ForecastAudit and chain segments; importing this would duplicate permanent histories. |
| [ha-predictive-heating](https://github.com/nic6911/ha-predictive-heating/tree/7cf786ec10a0b4ff9ea9e16e5f48a5b1d4cb1b59) | `models/identification.py:batch_fit_auto`, excitation-aware column selection | MIT; NumPy, local 3R2C/constants | Low variation holds coefficients at priors. This avoids unstable identification but prior-backed output is not learned evidence. Do not import another model or its numerical excitation thresholds. |
| [heatpump_optimizer](https://github.com/tvofi/heatpump_optimizer/tree/45adb0c3b425dc44e72fd9ce4ef51f480b5a3af6) | `button.py:SystemIdentificationButton`, coordinator's system-identification path; README commissioning experiment | MIT; integration coordinator/device dependencies | Explicitly armed physical step experiment and learned heat-loss/COP assumptions are outside passive scope. No code imported or physical experiment scheduled. Insufficient passive excitation remains a valid result. |

Upstream tests inspected include Pilot's pump-learning regressions (unknown activity,
quiet data, restart/revalidation), RoomMind physics/device tests, Heating Analytics
heat-source tests (short history unknown, pre-report days excluded), predictive-heating
identification tests (excitation/insufficient samples), and ThermoSmart ledger/schema
contracts. No assertion here means upstream thresholds are universally suitable.

## One implemented improvement

`tests/offline/roommind_adapter.py` runs six synthetic hours in RoomMind's World
and passes reports/commands to the existing ThermalIntelligence runtime. All
returned output is explicitly synthetic. Physical measurement time remains unknown
to PumpSteer even though the simulator knows its own clock. Simulated delivered
room gains are returned separately as `truth`, never fed as measured heating.
No fitting is invoked, no parameter trust or forecast performance is manufactured.

Scenarios cover fast wood-frame/radiators and slow passive-house/slab, three
70/20/10 sources, 0.1 C quantization, different report intervals and heartbeats,
sensor housing delay, missing source, collector restart/restore, history gap,
known heat-off/DHW interruption and undetected heat-off. DHW here represents
interrupted space-heating availability, **not** a simulated hot-water tank.
Emitter residual heat remains possible when the device is switched off.
The externally known interruption is passed via existing `suspend`, not inferred
from requested commands. An unannounced stopped plant remains undetected: the
adapter deliberately demonstrates this evidence limitation rather than adding a
fictional automatic classifier. Weather-only uses constant requested command to
check that changing outdoor values/keepalive do not become new output events.
It does not assert that all possible weather-correlated real commands are identifiable.

Known external power/heat interruptions can be represented by the same synthetic
heat-off/gap cases. They are not replays of any private household event, and cannot
establish its exact start/end or validate its physical explanation.

The adapter samples simulator report state every 30 seconds; reports between
those invocations are not claimed as complete HA event coverage. Six hours are
an integration-contract test, not a statistically adequate model evaluation.

## Reproduction

Use a clean external RoomMind checkout at the exact linked SHA. The adapter refuses
other revisions or tracked modifications. The optional tests skip visibly without
the environment variable; they are not reported as passing in ordinary CI.

```
git clone https://github.com/snazzybean/roommind /tmp/roommind
git -C /tmp/roommind checkout d374baa7ba7d7adc0e6e9beec03ae7e9d3c91119
# In an isolated Python 3.13 environment with pytest, pytest-asyncio, numpy, PyYAML:
PUMPSTEER_ROOMMIND_ROOT=/tmp/roommind python -m pytest tests/test_roommind_offline.py -q
python -m pytest -q
```

No upstream source is redistributed, so no copied license headers are removed;
RoomMind stays an optional MIT external development dependency. Existing Apache
vendor notices remain untouched. No private history/configuration/log is included.

## Remaining limits and next smallest step

This improves falsification tools, not field learning throughput. Source sparsity,
unknown physical measurement times, insufficient independent command excitation,
and missing verified plant activity still limit identification. A command-success
acknowledgement is not delivered heat. Neither electrical power nor a compressor
flag alone verifies room heating/circulation; DHW and residual heat complicate this.

Next: use this adapter to audit existing optional operation-status provenance and
segment exclusions before wiring any new classifier. Keep raw events and mark
known disturbances; do not qualify a model on synthetic tests. Independent frozen
ForecastAudit outcomes must beat unchanged-temperature reference in real operation.
No runtime code was removed because this test adapter replaces no production path;
it avoids writing/maintaining a new simulator or duplicating vendor/ledger code.

## Verification results

- Final full regression with external RoomMind enabled: **1642 passed (50.17s)**.
- Final targeted adapter run: **21 passed (5.72s)**.
- External RoomMind physics and device tests: **35 passed (0.87s)**.
- Ruff check and diff whitespace checks passed for the added Python files.
- `git diff aff5419 -- custom_components` is empty: all integration code,
  active outputs and existing safety functions remain byte-for-byte unchanged.
- Internal read-only reviewer found missing specific sensor-loss/delay assertions;
  these were added and passed. This is not independent field qualification.
- pytest-asyncio emits an unset fixture-loop-scope deprecation warning; no test
  failure. No production load or Store behavior was changed or benchmarked.
