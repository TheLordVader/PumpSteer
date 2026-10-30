import logging

import voluptuous as vol
from homeassistant import config_entries
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
    "thermal": {"thermal_intelligence_enabled", "external_heat_sources"},
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
_CLEARABLE = {
    "indoor_temp_entity_2",
    "indoor_temp_entity_3",
    "outdoor_temp_entity_2",
    "weather_entity",
    "ohmigo_entity",
    OHMIGO_RELAY_ENTITY,
    "thermal_heat_activity_entity",
    "thermal_supply_temp_entity",
    "thermal_heating_mode_entity",
    "thermal_power_entity",
    "thermal_energy_entities",
    "thermal_hot_water_mode_entity",
    "thermal_compressor_entity",
    "thermal_return_temp_entity",
}


class PumpSteerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for PumpSteer."""

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["basic", "thermal", "telemetry", "notifications", "ohmigo"],
        )

    async def async_step_basic(self, user_input=None):
        return await self._section("basic", user_input)

    async def async_step_thermal(self, user_input=None):
        return await self._section("thermal", user_input)

    async def async_step_telemetry(self, user_input=None):
        return await self._section("telemetry", user_input)

    async def async_step_notifications(self, user_input=None):
        return await self._section("notifications", user_input)

    async def async_step_ohmigo(self, user_input=None):
        return await self._section("ohmigo", user_input)

    async def _section(self, section, user_input=None):
        """Save only the selected page while preserving all other options."""
        errors = {}

        entry = self.config_entry
        current_data = {**entry.data, **entry.options}
        legacy_age = safe_float(current_data.get("temperature_report_max_age_minutes"))
        outdoor_default_age = (
            min(DEFAULT_OUTDOOR_REPORT_MAX_AGE_MINUTES, legacy_age)
            if legacy_age is not None and legacy_age > 0
            else DEFAULT_OUTDOOR_REPORT_MAX_AGE_MINUTES
        )

        # Build list of number entities for the Ohmigo dropdown.
        number_entities = sorted(
            s.entity_id for s in self.hass.states.async_all("number")
        )

        if user_input is not None:
            try:
                updated = {**entry.options, **user_input}
                for key in _GROUPS[section] & _CLEARABLE:
                    if key not in user_input and current_data.get(key):
                        updated[key] = [] if key == "thermal_energy_entities" else ""
                entity_errors = self._validate_entities({**entry.data, **updated})
                errors = {
                    k: v
                    for k, v in entity_errors.items()
                    if k in _GROUPS[section] or k == "base"
                }

                if not errors:
                    return self.async_create_entry(title="", data=updated)

            except Exception as err:
                _LOGGER.exception("Options flow save failed: %s", err)
                errors["base"] = "unknown"

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
                        "options": [
                            {
                                "value": "all",
                                "label": "All price notifications",
                            },
                            {"value": "braking_only", "label": "Braking only"},
                            {"value": "off", "label": "No price notifications"},
                        ],
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
                        "options": [
                            {"value": "0", "label": "No repeat limit"},
                            {"value": "30", "label": "30 minutes"},
                            {
                                "value": "60",
                                "label": "60 minutes (recommended)",
                            },
                            {"value": "120", "label": "120 minutes"},
                        ],
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
        selected = {
            k: v
            for k, v in fields.items()
            if getattr(k, "schema", k[0] if isinstance(k, tuple) else k)
            in _GROUPS[section]
        }
        return self.async_show_form(
            step_id=section, data_schema=vol.Schema(selected), errors=errors
        )

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
        supply = user_input.get("thermal_supply_temp_entity")
        if supply and not user_input.get("thermal_heating_mode_entity"):
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
