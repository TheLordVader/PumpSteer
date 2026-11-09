# Shared passive Thermal Intelligence evidence chain — 2026-11-09

Base: b762296d7cac6054b603a8d53d308b82cb14c4f8. Production reference: 5100ed83d1ff7ac643754a65460d8aa63e1265c5. No production calls, Recorder replays, installations or physical experiments were used.

## Implemented data path

ThermalIntelligence owns one chronological v2 event ledger. Independent events describe changed indoor/outdoor source values, aggregate temperature observations, operating context, completed output changes and interruptions. Sequence and capture time order the ledger; original report timestamps and quality remain separate. Unknown physical measurement time and unknown delivered heat are explicit. Calculated virtual outdoor, a completed HA service request and verified physical application are distinct: the last is never inferred.

Every relevant mode is captured, including normal PI, preheat, braking, transitions and unknown operation. Recording does not depend on fit availability, heat sensors, command qualification or model trust. Identical numerical republications do not become new temperature observations. Unordered, future and stale reports can remain quality-labelled source evidence but do not become accepted model observations. Weighted observations use real changed-source report time, not the newest unchanged republisher's clock.

Command-only Sample events enter the existing RTM grid when actual requests change, even if indoor temperature is unchanged. Indoor and outdoor Samples retain independent times. Dispatch coordinates are frozen at dispatch: changing weather does not manufacture command variation. Identical keepalives retain established_at and periodically refresh command coverage, without creating new command-change events. Not-due/attempt-only chatter does not flood the ledger.

Mode/regime, source/weights, receiver identity, GOS service/template identity and coordinate span changes break segments. SAFE, restart, unknown mode, lost output and significant gaps break continuity. Grid exclusions clear held indoor/outdoor/command fields; delayed regressors cannot cross those boundaries. A valid held outdoor context can seed a new segment with its original report provenance, without becoming a new measurement. Late reports older than finalized grid rows remain ledger evidence and cannot be shifted into a later model row.

## One passive candidate and one audit

RTM is the live passive model candidate. Its fitting dataset excludes unknown/partial command coverage and future rows. Each delay candidate requires usable continuous history and independent command variation. Fit scheduling counts genuinely new usable regression rows, including backfilled usable rows, rather than calendar/grid advance. Failed attempts record their evidence fingerprint, preventing retries on unchanged input. The existing range/variance requirements are reused; prior regularization cannot substitute for identification. Insufficient data or excitation leaves collection running. Summary exposes model_status=insufficient_evidence; every candidate remains fit_trusted=false and active_control=false.

The old private RTM pending-forecast, scores and forecast persistence were removed. Existing ForecastAudit now freezes persistence before any fit, and optionally the empirical RTM curve, for 30/60/120 minutes. Actual reported endpoints are common to all methods, with the existing five-minute endpoint window. Context/source/command interruptions invalidate pending curves; completed outcomes survive in fixed_v2 in the existing Store. Parameters are copied at issuance and never read from a later model during evaluation. The live audit consumes accepted numeric changes only. Flat periods without independently established new readings are unavailable; MAE/skill therefore describe those available changed-value endpoints and cannot establish superiority over persistence for all periods. Results are grouped by frozen revision and horizon; pooled historical accuracy cannot qualify a newer model. Internal retrospective RTM holdout is explicitly labelled evaluation_independent=false.

The separate live Pilot house/pump learning and hypothetical planner were retired from the foreground sensor path. Heat-mode, hot-water, alarm, price and other valuable diagnostics remain. Legacy offline planner/model APIs and historical Store readers remain for compatibility and independent comparisons; tests invoke them explicitly rather than treating them as a second live candidate. The older adaptive ThermalModel control behavior is preserved and is outside this passive candidate; this change never enables adaptive braking.

## Store, retention and lifecycle

The existing pumpsteer.pilot.<entry> HA Store holds the ledger and derived finalized RTM grid. No new database or independent history service exists. Ledger retention is bounded by 2048 events and 30 days, whichever is reached first. This is a maximum age, not a guarantee of 30 days' raw provenance: dense reporting shortens the retained window. The derived model grid is bounded by the existing 30-day history.

Thermal-only checkpoints are requested at most once per five minutes, with the existing 30-second HA delayed save. Other existing diagnostics retain their own save behavior. Graceful unload flushes the latest ledger, including events after the last checkpoint. A crash can lose the most recent checkpoint interval; open model windows, commands and predictions never resume as known observations. v2 restore is transactional, validates ordered events and preserves already-consumed source values. Incompatible prototype v1 RTM state is rejected, not promoted into new evidence. Existing unrelated Store fields are preserved.

## Verification and measured cost

New regressions cover independent command/temperature capture, quantized republications, keepalive age and flood resistance, all context boundaries, source identity on restart, genuine new fit evidence, delayed input gaps, rank-deficient inputs, corrupt stores, frozen forecast parameters, actual endpoint times, persistence before fit, Store roundtrip and write batching. Real offline HA controller tests retain Base/PI state and Ohmigo/GOS service-call parity. Existing direct legacy planner/cache/DST tests remain as explicit offline comparisons.

Local synthetic benchmark (not Recorder replay): 2880 five-minute cycles with changing quantized temperatures/commands, ledger at its 2048-event cap. Capture median 0.045 ms, p95 0.112 ms, maximum 0.275 ms; complete exported JSON 850569 bytes; export plus JSON encoding 24.7 ms. This measures this development host, not production HA latency or device acknowledgement. Thermal-only checkpoints on a comparable full ledger imply roughly 245 MB/day of rewritten JSON at the five-minute maximum; actual write rate depends on event/other diagnostic activity. Retention and checkpoint tradeoffs are explicit.

A 30-day/2880-row scheduling benchmark measured ~2.9 ms when checking eligible data and ~0.001 ms within cooldown. A sparse synthetic fit took ~0.18 s in executor. Fitting is scheduled after physical dispatch, uses copied grid state and never runs synchronously in the HA control coroutine. Snapshot and Store preparation still consume bounded event-loop time; real-host latency is not proven by these measurements.

A separate 10000-cycle realistic keepalive/republication test keeps event count stable and exported history below 8 KB when no new temperature or command information arrives.

Internal final review reproduced a two-hour capture-gap outcome crossing expired command coverage. The new capture-gap/clock-rollback boundary cancels that outcome; its failing-then-passing controller regression is retained.

Verification: 81 targeted tests passed in 0.91 s; full regression 1532 tests passed in 44.05 s. Existing pytest asyncio configuration warning remains. Exact commit is reported in the delivery message. Test commands: PYTHONPATH=/tmp/pumpsteer-python-deps python3 -m pytest tests/ -q and targeted thermal evidence/audit/fit/keepalive files. Ruff F/E9 and git diff --check cover modified implementation/new tests. Existing pytest asyncio configuration warning remains.

## Field decision and limits

GO for a limited passive evidence-capture field trial, subject to separate installation approval. It can gather useful future observations even when no candidate is fitted. Review actual checkpoint size, retained time window and HA latency during that trial; no model usefulness is assumed.

NO-GO for adaptive control. Natural command variation may still be insufficient or correlated with weather/PI feedback. The ordinal command has no calibrated relationship to delivered heat; external gains are generally unknown. Sparse quantized temperatures can bias inferred response delay (a synthetic zero delay was estimated as one 15-minute grid step). Imported radiator-oriented priors and parameter bounds remain estimation assumptions, never evidence or authorization. Future usefulness must be shown by frozen forecasts beating persistence on later, genuinely available outcomes across the relevant operating contexts.
