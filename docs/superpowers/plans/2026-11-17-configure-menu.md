# Four-page Configure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved four-page native Configure menu without changing settings semantics or control behavior.

**Architecture:** Four direct OptionsFlow steps share the existing field factory,
merge logic and validators. Page-owned section sets limit submitted/cleared fields;
storage remains flat. Existing validation scopes remain independent of the UI.

**Tech Stack:** Python, native HA OptionsFlow/selectors/sections, pytest, HA Core 2026.11.1.

**Spec:** `docs/CONFIGURE_MENU_20261117.md` (approved user design).

## Global Constraints

- Exactly 94 existing fields; page counts 22/2/60/10.
- Preserve defaults, entity IDs, storage precedence, validation and verification.
- No changes to control logic, runtime, Store format, production or HA lifecycle.
- No push, installation or activation; leave a local reviewable branch.
- Basic and passive TI require no manufacturer integration or internal telemetry.

## Review Focus

- A hidden zone recipient must survive saving basic price notifications.
- Cross-page source changes must revoke verification and expose relevant errors.
- No-op saves preserve implicit defaults, integer/select types and unknown keys.
- Stale client submissions cannot edit another page or silently clear its values.
- Cancellation, failed validation and re-opening must not write partial settings.

### Task 1: Menu, scoped saving and translations

**Files:** Modify `custom_components/pumpsteer/options_flow.py`, `strings.json`,
`translations/en.json`, `translations/sv.json`, `tests/test_configure_form.py`,
`tests/options_form_helpers.py`; add `tests/test_configure_menu.py`.

**Interfaces:** Four `async_step_basic/thermal/diagnostics/control(user_input=None)`
methods delegate to `_async_step_page(page, user_input)`. `async_step_init` only
shows the menu. `_PAGE_SECTIONS` owns fields; `_FORM_SECTIONS` assigns them once.

- [ ] Add failing tests for menu, field coverage, no-write navigation, page saves,
  hidden recipient, explicit clearing, source verification, visible errors and
  telemetry-free TI. Run and observe feature failures.
- [ ] Implement only page routing/scoping; retain `_fields`, `_validate_entities`
  and `_revoke_changed_verification` bodies. Adapt existing tests to real pages;
  cross-page assertions save sequentially, not through a retired all-page route.
- [ ] Translate all page/section keys and shorten nonessential explanations.
  Verify every field is translated on its single owning page.
- [ ] Run all options/configure regressions; expect no failures. Commit feature.

### Task 2: Native HA navigation and completion evidence

**Files:** Update `tests/probes/check_configure_native_ha.py`; add native navigation
probe if needed; complete `docs/CONFIGURE_MENU_20261117.md`.

**Interfaces:** Actual Core 2026.11.1 flow manager drives the four menu steps;
native schema serialization/validation submits all currently displayed fields.

- [ ] Add native checks of menu routing, cancelled/reopened flows, sequential
  page saves, no-op and clearing, 94 fields and cross-page verification.
- [ ] Run native tests without starting HA, calling services or writing production.
  Expect PASS with actual HA Core 2026.11.1.
- [ ] Run full regression, lint changed files, compare runtime files and validator
  ASTs with base; expect green suite and no runtime differences.
- [ ] Obtain one fresh read-only whole-branch review; address important findings
  with reproducing tests. Record test counts, limitations and final local commit.
