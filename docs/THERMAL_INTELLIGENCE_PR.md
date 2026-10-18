# Proposed title

Add passive braking forecasts and observed-error diagnostics to ThermalModel

# Proposed PR description

PumpSteer already learns a cooling coefficient during braking, but it does not
compare conditional forecasts with subsequent observed temperatures. Its old
attribute restore also allowed the fallback coefficient to become a learned
coefficient. This change connects the existing learner to bounded diagnostic
forecast evaluation and restores only explicitly valid learned parameters.

During qualified braking, the existing output sensor exposes conditional
30/60/120-minute temperature changes, actual changes, forecast error, valid/missed
counts and descriptive quality. Each comparison retains the original fitted
coefficient and revision. Missing or interrupted evidence produces no invented
outcome. Valid parameters persist through the existing sensor extra-data mechanism;
unfinished observation series and old diagnostic results do not become new data
after startup or reload. Legacy validity without at least 20 fit samples is
ambiguous because older code could mark fallback values valid; it requires new
learning.

The deterministic evaluator reuses the earlier local Thermal Intelligence work.
No episode framework, adaptive control, extra sensor, timer, polling loop, service,
cloud dependency or storage system is introduced. PI, price control, comfort
protection, brake settings and Ohmigo output remain unchanged. This candidate is
based directly on upstream main `78cbd853` and is separate from PRs #279/#280;
neither temperature-control fix is duplicated or required here.

Limitations: distinct post-start HA reports are checked, including explicit
restore flags, units, age and continuity. `last_reported` proves HA reception,
not physical measurement age; unmarked re-reports of cached values cannot be
detected. An optional existing on/off external-heat entity pauses learning and breaks
observations while active, unknown or restored; after the first observed off,
new temperature reports are required. No entity is selected by default.
Ten-minute reports now work within an explicit 15-minute report/history limit;
repeated polling never invents samples. The HA minimum is 2024.4 and startup
qualification uses actual event time with conservative fallback.
Braking does not prove zero heat input. Residual radiator heat, sun and
outdoor changes remain unmodelled, so quality is descriptive and conditional,
never a control/safety guarantee. No PI or brake tuning follows from these scores.

Validation:

- Upstream baseline: 292 tests passed; candidate follow-up: 432 tests passed.
- New tests first failed for qualification/persistence/bounds; review findings
  (source changes mixing series, historical fit counts and ambiguous legacy restore) were reproduced and fixed.
- Ruff, targeted Ruff format, fatal/import Flake8, version check and diff check passed.
- Control AST comparison and 20 synthetic output cases match upstream control.
- Real isolated HA Core 2026.10.1: actual template/integration reload, disk-backed
  ordinary restart and restart after four synthetic days; fitted `k` and count
  retained, unfinished samples discarded, 30-minute comparison observed, 24
  simulated receiver dispatches and zero PumpSteer error logs. The corrected
  runtime also passes real HA external-heat options, on/unknown/off and recovery.
- The Core probe uses synthetic time/inputs/receiver. Optional unrelated HA
  components lacked dependencies in the reused environment; it is not a full
  all-integrations HA validation or a production test. Current Hassfest/HACS results are recorded in the validation receipt.
- Internal separate code review completed; no external independent verification
  is claimed. Evidence and limitations are in `docs/thermal-validation-20261018/`.

This branch publication performs no production installation and creates no PR.
