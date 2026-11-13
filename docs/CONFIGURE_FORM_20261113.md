# One native Configure form

Base: `36f4ca15c933215cf02e4b6fef6592e6a139cf46`.
Branch: `feat/configure-sections-20261113`. Local implementation only.
The independent v2.2.8 Base adaptation is not included.

## Interface

Configure opens one HA form: five existing basic source fields and ten native,
initially collapsed sections. No menus, custom frontend, dashboard or additional
entities. First setup retains its five fields; Swedish labels are consistent.

1. Inomhusgivare
2. Utomhusgivare
3. Elpris idag
4. Elpris imorgon
5. Väderprognos (valfri)

Expandable sections, in order:

- Fler temperaturgivare och givarnas aktualitet
- Styrutgångar – Ohmigo och Generic Output
- Notiser (distinct price and shared zone recipients)
- Extra temperaturzon 1
- Extra temperaturzon 2
- Extra temperaturzon 3
- Extra temperaturzon 4
- Thermal Intelligence och adaptiv bromsning
- Värmepumpsdata och energimätning
- Avancerade kontrollfunktioner

All 94 existing fields remain available, with existing selector limits. Expanding
a section never enables a feature. Native HA determines the submit-button wording
(the tested Swedish frontend displays **Bekräfta**).

## Saving contract

- Storage remains flat; section names are never stored as settings.
- `options` still takes precedence over `data`. Unknown stored keys survive.
- No-op saves retain existing option keys and types, without copying `data` or
  displayed defaults into `options`.
- A completely omitted section is untouched. Native HA initializes its child
  values even when collapsed and submits their current values.
- Inside a submitted section, removing an optional selector omits that field.
  That clears only the existing clearable value in that section. Explicit empty
  values clear too; energy-meter selections use `[]` as before. Blank suggestions
  are omitted so HA does not send invalid empty entity IDs inside nested sections.
- Valid `False`, zero and empty-list values retain their meaning.
- A dynamically inferred default is persisted only when submitted unchanged but
  other edits would otherwise change its meaning (notably heat detection mode).
- Validation errors retain the entire submitted draft. A section error appears
  above the affected, automatically expanded section; basic fields retain field
  errors. HA does not render invented section/field error paths.
- Existing validation functions are AST-identical. Their original logical scopes
  remain independent of visual grouping: disabling one function remains possible
  despite an unrelated unavailable sensor or pre-existing configuration error.
- Source/calibration changes still revoke the corresponding previous verification,
  including when the same submission contains its old checked verification flag.

The obsolete menu routes are removed. Existing validation fixtures now submit
through the real single-form entry point; a test helper only fills neighbouring
fixture fields to match HA's full-section submission behaviour.

## Verification

Baseline: 1632 passed, 21 optional RoomMind tests skipped. With the pinned external
RoomMind checkout enabled, final regression: **1666 passed, no skipped tests** (50.66 seconds).

```sh
PUMPSTEER_ROOMMIND_ROOT=/path/to/pinned/roommind python -m pytest -q
python tests/probes/check_configure_native_ha.py
```

The optional probe requires a separate real-HA environment. It imports the actual
HA OptionsFlow, selectors, section validators and serialization; it does not start
HA or contact an installation. Tested with HA 2025.12.5 and frontend 20251203.3
(Python 3.13; test environment uses the available lru-dict 1.4.1 wheel).
It covers unchanged saves, stored integer/select display conversion, clearing,
error expansion and aliases when the first heat signal is selected.

Ruff passes for changed production code, new tests/probe, options and zone UI
regressions; formatting and whitespace checks pass. The existing large pilot
regression file and HA stubs still have unrelated legacy UTC/style warnings;
these are not part of the UI change (41 existing UP017 and one PIE808).

Internal read-only review found a dynamic-default/alias bug during implementation;
two regressions reproduced it before correction. Native-HA checks also reproduced
and caught an empty-entity suggestion problem before correction. Both are fixed.
No control, output, model or safety-rule implementation changed. ConfigFlow Python,
all entity implementations and the version manifest remain byte-identical to base.

## Native frontend check

Screenshots use an isolated local HA instance with a minimal preview integration
containing these exact form files and translations. All entities and entries are
synthetic; no PumpSteer control platforms or physical outputs are loaded. The
browser harness supplies an empty service catalogue and inactive Recorder metadata
so unrelated optional HA components need not run. The form, selectors, flow
submission and validation use HA's actual frontend/backend implementations.

See `ui/configure-collapsed.png`, `ui/configure-expanded.png` and
`ui/configure-mobile.png`. These are actual renders, not drawings or production
screenshots. The tall desktop capture includes the entire collapsed form; normal
screens scroll the same dialog. Expanded advanced sections naturally need scrolling.

Browser checks passed against the isolated synthetic entry: unchanged save retained
empty options; changing the notification recipient wrote only that key; clearing it
saved an explicit empty override; duplicate temperature sources produced a visible
section error without saving. Mobile rendering at 390 x 844 had no horizontal page
overflow. Browser checks reload between cases. No real integration was installed.
