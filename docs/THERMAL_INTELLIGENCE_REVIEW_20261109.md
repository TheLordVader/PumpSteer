# Thermal Intelligence independent review — 2026-11-09

Production reference: `5100ed83d1ff7ac643754a65460d8aa63e1265c5`.
Prototype: `56004ab30bed3cd227cda9958099f547bb9ef944`.
Reviewed parent: `faa175610fcd5a746249af1067518470e41ffc5e` (two hardening commits above prototype).
Comparison uses these commits, not the diverged main branch. No Recorder replay or production calls were performed.

## Verified fixes on the isolated review branch

Seven new failing regressions reproduced these problems before the changes:

1. Rejected outdoor data consumed an indoor value change. Retrying with valid outdoor context permanently lost the observation. Source memory is now committed only after capture accepts the temperatures.
2. A stale constituent could hide behind the latest aggregate report. Each constituent now meets the existing 60-minute capture bound; no age threshold was added.
3. Future constituent timestamps were accepted by the runtime when the aggregate timestamp was valid. Constituent timestamps are now checked individually.
4. An obsolete executor result reset the in-progress flag before its generation was checked. It now leaves a newer generation's flag intact.
5. Restore retained raw pending resampling data, permitting held command/temperature context across an unobserved restart. Restore now retains finalized grid history and discards the open resampling window.
6. The real SAFE entry never notified RTM: its forecast could survive SAFE and later score as continuous braking. A guarded passive suspension now cancels the forecast, clears pending resampling and command continuity. SAFE decisions and output behavior are unchanged.

7. Corrupt persisted forecast errors were accepted and could raise from summary arithmetic in the control lifecycle. Non-numeric, non-finite or negative stored errors and malformed record rows are now rejected before installation.

An eighth regression compares actual offline controller state, mode, Ohmigo and Generic Output service calls with passive capture disabled/enabled at the same PI operating point. It confirms parity for that case, not every possible trajectory.

## Assessment of the five requested areas

- Frozen forecast: the reviewed parent freezes a/b/c/dead time, evaluates the frozen constant-input curve at the actual reported elapsed time, and uses persistence at the same endpoint. Later fits do not replace those parameters. Report time remains HA evidence, not physical measurement time.
- Refit: unchanged grid data are throttled; insufficient command range prevents repeated attempts. However `MIN_NEW_DATA_HOURS` is tested against grid end time, not newly usable regression rows. Padding over gaps can qualify as new time. The range precheck also sees all grid efforts rather than only eligible delayed regression inputs. These remain limitations; no new qualification rule was introduced here.
- Keepalive: identical completed Ohmigo requests preserve established_at and update only last_dispatched_at. Genuine command changes reset the stability anchor. HA service completion still does not establish physical application.
- Execution: fitting uses an isolated grid snapshot in HA's executor, after physical dispatch; no control path awaits fit completion. Existing threaded tests pass. CPU contention/GIL effects on a real HA host have not been measured. Snapshot creation and diagnostic collection still run on the event loop.
- Persistence/capture: finalized model history and bounded observations survive; open forecasts do not. The fixes above prevent two continuity errors. Remaining capture limitations are described below.

## Fundamental limitation: collecting experience is not identifying a response

Normal PI, preheat and transitions are recorded in `_observations`, but only near-full braking enters `model.add_sample`. This cannot normally identify both command gain and background gain because command variation is too small. The excitation rejection is useful protection, not a learning solution. `fit_trusted` and `active_control` remain false unconditionally.

The coordinate is an ordinal virtual-outdoor request, not delivered heat. Outdoor normalization can vary that coordinate even with a fixed Ohmigo command. Correlation with outdoor conditions and PI feedback can produce apparent excitation without independently identifying command response. Radiator-oriented priors, gain bounds and three parameters do not make that response universal across houses.

The RTM grid also lacks regime boundaries: retained braking samples can bridge a brief non-braking interval if it is within the hold limit. Simply permitting every mode to train would compound that issue. Source capture uses the newest HA report for the aggregate, which may belong to an unchanged republishing contributor; observation clocks therefore do not establish simultaneous physical samples. The per-component age fix does not solve physical provenance.

Scores are grouped by source identity, not a frozen model revision. Pooled scores across refits describe the changing prediction process, not independent proof for the current coefficients. Internal holdout selects delay/fallback decisions using the full dataset before fitting the training split; it should not be presented as strictly independent forecast evidence.

Bounded output history includes not-due events. Its retention is a record count, not a guaranteed number of days. Forecast/evidence fields should not be interpreted as a complete physical command log.

## Architectural decision required before broader learning

Preferred direction: keep one empirical command-response model and one chronological capture path. Use the existing RTM Sample/excluded/grid mechanisms to preserve real command changes separately from indoor changes; mark unknown time, restarts, unavailable outputs and discontinuities explicitly. Capture normal PI, preheat and braking with mode labels, then evaluate future forecasts separately by those contexts. Training needs complete causal command trajectories including response delay, rather than unrelated endpoint samples relabeled as heating effort. Natural PI/preheat variation may help, but cannot guarantee identifiability; collecting qualified raw experience must remain possible without producing parameters.

Alternative if verified heat activity already exists: reuse PilotPassive/Heat Pump Pilot's interval collection and validation for that source. Do not turn compressor state or electrical power into delivered heat, and do not make a new sensor mandatory. Adopting that model would require retiring the redundant RTM learner rather than running both as competing learners.

No broadening of training was implemented: choosing the input semantics and boundary handling is the larger design decision requested by the user.

## Reuse and simplification

Reuse RTM's grid, excluded observations, snapshot fitting and frozen prediction math; reuse PumpSteer's source qualification, Store and ForecastAudit/persistence comparison; reuse Heat Pump Pilot's distinction between request and heat indication. Adaptive Preheat's preheat/coast optimization, shared priors and trust heuristics are not authorization for passive PumpSteer evidence.

After choosing one learner, retire duplicated collectors/audits and unused imported planning interfaces. Do not remove them opportunistically in this bugfix. Existing ThermalModel, PilotPassive and RTM are currently separate learning paths.

## Verification and field decision

Baseline: 1484 tests passed. Final suite: 1492 tests passed. Targeted runtime/hardening/keepalive/review set: 41 passed. Ruff F/E9 for runtime and new regressions and git diff --check passed. Existing pytest asyncio configuration warning remains.

Core PI, price control, comfort, Relay Guard, Generic Output and Ohmigo implementations are unchanged relative to the reviewed parent; sensor changes only add the guarded diagnostic SAFE notification. Production already differs from the prototype through its diagnostic hooks; full equivalence to production is not claimed from one parity case.

NO-GO for an installation intended to demonstrate a learnable, universal thermal response. The branch is suitable for further offline review. A passive capture-only field trial could be considered after the single-path input/continuity decision; at present bridging, timestamp attribution, pooled model scores and data retention still limit the evidential value. No active adaptive braking or production installation is authorized or performed.
