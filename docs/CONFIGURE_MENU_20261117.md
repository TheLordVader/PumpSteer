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

Verification results will be recorded here before completion. Existing isolated
tests use HA stubs; actual Core 2026.11.1 flow/selector/schema checks run separately
without starting Home Assistant or contacting production.
