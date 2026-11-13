import logging
import re

import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers.selector import selector

from .const import (
    DEFAULT_OUTDOOR_REPORT_MAX_AGE_MINUTES,
    DEFAULT_TEMPERATURE_REPORT_MAX_AGE_MINUTES,
    NOTIFICATION_COOLDOWN_CHOICES,
    NOTIFICATION_COOLDOWN_OPTION,
    NOTIFICATION_LEVEL_OPTION,
    NOTIFICATION_LEVELS,
    OHMIGO_RELAY_ENTITY,
)
from .utils import safe_float

_LOGGER = logging.getLogger(__name__)

DOMAIN = "pumpsteer"
_GROUPS = {
    "zones": {
        "zone_notify_service",
        *[
            f"zone_{i}_{field}"
            for i in range(1, 5)
            for field in (
                "name",
                "entity",
                "enabled",
                "profile",
                "threshold",
                "comfort_threshold",
                "climate",
                "notify_service",
                "alarm_delay_minutes",
            )
        ],
    },
    "thermal": {
        "thermal_intelligence_enabled",
        "external_heat_sources",
        "adaptive_braking_enabled",
    },
    "telemetry": {
        "thermal_detection_method",
        "thermal_compressor_entity",
        "thermal_return_temp_entity",
        "thermal_heat_activity_entity",
        "thermal_supply_temp_entity",
        "thermal_heating_mode_entity",
        "thermal_heating_mode_inverted",
        "thermal_supply_threshold",
        "thermal_power_entity",
        "thermal_energy_entities",
        "thermal_hot_water_mode_entity",
    },
    "notifications": {
        "notify_service",
        NOTIFICATION_LEVEL_OPTION,
        NOTIFICATION_COOLDOWN_OPTION,
    },
    "basic": {
        "indoor_temp_entity",
        "indoor_temp_entity_2",
        "indoor_temp_entity_3",
        "indoor_temp_weight_1",
        "indoor_temp_weight_2",
        "indoor_temp_weight_3",
        "real_outdoor_entity",
        "outdoor_temp_entity_2",
        "temperature_report_max_age_minutes",
        "outdoor_temperature_report_max_age_minutes",
        "weather_entity",
        "electricity_price_entity",
        "price_tomorrow_entity",
    },
    "ohmigo": {
        "ohmigo_entity",
        OHMIGO_RELAY_ENTITY,
        "ohmigo_interval_minutes",
        "modbus_service",
        "modbus_payload_template",
        "modbus_interval_minutes",
    },
}
# Existing logical groups retain their validation scopes and clearable fields.
_GROUPS["basic_settings"] = _GROUPS["basic"]
_GROUPS["heat_sensors"] = _GROUPS["telemetry"]
_GROUPS["ohmigo_settings"] = _GROUPS["ohmigo"]
_GROUPS["temperature_sources"] = {
    "indoor_temp_entity_2",
    "indoor_temp_entity_3",
    "indoor_temp_weight_1",
    "indoor_temp_weight_2",
    "indoor_temp_weight_3",
    "outdoor_temp_entity_2",
}
_GROUPS["report_age"] = {
    "temperature_report_max_age_minutes",
    "outdoor_temperature_report_max_age_minutes",
}
_GROUPS["basic_settings"] -= _GROUPS["temperature_sources"] | _GROUPS["report_age"]
_GROUPS["energy"] = {"thermal_power_entity", "thermal_energy_entities"}
_GROUPS["heat_sensors"] -= _GROUPS["energy"]
_GROUPS["heat_details"] = {
    "thermal_external_heat_entity",
    "thermal_compressor_entity",
    "thermal_return_temp_entity",
    "thermal_hot_water_mode_entity",
}
_GROUPS["heat_sensors"] -= _GROUPS["heat_details"]
_GROUPS["heat_pump_telemetry"] = {
    "thermal_balance_entity",
    "thermal_balance_kind",
    "thermal_supply_target_entity",
    "thermal_brine_pump_entity",
    "thermal_circulation_pump_entity",
    "thermal_brine_in_entity",
    "thermal_brine_out_entity",
    "thermal_aux_heat_entity",
    "thermal_alarm_entity",
    "thermal_alarm_numeric_verified",
    "thermal_alarm_numeric_normal",
}
_GROUPS["extended_control"] = {
    "control_aux_preheat_limit",
    "control_signals_verified",
    "control_measurement_time_attribute",
}
_GROUPS["advanced_control"] = {
    "control_balance_brake_limit",
    "control_balance_verified",
    "control_balance_scale",
    "control_balance_offset",
    "control_balance_deficit_threshold",
    "control_balance_deficit_full",
}
_GROUPS["generic_output"] = {
    "modbus_service",
    "modbus_payload_template",
    "modbus_interval_minutes",
}
_GROUPS["ohmigo_settings"] -= _GROUPS["generic_output"]
_GROUPS["zone_notifications"] = {"zone_notify_service"}
for _i in range(1, 5):
    _GROUPS[f"zone_{_i}"] = {k for k in _GROUPS["zones"] if k.startswith(f"zone_{_i}_")}

_CLEARABLE = {
    *[
        f"zone_{i}_{field}"
        for i in range(1, 5)
        for field in ("name", "entity", "climate", "notify_service")
    ],
    "zone_notify_service",
    "indoor_temp_entity_2",
    "indoor_temp_entity_3",
    "outdoor_temp_entity_2",
    "weather_entity",
    "ohmigo_entity",
    OHMIGO_RELAY_ENTITY,
    "thermal_heat_activity_entity",
    "thermal_external_heat_entity",
    "thermal_supply_temp_entity",
    "thermal_heating_mode_entity",
    "thermal_power_entity",
    "thermal_energy_entities",
    "thermal_hot_water_mode_entity",
    "thermal_compressor_entity",
    "thermal_return_temp_entity",
    *(
        _GROUPS["heat_pump_telemetry"]
        - {"thermal_balance_kind", "thermal_alarm_numeric_verified"}
    ),
    "notify_service",
    "modbus_service",
    "modbus_payload_template",
    "control_measurement_time_attribute",
    "control_balance_scale",
    "control_balance_offset",
    "control_balance_deficit_threshold",
    "control_balance_deficit_full",
}


# Preserve the original validation scopes even when a visual section combines them.
_VALIDATION_GROUPS = tuple(
    keys for name, keys in _GROUPS.items() if name not in {"zones"}
)
_BASIC_ORDER = (
    "indoor_temp_entity",
    "real_outdoor_entity",
    "electricity_price_entity",
    "price_tomorrow_entity",
    "weather_entity",
)
_BASIC_FIELDS = set(_BASIC_ORDER)
_REQUIRED_FIELDS = _BASIC_FIELDS - {"weather_entity"}
_FORM_SECTIONS = {
    "temperature_sources": _GROUPS["temperature_sources"] | _GROUPS["report_age"],
    "outputs": _GROUPS["ohmigo_settings"] | _GROUPS["generic_output"],
    "notifications": _GROUPS["notifications"] | _GROUPS["zone_notifications"],
    **{f"zone_{i}": _GROUPS[f"zone_{i}"] for i in range(1, 5)},
    "thermal": _GROUPS["thermal"],
    "heat_pump_data": (
        _GROUPS["heat_sensors"]
        | _GROUPS["heat_details"]
        | _GROUPS["heat_pump_telemetry"]
        | _GROUPS["energy"]
    ),
    "advanced_control": _GROUPS["extended_control"] | _GROUPS["advanced_control"],
}


def _suggested_value(marker):
    """Use the existing field's display conversion without materialising defaults."""
    default = marker.default
    fallback = None if default is vol.UNDEFINED else default()
    return (marker.description or {}).get("suggested_value", fallback)


class PumpSteerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for PumpSteer."""

    async def async_step_init(self, user_input=None):
        """Configure in one form; section names never enter stored options."""
        entry = self.config_entry
        current = {**entry.data, **entry.options}
        fields = self._fields(current)
        displayed = {key.schema: _suggested_value(key) for key in fields}
        errors = {}
        draft = dict(current)
        if user_input is not None:
            try:
                submitted = {
                    key: value
                    for key, value in user_input.items()
                    if key in _BASIC_FIELDS
                }
                for name, keys in _FORM_SECTIONS.items():
                    if name not in user_input:
                        continue  # An omitted section cannot clear anything.
                    values = user_input[name]
                    if not isinstance(values, dict):
                        errors[name] = "unknown"
                        continue
                    submitted.update({k: v for k, v in values.items() if k in keys})
                    # HA removes cleared optional selectors from the section's JSON.
                    # Existing suggested values are sent even when it is collapsed.
                    for key in keys & _CLEARABLE:
                        if key not in values and current.get(key) not in (None, "", []):
                            submitted[key] = (
                                [] if key == "thermal_energy_entities" else ""
                            )
                if (
                    _REQUIRED_FIELDS <= user_input.keys()
                    and "weather_entity" not in user_input
                    and current.get("weather_entity")
                ):
                    submitted["weather_entity"] = ""
                updated = dict(entry.options)
                changed = set()
                for key, value in submitted.items():
                    if value is None and key in _CLEARABLE:
                        value = [] if key == "thermal_energy_entities" else ""
                    # Preserve data/options ownership, types and implicit defaults.
                    if value == displayed.get(key):
                        continue
                    if value != current.get(key) or key not in current:
                        updated[key] = value
                        changed.add(key)
                candidate = {**entry.data, **updated}
                # A changed source can change an implicit fallback. Honour the
                # method/limit the user actually saw, while leaving no-op saves
                # and independent defaults untouched.
                for key in self._fields(candidate):
                    name = key.schema
                    if (
                        name in submitted
                        and submitted[name] == displayed[name]
                        and submitted[name] != _suggested_value(key)
                    ):
                        updated[name] = submitted[name]
                        changed.add(name)
                draft.update(submitted)
                candidate = {**entry.data, **updated}
                if changed:
                    affected = set(changed)
                    if changed & _BASIC_FIELDS:
                        affected.update(_BASIC_FIELDS)
                    for keys in _VALIDATION_GROUPS:
                        if changed & keys:
                            affected.update(keys)
                    baseline_errors = self._validate_entities(current)
                    for key, error in self._validate_entities(candidate).items():
                        if (
                            key == "base"
                            or key in affected
                            or baseline_errors.get(key) != error
                        ):
                            errors[key] = error
                    for secondary, primary in (
                        ("indoor_temp_entity_2", "indoor_temp_entity"),
                        ("indoor_temp_entity_3", "indoor_temp_entity"),
                        ("outdoor_temp_entity_2", "real_outdoor_entity"),
                    ):
                        if (
                            errors.get(secondary) == "duplicate_temperature_sensor"
                            and primary in changed
                        ):
                            errors[primary] = "duplicate_temperature_sensor"
                    if (
                        errors.get("thermal_heat_activity_entity")
                        == "heat_signal_alias"
                    ):
                        for key in (
                            "thermal_compressor_entity",
                            "thermal_heating_mode_entity",
                            "thermal_hot_water_mode_entity",
                        ):
                            if key in changed and candidate.get(key) == candidate.get(
                                "thermal_heat_activity_entity"
                            ):
                                errors[key] = "heat_signal_alias"
                if not errors:
                    self._revoke_changed_verification(current, candidate, updated)
                    return self.async_create_entry(title="", data=updated)
            except Exception:
                _LOGGER.exception("Options flow save failed")
                errors["base"] = "unknown"

        # Suggestions, rather than defaults, let an optional value be cleared.
        # Rebuild from the complete draft so errors never discard another edit.
        fields = self._fields(draft)
        suggested_fields = {}
        for key, validator in fields.items():
            name = key.schema
            description = dict(getattr(key, "description", None) or {})
            value = _suggested_value(key)
            if value not in (None, ""):
                description["suggested_value"] = value
            else:
                # Empty entity suggestions become invalid nested JSON in HA.
                # An absent suggestion renders the same empty optional field.
                description.pop("suggested_value", None)
            marker = vol.Required if name in _REQUIRED_FIELDS else vol.Optional
            suggested_fields[marker(name, description=description)] = validator
        schema = {
            next(key for key in suggested_fields if key.schema == name): next(
                value for key, value in suggested_fields.items() if key.schema == name
            )
            for name in _BASIC_ORDER
        }
        form_errors = {}
        for key, error in errors.items():
            section = next(
                (name for name, keys in _FORM_SECTIONS.items() if key in keys), None
            )
            # Native HA displays section errors above the expanded section;
            # arbitrary nested field paths are not rendered by ha-form.
            form_errors.setdefault(section or key, error)
        for name, keys in _FORM_SECTIONS.items():
            section_fields = {
                key: value
                for key, value in suggested_fields.items()
                if key.schema in keys
            }
            schema[vol.Optional(name)] = data_entry_flow.section(
                vol.Schema(section_fields),
                {"collapsed": not (name in errors or keys.intersection(errors))},
            )
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=form_errors
        )

    @staticmethod
    def _revoke_changed_verification(current, candidate, updated):
        """Verification belongs to the exact source/calibration, not its slot."""
        for flag, keys in (
            (
                "control_signals_verified",
                (
                    "thermal_compressor_entity",
                    "thermal_heating_mode_entity",
                    "thermal_heating_mode_inverted",
                    "thermal_hot_water_mode_entity",
                    "thermal_brine_pump_entity",
                    "thermal_aux_heat_entity",
                    "control_measurement_time_attribute",
                ),
            ),
            (
                "control_balance_verified",
                (
                    "thermal_balance_entity",
                    "thermal_balance_kind",
                    "control_balance_scale",
                    "control_balance_offset",
                    "control_balance_deficit_threshold",
                    "control_balance_deficit_full",
                ),
            ),
            (
                "thermal_alarm_numeric_verified",
                (
                    "thermal_alarm_entity",
                    "thermal_alarm_numeric_normal",
                ),
            ),
        ):
            if current.get(flag) is True and any(
                candidate.get(key) != current.get(key) for key in keys
            ):
                updated[flag] = False

    def _fields(self, current_data):
        """Reuse all existing selectors, defaults and limits."""
        legacy_age = safe_float(current_data.get("temperature_report_max_age_minutes"))
        outdoor_default_age = (
            min(DEFAULT_OUTDOOR_REPORT_MAX_AGE_MINUTES, legacy_age)
            if legacy_age is not None and legacy_age > 0
            else DEFAULT_OUTDOOR_REPORT_MAX_AGE_MINUTES
        )
        number_entities = sorted(
            s.entity_id for s in self.hass.states.async_all("number")
        )
        fields = {
            vol.Required(
                "indoor_temp_entity",
                default=current_data.get("indoor_temp_entity"),
            ): selector(
                {"entity": {"domain": "sensor", "device_class": "temperature"}}
            ),
            vol.Required(
                "real_outdoor_entity",
                default=current_data.get("real_outdoor_entity"),
            ): selector(
                {"entity": {"domain": "sensor", "device_class": "temperature"}}
            ),
            **{
                vol.Optional(
                    key, description={"suggested_value": current_data.get(key)}
                ): selector(
                    {"entity": {"domain": "sensor", "device_class": "temperature"}}
                )
                for key in (
                    "indoor_temp_entity_2",
                    "indoor_temp_entity_3",
                    "outdoor_temp_entity_2",
                )
            },
            **{
                vol.Optional(key, default=current_data.get(key, 1)): selector(
                    {"number": {"min": 0.01, "max": 1000, "step": 0.01, "mode": "box"}}
                )
                for key in (
                    "indoor_temp_weight_1",
                    "indoor_temp_weight_2",
                    "indoor_temp_weight_3",
                )
            },
            vol.Optional(
                "temperature_report_max_age_minutes",
                default=current_data.get(
                    "temperature_report_max_age_minutes",
                    DEFAULT_TEMPERATURE_REPORT_MAX_AGE_MINUTES,
                ),
            ): selector(
                {
                    "number": {
                        "min": 1,
                        "max": 10080,
                        "step": 1,
                        "unit_of_measurement": "min",
                        "mode": "box",
                    }
                }
            ),
            vol.Optional(
                "outdoor_temperature_report_max_age_minutes",
                default=current_data.get(
                    "outdoor_temperature_report_max_age_minutes",
                    outdoor_default_age,
                ),
            ): selector(
                {
                    "number": {
                        "min": 1,
                        "max": 10080,
                        "step": 1,
                        "unit_of_measurement": "min",
                        "mode": "box",
                    }
                }
            ),
            vol.Optional(
                "weather_entity",
                description={"suggested_value": current_data.get("weather_entity")},
            ): selector({"entity": {"domain": "weather"}}),
            vol.Required(
                "electricity_price_entity",
                default=current_data.get(
                    "electricity_price_entity",
                    "sensor.elpris_today",
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Required(
                "price_tomorrow_entity",
                default=current_data.get(
                    "price_tomorrow_entity",
                    "sensor.elpris_tomorrow",
                ),
            ): selector({"entity": {"domain": "sensor"}}),
            vol.Optional(
                "adaptive_braking_enabled",
                default=current_data.get("adaptive_braking_enabled", False),
            ): selector({"boolean": {}}),
            vol.Optional(
                "thermal_intelligence_enabled",
                default=current_data.get("thermal_intelligence_enabled", False),
            ): selector({"boolean": {}}),
            vol.Optional(
                "external_heat_sources",
                default=current_data.get("external_heat_sources", False),
            ): selector({"boolean": {}}),
            vol.Optional(
                "thermal_detection_method",
                default=current_data.get("thermal_detection_method")
                or (
                    "verified"
                    if current_data.get("thermal_heat_activity_entity")
                    else "manual"
                    if current_data.get("thermal_supply_temp_entity")
                    else "automatic"
                ),
            ): selector(
                {
                    "select": {
                        "options": ["automatic", "manual", "verified"],
                        "translation_key": "thermal_detection_method",
                    }
                }
            ),
            vol.Optional(
                "thermal_balance_kind",
                default=current_data.get("thermal_balance_kind", "unconfigured"),
            ): selector(
                {
                    "select": {
                        "options": [
                            "unconfigured",
                            "raw",
                            "degree_minutes",
                            "integral",
                        ],
                        "translation_key": "thermal_balance_kind",
                    }
                }
            ),
            **{
                vol.Optional(
                    key, description={"suggested_value": current_data.get(key)}
                ): selector({"entity": {"domain": domain}})
                for key, domain in (
                    ("thermal_balance_entity", "sensor"),
                    ("thermal_supply_target_entity", "sensor"),
                    ("thermal_brine_in_entity", "sensor"),
                    ("thermal_brine_out_entity", "sensor"),
                    ("thermal_brine_pump_entity", "binary_sensor"),
                    ("thermal_circulation_pump_entity", "binary_sensor"),
                    ("thermal_aux_heat_entity", "binary_sensor"),
                )
            },
            vol.Optional(
                "thermal_alarm_entity",
                description={
                    "suggested_value": current_data.get("thermal_alarm_entity")
                },
            ): selector({"entity": {"domain": ["binary_sensor", "sensor"]}}),
            vol.Optional(
                "thermal_alarm_numeric_verified",
                default=current_data.get("thermal_alarm_numeric_verified", False),
            ): selector({"boolean": {}}),
            vol.Optional(
                "thermal_alarm_numeric_normal",
                description={
                    "suggested_value": current_data.get("thermal_alarm_numeric_normal")
                },
            ): selector(
                {
                    "number": {
                        "min": -1000000000,
                        "max": 1000000000,
                        "step": "any",
                        "mode": "box",
                    }
                }
            ),
            vol.Optional(
                "thermal_external_heat_entity",
                description={
                    "suggested_value": current_data.get("thermal_external_heat_entity")
                },
            ): selector({"entity": {"domain": "binary_sensor"}}),
            vol.Optional(
                "thermal_compressor_entity",
                description={
                    "suggested_value": current_data.get("thermal_compressor_entity")
                },
            ): selector({"entity": {"domain": "binary_sensor"}}),
            vol.Optional(
                "thermal_return_temp_entity",
                description={
                    "suggested_value": current_data.get("thermal_return_temp_entity")
                },
            ): selector(
                {"entity": {"domain": "sensor", "device_class": "temperature"}}
            ),
            vol.Optional(
                "thermal_heat_activity_entity",
                description={
                    "suggested_value": current_data.get("thermal_heat_activity_entity")
                },
            ): selector({"entity": {"domain": "binary_sensor"}}),
            vol.Optional(
                "thermal_power_entity",
                description={
                    "suggested_value": current_data.get("thermal_power_entity")
                },
            ): selector({"entity": {"domain": "sensor", "device_class": "power"}}),
            vol.Optional(
                "thermal_energy_entities",
                description={
                    "suggested_value": current_data.get("thermal_energy_entities")
                },
            ): selector(
                {
                    "entity": {
                        "domain": "sensor",
                        "device_class": "energy",
                        "multiple": True,
                    }
                }
            ),
            vol.Optional(
                "thermal_hot_water_mode_entity",
                description={
                    "suggested_value": current_data.get("thermal_hot_water_mode_entity")
                },
            ): selector({"entity": {"domain": "binary_sensor"}}),
            vol.Optional(
                "thermal_supply_temp_entity",
                description={
                    "suggested_value": current_data.get("thermal_supply_temp_entity")
                },
            ): selector(
                {"entity": {"domain": "sensor", "device_class": "temperature"}}
            ),
            vol.Optional(
                "thermal_heating_mode_entity",
                description={
                    "suggested_value": current_data.get("thermal_heating_mode_entity")
                },
            ): selector({"entity": {"domain": "binary_sensor"}}),
            vol.Optional(
                "thermal_heating_mode_inverted",
                default=current_data.get("thermal_heating_mode_inverted", False),
            ): selector({"boolean": {}}),
            vol.Optional(
                "thermal_supply_threshold",
                default=current_data.get("thermal_supply_threshold", 30),
            ): selector(
                {
                    "number": {
                        "min": 0,
                        "max": 60,
                        "step": 0.5,
                        "unit_of_measurement": "°C",
                        "mode": "box",
                    }
                }
            ),
            vol.Optional(
                "notify_service",
                default=current_data.get("notify_service", ""),
            ): selector({"text": {}}),
            vol.Optional(
                NOTIFICATION_LEVEL_OPTION,
                default=current_data.get(NOTIFICATION_LEVEL_OPTION, "all"),
            ): selector(
                {
                    "select": {
                        "options": ["all", "braking_only", "off"],
                        "translation_key": "notification_level",
                        "mode": "dropdown",
                    }
                }
            ),
            vol.Optional(
                NOTIFICATION_COOLDOWN_OPTION,
                default=str(current_data.get(NOTIFICATION_COOLDOWN_OPTION, "0")),
            ): selector(
                {
                    "select": {
                        "options": ["0", "30", "60", "120"],
                        "translation_key": "notification_cooldown",
                        "mode": "dropdown",
                    }
                }
            ),
            vol.Optional(
                "ohmigo_entity",
                default=current_data.get("ohmigo_entity", ""),
            ): selector(
                {
                    "select": {
                        "options": number_entities,
                        "custom_value": True,
                        "mode": "dropdown",
                    }
                }
            ),
            vol.Optional(
                OHMIGO_RELAY_ENTITY,
                description={
                    "suggested_value": current_data.get(OHMIGO_RELAY_ENTITY, "")
                },
            ): selector({"entity": {"domain": "switch"}}),
            vol.Optional(
                "ohmigo_interval_minutes",
                default=current_data.get("ohmigo_interval_minutes", 5),
            ): selector(
                {
                    "number": {
                        "min": 1,
                        "max": 60,
                        "step": 1,
                        "unit_of_measurement": "min",
                        "mode": "slider",
                    }
                }
            ),
            vol.Optional(
                "modbus_service",
                default=current_data.get("modbus_service", ""),
            ): selector({"text": {}}),
            vol.Optional(
                "modbus_payload_template",
                default=current_data.get("modbus_payload_template", ""),
            ): selector({"text": {"multiline": True}}),
            vol.Optional(
                "modbus_interval_minutes",
                default=current_data.get("modbus_interval_minutes", 5),
            ): selector(
                {
                    "number": {
                        "min": 1,
                        "max": 60,
                        "step": 1,
                        "unit_of_measurement": "min",
                        "mode": "slider",
                    }
                }
            ),
        }
        fields[
            vol.Optional(
                "zone_notify_service",
                default=current_data.get("zone_notify_service", ""),
            )
        ] = selector({"text": {}})
        for i in range(1, 5):
            for suffix in (
                "name",
                "entity",
                "enabled",
                "profile",
                "threshold",
                "comfort_threshold",
                "climate",
                "notify_service",
                "alarm_delay_minutes",
            ):
                key = f"zone_{i}_{suffix}"
                if suffix == "entity":
                    fields[
                        vol.Optional(
                            key, description={"suggested_value": current_data.get(key)}
                        )
                    ] = selector(
                        {"entity": {"domain": "sensor", "device_class": "temperature"}}
                    )
                elif suffix == "climate":
                    fields[
                        vol.Optional(
                            key, description={"suggested_value": current_data.get(key)}
                        )
                    ] = selector({"entity": {"domain": "climate"}})
                elif suffix == "profile":
                    fields[
                        vol.Optional(key, default=current_data.get(key, "maintenance"))
                    ] = selector(
                        {
                            "select": {
                                "options": ["maintenance", "comfort"],
                                "mode": "dropdown",
                                "translation_key": "zone_profile",
                            }
                        }
                    )
                elif suffix in ("name", "notify_service"):
                    fields[vol.Optional(key, default=current_data.get(key, ""))] = (
                        selector({"text": {}})
                    )
                elif suffix == "enabled":
                    fields[vol.Optional(key, default=current_data.get(key, False))] = (
                        selector({"boolean": {}})
                    )
                elif suffix == "alarm_delay_minutes":
                    fields[vol.Optional(key, default=current_data.get(key, 15))] = (
                        selector(
                            {
                                "number": {
                                    "min": 1,
                                    "max": 240,
                                    "step": 1,
                                    "unit_of_measurement": "min",
                                    "mode": "box",
                                }
                            }
                        )
                    )
                else:
                    fields[
                        vol.Optional(
                            key,
                            default=current_data.get(
                                key, 18 if suffix == "comfort_threshold" else 5
                            ),
                        )
                    ] = selector(
                        {
                            "number": {
                                "min": -30,
                                "max": 40,
                                "step": 0.5,
                                "unit_of_measurement": "°C",
                                "mode": "box",
                            }
                        }
                    )
        for key in (
            "control_aux_preheat_limit",
            "control_signals_verified",
            "control_balance_brake_limit",
            "control_balance_verified",
        ):
            fields[vol.Optional(key, default=current_data.get(key, False))] = selector(
                {"boolean": {}}
            )
        fields[
            vol.Optional(
                "control_measurement_time_attribute",
                default=current_data.get(
                    "control_measurement_time_attribute", "measurement_time"
                ),
            )
        ] = selector({"text": {}})
        for key in (
            "control_balance_scale",
            "control_balance_offset",
            "control_balance_deficit_threshold",
            "control_balance_deficit_full",
        ):
            fields[
                vol.Optional(
                    key, description={"suggested_value": current_data.get(key)}
                )
            ] = selector(
                {"number": {"min": -1e9, "max": 1e9, "step": 0.001, "mode": "box"}}
            )
        return fields

    def _validate_entities(self, user_input):
        """Validate that entities exist and are available."""
        errors = {}

        required_entities = {
            "indoor_temp_entity": "Indoor temperature sensor",
            "real_outdoor_entity": "Outdoor temperature sensor",
            "electricity_price_entity": "Electricity price sensor",
            "price_tomorrow_entity": "Tomorrow electricity price sensor",
        }

        for field, description in required_entities.items():
            entity_id = user_input.get(field)
            if not entity_id:
                errors[field] = "required"
                continue
            if not self._entity_exists(entity_id):
                _LOGGER.warning("%s not found: %s", description, entity_id)
                errors[field] = "entity_not_found"
                continue
            if not self._entity_available(entity_id):
                _LOGGER.warning("%s unavailable: %s", description, entity_id)
                errors[field] = "entity_unavailable"

        for keys in (
            ("indoor_temp_entity", "indoor_temp_entity_2", "indoor_temp_entity_3"),
            ("real_outdoor_entity", "outdoor_temp_entity_2"),
        ):
            seen = set()
            for key in keys:
                entity = user_input.get(key)
                if not entity:
                    continue
                if entity in seen:
                    errors[key] = "duplicate_temperature_sensor"
                seen.add(entity)
                if key in {
                    "indoor_temp_entity_2",
                    "indoor_temp_entity_3",
                    "outdoor_temp_entity_2",
                } and not self._entity_exists(entity):
                    errors[key] = "entity_not_found"

        for i in range(1, 5):
            sensor_key = f"zone_{i}_entity"
            entity = user_input.get(sensor_key)
            if not entity and (
                user_input.get(f"zone_{i}_enabled")
                or user_input.get(f"zone_{i}_climate")
            ):
                errors[sensor_key] = "required"
            for suffix, domain in (("entity", "sensor."), ("climate", "climate.")):
                key = f"zone_{i}_{suffix}"
                selected = user_input.get(key)
                if selected and (
                    not isinstance(selected, str)
                    or not selected.startswith(domain)
                    or not self._entity_exists(selected)
                ):
                    errors[key] = "entity_not_found"
        for key in (
            "zone_notify_service",
            *[f"zone_{i}_notify_service" for i in range(1, 5)],
        ):
            service = user_input.get(key)
            if service and (
                not isinstance(service, str)
                or not re.fullmatch(r"notify\.[a-z0-9_]+", service)
            ):
                errors[key] = "invalid_notification_setting"

        # Restrict notification preferences to recognized selection values.
        level = user_input.get(NOTIFICATION_LEVEL_OPTION, "all")
        if level not in NOTIFICATION_LEVELS:
            errors[NOTIFICATION_LEVEL_OPTION] = "invalid_notification_setting"

        cooldown = user_input.get(NOTIFICATION_COOLDOWN_OPTION, "0")
        if str(cooldown) not in {str(value) for value in NOTIFICATION_COOLDOWN_CHOICES}:
            errors[NOTIFICATION_COOLDOWN_OPTION] = "invalid_notification_setting"

        # weather_entity is optional — validate only if set
        weather = user_input.get("weather_entity")
        if weather and not self._entity_exists(weather):
            errors["weather_entity"] = "entity_not_found"

        heat = user_input.get("thermal_heat_activity_entity")
        method = user_input.get("thermal_detection_method")
        if method and method not in {"automatic", "manual", "verified"}:
            errors["thermal_detection_method"] = "invalid_detection_method"
        if user_input.get("thermal_balance_kind", "unconfigured") not in {
            "unconfigured",
            "raw",
            "degree_minutes",
            "integral",
        }:
            errors["thermal_balance_kind"] = "invalid_balance_kind"
        if method == "verified" and not heat:
            errors["thermal_heat_activity_entity"] = "required"
        if (
            method in {"automatic", "verified"}
            and heat
            and heat
            in {
                user_input.get("thermal_compressor_entity"),
                user_input.get("thermal_heating_mode_entity"),
                user_input.get("thermal_hot_water_mode_entity"),
            }
        ):
            errors["thermal_heat_activity_entity"] = "heat_signal_alias"
        if method == "manual" and not user_input.get("thermal_supply_temp_entity"):
            errors["thermal_supply_temp_entity"] = "required"
        if heat and (
            not isinstance(heat, str)
            or not heat.startswith("binary_sensor.")
            or not self._entity_exists(heat)
        ):
            errors["thermal_heat_activity_entity"] = "entity_not_found"

        for key, domain in (
            ("thermal_external_heat_entity", "binary_sensor."),
            ("thermal_balance_entity", "sensor."),
            ("thermal_supply_target_entity", "sensor."),
            ("thermal_brine_in_entity", "sensor."),
            ("thermal_brine_out_entity", "sensor."),
            ("thermal_brine_pump_entity", "binary_sensor."),
            ("thermal_circulation_pump_entity", "binary_sensor."),
            ("thermal_aux_heat_entity", "binary_sensor."),
            ("thermal_power_entity", "sensor."),
            ("thermal_hot_water_mode_entity", "binary_sensor."),
            ("thermal_supply_temp_entity", "sensor."),
            ("thermal_heating_mode_entity", "binary_sensor."),
            ("thermal_compressor_entity", "binary_sensor."),
            ("thermal_return_temp_entity", "sensor."),
        ):
            entity = user_input.get(key)
            if entity and (
                not isinstance(entity, str)
                or not entity.startswith(domain)
                or not self._entity_exists(entity)
            ):
                errors[key] = "entity_not_found"
        external = user_input.get("thermal_external_heat_entity")
        if external and any(
            external == user_input.get(key)
            for key in (
                "thermal_heat_activity_entity",
                "thermal_compressor_entity",
                "thermal_heating_mode_entity",
                "thermal_hot_water_mode_entity",
                "thermal_aux_heat_entity",
            )
        ):
            errors["thermal_external_heat_entity"] = "external_heat_alias"
        alarm_entity = user_input.get("thermal_alarm_entity")
        if alarm_entity and (
            not isinstance(alarm_entity, str)
            or not alarm_entity.startswith(("sensor.", "binary_sensor."))
            or not self._entity_exists(alarm_entity)
        ):
            errors["thermal_alarm_entity"] = "entity_not_found"
        numeric_normal = user_input.get("thermal_alarm_numeric_normal")
        if (
            isinstance(alarm_entity, str)
            and alarm_entity.startswith("sensor.")
            and user_input.get("thermal_alarm_numeric_verified") is True
            and (isinstance(numeric_normal, bool) or safe_float(numeric_normal) is None)
        ):
            errors["thermal_alarm_numeric_normal"] = "invalid_numeric_alarm"
        meters = user_input.get("thermal_energy_entities") or []
        if (
            not isinstance(meters, list)
            or any(not isinstance(entity, str) for entity in meters)
            or len(meters) != len(set(meters))
            or any(
                not isinstance(entity, str)
                or not entity.startswith("sensor.")
                or not self._entity_exists(entity)
                for entity in meters
            )
        ):
            errors["thermal_energy_entities"] = "entity_not_found"
        if method == "manual" and not user_input.get("thermal_heating_mode_entity"):
            errors["thermal_heating_mode_entity"] = "entity_not_found"

        # ohmigo_entity is optional — validate only if set
        ohmigo = user_input.get("ohmigo_entity", "")
        if ohmigo and not self._entity_exists(ohmigo):
            errors["ohmigo_entity"] = "entity_not_found"

        # Relay Guard is optional. A configured relay must exist, but it may be
        # temporarily unavailable when options are saved.
        relay = user_input.get(OHMIGO_RELAY_ENTITY, "")
        if relay and not self._entity_exists(relay):
            errors[OHMIGO_RELAY_ENTITY] = "entity_not_found"

        active = [
            key
            for key in ("control_aux_preheat_limit", "control_balance_brake_limit")
            if user_input.get(key) is True
        ]
        if active:
            mode_signals = (
                "thermal_heating_mode_entity",
                "thermal_hot_water_mode_entity",
            )
            attribute = user_input.get(
                "control_measurement_time_attribute", "measurement_time"
            )
            signals_ready = (
                user_input.get("control_signals_verified") is True
                and isinstance(attribute, str)
                and bool(attribute.strip())
                and all(
                    user_input.get(key) and self._entity_exists(user_input[key])
                    for key in mode_signals
                )
                and (
                    user_input.get(mode_signals[0]) != user_input.get(mode_signals[1])
                    or user_input.get("thermal_heating_mode_inverted") is True
                )
            )
            for key in active:
                if not signals_ready:
                    errors[key] = "control_signals_required"
            if "control_aux_preheat_limit" in active:
                extra_signals = ("thermal_compressor_entity", "thermal_aux_heat_entity")
                extras_ready = all(
                    user_input.get(key) and self._entity_exists(user_input[key])
                    for key in extra_signals
                )
                brine = user_input.get("thermal_brine_pump_entity")
                if brine and not self._entity_exists(brine):
                    extras_ready = False
                # Distinct physical roles are required, except a verified
                # complementary radiator/hot-water changeover signal.
                other = [user_input.get(key) for key in (*extra_signals, *mode_signals)]
                if brine:
                    other.append(brine)
                ids = [value for value in other if value]
                valid_shared = (
                    user_input.get(mode_signals[0])
                    and user_input.get(mode_signals[0])
                    == user_input.get(mode_signals[1])
                    and user_input.get("thermal_heating_mode_inverted") is True
                )
                if len(ids) - len(set(ids)) > (1 if valid_shared else 0):
                    extras_ready = False
                if not extras_ready:
                    errors["control_aux_preheat_limit"] = "control_signals_required"
            if "control_balance_brake_limit" in active:
                scale = safe_float(user_input.get("control_balance_scale"))
                offset = safe_float(user_input.get("control_balance_offset"))
                threshold = safe_float(
                    user_input.get("control_balance_deficit_threshold")
                )
                full = safe_float(user_input.get("control_balance_deficit_full"))
                if not (
                    user_input.get("control_balance_verified") is True
                    and user_input.get("thermal_balance_entity")
                    and self._entity_exists(user_input["thermal_balance_entity"])
                    and scale is not None
                    and scale != 0
                    and offset is not None
                    and threshold is not None
                    and full is not None
                    and 0 < threshold < full
                ):
                    errors["control_balance_brake_limit"] = "control_balance_required"
        return errors

    def _entity_exists(self, entity_id: str) -> bool:
        """Return True if the entity exists."""
        return self.hass.states.get(entity_id) is not None

    def _entity_available(self, entity_id: str) -> bool:
        """Return True if the entity is available."""
        entity = self.hass.states.get(entity_id)
        if not entity:
            return False
        return entity.state not in {
            STATE_UNAVAILABLE,
            STATE_UNKNOWN,
            "unavailable",
            "unknown",
        }
