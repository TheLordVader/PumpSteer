# Single Configure form implementation plan

**Goal:** Replace the Configure menu tree with one native HA form; leave first setup and active control unchanged.
**Architecture:** Reuse existing selectors and validation. Present five base fields and flat expandable sections. Store flat option keys; sections are presentation only.
**Base:** 36f4ca15c933215cf02e4b6fef6592e6a139cf46; isolated branch feat/configure-sections-20261113.
**Specification:** Approved before/after design in the conversation, 2026-11-13.

## Constraints and review focus
- Preserve keys, defaults, values, entities and options-over-data precedence; do not materialize untouched defaults into options.
- An omitted section is untouched. HA sends a section's existing suggested values even when collapsed; removing an optional selector omits that field in the submitted section. Explicit empty values also clear optional fields.
- Keep validation, source/calibration verification revocation, and ability to disable a feature despite an unrelated unavailable sensor.
- Preserve edits and show the affected section after a validation error.
- No production access, installation, push, version change or control-code change.

## Tasks
- [x] Add failing tests for native section layout, unchanged saves, omitted sections, explicit clearing, validation retries, and safety verification.
- [x] Replace menu routing with one form, reuse existing field definitions and validator, flatten only known keys and safely merge actual changes.
- [x] Move existing labels/help into the new section translation structure; improve Swedish wording and preserve all field coverage.
- [x] Adapt existing options tests, run focused tests, full regression, Ruff and real-HA schema/frontend smoke checks where available.
- [x] Document exact UI and verification; review the final diff, create a local commit and report limitations.

## Baseline
1632 passed, 21 skipped (optional RoomMind source not configured), 46.93 seconds in /tmp/ps-offline-venv. Existing pytest-asyncio loop-scope deprecation warning.

## Results and implementation rulings
- Original validation scopes remain separate from visual sections so disabling one function is not blocked by an unrelated existing error.
- Submitted dynamic defaults are retained when another edit would change their meaning; unchanged saves still do not materialise defaults.
- Native HA renders section-level errors, not arbitrary section/field paths.
- Empty suggestions are omitted: HA otherwise submits invalid empty nested entity IDs.
- Final full regression with pinned RoomMind: 1666 passed in 50.66 seconds.
- Real HA selector/section/serialization probe passes; internal read-only review has no remaining important finding.
- No production access, push or installation. Exact final commit is recorded by git.
