# Thermal Intelligence validation summary — 2026-10-18

Publication branch: `feature/thermal-intelligence-review-20261018`.
Baseline: JohanAlvedal/PumpSteer upstream main
`78cbd853497c4ee84c2e53cff3b29a2461b0b026` (version 2.2.3).

Runtime code and tests are byte-identical to the previously verified local
candidate. That original branch, commit and raw test evidence remain preserved
locally. The publication starts directly from the public upstream baseline;
the unsanitized local commit is not an ancestor. No configuration, credentials,
production observations, raw Core logs or machine-specific paths are included.

## Reused implementation

The reviewed pure evaluator and its tests were reused from earlier Thermal
Intelligence work. Existing `ThermalModel` least-squares fitting and brake-end
fitting points remain. The candidate adds qualified observations, bounded training
storage, passive evaluation and strict restore. Experimental episode/reliability
research modules and their framework are excluded. PRs #279 and #280 are separate;
neither patch is duplicated or required, and neither PR is changed.

## Verified results

| Check | Observed result |
| --- | --- |
| Unmodified upstream baseline | 292 tests passed |
| Initial qualification/restore/bounds tests | 20 failed, 1 passed before implementation |
| Restored stale diagnostic attributes | Reproduced failure, then fixed |
| Source changes and historical fit counts | Three failing cases before fixes |
| Ambiguous legacy fallback restore | Five failures before guard; two valid-fit cases passed |
| Final complete offline suite | 413 tests passed |
| Ruff / targeted format / fatal and import Flake8 | Passed |
| Version and diff checks | Passed |
| Existing control/output comparison | 20 synthetic cases byte-identical |
| Control AST and unchanged modules | Matched upstream |
| Real isolated Core lifecycle probe | PASS, Core 2026.10.1 |

`final-checks.json` preserves results from the original candidate; command paths
are normalized to portable tool names and local tool-execution metadata is
omitted. Raw logs, including intermediate failing runs and the native Core log,
are preserved locally rather than published. No test result was changed.

The offline suite covers cooling fits, frozen forecast/outcome metadata,
insufficient/invalid/missing input, duplicate/out-of-order reports, missed targets,
bounded buffers, UTC/DST/manual clock reversal, interruptions, source changes,
fallback validity, legacy restore, safe-mode persistence and observer exceptions.

The standalone native probe loads the actual integration and four platforms,
uses real SQLite and HA restore files, and exercises actual template and
config-entry integration reload. Synthetic braking produces a fit from 27 samples
(`k = 0.03717385546315693`) and a subsequent 30-minute observed comparison.
Parameters survive normal disk restart and restart after four synthetic days.
Explicitly restored temperatures are rejected; unfinished samples are not restored.
Existing Ohmigo code made 24 calls to a simulated number service. External socket
connections were prohibited; PumpSteer logged no errors. Inputs, time and receiver
are synthetic, not a hardware safety trial.

The reused Core environment lacked optional unrelated frontend, conversation,
radio/IR and ffmpeg-related dependencies. The target integration loaded and all
target assertions passed. This is not a successful full HA configuration check or
an all-integrations validation. Hassfest was not run. The native run precedes only
the final legacy-restore eligibility guard, which was checked by seven offline
restore cases. The exercised new-format disk restore path is unchanged. Tested
native runtime hashes are in `native-runtime-sha256.json`; final runtime hashes
are in `runtime-sha256.json`.

## Portable reproduction commands

From a clean checkout, using an isolated Python 3.14 environment:

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install pytest==8.4.2 ruff==0.15.8 flake8
python -m pytest -q
ruff check custom_components/pumpsteer tests
ruff format --check custom_components/pumpsteer/thermal_model.py custom_components/pumpsteer/thermal_observer.py custom_components/pumpsteer/thermal_intelligence.py custom_components/pumpsteer/sensor.py tests/test_thermal_observer.py tests/test_thermal_intelligence.py tests/test_thermal_evaluation.py tests/test_thermal_measurement_hygiene.py tests/test_brake_provenance.py tests/thermal_core_probe.py
python -m flake8 custom_components/pumpsteer --select=E9,F63,F7,F82,F401
python scripts/check_version.py
git diff 78cbd853497c4ee84c2e53cff3b29a2461b0b026...HEAD --check
cmp docs/thermal-validation-20261018/output-base.json docs/thermal-validation-20261018/output-head.json
```

For the native probe, use a separate HA Core 2026.10.1 environment with its
matching custom-component pytest helpers. Do not run it against a live HA config:

```bash
python3.14 -m venv .venv-ha
. .venv-ha/bin/activate
python -m pip install homeassistant==2026.10.1 pytest-homeassistant-custom-component==0.13.372
PYTHONPATH="$PWD" python -B tests/thermal_core_probe.py
```

The probe creates and removes its own temporary configuration below
`.local-audit/`. No product dependencies are added. The exact reused Core test
environment is not packaged; its optional-dependency limitations remain above.

The two output snapshots come from a retained local comparison generator with
four configurations and five PI/precool/holiday cases each. That auxiliary script
is not part of the product or publication. The snapshots are narrower than a
complete production equivalence guarantee. Existing automated control tests,
including `tests/test_precool_pi_clock.py`, remain in the suite. A separate
observer-exception test covers diagnostics after control.

## Review, cost and limitations

An internal separate code review checked the exact upstream baseline, candidate,
requirements and evidence. Findings about mixed-source series, lost historical
fit counts and ambiguous legacy restore were reproduced and fixed. Final review
found no remaining concrete findings. The reviewer read results rather than
running tests; this is not external independent verification.

Runtime changes affect only `sensor.py`, `thermal_model.py` and two diagnostic
modules. No control/Ohmigo module, package, manifest, integration setup, option or
external dependency changes. No new entity or update schedule is introduced.
Existing Recorder rows gain diagnostic attributes.

`resources.json` records a synthetic 10,000-update timing measurement: about
52.7 microseconds/update, representative attribute JSON of 1,278 bytes, bounded
240 raw training points, seven pending forecasts in that workload and 192 resolved
records. This is not production load or a worst-case database estimate.

Physical sample age remains unknown. Fresh HA reception cannot distinguish a new
hardware measurement from an unmarked cached re-report. Residual heat and heating
input remain unknown. Model errors are conditional observations, not statistical
confidence or comfort protection. No automatic tuning or control follows.
See [the functional contract](../THERMAL_INTELLIGENCE.md).

## Publication and rollback

This is a sanitized branch for independent review. The [proposed PR text](../THERMAL_INTELLIGENCE_PR.md)
is prepared, but no PR, merge, release or production deployment is authorized by
this publication. Later installation should use an official reviewed integration
update and its supported reload/restart. Rollback restores the previous
integration version and uses a separately authorized lifecycle action. No new
configuration, database migration or temperature-source change is needed.
