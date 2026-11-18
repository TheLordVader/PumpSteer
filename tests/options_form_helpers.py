"""Submit existing validation fixtures through their real native Configure form.

The old fixtures cover smaller field groups than the new visual sections. Include
existing values from the rest of that visual section, just as HA does, so a fixture
for one group cannot accidentally simulate clearing its neighbours.
"""

from custom_components.pumpsteer.options_flow import (
    _BASIC_FIELDS,
    _CLEARABLE,
    _FORM_SECTIONS,
    _GROUPS,
)


async def submit_group(flow, group, values=None):
    keys = _GROUPS[group]
    if group == "thermal":
        keys = keys - {"adaptive_braking_enabled"}
    section = next(
        (name for name, owned in _FORM_SECTIONS.items() if keys <= owned), None
    )
    step = flow.async_step_init
    if values is None:
        result = await step()
        if section:
            result = {**result, "data_schema": result["data_schema"][section].schema}
        return result
    current = {**flow.config_entry.data, **flow.config_entry.options}
    rest = {
        key: value
        for key, value in current.items()
        if section and key in _FORM_SECTIONS[section] - keys
    }
    submitted = {**rest, **values}
    # Only these legacy groups intentionally model a user clearing omitted fields.
    for key in keys & _CLEARABLE:
        if key not in values and current.get(key) not in (None, "", []):
            submitted[key] = [] if key == "thermal_energy_entities" else ""
    payload = (
        {section: submitted}
        if section
        else {
            **{key: current[key] for key in _BASIC_FIELDS if key in current},
            **submitted,
        }
    )
    result = await step(payload)
    if "errors" in result:
        result = {
            **result,
            "errors": {
                key.rsplit("/", 1)[-1]: value for key, value in result["errors"].items()
            },
        }
    return result


def schema_marker(schema, name):
    return next(key for key in schema if key.schema == name)
