# Configure: basic settings and optional advanced pages

Approved scope: one native Home Assistant menu with four direct pages, based on
`89cdc7afd682b6c83a5b7eda5b0cb86568548dd6`. No control or runtime changes.

| Page | Existing fields | Purpose |
| --- | ---: | --- |
| Basic settings | 22 | All 14 v2 source/output/notification options, plus eight existing optional source/age settings |
| Thermal Intelligence | 2 | Passive evidence collection and possible external heat sources |
| Diagnostics and energy | 60 | Optional generic telemetry roles, energy and four monitored zones |
| Advanced control | 10 | Existing control permissions/calibration, including adaptive braking |

Use native menus, selectors and expandable sections. The single sections on the
TI and control pages open expanded. Basic and diagnostic detail sections remain
collapsed. There is no nested menu or section, custom frontend or UI enable flag.
Opening a page does not enable a feature. Basic operation and passive TI require
no manufacturer-specific integration or internal heat-pump telemetry.

Each field has one editing location. Storage remains flat; entity IDs, selectors,
defaults, `options` precedence, unknown stored keys and implicit defaults survive.
Only clearable fields actually displayed on the submitted page may be cleared.
Omitted pages/sections are untouched. Explicit clearing remains possible.
Validation and source/calibration verification rules remain unchanged.

Save finishes the selected page and the flow. To edit another category, reopen
Configure. Cancel/back does not save edits. There is no multi-page pending draft.
Errors retain the page draft; an error concerning another page is presented as
an actionable visible error rather than a nonexistent field.

Labels use generic roles such as space-heating mode, DHW mode, compressor,
supply/return and electrical power/energy. Neither manufacturer identity nor
entity names imply physical heat delivery, polarity or a physical timestamp.
Keep concise safety explanations; remove repeated general instructions.

## Verified behavior

- Menu and page tests were run against the original single-form version and failed
  before implementation. The native manager probe likewise fails on `89cdc7a`.
- **1,689 tests passed** in the Python 3.14 / HA Core 2026.11.1 environment
  (42.90 seconds); also 1,689 passed in the existing Python 3.13 test environment.
  The ordinary unit suite uses the project's HA stubs. Native checks are separate.
- **138 focused options/UI tests passed**, covering page ownership, no-op saves,
  independent flags, intentional clearing, retained drafts and visible errors.
- The actual **HA Core 2026.11.1 OptionsFlowManager** passes menu selection,
  cancellation, re-opening, unchanged saves on all pages and sequential page saves.
  Native selectors/schema serialization submit all 94 existing fields exactly once.
- Ohmigo, relay, Generic Output, energy selection and zone-recipient clearing do
  not change fields on another page. No-op saves preserve integer/select display
  conversions, implicit defaults, unknown options and `data`/`options` ownership.
- All three safety-verification families remain unchanged on no-op and are revoked
  on the same source/calibration changes as before. Cross-page validation errors
  appear as visible form errors and do not write partial options.
- Basic operation/passive TI configuration has no required internal heat-pump
  signal. Selecting a diagnostic source does not grant control permission.
- `_fields`, `_validate_entities` and `_revoke_changed_verification` are
  AST-identical to `89cdc7a`. Only OptionsFlow routing/scoping and UI translations
  changed under `custom_components`; control, outputs, entities, Store and manifest
  remain byte-identical. No stored-key migration or new dependencies.

Run the full unit regression using the project's existing test dependencies:

```sh
python -m pytest -q
```

Run the native probe in the isolated HA Core 2026.11.1 Python environment:

```sh
python tests/probes/check_configure_native_ha.py
```

The native probe instantiates HA objects but never starts HA, loads output
platforms, calls services, reloads an integration or writes Store. Only integration
loader lookup and config-entry persistence are synthetic boundaries. This checks
the actual backend navigation/schema contract; it is not a browser screenshot or
a production test. Production installation and first-load checks require separate GO.
