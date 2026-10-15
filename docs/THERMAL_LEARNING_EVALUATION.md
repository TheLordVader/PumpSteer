# Explainable learning: offline evaluation before adaptation

The runtime diagnostic API preserves forecast evidence and version-specific delayed
evaluation. The separate helper `tests/thermal_learning_experiment.py` is a finite,
synthetic shadow experiment. No learned parameter enters ThermalModel or control.
There is no HA adapter, database, persistent state, new dependency or runtime task.

## What existing signals can identify

An indoor temperature trace and measured outdoor temperature reveal **net observed
response**. With authentic timing and independent episodes they can describe net
cooling/recovery rates and conditional errors across operating states and weather.
Mode/brake-factor commands explain requested operation, not actual delivered heat.

A useful balance equation is:

`dTin/dt = -k*(Tin-Tout) + q_heat + q_residual + q_solar + q_other`

The q terms are effective degrees per hour, not measured watts. If heating is unknown,
any changed k can be offset by changed heat input: `q' = q+(k'-k)*(Tin-Tout)` produces
the same measured trajectory. Physical envelope loss and heat input are therefore
not separately identifiable from these temperatures alone. Observed braking cooling
must not be labelled passive building loss. A fitted k may be an effective coefficient
for the observed operating distribution rather than a physical building parameter.

| Quantity | Existing evidence | Additional optional evidence that could help |
| --- | --- | --- |
| Net cooling and recovery | Indoor/outdoor values with real observation times | Reliable source cadence, indoor sensor precision/location |
| Outdoor dependence | Independent episodes spanning weather and operating states | Already available wind/solar measurements with provenance; no cloud requirement |
| Residual radiator heat | Post-transition response is suggestive but confounded | Supply/return/radiator temperatures, pump/circulation state, time since heating |
| Delivered heating input | Brake/preheat commands are insufficient | Existing thermal-energy meter or flow plus supply/return delta; electrical power alone needs a qualified COP model |
| Braking/recovery differences | Mode and actual brake-factor/time provenance | Compressor/valve status and observed transitions; command strength is not delivered heat |

These are optional signals, not hardware requirements. If absent, retain conditional
empirical predictions and say what is unknown. Do not invent zeros or calibrate
separate radiator/envelope parameters from an underdetermined observation set.

## Small justified experiment

Calibrate nine mean residual offsets: one per initial mode (`braking`, `normal`,
`preheating`) and horizon (30/60/120). Add each frozen offset to the passive reference.
This is observational bias calibration, **not** a fitted radiator model or improved
physical k. At least two distinct whole episodes per cell are required. Training is
limited to twelve episodes. The reference coefficient is fixed at 0.05/h; incompatible
coefficients are explicitly rejected. No correction is transferred to a revised k.
This keeps the experiment small and prevents silently mixed parameter populations.

Four predictors receive identical issuance data and future target availability:

1. Existing `ThermalModel.predict_drop` linear helper on an experiment-local model.
2. Exact passive reference from `ThermalDiagnostics`, k=0.05/h.
3. Recent trend from the two real past/origin observations ten minutes apart.
4. The reference plus frozen mode/horizon residual correction.

The simulator has known envelope k=0.04/h, one-minute Euler integration, changing
outdoor temperature, additive bounded deterministic sensor noise, decaying residual
radiator heat and changes between reduced heating, recovery and normal/preheat input.
Input values are synthetic scenario assumptions, not empirical Swedish-house bounds.
Future heat/outdoor trajectories and ground-truth k are hidden from all predictors.
Only five sensor points per episode are retained; minute integration is not evidence
of independent sample count.

Training days 0–11 contain four independent episodes per mode. Freeze parameters,
then evaluate unused days 12–23. One origin per episode supplies three correlated
horizons; do not count them as independent houses or independent confidence evidence.
The regular validation shares the training input distribution by design. Additional
unused days 24–26 remove supplied/residual heat and add a cold front, deliberately
challenging transferability. No test episode is used to retune corrections.

## Measured synthetic results

These are descriptive paired results, aggregated across modes for readability.
Full output also reports every mode/horizon/method separately. Errors are actual minus
forecast. Missing targets remain in the eligible denominator, never become zero errors.

| Horizon | Eligible/evaluated | Linear MAE | Reference MAE | Trend MAE | Calibrated MAE | Calibrated bias |
| --- | --- | --- | --- | --- | --- | --- |
| 30 min | 12/12 | 0.5898 | 0.5843 | 0.2089 | 0.0278 | +0.0024 |
| 60 min | 12/10 | 1.0945 | 1.0724 | 0.4074 | 0.0473 | +0.0122 |
| 120 min | 12/12 | 1.9589 | 1.8726 | 0.6488 | 0.0474 | +0.0042 |

Temperatures/errors are degrees C. Coverage is 100%, 83.33%, 100%, respectively,
for all four predictors. The correction improved accuracy on these unused episodes
from the same synthetic distribution. This supports only that narrow statement.

| Shifted horizon | Eligible/evaluated | Linear MAE | Reference MAE | Trend MAE | Calibrated MAE | Calibrated bias |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 30 min | 3/3 | 0.0751 | 0.0696 | 0.3504 | 0.5123 | -0.5123 |
| 60 min | 3/3 | 0.1366 | 0.1147 | 0.7144 | 0.9489 | -0.9489 |
| 120 min | 3/3 | 0.1990 | 0.1130 | 1.5031 | 1.7554 | -1.7554 |

Coverage is 100% for all shifted cases. Adaptation was substantially worse when heat
input changed: the mode tag alone did not establish transferable conditions. There
is no evidence of general superiority or real-world learning accuracy. No confidence
interval is justified by this tiny, correlated, deterministic synthetic dataset.

## Reproducibility and next learning gate

Run `python3 -m pytest -p no:cacheprovider -s -q tests/test_thermal_learning_experiment.py`
with the project's test dependencies available. The test prints complete JSON results.
Tests verify known-bias recovery on unused episodes, independent IDs, limited training,
incompatible-k rejection, missing-target coverage and a case where learning hurts.
Evaluation regressions separately prove long horizons survive changing k and sparse
sensors; the offline correction experiment intentionally calibrates a fixed reference.

A next local step is an episode specification: authentic sample/transition times,
requested mode/brake factor, known/unknown heat-input evidence, minimum coverage,
weather coverage, version/run provenance and explicit rejection reasons. Keep distinct
cooling and recovery episodes; split chronological blocks before choosing parameters.
Do not treat adjacent minute windows or overlapping horizons as independent episodes.

Only after trusted observational evidence exists should a separate shadow candidate
try net-rate or residual corrections. Preserve a frozen incumbent and trend baseline,
report paired errors/coverage per horizon and condition, detect distribution changes,
and abstain when the regime/coefficient lacks validated history. An experiment that
wins only under recurring synthetic heating schedules must not tune live k or PI.
A two-state radiator model should wait for signals that constrain its extra parameters.

No continuous production learner, persistent learner state or automatic tuning is
implemented. All hardware, comfort protection, price decisions and outputs remain
outside this work. Publication and production adoption are separate decisions.
