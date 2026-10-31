import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import homeassistant.util.dt as dt_util
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType

from .thermal_model import VALIDATION_BASIS, ThermalModel
from .const import (
    DEFAULT_OUTDOOR_REPORT_MAX_AGE_MINUTES,
    DEFAULT_TEMPERATURE_REPORT_MAX_AGE_MINUTES,
)

from .control import PIController
from .control_ramps import (
    BRAKE_RAMP_MAX_DT_SECONDS as _BRAKE_RAMP_MAX_DT_SECONDS,
    PREHEAT_RAMP_MAX_DT_SECONDS as _PREHEAT_RAMP_MAX_DT_SECONDS,
    brake_ramp_step,
    preheat_ramp_step,
)
from .electricity_price import (
    PRICE_EXPENSIVE,
    PRICE_NORMAL,
    async_get_price_thresholds,
    classify_price_list,
    filter_short_peaks,
    price_cache_refresh_due,
    price_cache_transition,
)
from .holiday import async_update_holiday
from .ohmigo import async_push_ohmigo
from .pump_log import log_event, log_mode_change
from .settings import (
    BRAKE_DELTA_C,
    BRAKE_HOLD_MINUTES,
    COMFORT_FLOOR_BY_AGGRESSIVENESS,
    DEFAULT_AGGRESSIVENESS,
    DEFAULT_HOUSE_INERTIA,
    DEFAULT_SUMMER_THRESHOLD,
    DEFAULT_TARGET_TEMP,
    HOLIDAY_TEMP,
    MAX_FAKE_TEMP,
    MIN_FAKE_TEMP,
    PEAK_FILTER_MIN_DURATION_MINUTES,
    PID_INTEGRAL_CLAMP,
    PID_KD,
    PID_KI,
    PID_KP,
    PID_OUTPUT_CLAMP,
    PRECOOL_LOOKAHEAD,
    PRECOOL_MARGIN,
    PREHEAT_BOOST_C,
    PREHEAT_HEADROOM_BY_AGGRESSIVENESS,
    PREHEAT_ON_MISSING_FORECAST,
    PRICE_LOOKAHEAD_HOURS,
    RAMP_OUT_FACTOR,
    RAMP_MAX_MINUTES,
    RAMP_MIN_MINUTES,
    RAMP_SCALE,
)
from .utils import (
    compute_price_slot_index,
    detect_price_interval_minutes,
    get_attr,
    get_state,
    get_temperature,
    get_version,
    safe_float,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "pumpsteer"
SW_VERSION = get_version()

# Operating modes.
MODE_SUMMER = "summer_mode"
MODE_PRECOOL = "precool"
MODE_PREHEAT = "preheating"
MODE_PREBRAKE = "pre_braking"
MODE_BRAKING = "braking"
MODE_BRAKE_HOLD = "brake_hold"
MODE_PI = "normal"
MODE_HOLIDAY = "holiday"
MODE_ERROR = "error"
MODE_SAFE = "safe_mode"

_RESTORE_BRAKE_RAMP = "brake_ramp"
_RESTORE_BRAKE_LAST_T = "brake_last_t"
_RESTORE_BRAKE_LAST_EXPENSIVE_T = "brake_last_expensive_t"
_RESTORE_WAS_BRAKING = "was_braking_last_cycle"


class PumpSteerSensor(RestoreEntity):
    """
    PumpSteer heat pump controller.

    State machine:
        safe_mode    -> required temperature/price input missing or invalid
        summer_mode  -> outdoor >= summer_threshold, passthrough
        precool      -> forecast shows warm period coming, raise fake temp
        preheating   -> expensive period coming, bounded forecast-driven boost
        pre_braking  -> expensive period imminent, price-only brake ramp
        braking      -> expensive period active, PI integral frozen
        brake_hold   -> short gap before another expensive period, factor held
        holiday      -> lower target, otherwise same PI/price logic
        normal       -> PI regulates fake temp toward target
    """

    # Keep detailed, potentially large passive plans visible in HA without
    # duplicating them in Recorder on every controller state update. Qualified
    # outcome evidence is persisted separately in the bounded HA Store.
    _attr_has_entity_name = True
    _attr_translation_key = "control_temperature"
    _unrecorded_attributes = frozenset(
        {
            "thermal_intelligence_plan_detail",
            "thermal_intelligence_plan_outcomes",
            "thermal_fixed_horizon_audit",
            "thermal_intelligence_grid_marginal",
        }
    )

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.hass = hass
        self._config_entry = config_entry
        self._control_config = {
            k: v for k, v in self._cfg().items() if not k.startswith("zone_")
        }
        self._state: Optional[float] = None
        self._attributes: Dict[str, Any] = {}
        # Only live, qualified inputs; never populated from restored attributes.
        self._input_temperatures: Dict[str, Optional[float]] = {}
        self._input_temperature_signal = (
            f"{DOMAIN}_{config_entry.entry_id}_input_temperatures"
        )
        self._ohmigo_last_push = None
        self._temperature_reported_after: Optional[datetime] = None
        self._remove_start_listener = None
        self._temperature_unloaded = False
        self._update_in_progress = False
        self._options_update_pending = False

        self._pi = PIController()
        self._pi_precool_paused: bool = False
        self._thermal_model = ThermalModel()
        self._pilot = None
        from .pilot_forecast_audit import ForecastAudit

        self._plan_audit = ForecastAudit()
        self._plan_audit_loaded = False
        self._pilot_store_read_failed = False
        self._plan_cycle_identity = None
        self._pilot_persisted = None
        self._pilot_sources = None
        self._pilot_detector = None
        self._pilot_detection_after = None
        self._pilot_heat_source = "unavailable"
        self._pilot_status = "heat_signal_not_configured"
        self._pilot_operating_mode = "unknown"
        self._pilot_operating_quality = "insufficient_evidence"
        self._heat_operation = None
        self._heat_operation_signature = None
        self._heat_operation_detail = {}
        self._pilot_operation_checked_at = None
        self._pilot_price_available = False
        self._pilot_forecast_available = False
        self._pilot_price_forecast = False
        self._pilot_grid_marginal = {"status": "unavailable", "reason": "not_evaluated"}
        self._pilot_price_context = None
        self._pilot_plan = {"planning_status": "unavailable", "reason": "not_evaluated"}
        self._pilot_plan_next = None
        self._pilot_plan_signature = None
        self._pilot_replay_state_signature = None
        self._pilot_house_candidate = None
        self._pilot_house_candidate_at = None
        self._pilot_house_candidate_owner = None
        self._pilot_weather_raw = None
        self._pilot_holiday_current = None
        self._pilot_hot_water_checked_at = None
        self._power_ledger = None
        self._power_unsubscribe = None
        self._power_signature = None
        self._power_last_report = None
        self._pilot_store = None
        self._pilot_saved = None
        self._pilot_restore = None
        # Thermal Learning is observational and cannot change control output.
        self._thermal_learning_active: bool = False
        self._thermal_learning_reason: str = "not_evaluated"
        self._last_outlook: Optional[Any] = None
        self._last_pi_result = None

        self._brake_ramp: float = 0.0
        self._preheat_ramp: float = 0.0
        self._preheat_last_t: Optional[datetime] = None
        self._brake_last_t: Optional[datetime] = None
        self._brake_last_expensive_t: Optional[datetime] = None

        # Track whether the previous control cycle had active braking.
        # Used to trigger a thermal-model fit only when a brake phase has ended.
        self._was_braking_last_cycle: bool = False
        self._ramp_in_minutes: float = 15.0
        self._ramp_out_minutes: float = 15.0

        self._last_price_categories: List[str] = []
        self._p30: float = 0.0
        self._p80: float = 0.0
        self._current_price: Optional[float] = None
        self._current_price_unit: Optional[str] = None

        # Cache price thresholds once per calendar day per entity.
        # Recomputing every hour caused mid-slot reclassification: P80 could
        # shift just enough to flip an ongoing expensive slot to normal,
        # releasing the brake unexpectedly. Caching per day means thresholds
        # are stable throughout the day and only refresh when new price data
        # arrives at midnight.
        self._price_thresholds_cached_date: Optional[str] = None  # "YYYY-MM-DD"
        self._price_thresholds_entity_id: Optional[str] = None
        self._cached_p30: float = 0.0
        self._cached_p80: float = 0.0

        # Track previous aggressiveness to detect transition into aggressiveness=0.
        # The PI reset should happen once on entry, not on every cycle.
        self._prev_aggressiveness: Optional[int] = None
        self._prev_mode: Optional[str] = None
        self._bridge_short_dip_active: bool = False

        # Store the remove callback returned by add_update_listener so it can
        # be cleaned up on unload.
        self._remove_update_listener = None

        self._attr_unique_id = config_entry.entry_id
        # Keep the legacy default ID used by notifications before registration.
        # HA still resolves existing (including user-renamed) entities by unique_id.
        self.entity_id = "sensor.pumpsteer"
        self._forecast_available_last: Optional[bool] = None
        self._safe_mode_warned: bool = False
        self._helper_fallback_issues: Dict[str, Optional[str]] = {
            "target_temperature": None,
            "summer_threshold": None,
            "aggressiveness": None,
            "house_inertia": None,
        }
        self._price_issue: Optional[str] = None
        self._attr_unit_of_measurement = "°C"
        self._attr_device_class = "temperature"
        self._attr_state_class = "measurement"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name="PumpSteer",
            manufacturer="PumpSteer",
            model="Heat Pump Controller",
            sw_version=SW_VERSION,
        )

    @property
    def unique_id(self) -> str:
        return self._attr_unique_id

    @property
    def state(self) -> StateType:
        return self._state

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {**self._attributes}
        attrs.pop("extra_temperature_zones", None)
        if getattr(self, "_zone_diagnostics", None):
            attrs["extra_temperature_zones"] = self._zone_diagnostics
        if hasattr(self, "_temperature_coverage"):
            attrs["temperature_sources"] = self._temperature_coverage
        for key in (
            "thermal_intelligence_status",
            "thermal_intelligence_reason",
            "thermal_intelligence_learning_intervals",
            "thermal_intelligence_plan",
            "thermal_intelligence_forecast",
            "thermal_intelligence_price",
            "thermal_intelligence_price_forecast",
            "thermal_intelligence_grid_marginal",
            "thermal_intelligence_model",
            "thermal_intelligence_physical_freshness",
            "thermal_intelligence_external_heat",
            "thermal_intelligence_planning_status",
            "thermal_intelligence_plan_detail",
            "thermal_intelligence_forecast_quality",
            "thermal_intelligence_plan_outcomes",
            "thermal_intelligence_electrical",
            "thermal_intelligence_heat_source",
            "thermal_intelligence_response",
            "thermal_intelligence_operating_mode",
            "thermal_intelligence_operating_quality",
            "thermal_intelligence_operation_detail",
        ):
            attrs.pop(key, None)
        if self._cfg().get("thermal_intelligence_enabled") is True:
            interrupted = (
                attrs.get("mode") == MODE_SAFE or attrs.get("status") == "error"
            )
            attrs["thermal_intelligence_operating_mode"] = (
                "unknown" if interrupted else self._pilot_operating_mode
            )
            attrs["thermal_intelligence_operating_quality"] = (
                "insufficient_evidence"
                if interrupted
                else self._pilot_operating_quality
            )
            attrs["thermal_intelligence_operation_detail"] = (
                {} if interrupted else self._heat_operation_detail
            )
            attrs["thermal_intelligence_status"] = (
                "suspended_safe"
                if attrs.get("mode") == MODE_SAFE
                else self._pilot_status
            )
            if attrs.get("mode") == MODE_SAFE:
                reason = "safe_mode"
            elif attrs.get("status") == "error":
                reason = "update_error"
            elif self._pilot_status in {
                "heat_signal_not_configured",
                "heat_signal_unavailable",
                "temperature_report_unavailable",
                "hot_water_active",
                "hot_water_mode_unavailable",
                "history_unavailable",
            }:
                reason = self._pilot_status
            elif not self._pilot_price_forecast:
                reason = "timestamped_price_forecast_unavailable"
            elif not self._pilot_forecast_available:
                reason = "weather_forecast_unavailable"
            else:
                reason = "validated_model_and_weather_issue_time_unavailable"
            attrs["thermal_intelligence_reason"] = reason
            attrs["thermal_intelligence_learning_intervals"] = (
                self._pilot.house_intervals if self._pilot else 0
            )
            plan = (
                self._pilot_plan
                if not interrupted
                else self._unavailable_pilot_plan("control_interrupted")
            )
            attrs["thermal_intelligence_plan"] = (
                "hypothetical_only"
                if plan["planning_status"] == "hypothetical_unvalidated"
                else "validated_passive"
                if plan["planning_status"] == "validated_passive"
                else "not_available"
            )
            attrs["thermal_intelligence_planning_status"] = plan["planning_status"]
            attrs["thermal_intelligence_plan_detail"] = plan
            if (
                self._plan_audit.plan_pending
                or self._plan_audit.plan_records
                or self._plan_audit.plan_unverifiable
            ):
                cfg = self._cfg()
                attrs["thermal_intelligence_plan_outcomes"] = (
                    self._plan_audit.plan_summary(
                        cfg.get("indoor_temp_entity") or "",
                        self._plan_current_identity(cfg),
                    )
                )
            if self._power_ledger is not None:
                attrs["thermal_intelligence_electrical"] = self._power_ledger.summary()
            if (
                self._pilot is not None
                and self._pilot.audit.records
                and not interrupted
            ):
                revision = self._pilot.audit.records[-1]["revision"]
                attrs["thermal_intelligence_forecast_quality"] = {
                    "model_revision": revision,
                    **self._pilot.audit.summary(revision),
                }
            attrs["thermal_intelligence_forecast"] = (
                "weather_outlook_unverified"
                if not interrupted and self._pilot_forecast_available
                else "unavailable"
            )
            attrs["thermal_intelligence_price"] = (
                "current_available"
                if not interrupted and self._pilot_price_available
                else "unavailable"
            )
            attrs["thermal_intelligence_price_forecast"] = (
                "coverage_complete_issue_time_unknown"
                if not interrupted and self._pilot_price_forecast
                else "unavailable"
            )
            attrs["thermal_intelligence_grid_marginal"] = (
                {"status": "unavailable", "reason": "control_interrupted"}
                if interrupted
                else self._pilot_grid_marginal
            )
            attrs["thermal_intelligence_model"] = (
                "validated_conditional"
                if plan["planning_status"] == "validated_passive"
                else "unvalidated"
            )
            attrs["thermal_intelligence_heat_source"] = (
                self._pilot_heat_source if not interrupted else "unavailable"
            )
            attrs["thermal_intelligence_response"] = (
                self._pilot.manager.pump.reason
                if self._pilot is not None and not interrupted
                else "unavailable"
            )
            attrs["thermal_intelligence_physical_freshness"] = "unverified"
            attrs["thermal_intelligence_external_heat"] = (
                "background_unverified"
                if self._cfg().get("external_heat_sources") is True
                else "not_enabled"
            )
        return attrs

    @property
    def unit_of_measurement(self) -> str:
        return self._attr_unit_of_measurement

    @property
    def device_class(self) -> str:
        return self._attr_device_class

    @property
    def icon(self) -> str:
        return "mdi:thermostat-box"

    @property
    def available(self) -> bool:
        return self._state is not None

    @property
    def should_poll(self) -> bool:
        return True

    @property
    def extra_restore_state_data(self):
        """Persist brake ramp state across restarts."""
        from homeassistant.helpers.restore_state import ExtraStoredData

        class _BrakeData(ExtraStoredData):
            def __init__(
                self,
                brake_ramp,
                brake_last_t,
                brake_last_expensive_t,
                was_braking,
                zones,
            ):
                self.zones = zones
                self.brake_ramp = brake_ramp
                self.brake_last_t = brake_last_t
                self.brake_last_expensive_t = brake_last_expensive_t
                self.was_braking = was_braking

            def as_dict(self):
                return {
                    "extra_temperature_zones": self.zones,
                    _RESTORE_WAS_BRAKING: self.was_braking,
                    _RESTORE_BRAKE_RAMP: self.brake_ramp,
                    _RESTORE_BRAKE_LAST_T: (
                        self.brake_last_t.isoformat() if self.brake_last_t else None
                    ),
                    _RESTORE_BRAKE_LAST_EXPENSIVE_T: (
                        self.brake_last_expensive_t.isoformat()
                        if self.brake_last_expensive_t
                        else None
                    ),
                }

        return _BrakeData(
            self._brake_ramp,
            self._brake_last_t,
            self._brake_last_expensive_t,
            self._was_braking_last_cycle,
            getattr(self, "_zone_diagnostics", getattr(self, "_zone_restore", {})),
        )

    async def async_added_to_hass(self) -> None:
        self._temperature_reported_after = dt_util.now()
        self._temperature_unloaded = False
        await super().async_added_to_hass()
        self._setup_power_telemetry()
        if self._cfg().get("thermal_intelligence_enabled") is True:
            await self._load_pilot_store()

        self._remove_update_listener = self._config_entry.add_update_listener(
            self.async_options_update_listener
        )

        # Restore basic state (fake temp + attributes).
        last = await self.async_get_last_state()
        if last is not None:
            restored = safe_float(last.state)
            if restored is not None:
                self._state = restored
                self._attributes = {
                    key: value
                    for key, value in last.attributes.items()
                    if key != "friendly_name"
                }

        # Restore brake ramp state so braking survives HA restarts.
        extra = await self.async_get_last_extra_data()
        if extra is not None:
            data = extra.as_dict()
            self._zone_restore = data.get("extra_temperature_zones", {})
            # Older timestamps also record pre-brake/precool requests, so they
            # cannot prove that an expensive braking phase actually occurred.
            self._was_braking_last_cycle = data.get(_RESTORE_WAS_BRAKING) is True
            self._brake_ramp = float(data.get(_RESTORE_BRAKE_RAMP, 0.0))
            raw_last_t = data.get(_RESTORE_BRAKE_LAST_T)
            raw_last_exp = data.get(_RESTORE_BRAKE_LAST_EXPENSIVE_T)
            try:
                self._brake_last_t = (
                    datetime.fromisoformat(raw_last_t) if raw_last_t else None
                )
            except (ValueError, TypeError):
                self._brake_last_t = None
            try:
                self._brake_last_expensive_t = (
                    datetime.fromisoformat(raw_last_exp) if raw_last_exp else None
                )
            except (ValueError, TypeError):
                self._brake_last_expensive_t = None
        _LOGGER.debug(
            "PumpSteer restored brake state: ramp=%.3f last_t=%s last_exp=%s",
            self._brake_ramp,
            self._brake_last_t,
            self._brake_last_expensive_t,
        )

        saved_k = safe_float(
            self._attributes.get("thermal_k_restore", self._attributes.get("thermal_k"))
        )
        if saved_k is not None and self._attributes.get("thermal_k_valid") is True:
            self._thermal_model.restore_k(saved_k)
            self._thermal_model.restore_diagnostics(
                sample_count=self._attributes.get("thermal_k_samples", 0),
                fit_rmse=self._attributes.get("thermal_fit_rmse_c_per_hour"),
                learning_sessions=self._attributes.get("thermal_learning_sessions", 0),
                validated_sessions=self._attributes.get(
                    "thermal_validated_sessions", 0
                ),
                prediction_mae=self._attributes.get("thermal_prediction_mae_c"),
                validation_basis=self._attributes.get("thermal_validation_basis"),
                last_prediction_error=self._attributes.get(
                    "thermal_last_prediction_error_c"
                ),
                last_session_duration_minutes=self._attributes.get(
                    "thermal_last_session_duration_minutes"
                ),
                last_session_actual_drop=self._attributes.get(
                    "thermal_last_session_actual_drop_c"
                ),
                last_session_predicted_drop=self._attributes.get(
                    "thermal_last_session_predicted_drop_c"
                ),
            )

        # Wait until Home Assistant is fully started before the first real update.
        # If HA is already running (for example on reload), update immediately.
        if self.hass.is_running:
            await self.async_update()
            self.async_write_ha_state()
        else:
            self._remove_start_listener = self.hass.bus.async_listen_once(
                "homeassistant_started", self._handle_ha_started
            )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel options update listener on unload."""
        self._temperature_unloaded = True
        pending = list(getattr(self, "_zone_notify_tasks", {}).values())
        for _, task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)
        self._input_temperatures = {}
        async_dispatcher_send(self.hass, self._input_temperature_signal)
        if self._cfg().get("thermal_intelligence_enabled") is True:
            try:
                self._plan_audit.cancel_plans("unload")
                await self._save_pilot_store()
                await self._flush_pilot_store()
            except Exception:
                _LOGGER.exception("Passive plan audit cleanup failed")
        self._stop_power_telemetry()
        if self._remove_start_listener is not None:
            self._remove_start_listener()
            self._remove_start_listener = None
        if self._remove_update_listener is not None:
            self._remove_update_listener()
            self._remove_update_listener = None
        await super().async_will_remove_from_hass()

    async def async_options_update_listener(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Handle updated options."""
        self._config_entry = entry
        control_config = {
            k: v for k, v in self._cfg().items() if not k.startswith("zone_")
        }
        if control_config == self._control_config:
            if self._update_in_progress:
                self._options_update_pending = True
            else:
                await self.async_update()
            return
        self._control_config = control_config
        if (
            self._heat_operation is not None
            and self._cfg().get("thermal_intelligence_enabled") is True
        ):
            try:
                await self._save_pilot_store()
                await self._flush_pilot_store()
            except Exception:
                _LOGGER.exception("Passive heat history cleanup failed")
        if self._cfg().get("thermal_intelligence_enabled") is not True:
            try:
                self._plan_audit.cancel_plans("disabled")
                await self._save_pilot_store()
                await self._flush_pilot_store()
                self._pilot_store = None
            except Exception:
                _LOGGER.exception("Passive plan audit cleanup failed")
        self._setup_power_telemetry()
        self._heat_operation = None
        self._heat_operation_signature = None
        self._heat_operation_detail = {}
        self._pilot_saved = None
        self._pilot = None
        self._pilot_sources = None
        self._pilot_price_available = False
        self._pilot_forecast_available = False
        self._pilot_price_forecast = False
        self._pilot_grid_marginal = {
            "status": "unavailable",
            "reason": "options_changed",
        }
        self._pilot_plan = self._unavailable_pilot_plan("options_changed")
        self._pilot_plan_next = None
        self._pilot_weather_raw = None
        self._pilot_restore = None
        self._pilot_saved = None
        if self._cfg().get("thermal_intelligence_enabled") is True:
            await self._load_pilot_store()
        if self._update_in_progress:
            self._options_update_pending = True
            return
        await self.async_update()

    async def _handle_ha_started(self, event) -> None:
        """Run when HA is fully started and entities are ready."""
        self._remove_start_listener = None
        now = dt_util.now()
        fired = getattr(event, "time_fired", None)
        # Producers may report after the event but before this coroutine runs.
        self._temperature_reported_after = (
            fired
            if isinstance(fired, datetime)
            and fired.utcoffset() is not None
            and fired <= now
            else now
        )
        await self.async_update()
        self.async_write_ha_state()

    def _cfg(self) -> Dict[str, Any]:
        return {**self._config_entry.data, **self._config_entry.options}

    def _read_entity(
        self,
        entity_id: str,
        default: Optional[float] = None,
    ) -> Optional[float]:
        return safe_float(get_state(self.hass, entity_id)) if entity_id else default

    def _number_entity_id(self, key: str) -> Optional[str]:
        """Look up entity_id for a PumpSteer NumberEntity by its unique_id."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        return registry.async_get_entity_id(
            "number", DOMAIN, f"{self._config_entry.entry_id}_{key}"
        )

    def _preheat_enabled(self, cfg: Dict[str, Any]) -> bool:
        """Return True if preheat boost is enabled via UI switch or options fallback."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "switch", DOMAIN, f"{self._config_entry.entry_id}_preheat_enabled"
        )

        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is not None:
                return state.state == "on"

        return bool(cfg.get("preheat_boost_enabled", True))

    def _aggressiveness(self, cfg: Dict[str, Any]) -> int:
        value = self._helper_value_with_fallback(
            helper_key="aggressiveness",
            cfg=cfg,
            default=float(DEFAULT_AGGRESSIVENESS),
        )
        return max(0, min(5, int(round(float(value)))))

    def _house_inertia(self, cfg: Dict[str, Any]) -> float:
        value = self._helper_value_with_fallback(
            helper_key="house_inertia",
            cfg=cfg,
            default=float(DEFAULT_HOUSE_INERTIA),
        )
        return max(0.5, min(10.0, float(value)))

    def _set_helper_issue(self, helper_key: str, issue: str, message: str) -> None:
        """Warn once per helper issue and downgrade repeats to debug."""
        previous = self._helper_fallback_issues.get(helper_key)
        if previous != issue:
            _LOGGER.warning(message)
            self._helper_fallback_issues[helper_key] = issue
        else:
            _LOGGER.debug(message)

    def _clear_helper_issue(self, helper_key: str, entity_id: Optional[str]) -> None:
        """Log helper recovery once when a fallback issue clears."""
        previous = self._helper_fallback_issues.get(helper_key)
        if previous is not None:
            _LOGGER.info(
                "PumpSteer helper '%s' recovered and now uses %s",
                helper_key,
                entity_id or "configured option value",
            )
            self._helper_fallback_issues[helper_key] = None

    def _helper_value_with_fallback(
        self,
        helper_key: str,
        cfg: Dict[str, Any],
        default: float,
    ) -> float:
        """Read helper value with warn-once fallback logging."""
        cfg_raw = cfg.get(helper_key)
        if cfg_raw is not None:
            parsed_cfg = safe_float(cfg_raw)
            if parsed_cfg is not None:
                self._clear_helper_issue(helper_key, None)
                return parsed_cfg
            self._set_helper_issue(
                helper_key,
                "invalid_option",
                (
                    f"PumpSteer helper '{helper_key}' has invalid configured value "
                    f"'{cfg_raw}', falling back to default {default}"
                ),
            )
            return default

        entity_id = self._number_entity_id(helper_key)
        if not entity_id:
            self._set_helper_issue(
                helper_key,
                "missing_entity",
                (
                    f"PumpSteer helper '{helper_key}' has no number entity "
                    f"(entry_id={self._config_entry.entry_id}), falling back to default "
                    f"{default}"
                ),
            )
            return default

        state_obj = self.hass.states.get(entity_id)
        if state_obj is None:
            self._set_helper_issue(
                helper_key,
                "entity_not_found",
                (
                    f"PumpSteer helper '{helper_key}' entity '{entity_id}' not found, "
                    f"falling back to default {default}"
                ),
            )
            return default

        state_raw = state_obj.state
        if state_raw in ("unknown", "unavailable", None):
            self._set_helper_issue(
                helper_key,
                "temporarily_unavailable",
                (
                    f"PumpSteer helper '{helper_key}' entity '{entity_id}' is "
                    f"temporarily unavailable (state={state_raw}), falling back to "
                    f"default {default}"
                ),
            )
            return default

        parsed = safe_float(state_raw)
        if parsed is None:
            self._set_helper_issue(
                helper_key,
                "invalid_state",
                (
                    f"PumpSteer helper '{helper_key}' entity '{entity_id}' has invalid "
                    f"state '{state_raw}', falling back to default {default}"
                ),
            )
            return default

        self._clear_helper_issue(helper_key, entity_id)
        return parsed

    def _set_price_issue(self, issue: str, message: str) -> None:
        """Warn once per price issue and downgrade repeats to debug."""
        if self._price_issue != issue:
            _LOGGER.warning(message)
            self._price_issue = issue
        else:
            _LOGGER.debug(message)

    def _clear_price_issue(self, today_entity_id: str, tomorrow_entity_id: str) -> None:
        """Log price data recovery once when an issue clears."""
        if self._price_issue is not None:
            _LOGGER.info(
                "PumpSteer price data recovered for today='%s', tomorrow='%s'",
                today_entity_id,
                tomorrow_entity_id,
            )
            self._price_issue = None

    def _comfort_floor(self, target: float, aggressiveness: int) -> float:
        drop = COMFORT_FLOOR_BY_AGGRESSIVENESS[aggressiveness]
        return target - drop

    def _preheat_headroom(self, aggressiveness: int) -> float:
        """Return maximum indoor temperature headroom allowed for preheat."""
        return PREHEAT_HEADROOM_BY_AGGRESSIVENESS[aggressiveness]

    @staticmethod
    def _preheat_headroom_factor(
        indoor: float,
        target: float,
        headroom: float,
    ) -> float:
        """Return 0-1 preheat allowance based on remaining thermal headroom."""
        if headroom <= 0.0:
            return 0.0
        if indoor <= target:
            return 1.0

        ceiling = target + headroom
        if indoor >= ceiling:
            return 0.0

        return max(0.0, min(1.0, (ceiling - indoor) / headroom))

    def _brake_temp(self, outdoor: float, delta_c: Optional[float] = None) -> float:
        delta = delta_c if delta_c is not None else BRAKE_DELTA_C
        return min(max(outdoor + delta, MIN_FAKE_TEMP), MAX_FAKE_TEMP)

    def _compute_ramp_minutes(self, house_inertia: float) -> float:
        ramp = house_inertia * RAMP_SCALE
        return max(RAMP_MIN_MINUTES, min(RAMP_MAX_MINUTES, ramp))

    def _next_period_category(
        self,
        categories: List[str],
        current_slot: int,
        interval_minutes: int,
    ) -> Optional[str]:
        del interval_minutes
        next_slot = current_slot + 1
        if next_slot < len(categories):
            return categories[next_slot]
        return None

    def _minutes_until_expensive(
        self,
        categories: List[str],
        current_slot: int,
        interval_minutes: int,
        now: datetime,
    ) -> Optional[float]:
        for index in range(1, len(categories) - current_slot):
            if categories[current_slot + index] == PRICE_EXPENSIVE:
                minutes_into_slot = now.minute % interval_minutes
                minutes_left_in_slot = interval_minutes - minutes_into_slot
                minutes_total = minutes_left_in_slot + (index - 1) * interval_minutes
                return float(minutes_total)
        return None

    def _upcoming_expensive(
        self,
        categories: List[str],
        current_slot: int,
        lookahead_slots: int,
    ) -> bool:
        for index in range(
            current_slot + 1,
            min(len(categories), current_slot + lookahead_slots + 1),
        ):
            if categories[index] == PRICE_EXPENSIVE:
                return True
        return False

    async def _forecast_temps(self) -> Optional[List[float]]:
        cfg = self._cfg()
        weather_entity = cfg.get("weather_entity")
        price_entity = cfg.get("electricity_price_entity")

        if not weather_entity or not price_entity:
            if self._forecast_available_last is not False:
                _LOGGER.debug(
                    "PumpSteer forecast unavailable: %s",
                    (
                        "weather_entity not configured"
                        if not weather_entity
                        else "electricity_price_entity not configured"
                    ),
                )
                self._forecast_available_last = False
            return None

        from .forecast import async_build_forecast

        try:
            points = await async_build_forecast(
                self.hass,
                price_entity_id=price_entity,
                weather_entity_id=weather_entity,
                horizon_hours=PRECOOL_LOOKAHEAD,
            )
        except Exception as err:
            if self._forecast_available_last is not False:
                _LOGGER.debug("PumpSteer forecast build failed: %s", err)
                self._forecast_available_last = False
            return None

        temps = [p.outdoor_temp for p in points if p.outdoor_temp is not None]
        available_now = bool(temps)

        if available_now != self._forecast_available_last:
            if available_now:
                _LOGGER.debug(
                    "PumpSteer forecast available (weather=%s, %d points)",
                    weather_entity,
                    len(temps),
                )
            else:
                _LOGGER.debug(
                    "PumpSteer forecast returned no usable temperatures "
                    "(weather=%s, price=%s)",
                    weather_entity,
                    price_entity,
                )
            self._forecast_available_last = available_now

        return temps if temps else None

    def _should_precool(
        self, summer_threshold: float, temps: Optional[List[float]]
    ) -> bool:
        if not temps:
            return False
        return any(temp >= summer_threshold + PRECOOL_MARGIN for temp in temps)

    def _forecast_is_cold(
        self, summer_threshold: float, temps: Optional[List[float]], hours: int = 6
    ) -> bool:
        if not temps:
            if PREHEAT_ON_MISSING_FORECAST:
                _LOGGER.debug(
                    "_forecast_is_cold: no forecast, assuming cold "
                    "(PREHEAT_ON_MISSING_FORECAST=True)"
                )
            else:
                _LOGGER.debug(
                    "_forecast_is_cold: no forecast, assuming not cold "
                    "(PREHEAT_ON_MISSING_FORECAST=False)"
                )
            return PREHEAT_ON_MISSING_FORECAST

        window = temps[:hours]
        cold_hours = sum(1 for temp in window if temp < summer_threshold)
        return cold_hours >= max(1, len(window) // 2)

    def _base_attrs(
        self,
        indoor: float,
        target: float,
        outdoor: float,
        price_category: str,
        aggressiveness: int,
        heating_demand_c: float = 0.0,
    ) -> Dict[str, Any]:
        """Return standard attributes included in all modes."""
        return {
            "heating_demand_c": round(heating_demand_c, 2),
            "indoor_temperature": indoor,
            "target_temperature": target,
            "outdoor_temperature": outdoor,
            "price_category": price_category,
            "current_price": (
                round(self._current_price, 4)
                if self._current_price is not None
                else None
            ),
            "current_price_unit": self._current_price_unit,
            "aggressiveness": aggressiveness,
            "p30": round(self._p30, 3),
            "p80": round(self._p80, 3),
            "pi_error_c": (
                round(self._last_pi_result.error, 3)
                if self._last_pi_result is not None
                else None
            ),
            "pi_p_term": (
                round(self._last_pi_result.p_term, 3)
                if self._last_pi_result is not None
                else None
            ),
            "pi_i_term": (
                round(self._last_pi_result.i_term, 3)
                if self._last_pi_result is not None
                else None
            ),
            "thermal_k": round(self._thermal_model.k, 4),
            "thermal_k_restore": self._thermal_model.k,
            "thermal_k_valid": self._thermal_model.is_valid,
            "thermal_k_samples": self._thermal_model.sample_count,
            "thermal_pending_samples": self._thermal_model.pending_samples,
            "thermal_confidence": round(self._thermal_model.confidence, 3),
            "thermal_confidence_percent": round(
                self._thermal_model.confidence * 100, 1
            ),
            "thermal_fit_rmse_c_per_hour": (
                round(self._thermal_model.fit_rmse, 3)
                if self._thermal_model.fit_rmse is not None
                else None
            ),
            "thermal_learning_active": self._thermal_learning_active,
            "thermal_learning_reason": self._thermal_learning_reason,
            "thermal_learning_sessions": self._thermal_model.learning_sessions,
            "thermal_validated_sessions": self._thermal_model.validated_sessions,
            "thermal_validation_basis": VALIDATION_BASIS,
            "thermal_fixed_horizon_audit": (
                self._plan_audit.fixed_summary(self._fixed_audit_identity(self._cfg()))
                if self._cfg().get("thermal_intelligence_enabled") is True
                else None
            ),
            "thermal_conditional_drop_60m_c": (
                self._thermal_model.predict_bounded_drop(indoor, outdoor, 60)
                if self._thermal_learning_active
                and self._thermal_model.is_valid
                and indoor is not None
                and outdoor is not None
                else None
            ),
            "thermal_conditional_forecast_status": "experimental_unvalidated",
            "thermal_prediction_mae_c": (
                round(self._thermal_model.prediction_mae, 3)
                if self._thermal_model.prediction_mae is not None
                else None
            ),
            "thermal_last_prediction_error_c": (
                round(self._thermal_model.last_prediction_error, 3)
                if self._thermal_model.last_prediction_error is not None
                else None
            ),
            "thermal_last_session_duration_minutes": (
                round(self._thermal_model.last_session_duration_minutes, 1)
                if self._thermal_model.last_session_duration_minutes is not None
                else None
            ),
            "thermal_last_session_actual_drop_c": (
                round(self._thermal_model.last_session_actual_drop, 3)
                if self._thermal_model.last_session_actual_drop is not None
                else None
            ),
            "thermal_last_session_predicted_drop_c": (
                round(self._thermal_model.last_session_predicted_drop, 3)
                if self._thermal_model.last_session_predicted_drop is not None
                else None
            ),
        }

    def _update_thermal_learning(
        self,
        *,
        active: bool,
        reason: str,
        now: datetime,
        indoor: float,
        outdoor: float,
    ) -> None:
        """Observe stable braking without changing the heat pump control path."""
        was_active = self._thermal_learning_active
        self._thermal_learning_active = active
        self._thermal_learning_reason = reason

        if not active:
            if self._thermal_model.validation_session_active:
                self._thermal_model.end_validation_session(now, indoor)
            return

        if not was_active and self._cfg().get("thermal_intelligence_enabled") is True:
            cfg = self._cfg()
            if self._thermal_model.is_valid:
                self._plan_audit.issue_fixed(
                    now,
                    indoor,
                    outdoor,
                    self._thermal_model.k,
                    self._fixed_audit_identity(cfg),
                )
        if not was_active:
            # Never infer the stable brake cooling rate from earlier PI/ramp history.
            self._thermal_model.reset_temp_history(now, indoor)

        if not self._thermal_model.validation_session_active:
            self._thermal_model.start_validation_session(now, indoor, outdoor)
        elif not self._thermal_model.update_validation_session(now, indoor, outdoor):
            # An interrupted session is discarded, then restarted next cycle.
            self._thermal_learning_reason = "observation_gap"

        self._thermal_model.collect_braking_sample(indoor, outdoor)

    async def _enter_safe_mode(
        self,
        reason: str,
        outdoor: Optional[float],
        now: datetime,
    ) -> None:
        self._pilot_plan = self._unavailable_pilot_plan("safe_mode")
        self._pilot_detector = None
        self._pilot_detection_after = now
        self._pilot_plan_next = None
        self._pilot_weather_raw = None
        if self._pilot is not None:
            if self._cfg().get("thermal_intelligence_enabled") is True:
                self._pilot.reset_interval()
                self._pilot.status = "suspended_safe"
            else:
                self._pilot = None
                self._pilot_sources = None
        self._thermal_model.cancel_validation_session()
        self._thermal_learning_active = False
        self._thermal_learning_reason = "safe_mode"
        if not self._safe_mode_warned:
            _LOGGER.warning(
                "PumpSteer entered SAFE MODE: %s — optimization paused",
                reason,
            )
            self._safe_mode_warned = True
            log_event("SAFE_MODE_ENTER", reason=reason)
        else:
            _LOGGER.debug("PumpSteer safe mode still active: %s", reason)

        self._pi.reset(now)
        self._pi_precool_paused = False
        self._last_pi_result = None
        self._brake_ramp = 0.0
        self._preheat_ramp = 0.0
        self._preheat_last_t = None
        self._brake_last_t = None
        self._brake_last_expensive_t = None
        self._was_braking_last_cycle = False

        if outdoor is not None:
            self._state = round(outdoor, 1)
            self._attributes = {
                "mode": MODE_SAFE,
                "fake_outdoor_temperature": self._state,
                "status": f"safe_mode: {reason}",
                "last_updated": now.isoformat(),
                "outdoor_temperature": outdoor,
            }
            await self._push_validated_temperature(
                outdoor, self._cfg(), {"real_outdoor_entity": outdoor}
            )
        else:
            self._state = None
            self._attributes = {
                "mode": MODE_SAFE,
                "status": f"safe_mode (no data): {reason}",
                "last_updated": now.isoformat(),
                "receiver_state": "unverified",
            }
        if self._cfg().get("thermal_intelligence_enabled") is True:
            try:
                self._plan_audit.cancel_plans("safe_mode")
                await self._save_pilot_store()
            except Exception:
                _LOGGER.exception("Passive plan audit cleanup failed in safe mode")

    def _temperature_cutoff(self, cfg, *, outdoor=False):
        """Use a configured HA-report age, without claiming physical freshness.

        The lifecycle startup gate remains in force. The age limit is a bound
        on HA reports, not a claim about physical sampling.
        """
        # Before HA lifecycle setup, offline callers can lack report timestamps.
        # A running integration always establishes a startup cutoff.
        configured_default = (
            DEFAULT_OUTDOOR_REPORT_MAX_AGE_MINUTES
            if outdoor
            else DEFAULT_TEMPERATURE_REPORT_MAX_AGE_MINUTES
        )
        if outdoor:
            legacy_limit = safe_float(cfg.get("temperature_report_max_age_minutes"))
            if legacy_limit is not None and legacy_limit > 0:
                configured_default = min(configured_default, legacy_limit)
        key = (
            "outdoor_temperature_report_max_age_minutes"
            if outdoor
            else "temperature_report_max_age_minutes"
        )
        default = (
            configured_default if self._temperature_reported_after is not None else 0
        )
        minutes = safe_float(cfg.get(key, default))
        if minutes is None or minutes <= 0:
            if self._temperature_reported_after is None:
                return None
            minutes = configured_default
        cutoff = dt_util.now() - timedelta(minutes=minutes)
        if self._temperature_reported_after is not None:
            cutoff = max(cutoff, self._temperature_reported_after)
        return cutoff

    def _temperature_state(self, cfg, key):
        """Read the selected control temperatures and their actual HA reports."""
        from types import SimpleNamespace

        cutoff = self._temperature_cutoff(cfg, outdoor=key == "real_outdoor_entity")
        if key == "real_outdoor_entity":
            primary, backup = (
                cfg.get("real_outdoor_entity"),
                cfg.get("outdoor_temp_entity_2"),
            )
            active = getattr(self, "_outdoor_active_entity", None)
            if (
                active == backup
                and backup
                and get_temperature(self.hass, backup, cutoff) is not None
                and (getattr(self.hass.states.get(backup), "attributes", {}) or {}).get(
                    "unit_of_measurement"
                )
                in (None, "°C")
            ):
                return self.hass.states.get(backup)
            for entity_id in (primary, backup):
                state = self.hass.states.get(entity_id) if entity_id else None
                unit = (getattr(state, "attributes", {}) or {}).get(
                    "unit_of_measurement"
                )
                if get_temperature(self.hass, entity_id, cutoff) is not None and (
                    entity_id != backup or unit in (None, "°C")
                ):
                    return state
            return None
        ids = [
            cfg.get("indoor_temp_entity"),
            cfg.get("indoor_temp_entity_2"),
            cfg.get("indoor_temp_entity_3"),
        ]
        if not any(ids[1:]):
            return (
                self.hass.states.get(ids[0])
                if get_temperature(self.hass, ids[0], cutoff) is not None
                else None
            )
        rows = []
        seen = set()
        for index, entity_id in enumerate(ids, 1):
            if not entity_id or entity_id in seen:
                continue
            seen.add(entity_id)
            weight = safe_float(cfg.get(f"indoor_temp_weight_{index}", 1))
            value = get_temperature(self.hass, entity_id, cutoff)
            state = self.hass.states.get(entity_id)
            unit = (getattr(state, "attributes", {}) or {}).get("unit_of_measurement")
            if (
                value is None
                or weight is None
                or weight <= 0
                or unit not in (None, "°C")
            ):
                continue
            rows.append((entity_id, weight, value, state))
        if not rows:
            return None
        total = sum(row[1] for row in rows)
        value = sum(row[1] * row[2] for row in rows) / total
        timestamps = [getattr(row[3], "last_reported", None) for row in rows]
        changes = [getattr(row[3], "last_changed", None) for row in rows]

        def timestamp(values, operation):
            try:
                return (
                    operation(values)
                    if all(
                        isinstance(v, datetime) and v.utcoffset() is not None
                        for v in values
                    )
                    else None
                )
            except (TypeError, ValueError):
                return None

        return SimpleNamespace(
            state=str(value),
            attributes={
                "unit_of_measurement": "°C",
                "source_temperatures_used": {r[0]: r[2] for r in rows},
                "source_weights": {r[0]: r[1] for r in rows},
                "physical_measurement_time": "unknown",
            },
            last_reported=timestamp(timestamps, min),
            last_changed=timestamp(changes, max),
            last_updated=timestamp(changes, max),
        )

    def _update_outdoor_selection(self, cfg):
        """Switch immediately on failure; require two good cycles to fail back."""
        primary = cfg.get("real_outdoor_entity")
        backup = cfg.get("outdoor_temp_entity_2")
        cutoff = self._temperature_cutoff(cfg, outdoor=True)
        good_primary = get_temperature(self.hass, primary, cutoff) is not None
        backup_state = self.hass.states.get(backup) if backup else None
        backup_unit = (getattr(backup_state, "attributes", {}) or {}).get(
            "unit_of_measurement"
        )
        good_backup = get_temperature(
            self.hass, backup, cutoff
        ) is not None and backup_unit in (None, "°C")
        active = getattr(self, "_outdoor_active_entity", None)
        recovery = getattr(self, "_outdoor_primary_recovery", 0)
        if active == backup and backup and good_backup:
            recovery = recovery + 1 if good_primary else 0
            active = primary if recovery >= 2 else backup
        else:
            active = primary if good_primary else backup if good_backup else None
            recovery = 0
        self._outdoor_active_entity = active
        self._outdoor_primary_recovery = recovery

    def _temperature_value(self, cfg, key):
        state = self._temperature_state(cfg, key)
        return safe_float(getattr(state, "state", None))

    def _temperature_identity(self, cfg):
        inside = self._temperature_state(cfg, "indoor_temp_entity")
        outside = self._temperature_state(cfg, "real_outdoor_entity")
        attrs = getattr(inside, "attributes", {}) or {}
        weights = attrs.get("source_weights")
        components = attrs.get("source_temperatures_used")
        inside_identity = (
            tuple(sorted(weights.items()))
            if isinstance(weights, dict)
            else (cfg.get("indoor_temp_entity"), tuple(sorted(components)))
            if isinstance(components, dict)
            and all(isinstance(k, str) for k in components)
            else cfg.get("indoor_temp_entity")
        )
        outside_id = None
        for key in ("real_outdoor_entity", "outdoor_temp_entity_2"):
            if outside is not None and self.hass.states.get(cfg.get(key)) is outside:
                outside_id = cfg.get(key)
                break
        outside_attrs = getattr(outside, "attributes", {}) or {}
        return inside_identity, outside_id, outside_attrs.get("selected_source")

    def _temperature_inputs_match(self, cfg, expected) -> bool:
        current_cfg = self._cfg()
        return (
            not self._temperature_unloaded
            and current_cfg == cfg
            and (
                not any(
                    cfg.get(k)
                    for k in (
                        "indoor_temp_entity_2",
                        "indoor_temp_entity_3",
                        "outdoor_temp_entity_2",
                    )
                )
                or not hasattr(self, "_temperature_cycle_identity")
                or self._temperature_cycle_identity == self._temperature_identity(cfg)
            )
            and all(
                value is not None
                and current_cfg.get(key) == cfg.get(key)
                and self._temperature_value(cfg, key) == value
                for key, value in expected.items()
            )
        )

    async def _temperature_interrupted(self, cfg) -> None:
        self._input_temperatures = {}
        # Options invalidation is retried; it must not reset PI/ramp history.
        if self._cfg() != cfg:
            return
        # No receiver reset/bypass is implied: its physical fallback is unknown.
        await self._enter_safe_mode(
            "Temperature inputs changed during update", None, dt_util.now()
        )
        self._attributes["temperature_input_status"] = "interrupted"

    async def _push_validated_temperature(self, fake_temp, cfg, expected) -> None:
        if not cfg.get("ohmigo_entity") and not cfg.get("modbus_service"):
            return
        interrupted = False

        def check():
            nonlocal interrupted
            interrupted = interrupted or not self._temperature_inputs_match(
                cfg, expected
            )
            return not interrupted

        if check():
            self._ohmigo_last_push = await async_push_ohmigo(
                self.hass,
                self._config_entry,
                fake_temp,
                self._ohmigo_last_push,
                temperature_check=check,
            )
        if not check():
            await self._temperature_interrupted(cfg)

    def _update_brake_ramp(
        self,
        brake_requested: bool,
        now: datetime,
        ramp_in: float,
        ramp_out: float,
        hold_minutes: float = BRAKE_HOLD_MINUTES,
    ) -> float:
        """Update brake ramp factor with optional hold time.

        dt_seconds is capped to _BRAKE_RAMP_MAX_DT_SECONDS (60 s) so that a
        long gap between polling cycles — e.g. at a mode transition or after an
        HA restart — never causes a large jump in brake_ramp in a single step.
        This applies equally to ramp-in and ramp-out.
        """
        self._brake_ramp, self._brake_last_t, self._brake_last_expensive_t = (
            brake_ramp_step(
                self._brake_ramp,
                self._brake_last_t,
                self._brake_last_expensive_t,
                brake_requested,
                now,
                ramp_in,
                ramp_out,
                hold_minutes,
                max_dt_seconds=_BRAKE_RAMP_MAX_DT_SECONDS,
            )
        )
        return self._brake_ramp

    def _update_preheat_ramp(
        self,
        preheat_requested: bool,
        ramp_in: float,
        ramp_out: float,
        dt_seconds: float = 60.0,
    ) -> float:
        """Update preheat ramp factor for smooth preheat entry and exit."""
        self._preheat_ramp = preheat_ramp_step(
            self._preheat_ramp,
            preheat_requested,
            ramp_in,
            ramp_out,
            dt_seconds,
            max_dt_seconds=_PREHEAT_RAMP_MAX_DT_SECONDS,
        )
        return self._preheat_ramp

    def _pi_output(
        self,
        target: float,
        indoor: float,
        outdoor: float,
        now: datetime,
        cfg: Dict[str, Any],
        freeze_integral: bool = False,
    ) -> float:
        """Compute heating demand from the PI controller."""
        kp = float(cfg.get("pid_kp", PID_KP))
        ki = float(cfg.get("pid_ki", PID_KI))
        kd = float(cfg.get("pid_kd", PID_KD))
        integral_clamp = float(cfg.get("pid_integral_clamp", PID_INTEGRAL_CLAMP))
        output_clamp = float(cfg.get("pid_output_clamp", PID_OUTPUT_CLAMP))

        result = self._pi.compute(
            target_temp=target,
            indoor_temp=indoor,
            outdoor_temp=outdoor,
            aggressiveness=1.0,
            update_time=now,
            braking_active=freeze_integral or self._pi_precool_paused,
            kp=kp,
            ki=ki,
            kd=kd,
            feedforward_bias=0.0,
            integral_clamp=integral_clamp,
            output_clamp=output_clamp,
            min_fake_temp=MIN_FAKE_TEMP,
            max_fake_temp=MAX_FAKE_TEMP,
            brake_behavior="freeze",
            decay_per_minute_on_brake=0.98,
        )

        self._pi_precool_paused = False
        self._last_pi_result = result
        return -result.offset

    async def async_update(self) -> None:
        """Update sensor state."""
        # Options/startup invoke this directly, outside HA's polling update guard.
        if self._update_in_progress:
            return
        self._update_in_progress = True
        try:
            while True:
                self._options_update_pending = False
                self._input_temperatures = {}
                try:
                    if self._temperature_unloaded or not getattr(
                        self.hass, "is_running", True
                    ):
                        await self._enter_safe_mode(
                            "Waiting for Home Assistant startup", None, dt_util.now()
                        )
                        return
                    await self._do_update()
                except Exception as err:
                    self._input_temperatures = {}
                    if self._pilot is not None:
                        self._pilot.reset_interval()
                    self._pilot_status = "update_error"
                    self._thermal_model.cancel_validation_session()
                    self._thermal_learning_active = False
                    self._thermal_learning_reason = "update_error"
                    _LOGGER.exception("PumpSteer update failed: %s", err)
                    self._last_pi_result = None
                    self._state = None
                    self._attributes = {
                        "status": "error",
                        "last_error": str(err),
                        "last_updated": dt_util.now().isoformat(),
                    }
                if not self._options_update_pending:
                    break
        except asyncio.CancelledError:
            self._input_temperatures = {}
            raise
        finally:
            self._update_in_progress = False
            async_dispatcher_send(self.hass, self._input_temperature_signal)

    def _queue_zone_notification(self, monitor, alert, service):
        """Deliver outside the control task; acknowledge only a current event."""
        if not hasattr(self, "_zone_notify_tasks"):
            self._zone_notify_tasks = {}
        key = (alert["slot"], alert["event"])
        pending = self._zone_notify_tasks.get(key)
        if pending is not None:
            if pending[0]["generation"] is alert["generation"]:
                return
            pending[1].cancel()
        create_task = getattr(self.hass, "async_create_task", asyncio.create_task)
        task = create_task(self._send_zone_notification(monitor, alert, service))
        self._zone_notify_tasks[key] = (alert, task)

        def done(finished):
            if self._zone_notify_tasks.get(key, (None, None))[1] is finished:
                self._zone_notify_tasks.pop(key, None)

        task.add_done_callback(done)

    async def _send_zone_notification(self, monitor, alert, service):
        try:
            if self._temperature_unloaded or not monitor.notification_current(alert):
                return
            event, slot = alert["event"], alert["slot"]
            reasons = {
                "temperature": "Temperaturlarm",
                "forecast": "Förvarning: temperaturgränsen kan nås inom två timmar",
                "heat_shortage": "Misstänkt värmebrist: värmebegäran utan temperaturåterhämtning",
                "sensor_fault": "Givarfel: temperaturdata saknas eller är för gamla",
                "recovery": "Återhämtning: zonen är normal igen",
            }
            value = alert["temperature"]
            detail = f" ({value:.1f} °C)" if value is not None else ""
            async with asyncio.timeout(30):
                await self.hass.services.async_call(
                    "notify",
                    service[7:],
                    {"message": f"{alert['name']}: {reasons[event]}{detail}"},
                    blocking=True,
                )
            if getattr(
                self, "_extra_zones", None
            ) is not monitor or not monitor.notification_current(alert):
                return
            monitor.mark_sent(slot, event)
            diagnostic = self._zone_diagnostics[str(slot)]
            diagnostic["notifications_sent"][event] = True
            if event == "temperature":
                diagnostic["notification_sent"] = True
            if event == "recovery":
                diagnostic["recovery_pending"] = False
            async_dispatcher_send(self.hass, self._input_temperature_signal)
            if getattr(self, "platform", None) is not None:
                self.async_write_ha_state()
        except Exception:
            _LOGGER.warning(
                "Zone notification failed for slot %s", alert["slot"], exc_info=True
            )

    async def _do_update(self) -> None:
        """Run the main control state machine."""
        now = dt_util.now()
        self._consume_power_report(now)
        # Advance the clock on every control cycle, including branches that
        # pause the preheat ramp. Never count a backwards clock interval twice.
        preheat_dt_seconds = 60.0
        if self._preheat_last_t is not None:
            preheat_dt_seconds = max(
                0.0, now.timestamp() - self._preheat_last_t.timestamp()
            )
        if self._preheat_last_t is None or preheat_dt_seconds > 0.0:
            self._preheat_last_t = now
        cfg = self._cfg()

        self._update_outdoor_selection(cfg)

        if any(cfg.get(f"zone_{i}_entity") for i in range(1, 5)):
            try:
                from .extra_zones import ExtraZones

                if not hasattr(self, "_extra_zones"):
                    self._extra_zones = ExtraZones(
                        getattr(self, "_zone_restore", None)
                        or self._attributes.get("extra_temperature_zones")
                    )
                    self._zone_restore = {}
                    self._attributes.pop("extra_temperature_zones", None)
                zones = [
                    {
                        field: cfg.get(f"zone_{i}_{field}")
                        for field in (
                            "name",
                            "entity",
                            "threshold",
                            "enabled",
                            "climate",
                            "profile",
                            "comfort_threshold",
                            "notify_service",
                            "alarm_delay_minutes",
                        )
                    }
                    for i in range(1, 5)
                ]
                self._zone_diagnostics, alerts = self._extra_zones.observe(
                    now,
                    zones,
                    self.hass.states,
                    outdoor=self._temperature_state(cfg, "real_outdoor_entity"),
                )
                for alert in alerts:
                    slot = alert["slot"]
                    service = zones[slot - 1].get("notify_service") or cfg.get(
                        "zone_notify_service", ""
                    )
                    if isinstance(service, str) and service.startswith("notify."):
                        self._queue_zone_notification(self._extra_zones, alert, service)
                for pending_alert, task in list(
                    getattr(self, "_zone_notify_tasks", {}).values()
                ):
                    if not self._extra_zones.notification_current(pending_alert):
                        task.cancel()
            except Exception:
                _LOGGER.exception("Passive extra zone monitoring failed")
        else:
            for _, task in list(getattr(self, "_zone_notify_tasks", {}).values()):
                task.cancel()
            self._zone_diagnostics = {}
            self._zone_restore = {}
            self._attributes.pop("extra_temperature_zones", None)
            if hasattr(self, "_extra_zones"):
                del self._extra_zones

        indoor = self._temperature_value(cfg, "indoor_temp_entity")
        outdoor = self._temperature_value(cfg, "real_outdoor_entity")
        identity = self._temperature_identity(cfg)
        previous = getattr(self, "_temperature_identity_previous", None)
        if previous is not None and identity != previous:
            self._thermal_model.cancel_validation_session()
            if indoor is not None:
                self._thermal_model.reset_temp_history(now, indoor)
            self._plan_audit.cancel_plans("temperature_source_changed")
            self._plan_audit.fixed_pending = None
            if self._pilot is not None:
                self._pilot.reset_interval()
        self._temperature_identity_previous = identity
        self._temperature_cycle_identity = identity
        indoor_state = self._temperature_state(cfg, "indoor_temp_entity")
        indoor_attrs = getattr(indoor_state, "attributes", {}) or {}
        active = indoor_attrs.get("source_temperatures_used")
        if not any(
            cfg.get(key) for key in ("indoor_temp_entity_2", "indoor_temp_entity_3")
        ) or not isinstance(active, dict):
            active = (
                {cfg.get("indoor_temp_entity"): indoor} if indoor is not None else {}
            )
        configured = [
            cfg.get(k)
            for k in (
                "indoor_temp_entity",
                "indoor_temp_entity_2",
                "indoor_temp_entity_3",
            )
            if cfg.get(k)
        ]
        self._temperature_coverage = {
            "indoor_active": list(active),
            "indoor_configured": len(configured),
            "indoor_coverage": "complete"
            if len(active) == len(configured)
            else "reduced",
            "outdoor_active": identity[1],
            "physical_measurement_time": "unknown",
        }
        if cfg.get("thermal_intelligence_enabled") is True:
            try:
                if indoor is None or outdoor is None:
                    self._plan_audit.fixed_pending = None
                else:
                    self._plan_audit.observe_fixed(
                        now,
                        self._temperature_state(cfg, "indoor_temp_entity"),
                        self._fixed_audit_identity(cfg),
                    )
                await self._save_pilot_store()
            except Exception:
                self._plan_audit.fixed_pending = None
                _LOGGER.debug("Passive fixed-horizon audit unavailable", exc_info=True)
        self._input_temperatures = {
            "indoor_temperature": indoor,
            "outdoor_temperature": outdoor,
        }
        if cfg.get("thermal_intelligence_enabled") is True:
            self._plan_cycle_identity = self._plan_current_identity(cfg)
        target = self._helper_value_with_fallback(
            helper_key="target_temperature",
            cfg=cfg,
            default=float(DEFAULT_TARGET_TEMP),
        )
        summer_threshold = self._helper_value_with_fallback(
            helper_key="summer_threshold",
            cfg=cfg,
            default=float(DEFAULT_SUMMER_THRESHOLD),
        )
        aggressiveness = self._aggressiveness(cfg)
        house_inertia = self._house_inertia(cfg)

        self._thermal_model.record_temp(now, indoor)

        if indoor is None or outdoor is None:
            missing = []
            if indoor is None:
                missing.append(
                    f"indoor sensor '{cfg.get('indoor_temp_entity') or 'not configured'}'"
                )
            if outdoor is None:
                missing.append(
                    f"outdoor sensor '{cfg.get('real_outdoor_entity') or 'not configured'}'"
                )
            await self._enter_safe_mode(
                "Missing sensors: " + ", ".join(missing),
                outdoor,
                now,
            )
            return

        holiday = await async_update_holiday(
            self.hass, self._config_entry.entry_id, self._config_entry
        )
        self._pilot_holiday_current = (
            bool(holiday) if cfg.get("thermal_intelligence_enabled") is True else None
        )
        if holiday:
            target = HOLIDAY_TEMP

        prices, categories, interval_minutes, current_slot = await self._get_prices(
            cfg,
            now,
        )
        self._pilot_price_context = (
            (tuple(categories), tuple(prices), interval_minutes, current_slot)
            if cfg.get("thermal_intelligence_enabled") is True and categories
            else None
        )

        expected = {"indoor_temp_entity": indoor, "real_outdoor_entity": outdoor}
        if not self._temperature_inputs_match(cfg, expected):
            await self._temperature_interrupted(cfg)
            return

        if not prices:
            today_entity_id = cfg.get("electricity_price_entity") or "not configured"
            tomorrow_entity_id = cfg.get("price_tomorrow_entity") or today_entity_id
            await self._enter_safe_mode(
                f"No price data from today='{today_entity_id}', "
                f"tomorrow='{tomorrow_entity_id}'",
                outdoor,
                now,
            )
            return

        current_cat = categories[current_slot] if categories else PRICE_NORMAL

        lookahead_slots = max(
            1,
            math.ceil((PRICE_LOOKAHEAD_HOURS * 60) / max(interval_minutes, 1)),
        )

        # ramp_in/out derived directly from house_inertia — independent of price jump.
        # Higher thermal mass = longer ramp, giving a heavier house more time to respond.
        upcoming = self._upcoming_expensive(categories, current_slot, lookahead_slots)
        ramp_in = self._compute_ramp_minutes(house_inertia)
        ramp_out = max(RAMP_MIN_MINUTES, ramp_in * RAMP_OUT_FACTOR)

        comfort_floor = self._comfort_floor(target, aggressiveness)
        comfort_floor_reached = indoor < comfort_floor
        forecast_temps = await self._forecast_temps()

        # Compute ThermalOutlook for preheat gating (block 5b).
        # Reuses the same weather entity already used by _forecast_temps.
        self._last_outlook = None
        self._pilot_weather_raw = None
        _cfg_weather = cfg.get("weather_entity")
        _cfg_price = cfg.get("electricity_price_entity")
        if _cfg_weather and _cfg_price:
            try:
                from .forecast import async_build_forecast, analyze_thermal_outlook

                _outlook_points = await async_build_forecast(
                    self.hass,
                    price_entity_id=_cfg_price,
                    weather_entity_id=_cfg_weather,
                    horizon_hours=24,
                    **(
                        {"raw_weather_callback": self._capture_pilot_weather}
                        if cfg.get("thermal_intelligence_enabled") is True
                        else {}
                    ),
                )
                self._last_outlook = analyze_thermal_outlook(
                    _outlook_points,
                    summer_threshold=summer_threshold,
                )
            except Exception as _err:
                _LOGGER.debug("ThermalOutlook computation failed: %s", _err)

        if not self._temperature_inputs_match(cfg, expected):
            await self._temperature_interrupted(cfg)
            return

        await self._run_control_logic(
            now=now,
            cfg=cfg,
            indoor=indoor,
            outdoor=outdoor,
            target=target,
            summer_threshold=summer_threshold,
            aggressiveness=aggressiveness,
            holiday=holiday,
            categories=categories,
            current_slot=current_slot,
            current_cat=current_cat,
            upcoming=upcoming,
            ramp_in=ramp_in,
            ramp_out=ramp_out,
            comfort_floor=comfort_floor,
            comfort_floor_reached=comfort_floor_reached,
            forecast_temps=forecast_temps,
            interval_minutes=interval_minutes,
            preheat_dt_seconds=preheat_dt_seconds,
        )

    def _control_event(self, event, **details):
        log_event(event, **details)

    async def _run_control_logic(
        self,
        *,
        now,
        cfg,
        indoor,
        outdoor,
        target,
        summer_threshold,
        aggressiveness,
        holiday,
        categories,
        current_slot,
        current_cat,
        upcoming,
        ramp_in,
        ramp_out,
        comfort_floor,
        comfort_floor_reached,
        forecast_temps,
        interval_minutes,
        preheat_dt_seconds,
    ):
        """Run the existing mode decisions on the supplied control state."""
        # 1. Summer mode.
        if outdoor >= summer_threshold:
            self._update_thermal_learning(
                active=False,
                reason="summer_mode",
                now=now,
                indoor=indoor,
                outdoor=outdoor,
            )
            self._pi.reset(now)
            self._pi_precool_paused = False
            self._last_pi_result = None
            self._brake_ramp = 0.0
            self._preheat_ramp = 0.0
            self._preheat_last_t = None
            self._brake_last_t = None
            self._brake_last_expensive_t = None
            self._was_braking_last_cycle = False

            fake_temp = outdoor
            await self._set_state(
                fake_temp,
                MODE_SUMMER,
                {
                    **self._base_attrs(
                        indoor,
                        target,
                        outdoor,
                        current_cat,
                        aggressiveness,
                    ),
                    "summer_threshold": summer_threshold,
                },
                now,
            )
            return

        # 2. Precool.
        if self._should_precool(summer_threshold, forecast_temps):
            self._update_thermal_learning(
                active=False,
                reason="precool",
                now=now,
                indoor=indoor,
                outdoor=outdoor,
            )
            # Keep PI memory and its clock current, but do not use its output.
            self._pi_output(target, indoor, outdoor, now, cfg, freeze_integral=True)
            # The interval until the first resume also belongs to precool.
            self._pi_precool_paused = True
            brake_temp = self._brake_temp(outdoor)
            factor = self._update_brake_ramp(
                True,
                now,
                ramp_in=10.0,
                ramp_out=20.0,
            )
            fake_temp = outdoor + (brake_temp - outdoor) * factor
            fake_temp = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, fake_temp))
            self._last_pi_result = None

            await self._set_state(
                fake_temp,
                MODE_PRECOOL,
                {
                    **self._base_attrs(
                        indoor,
                        target,
                        outdoor,
                        current_cat,
                        aggressiveness,
                    ),
                    "brake_factor": round(factor, 3),
                },
                now,
            )
            return

        # 3. Aggressiveness 0 -> pure PI, no price logic.
        if aggressiveness == 0:
            self._update_thermal_learning(
                active=False,
                reason="price_control_disabled",
                now=now,
                indoor=indoor,
                outdoor=outdoor,
            )
            if self._prev_aggressiveness != 0:
                if not getattr(self, "_control_passive", False):
                    _LOGGER.debug(
                        "Aggressiveness changed to 0 (was %s): resetting PI integral once",
                        self._prev_aggressiveness,
                    )
                self._pi.reset(now)
                self._pi_precool_paused = False
                self._last_pi_result = None
                self._brake_ramp = 0.0
                self._preheat_ramp = 0.0
                self._preheat_last_t = None
                self._brake_last_t = None
                self._brake_last_expensive_t = None
                self._was_braking_last_cycle = False

            self._prev_aggressiveness = 0

            demand = self._pi_output(target, indoor, outdoor, now, cfg)
            fake_temp = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, outdoor - demand))

            await self._set_state(
                fake_temp,
                MODE_PI,
                {
                    **self._base_attrs(indoor, target, outdoor, current_cat, 0, demand),
                    "note": "Price control disabled (aggressiveness=0)",
                },
                now,
            )
            return

        self._prev_aggressiveness = aggressiveness

        # 4. Braking during expensive period.
        if current_cat == PRICE_EXPENSIVE:
            brake_delta = float(cfg.get("brake_delta_c", BRAKE_DELTA_C))
            brake_hold = float(cfg.get("brake_hold_minutes", BRAKE_HOLD_MINUTES))

            if comfort_floor_reached:
                if not getattr(self, "_control_passive", False):
                    _LOGGER.info(
                        "Comfort floor reached (%.1f < %.1f), releasing brake",
                        indoor,
                        comfort_floor,
                    )
                self._control_event(
                    "COMFORT_FLOOR_RELEASE",
                    indoor=round(indoor, 1),
                    floor=round(comfort_floor, 1),
                )
                brake_requested = False
                brake_hold = 0.0
            else:
                brake_requested = True

            factor = self._update_brake_ramp(
                brake_requested,
                now,
                ramp_in,
                ramp_out,
                hold_minutes=brake_hold,
            )

            if factor > 0.0:
                pi_demand = self._pi_output(
                    target,
                    indoor,
                    outdoor,
                    now,
                    cfg,
                    freeze_integral=True,
                )

                # Learn only from actual, stable heating-season price braking.
                # Ramp-in, comfort-floor release and precooling are not training.
                learning_active = (
                    brake_requested
                    and factor >= 0.75
                    and outdoor <= summer_threshold - 2.0
                    and indoor - outdoor >= 2.0
                )
                learning_reason = (
                    "active"
                    if learning_active
                    else "comfort_floor"
                    if not brake_requested
                    else "brake_ramp_not_stable"
                    if factor < 0.75
                    else "outdoor_too_warm"
                    if outdoor > summer_threshold - 2.0
                    else "small_indoor_outdoor_difference"
                )
                self._update_thermal_learning(
                    active=learning_active,
                    reason=learning_reason,
                    now=now,
                    indoor=indoor,
                    outdoor=outdoor,
                )

                # Mark that this cycle had active braking so we can fit the thermal
                # model once the brake phase has fully ended.
                self._was_braking_last_cycle = True
                pi_fake = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, outdoor - pi_demand))
                brake_temp = self._brake_temp(outdoor, delta_c=brake_delta)
                fake_temp = pi_fake + (brake_temp - pi_fake) * factor
                mode = MODE_BRAKING
            else:
                self._update_thermal_learning(
                    active=False,
                    reason="brake_inactive",
                    now=now,
                    indoor=indoor,
                    outdoor=outdoor,
                )
                # Brake phase has ended before this branch. Fit the thermal model only
                # once, using the samples collected during the completed brake phase.
                if self._was_braking_last_cycle:
                    if self._thermal_model.should_refit():
                        self._thermal_model.fit()
                    self._was_braking_last_cycle = False

                pi_demand = self._pi_output(target, indoor, outdoor, now, cfg)
                fake_temp = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, outdoor - pi_demand))
                mode = MODE_PI

            fake_temp = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, fake_temp))

            await self._set_state(
                fake_temp,
                mode,
                {
                    **self._base_attrs(
                        indoor,
                        target,
                        outdoor,
                        current_cat,
                        aggressiveness,
                    ),
                    "brake_factor": round(factor, 3),
                    "comfort_floor_c": round(comfort_floor, 2),
                    "ramp_in_minutes": round(ramp_in, 1),
                    "ramp_out_minutes": round(ramp_out, 1),
                },
                now,
            )
            return

        # The price brake is over; close the last stable learning session.
        # This runs before any pre-brake, preheat or normal-mode early return.
        self._update_thermal_learning(
            active=False,
            reason="price_not_expensive",
            now=now,
            indoor=indoor,
            outdoor=outdoor,
        )

        # 5. Pre-brake / preheating before expensive period.
        #
        # These are two distinct behaviours with different trigger conditions:
        #
        # 5a. Pre-brake — pure price signal.
        #     Ramp the brake in during the window before the expensive slot so
        #     the brake is already engaged the moment the slot starts.
        #     Weather data is irrelevant here: if the price is going expensive,
        #     we brake regardless of outdoor temperature.
        #
        # 5b. Preheat-boost — forecast signal.
        #     While prices are still normal and an expensive period is coming,
        #     heat extra so the house has thermal mass to draw on.
        #     Only makes sense when it is actually cold outside — boosting in
        #     warm weather wastes energy without benefit.

        brake_hold = float(cfg.get("brake_hold_minutes", BRAKE_HOLD_MINUTES))
        minutes_until_expensive = (
            self._minutes_until_expensive(
                categories,
                current_slot,
                interval_minutes,
                now,
            )
            if upcoming
            else None
        )

        # Bridge only a genuinely short price dip between brake periods.
        # The bridge is price-only and must never override the comfort floor.
        bridge_short_dip = (
            upcoming
            and self._was_braking_last_cycle
            and minutes_until_expensive is not None
            and minutes_until_expensive <= brake_hold
            and self._brake_ramp > 0.0
            and not comfort_floor_reached
        )

        if upcoming:
            # 5a. Pre-brake: engage ramp when expensive is within ramp_in window.
            if (
                minutes_until_expensive is not None
                and minutes_until_expensive <= ramp_in
                and not comfort_floor_reached
                and not bridge_short_dip
            ):
                factor = self._update_brake_ramp(True, now, ramp_in, ramp_out)
                pi_demand = self._pi_output(
                    target,
                    indoor,
                    outdoor,
                    now,
                    cfg,
                    freeze_integral=True,
                )
                pi_fake = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, outdoor - pi_demand))
                brake_temp = self._brake_temp(outdoor)
                fake_temp = pi_fake + (brake_temp - pi_fake) * factor
                fake_temp = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, fake_temp))

                await self._set_state(
                    fake_temp,
                    MODE_PREBRAKE,
                    {
                        **self._base_attrs(
                            indoor,
                            target,
                            outdoor,
                            current_cat,
                            aggressiveness,
                        ),
                        "brake_factor": round(factor, 3),
                        "ramp_in_minutes": round(ramp_in, 1),
                        "minutes_until_expensive": round(minutes_until_expensive, 0),
                        "upcoming_expensive": True,
                    },
                    now,
                )
                return

            # 5b. Preheat-boost: heat extra while still cheap, only when cold.
            # Uses ThermalOutlook.preheat_worthwhile to gate the decision, and
            # preheat_strength to scale the boost — colder night = stronger boost.
            # Falls back to _forecast_is_cold if ThermalOutlook is unavailable.
            outlook_worthwhile = (
                self._last_outlook is not None and self._last_outlook.preheat_worthwhile
            )
            forecast_cold_fallback = (
                self._last_outlook is None
                and self._forecast_is_cold(
                    summer_threshold,
                    forecast_temps,
                    hours=PRICE_LOOKAHEAD_HOURS,
                )
            )
            preheat_headroom = self._preheat_headroom(aggressiveness)
            preheat_ceiling = target + preheat_headroom
            headroom_factor = self._preheat_headroom_factor(
                indoor,
                target,
                preheat_headroom,
            )

            if (
                not bridge_short_dip
                and (outlook_worthwhile or forecast_cold_fallback)
                and self._preheat_enabled(cfg)
                and headroom_factor > 0.0
            ):
                # Preheat replaces the brake output, so the old phase is over.
                # Do not let its hidden ramp revive as a later short-dip hold.
                self._brake_ramp = 0.0
                self._brake_last_t = None
                self._brake_last_expensive_t = None
                if self._was_braking_last_cycle:
                    if self._thermal_model.should_refit():
                        self._thermal_model.fit()
                    self._was_braking_last_cycle = False

                preheat_factor = self._update_preheat_ramp(
                    True,
                    ramp_in=max(ramp_in, 10.0),
                    ramp_out=max(ramp_out, 10.0),
                    dt_seconds=preheat_dt_seconds,
                )

                base_demand = self._pi_output(target, indoor, outdoor, now, cfg)
                strength = (
                    self._last_outlook.preheat_strength
                    if self._last_outlook is not None
                    else 1.0
                )
                boost = PREHEAT_BOOST_C * preheat_factor * strength * headroom_factor
                boosted_demand = base_demand + boost
                fake_temp = max(
                    MIN_FAKE_TEMP,
                    min(MAX_FAKE_TEMP, outdoor - boosted_demand),
                )

                await self._set_state(
                    fake_temp,
                    MODE_PREHEAT,
                    {
                        **self._base_attrs(
                            indoor,
                            target,
                            outdoor,
                            current_cat,
                            aggressiveness,
                            boosted_demand,
                        ),
                        "preheat_enabled": self._preheat_enabled(cfg),
                        "preheat_boost_c": round(boost, 2),
                        "preheat_factor": round(preheat_factor, 3),
                        "preheat_strength": round(strength, 2),
                        "preheat_headroom_c": round(preheat_headroom, 2),
                        "preheat_ceiling_c": round(preheat_ceiling, 2),
                        "preheat_headroom_factor": round(headroom_factor, 3),
                        "brake_factor": 0.0,
                        "minutes_until_expensive": (
                            round(minutes_until_expensive, 0)
                            if minutes_until_expensive is not None
                            else None
                        ),
                        "upcoming_expensive": True,
                    },
                    now,
                )
                return

        if bridge_short_dip and not self._bridge_short_dip_active:
            self._control_event(
                "BRIDGE_SHORT_DIP_START",
                brake_factor=round(self._brake_ramp, 3),
                minutes_until_expensive=round(minutes_until_expensive, 1),
                bridge_limit_minutes=round(brake_hold, 1),
            )
        elif not bridge_short_dip and self._bridge_short_dip_active:
            self._control_event(
                "BRIDGE_SHORT_DIP_END",
                brake_factor=round(self._brake_ramp, 3),
            )
        self._bridge_short_dip_active = bridge_short_dip

        # 6. Normal PI control.
        # A genuine short dip holds the current brake factor unchanged. Longer
        # gaps ramp out immediately so a distant expensive period cannot keep
        # the heat pump unnecessarily braked for hours.

        self._update_preheat_ramp(
            False,
            ramp_in=max(ramp_in, 10.0),
            ramp_out=max(ramp_out, 10.0),
            dt_seconds=preheat_dt_seconds,
        )

        preheat_headroom = self._preheat_headroom(aggressiveness)
        preheat_ceiling = target + preheat_headroom
        headroom_factor = self._preheat_headroom_factor(
            indoor,
            target,
            preheat_headroom,
        )

        brake_should_hold = bridge_short_dip

        if brake_should_hold:
            # Keep the current factor flat during the short dip. Treating this
            # as a new brake request would incorrectly continue ramping upward.
            self._brake_last_t = now
            factor = self._brake_ramp
        else:
            factor = self._update_brake_ramp(
                False,
                now,
                ramp_in,
                ramp_out,
                hold_minutes=0.0,
            )

        # Hold and ramp-out still limit heating through the brake overlay.
        # Resume integration only once that overlay has fully released.
        pi_demand = self._pi_output(
            target, indoor, outdoor, now, cfg, freeze_integral=factor > 0.0
        )
        pi_fake = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, outdoor - pi_demand))

        # Fit the thermal model once after a completed brake phase has fully ramped out.
        if self._was_braking_last_cycle and factor <= 0.0:
            if self._thermal_model.should_refit():
                self._thermal_model.fit()
            self._was_braking_last_cycle = False

        if factor > 0.0:
            brake_temp = self._brake_temp(outdoor)
            fake_temp = pi_fake + (brake_temp - pi_fake) * factor
            fake_temp = max(MIN_FAKE_TEMP, min(MAX_FAKE_TEMP, fake_temp))
        else:
            fake_temp = pi_fake

        mode = (
            MODE_HOLIDAY
            if holiday
            else MODE_BRAKE_HOLD
            if brake_should_hold
            else MODE_PI
        )
        await self._set_state(
            fake_temp,
            mode,
            {
                **self._base_attrs(
                    indoor,
                    target,
                    outdoor,
                    current_cat,
                    aggressiveness,
                    pi_demand,
                ),
                "preheat_enabled": self._preheat_enabled(cfg),
                "brake_factor": round(factor, 3),
                "ramp_in_minutes": round(ramp_in, 1),
                "ramp_out_minutes": round(ramp_out, 1),
                "bridge_short_dip": bridge_short_dip,
                "brake_should_hold": brake_should_hold,
                "minutes_until_expensive": (
                    round(minutes_until_expensive, 0)
                    if minutes_until_expensive is not None
                    else None
                ),
                "bridge_limit_minutes": round(brake_hold, 1),
                "preheat_headroom_c": round(preheat_headroom, 2),
                "preheat_ceiling_c": round(preheat_ceiling, 2),
                "preheat_headroom_factor": round(headroom_factor, 3),
            },
            now,
        )

    async def _get_prices(
        self,
        cfg: Dict[str, Any],
        now: datetime,
    ) -> Tuple[List[float], List[str], int, int]:
        self._current_price = None
        self._current_price_unit = None

        today_entity_id = cfg.get("electricity_price_entity")
        tomorrow_entity_id = cfg.get("price_tomorrow_entity") or today_entity_id

        if not today_entity_id:
            self._set_price_issue(
                "missing_today_entity",
                "PumpSteer price fetch failed: electricity_price_entity is not configured",
            )
            return [], [], 60, 0

        today_attr = get_attr(self.hass, today_entity_id, "today")
        today_raw_attr = get_attr(self.hass, today_entity_id, "raw_today")
        # Prefer a timestamped source when both numeric and detailed data exist.
        # A shortened numeric list can otherwise mimic a different valid interval.
        raw_today = self._prefer_timestamped_prices(today_attr, today_raw_attr)

        tomorrow_attr = get_attr(self.hass, tomorrow_entity_id, "tomorrow")
        tomorrow_raw_attr = get_attr(self.hass, tomorrow_entity_id, "raw_tomorrow")
        fallback_tomorrow_attr = get_attr(self.hass, today_entity_id, "tomorrow")
        fallback_tomorrow_raw_attr = get_attr(
            self.hass, today_entity_id, "raw_tomorrow"
        )
        if isinstance(tomorrow_attr, list) or isinstance(tomorrow_raw_attr, list):
            raw_tomorrow = self._prefer_timestamped_prices(
                tomorrow_attr, tomorrow_raw_attr
            )
        else:
            raw_tomorrow = self._prefer_timestamped_prices(
                fallback_tomorrow_attr, fallback_tomorrow_raw_attr
            )

        # Nord Pool sorts the repeated DST hour by local wall-clock time, which
        # interleaves +02:00 and +01:00 slots. Order by real (UTC) start so a
        # correct 25-hour day validates; gaps and duplicates are still rejected.
        raw_today = self._sort_by_utc_start(raw_today)
        raw_tomorrow = self._sort_by_utc_start(raw_tomorrow)

        prices_raw = [*raw_today, *raw_tomorrow]

        if not prices_raw:
            if (
                today_attr is None
                and today_raw_attr is None
                and tomorrow_attr is None
                and tomorrow_raw_attr is None
                and fallback_tomorrow_attr is None
                and fallback_tomorrow_raw_attr is None
            ):
                self._set_price_issue(
                    "missing_price_attributes",
                    (
                        "PumpSteer price fetch failed: no price list attributes found "
                        f"(today='{today_entity_id}', tomorrow='{tomorrow_entity_id}', "
                        "checked today/raw_today/tomorrow/raw_tomorrow)"
                    ),
                )
            else:
                unsupported_types = []
                for label, value in (
                    ("today", today_attr),
                    ("raw_today", today_raw_attr),
                    ("tomorrow", tomorrow_attr),
                    ("raw_tomorrow", tomorrow_raw_attr),
                    ("fallback_tomorrow", fallback_tomorrow_attr),
                    ("fallback_raw_tomorrow", fallback_tomorrow_raw_attr),
                ):
                    if value is not None and not isinstance(value, list):
                        unsupported_types.append(f"{label}={type(value).__name__}")
                self._set_price_issue(
                    "unsupported_price_format",
                    (
                        "PumpSteer price fetch failed: unsupported price sensor format "
                        f"(today='{today_entity_id}', tomorrow='{tomorrow_entity_id}', "
                        f"details={unsupported_types or ['no list entries']})"
                    ),
                )
            return [], [], 60, 0

        # A list position is a time slot. Dropping a malformed entry would
        # silently move every later price to an earlier time (or even day).
        interval_minutes = detect_price_interval_minutes(raw_today)
        today_prices = self._parse_price_day(raw_today)
        if (
            today_prices
            and self._has_slot_timestamps(raw_today)
            and self._has_slot_timestamps(raw_tomorrow)
            and not self._price_day_matches_slots(raw_today, now, interval_minutes)
        ):
            # Just after midnight the price sensor may still expose yesterday as
            # "today" while "tomorrow" already holds today's timestamped slots.
            # Use those slots instead of entering safe mode until it rolls over.
            rollover_interval = detect_price_interval_minutes(raw_tomorrow)
            rollover_prices = self._parse_price_day(raw_tomorrow)
            if rollover_prices and self._price_day_matches_slots(
                raw_tomorrow, now, rollover_interval
            ):
                _LOGGER.debug(
                    "Price sensor not rolled over yet; using its 'tomorrow' slots "
                    "as today (today='%s')",
                    today_entity_id,
                )
                raw_today, raw_tomorrow = raw_tomorrow, []
                interval_minutes = rollover_interval
                today_prices = rollover_prices
        if not today_prices or not self._price_day_matches_slots(
            raw_today, now, interval_minutes
        ):
            self._set_price_issue(
                "invalid_today_prices",
                (
                    "PumpSteer price parsing failed: today's price list has missing, "
                    f"invalid or misaligned slots (today='{today_entity_id}'); "
                    "cannot safely map prices to time slots"
                ),
            )
            return [], [], 60, 0

        tomorrow_prices = self._parse_price_day(raw_tomorrow)
        if tomorrow_prices and not self._price_day_matches_slots(
            raw_tomorrow, now + timedelta(days=1), interval_minutes
        ):
            tomorrow_prices = None
        if tomorrow_prices is None:
            self._set_price_issue(
                "invalid_tomorrow_prices",
                (
                    "PumpSteer ignoring tomorrow's price list because it has missing, "
                    f"invalid or misaligned slots (tomorrow='{tomorrow_entity_id}'); "
                    "using today's prices only"
                ),
            )
        else:
            self._clear_price_issue(today_entity_id, tomorrow_entity_id)

        prices = today_prices + (tomorrow_prices or [])

        # Cache price thresholds once per calendar day per entity.
        # Recomputing hourly caused mid-slot reclassification: P80 could shift
        # just enough to flip an ongoing expensive slot to normal, releasing the
        # brake unexpectedly. Daily caching keeps thresholds stable throughout
        # the day; they refresh at midnight when new price data arrives.
        today_date = now.strftime("%Y-%m-%d")
        recalc_thresholds = (
            self._price_thresholds_cached_date is None
            or self._price_thresholds_entity_id != today_entity_id
            or self._price_thresholds_cached_date != today_date
        )

        due = price_cache_refresh_due(
            now=now,
            entity_id=today_entity_id,
            today_prices=today_prices,
            cached_date=self._price_thresholds_cached_date,
            cached_entity_id=self._price_thresholds_entity_id,
        )
        thresholds = await async_get_price_thresholds(today_prices) if due else None
        transition = price_cache_transition(
            now=now,
            entity_id=today_entity_id,
            today_prices=today_prices,
            cached_date=self._price_thresholds_cached_date,
            cached_entity_id=self._price_thresholds_entity_id,
            cached_p30=self._cached_p30,
            cached_p80=self._cached_p80,
            thresholds=thresholds,
        )
        self._cached_p30, self._cached_p80 = transition.p30, transition.p80
        self._price_thresholds_cached_date = transition.date
        self._price_thresholds_entity_id = transition.entity_id
        if recalc_thresholds:
            midnight_grace = now.hour == 0 and now.minute < 15
            if transition.refreshed:
                _LOGGER.debug(
                    "Recomputed price thresholds: p30=%.3f p80=%.3f for entity %s (date=%s)",
                    self._cached_p30,
                    self._cached_p80,
                    today_entity_id,
                    today_date,
                )
                log_event(
                    "THRESHOLDS_SET",
                    p30=round(self._cached_p30, 4),
                    p80=round(self._cached_p80, 4),
                    date=today_date,
                )
            else:
                _LOGGER.debug(
                    "Price thresholds not cached yet (today_prices=%s, midnight_grace=%s), "
                    "keeping previous cached values: p30=%.3f p80=%.3f",
                    bool(today_prices),
                    midnight_grace,
                    self._cached_p30,
                    self._cached_p80,
                )
        self._p30 = self._cached_p30
        self._p80 = self._cached_p80

        if not math.isfinite(self._p30):
            self._p30 = 0.0
        if not math.isfinite(self._p80):
            self._p80 = 0.0

        categories = classify_price_list(prices, self._p30, self._p80)
        categories = filter_short_peaks(
            categories,
            interval_minutes,
            PEAK_FILTER_MIN_DURATION_MINUTES,
        )
        current_slot = compute_price_slot_index(now, interval_minutes, len(prices))

        # Capture the exact price value from the same parsed price snapshot and
        # slot index used for this control cycle's price classification. This
        # lets notifications and diagnostics report the price that actually
        # drove the mode decision, without re-reading a separate entity later.
        if 0 <= current_slot < len(prices):
            self._current_price = prices[current_slot]

        unit = get_attr(self.hass, today_entity_id, "unit_of_measurement")
        if isinstance(unit, str) and unit.strip():
            self._current_price_unit = unit.strip()

        return prices, categories, interval_minutes, current_slot

    @staticmethod
    def _sort_by_utc_start(items: List[Any]) -> List[Any]:
        """Sort fully timestamped, timezone-aware slots by their UTC start.

        Anything else is returned unchanged so the slot validator can reject it.
        Each price keeps its own timestamp, so sorting never moves a price
        to another slot.
        """
        keyed = []
        for item in items:
            if not isinstance(item, dict) or "start" not in item:
                return items
            try:
                start = datetime.fromisoformat(str(item["start"]))
            except (TypeError, ValueError):
                return items
            if start.utcoffset() is None:
                return items
            keyed.append((start.astimezone(timezone.utc), item))
        keyed.sort(key=lambda pair: pair[0])
        return [item for _, item in keyed]

    @staticmethod
    def _has_slot_timestamps(items: Any) -> bool:
        """True when every entry carries an explicit slot start time."""
        return (
            isinstance(items, list)
            and bool(items)
            and all(isinstance(item, dict) and "start" in item for item in items)
        )

    @staticmethod
    def _prefer_timestamped_prices(values: Any, detailed: Any) -> List[Any]:
        """Prefer explicit slot identity without rejecting numeric-only sensors."""
        if isinstance(detailed, list) and any(
            isinstance(item, dict) and ("start" in item or "end" in item)
            for item in detailed
        ):
            return detailed
        if isinstance(values, list):
            return values
        return detailed if isinstance(detailed, list) else []

    @staticmethod
    def _price_day_matches_slots(
        items: List[Any], day: datetime, interval_minutes: int
    ) -> bool:
        """Require a complete day and check explicit timestamps when supplied."""
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        if start.utcoffset() is not None:
            # Count elapsed slots, including 23/25-hour DST days.
            start = start.astimezone(timezone.utc)
            end = end.astimezone(timezone.utc)
        interval = timedelta(minutes=interval_minutes)
        if interval * len(items) != end - start:
            return False
        # Mixed timestamped and timestamp-free entries cannot be verified safely.
        has_timestamps = any(
            isinstance(item, dict) and ("start" in item or "end" in item)
            for item in items
        )
        for index, item in enumerate(items):
            if not has_timestamps:
                continue
            if not isinstance(item, dict) or "start" not in item or "end" not in item:
                return False
            try:
                slot_start = datetime.fromisoformat(str(item.get("start")))
                slot_end = datetime.fromisoformat(str(item.get("end")))
            except (TypeError, ValueError):
                return False
            if (
                slot_start != start + interval * index
                or slot_end != start + interval * (index + 1)
            ):
                return False
        return True

    @staticmethod
    def _parse_price_day(items: List[Any]) -> Optional[List[float]]:
        """Parse a day atomically so invalid slots cannot shift later prices."""
        prices: List[float] = []
        for item in items:
            value = PumpSteerSensor._extract_price(item)
            if value is None or not math.isfinite(value):
                return None
            prices.append(value)
        return prices

    @staticmethod
    def _extract_price(item: Any) -> Optional[float]:
        # bool is an int subclass, but never a valid electricity price.
        if item is None or isinstance(item, bool):
            return None

        if isinstance(item, dict):
            for key in ("value", "price"):
                if key in item:
                    return PumpSteerSensor._extract_price(item[key])
            return None

        if isinstance(item, (float, int)):
            return float(item)

        if isinstance(item, str):
            stripped = item.strip()
            if not stripped or stripped.lower() in ("unknown", "unavailable"):
                return None
            try:
                return float(stripped)
            except ValueError:
                return None

        return None

    def _close_short_dip(self, mode):
        # Close an active bridge when another control branch takes over before
        # the normal-control bridge block is reached.
        if self._bridge_short_dip_active and mode != MODE_BRAKE_HOLD:
            self._control_event(
                "BRIDGE_SHORT_DIP_END",
                brake_factor=round(self._brake_ramp, 3),
                new_mode=mode,
            )
            self._bridge_short_dip_active = False

    async def _set_state(
        self,
        fake_temp: float,
        mode: str,
        extra: Dict[str, Any],
        now: datetime,
    ) -> None:
        self._close_short_dip(mode)

        if self._safe_mode_warned and mode != MODE_SAFE:
            _LOGGER.info(
                "PumpSteer exited SAFE MODE and returned to normal control (mode=%s)",
                mode,
            )
            log_event("SAFE_MODE_EXIT", new_mode=mode)
            self._safe_mode_warned = False
        self._state = round(fake_temp, 1)
        log_mode_change(
            old_mode=self._prev_mode,
            new_mode=mode,
            fake_temp=fake_temp,
            indoor=extra.get("indoor_temperature"),
            outdoor=extra.get("outdoor_temperature"),
            price_cat=extra.get("price_category"),
            p30=getattr(self, "_p30", None),
            p80=getattr(self, "_p80", None),
            brake_factor=extra.get("brake_factor"),
            ramp_in=extra.get("ramp_in_minutes"),
            comfort_floor=extra.get("comfort_floor_c"),
        )
        self._prev_mode = mode
        self._attributes = {
            "mode": mode,
            "fake_outdoor_temperature": self._state,
            "status": "ok",
            "last_updated": now.isoformat(),
            **extra,
        }

        cfg = self._cfg()
        expected = {
            key: extra.get(
                attribute,
                self._temperature_value(cfg, key),
            )
            for key, attribute in (
                ("indoor_temp_entity", "indoor_temperature"),
                ("real_outdoor_entity", "outdoor_temperature"),
            )
        }
        await self._push_validated_temperature(
            fake_temp,
            cfg,
            expected,
        )
        if cfg.get("thermal_intelligence_enabled") is True:
            try:
                if self._pilot_store_read_failed:
                    await self._load_pilot_store()
                if self._temperature_inputs_match(cfg, expected):
                    self._observe_plan_outcomes(cfg, dt_util.now())
                    sample = self._observe_pilot(cfg, now, mode)
                    if sample is not None and self._pilot is not None:
                        import asyncio
                        import copy
                        from .ohmigo import _switch_entity_id

                        observed_pilot = self._pilot
                        entities = [
                            cfg.get(key)
                            for key in (
                                "indoor_temp_entity",
                                "indoor_temp_entity_2",
                                "indoor_temp_entity_3",
                                "real_outdoor_entity",
                                "outdoor_temp_entity_2",
                                "electricity_price_entity",
                                "price_tomorrow_entity",
                                "thermal_heat_activity_entity",
                                "thermal_supply_temp_entity",
                                "thermal_heating_mode_entity",
                                "thermal_hot_water_mode_entity",
                                "thermal_compressor_entity",
                                "thermal_return_temp_entity",
                                "thermal_power_entity",
                                "ohmigo_entity",
                                "ohmigo_relay_entity",
                            )
                        ]
                        entities.append(
                            _switch_entity_id(self.hass, self._config_entry.entry_id)
                        )

                        def snapshot():
                            states = [
                                self.hass.states.get(entity) if entity else None
                                for entity in entities
                            ]
                            return tuple(
                                (
                                    getattr(state, "state", None),
                                    getattr(state, "last_reported", None),
                                    getattr(state, "last_changed", None),
                                    repr(getattr(state, "attributes", None)),
                                )
                                for state in states
                            )

                        before = snapshot()
                        fitted_pump = copy.deepcopy(observed_pilot.manager.pump)
                        try:
                            await asyncio.to_thread(fitted_pump.add_interval, sample)
                        except asyncio.CancelledError:
                            if self._pilot is observed_pilot:
                                observed_pilot.reset_interval()
                                self._pilot_plan = {
                                    "planning_status": "unavailable",
                                    "reason": "learning_cancelled",
                                }
                                self._pilot_plan_next = None
                            raise
                        if self._pilot is not observed_pilot:
                            return
                        if (
                            self._temperature_unloaded
                            or snapshot() != before
                            or self._cfg() != cfg
                            or not self._temperature_inputs_match(cfg, expected)
                        ):
                            observed_pilot.reset_interval()
                            self._pilot_grid_marginal = {
                                "status": "unavailable",
                                "reason": "inputs_changed_during_learning",
                            }
                            self._pilot_plan = {
                                "planning_status": "unavailable",
                                "reason": "inputs_changed_during_learning",
                            }
                            self._pilot_plan_next = None
                            return
                        observed_pilot.manager.pump = fitted_pump
                    await self._save_pilot_store()
                    try:
                        await self._plan_pilot(cfg, now, extra, fake_temp)
                    except Exception:
                        _LOGGER.exception(
                            "Passive plan failed; standard control continues"
                        )
                        self._pilot_plan = {
                            "planning_status": "unavailable",
                            "reason": "planning_error",
                        }
                        self._pilot_plan_next = None
                else:
                    self._pilot_grid_marginal = {
                        "status": "unavailable",
                        "reason": "temperature_changed_during_update",
                    }
                    self._plan_audit.cancel_plans("temperature_changed_during_update")
                    await self._save_pilot_store()
                    if self._pilot is not None:
                        self._pilot.reset_interval()
                        self._pilot_status = "temperature_changed_during_update"
            except Exception:
                _LOGGER.exception(
                    "Passive thermal diagnostics failed; standard control continues"
                )
                self._pilot = None
                self._pilot_status = "learning_error"
                self._pilot_price_available = False
                self._pilot_forecast_available = False
                self._pilot_price_forecast = False
                self._pilot_grid_marginal = {
                    "status": "unavailable",
                    "reason": "learning_error",
                }
                self._pilot_plan = {
                    "planning_status": "unavailable",
                    "reason": "planning_error",
                }
        elif self._pilot is not None:
            self._pilot = None
            self._pilot_sources = None

    def _stop_power_telemetry(self):
        if self._power_unsubscribe is not None:
            self._power_unsubscribe()
            self._power_unsubscribe = None
        if self._power_ledger is not None:
            self._power_ledger.reset_anchor()
        self._power_signature = None
        self._power_last_report = None

    def _heating_mode_state(self, cfg):
        """Interpret the configured polarity without changing report timestamps."""
        from types import SimpleNamespace

        entity_id = cfg.get("thermal_heating_mode_entity")
        state = self.hass.states.get(entity_id) if entity_id else None
        if cfg.get("thermal_heating_mode_inverted") is not True or getattr(
            state, "state", None
        ) not in ("on", "off"):
            return state
        return SimpleNamespace(
            state="off" if state.state == "on" else "on",
            last_reported=getattr(state, "last_reported", None),
            last_changed=getattr(state, "last_changed", None),
            last_updated=getattr(state, "last_updated", None),
            attributes=getattr(state, "attributes", {}),
        )

    def _record_power_snapshot(self, when):
        """Read existing HA in-memory states; preserve their actual report times."""
        cfg = self._cfg()
        today, tomorrow = (
            cfg.get("electricity_price_entity"),
            cfg.get("price_tomorrow_entity"),
        )
        power = self.hass.states.get(cfg.get("thermal_power_entity"))
        try:
            self._power_ledger.observe(
                when,
                power,
                self._heating_mode_state(cfg),
                self.hass.states.get(cfg.get("thermal_hot_water_mode_entity"))
                if cfg.get("thermal_hot_water_mode_entity")
                else None,
                (
                    self.hass.states.get(today) if today else None,
                    self.hass.states.get(tomorrow)
                    if tomorrow and tomorrow != today
                    else None,
                ),
                heating_configured=bool(cfg.get("thermal_heating_mode_entity")),
                hot_water_configured=bool(cfg.get("thermal_hot_water_mode_entity")),
            )
            from .pilot_forecast import _timestamp

            reported = _timestamp(getattr(power, "last_reported", None))
            instant = _timestamp(when)
            if (
                reported is not None
                and instant is not None
                and reported <= instant
                and (
                    self._power_last_report is None
                    or reported > self._power_last_report
                )
            ):
                self._power_last_report = reported
        except Exception:
            self._power_ledger.reset_anchor()
            _LOGGER.exception(
                "Passive power telemetry failed; standard control continues"
            )

    def _record_energy_report(self, entity_id, state, when, energy_ids):
        """Process the event's own state, not a later state already in HA."""
        from .pilot_forecast import _timestamp

        try:
            cfg = self._cfg()
            price_ids = (
                cfg.get("electricity_price_entity"),
                cfg.get("price_tomorrow_entity"),
            )
            prices = tuple(
                self.hass.states.get(key) if key else None for key in price_ids
            )
            clock = _timestamp(when)
            report = _timestamp(getattr(state, "last_reported", None))
            if clock is not None:
                self._power_ledger.sync_energy_prices(prices, clock)
            if state is None and clock is not None:
                self._power_ledger.observe_energy(
                    clock, {entity_id: None}, prices, meter_ids=energy_ids
                )
            elif report is not None and clock is not None and report <= clock:
                self._power_ledger.observe_energy(
                    report, {entity_id: state}, prices, meter_ids=energy_ids
                )
        except Exception:
            self._power_ledger.reset_anchor(gap=True)
            _LOGGER.exception(
                "Passive energy telemetry failed; standard control continues"
            )

    def _consume_power_report(self, now):
        """Catch new HA reports during the existing control cycle, without polling APIs."""
        if (
            self._power_ledger is None
            or self._temperature_unloaded
            or self._cfg().get("thermal_intelligence_enabled") is not True
        ):
            return
        from .pilot_forecast import _timestamp

        energy_ids = self._cfg().get("thermal_energy_entities") or []
        if energy_ids:
            try:
                for entity_id in energy_ids:
                    state = self.hass.states.get(entity_id)
                    if self._power_ledger.can_poll_energy(entity_id, state):
                        self._record_energy_report(entity_id, state, now, energy_ids)
            except Exception:
                self._power_ledger.reset_anchor(gap=True)
                _LOGGER.exception(
                    "Passive energy telemetry failed; standard control continues"
                )
            return

        state = self.hass.states.get(self._cfg().get("thermal_power_entity"))
        report = _timestamp(getattr(state, "last_reported", None))
        clock = _timestamp(now)
        if (
            report is None
            or clock is None
            or report > clock
            or (
                self._power_last_report is not None
                and report <= self._power_last_report
            )
        ):
            return
        anchor = self._power_ledger.anchor
        if anchor is None or report > anchor["time"]:
            self._record_power_snapshot(report)

    def _setup_power_telemetry(self):
        """Subscribe only to selected states; the normal cycle catches unchanged reports."""
        cfg = self._cfg()
        power_id = cfg.get("thermal_power_entity")
        energy_ids = tuple(cfg.get("thermal_energy_entities") or ())
        keys = (
            "thermal_power_entity",
            "thermal_heating_mode_entity",
            "thermal_hot_water_mode_entity",
            "electricity_price_entity",
            "price_tomorrow_entity",
        )
        entities = tuple(cfg.get(key) for key in keys)
        signature = entities + (
            energy_ids,
            cfg.get("thermal_heating_mode_inverted") is True,
        )
        if (
            cfg.get("thermal_intelligence_enabled") is True
            and self._power_signature == signature
            and self._power_unsubscribe is not None
        ):
            return
        self._stop_power_telemetry()
        self._power_ledger = None
        if (
            cfg.get("thermal_intelligence_enabled") is not True
            or (
                not energy_ids
                and (
                    not isinstance(power_id, str) or not power_id.startswith("sensor.")
                )
            )
            or any(
                not isinstance(entity, str) or not entity.startswith("sensor.")
                for entity in energy_ids
            )
        ):
            return
        from homeassistant.helpers.event import async_track_state_change_event
        from .pilot_power_ledger import PowerLedger

        self._power_signature = signature
        self._power_ledger = PowerLedger(started_after=dt_util.now())
        entity_ids = list(
            dict.fromkeys(value for value in (*entities, *energy_ids) if value)
        )

        @callback
        def changed(event):
            if (
                self._temperature_unloaded
                or self._power_ledger is None
                or self._cfg().get("thermal_intelligence_enabled") is not True
            ):
                return
            entity_id = event.data.get("entity_id")
            if energy_ids:
                if entity_id in energy_ids:
                    state = (
                        event.data.get("new_state")
                        if "new_state" in event.data
                        else self.hass.states.get(entity_id)
                    )
                    self._record_energy_report(
                        entity_id, state, event.time_fired, energy_ids
                    )
                elif entity_id in (
                    cfg.get("electricity_price_entity"),
                    cfg.get("price_tomorrow_entity"),
                ):
                    try:
                        prices = tuple(
                            (
                                event.data.get("new_state")
                                if "new_state" in event.data
                                else self.hass.states.get(key)
                            )
                            if key == entity_id
                            else self.hass.states.get(key)
                            if key
                            else None
                            for key in (
                                cfg.get("electricity_price_entity"),
                                cfg.get("price_tomorrow_entity"),
                            )
                        )
                        self._power_ledger.invalidate_energy_prices(
                            prices, event.time_fired
                        )
                    except Exception:
                        self._power_ledger.invalidate_energy_prices()
                        _LOGGER.exception("Passive energy price telemetry failed")
                return
            if entity_id == power_id:
                previous_report = self._power_last_report
                self._consume_power_report(event.time_fired)
                if self._power_last_report == previous_report:
                    self._power_ledger.reset_anchor(gap=True)
            elif entity_id in entity_ids:
                # Do not infer the power or its mode at a non-power event. A
                # later fresh power report starts a new qualified interval.
                self._power_ledger.reset_anchor(gap=True)

        self._power_unsubscribe = async_track_state_change_event(
            self.hass, entity_ids, changed
        )

    async def _load_pilot_store(self):
        """Restore only historical model evidence, never a fresh measurement."""
        try:
            from homeassistant.helpers.storage import Store

            if self._pilot_store is None:
                self._pilot_store = Store(
                    self.hass, 1, f"pumpsteer.pilot.{self._config_entry.entry_id}"
                )
            try:
                saved = await self._pilot_store.async_load()
            except Exception:
                saved = None
                self._pilot_store_read_failed = (
                    self._pilot_store_read_failed or self._pilot_persisted is None
                )
                _LOGGER.warning(
                    "Passive thermal history could not be read", exc_info=True
                )
            else:
                self._pilot_store_read_failed = False
                if saved is not None and (
                    not isinstance(saved, dict) or saved.get("version") != 1
                ):
                    # Do not replace a payload written by an unknown version.
                    self._pilot_store_read_failed = True
            if self._pilot_store_read_failed:
                return
            if isinstance(saved, dict) and saved.get("version") == 1:
                if self._pilot_persisted is None:
                    self._pilot_persisted = saved
            if (
                isinstance(self._pilot_persisted, dict)
                and self._pilot_persisted.get("version") == 1
            ):
                self._pilot_restore = self._pilot_persisted
                if not self._plan_audit_loaded:
                    audit = self._plan_audit
                    if not (
                        audit.plan_pending
                        or audit.plan_records
                        or audit.plan_unverifiable
                    ):
                        audit.restore_plan(
                            self._pilot_persisted.get("plan_audit"), dt_util.now()
                        )
            self._plan_audit_loaded = True
        except Exception:
            self._pilot_restore = None
            _LOGGER.warning(
                "Passive thermal history could not be restored", exc_info=True
            )

    async def _flush_pilot_store(self):
        """Finish any pending history write before this instance is discarded."""
        if (
            not self._pilot_store_read_failed
            and self._pilot_store is not None
            and self._pilot_persisted is not None
        ):
            if (
                self._heat_operation is not None
                and self._heat_operation_signature is not None
            ):
                payload = dict(self._pilot_persisted)
                payload["heat_operation"] = {
                    "sources": list(self._heat_operation_signature),
                    "samples": self._heat_operation.export(),
                }
                self._pilot_persisted = payload
            await self._pilot_store.async_save(self._pilot_persisted)

    async def _save_pilot_store(self):
        """Save bounded model or plan evidence, only when it changes."""
        pilot = self._pilot
        if self._pilot_store is None or self._pilot_store_read_failed:
            return
        model_available = pilot is not None and (
            pilot.house_intervals or pilot.manager.pump.history
        )
        operation = self._heat_operation
        operation_samples = operation.export() if operation is not None else None
        operation_available = bool(
            operation_samples and any(operation_samples.values())
        )
        plan_data = self._plan_audit.export_plan()
        if (
            not model_available
            and not operation_available
            and not (
                plan_data["records"]
                or plan_data["pending_count"]
                or plan_data["unverifiable"]
                or plan_data["fixed_v1"]
            )
        ):
            return
        pump_last = (
            pilot.manager.pump.history[-1][0]
            if model_available and pilot.manager.pump.history
            else None
        )
        audit_last = (
            pilot.audit.records[-1]["observed"]
            if model_available and pilot.audit.records
            else None
        )
        audit_pending = (
            pilot.audit.pending["issued"].isoformat()
            if model_available and pilot.audit.pending
            else None
        )
        plan_last = (
            plan_data["records"][-1]["observed"] if plan_data["records"] else None
        )
        key = (
            self._pilot_sources,
            pilot.house_intervals if model_available else None,
            pump_last,
            audit_last,
            audit_pending,
            plan_last,
            plan_data["pending_count"],
            plan_data["unverifiable"],
            len(plan_data["fixed_v1"]),
            plan_data["fixed_v1"][-1]["observed"] if plan_data["fixed_v1"] else None,
            self._heat_operation_signature if operation_available else None,
            operation.sample_count // 18 if operation_available else None,
        )
        if key == self._pilot_saved:
            return
        payload = dict(self._pilot_persisted or {})
        payload.update(version=1, plan_audit=plan_data)
        if operation_available:
            payload["heat_operation"] = {
                "sources": list(self._heat_operation_signature),
                "samples": operation_samples,
            }
        if model_available:
            payload.update(
                sources=list(self._pilot_sources),
                identity=pilot.source_identity,
                house=pilot.manager.model.export_state(),
                pump=pilot.manager.pump.export_state(),
                forecast_audit=pilot.audit.export_state(),
            )
        try:
            self._pilot_store.async_delay_save(lambda: payload, 30)
            self._pilot_saved = key
            self._pilot_persisted = payload
        except Exception:
            _LOGGER.warning("Passive thermal history could not be saved", exc_info=True)

    def _restore_pilot_history(self, pilot, signature, now):
        saved, self._pilot_restore = self._pilot_restore, None
        if not isinstance(saved, dict):
            return
        old_sources = saved.get("sources")
        old_pump = saved.get("pump")
        compatible_legacy = (
            isinstance(old_sources, list)
            and len(old_sources) == 4
            and list(signature[:4]) == old_sources
            and isinstance(old_pump, dict)
            and not old_pump.get("history")
        )
        if old_sources != list(signature) and not compatible_legacy:
            return
        house, pump = saved.get("house"), old_pump
        if not isinstance(house, dict) or not isinstance(pump, dict):
            return

        def finite_tree(value):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return math.isfinite(value)
            if isinstance(value, str):
                try:
                    return math.isfinite(float(value))
                except ValueError:
                    return True
            if isinstance(value, dict):
                return all(finite_tree(child) for child in value.values())
            if isinstance(value, (list, tuple)):
                return all(finite_tree(child) for child in value)
            return True

        if not finite_tree(house) or not finite_tree(pump):
            return
        rows = house.get("observations")
        if not isinstance(rows, list) or len(rows) > 336:
            return
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("end"), (int, float))
            or not 0 <= row["end"] <= now.timestamp()
            for row in rows
        ):
            return
        pump_rows = pump.get("history", [])
        if not isinstance(pump_rows, list) or len(pump_rows) > 288:
            return
        if any(
            not isinstance(row, (list, tuple))
            or not row
            or not isinstance(row[0], (int, float))
            or not 0 <= row[0] <= now.timestamp()
            for row in pump_rows
        ):
            return
        try:
            from .pilot_passive import PilotPassive

            restored = PilotPassive()
            if not restored.manager.model.restore(house):
                return
            restored.manager.pump.restore(pump)
            restored.manager.pump.ready = False
            restored.audit.restore(saved.get("forecast_audit"), now)
            pilot.manager = restored.manager
            pilot.audit = restored.audit
            identity = saved.get("identity")
            if isinstance(identity, (tuple, list)) and len(identity) == 2:
                first = (
                    tuple(identity[0])
                    if isinstance(identity[0], (list, tuple))
                    else None
                )
                pilot.source_identity = (first, identity[1])
            pilot.house_intervals = len(pilot.manager.model.history)
            pilot.status = "restored_unvalidated"
            pump_last = (
                pilot.manager.pump.history[-1][0]
                if pilot.manager.pump.history
                else None
            )
            audit_last = (
                pilot.audit.records[-1]["observed"] if pilot.audit.records else None
            )
            self._pilot_saved = (
                signature,
                pilot.house_intervals,
                pump_last,
                audit_last,
                None,
            )
        except (TypeError, ValueError, KeyError, OverflowError):
            _LOGGER.warning("Passive thermal history was invalid and was ignored")

    def _capture_pilot_weather(self, rows):
        """Capture only the forecast already fetched for the normal outlook."""
        self._pilot_weather_raw = rows

    def _fixed_audit_identity(self, cfg):
        return (
            cfg.get("indoor_temp_entity") or "",
            repr((cfg.get("real_outdoor_entity"), self._plan_source_identity(cfg))),
        )

    def _plan_source_identity(self, cfg):
        """Identify selected HA sources without asserting physical freshness."""
        inside = self._temperature_state(cfg, "indoor_temp_entity")
        outside = self._temperature_state(cfg, "real_outdoor_entity")
        inside_attrs = getattr(inside, "attributes", {}) or {}
        outside_attrs = getattr(outside, "attributes", {}) or {}
        components = inside_attrs.get("source_temperatures_used")
        if isinstance(components, dict) and all(isinstance(k, str) for k in components):
            components = tuple(sorted(components))
        else:
            components = None
        selected = outside_attrs.get("selected_source")
        return (
            components,
            selected if isinstance(selected, str) else None,
            self._temperature_identity(cfg)
            if any(
                cfg.get(k)
                for k in (
                    "indoor_temp_entity_2",
                    "indoor_temp_entity_3",
                    "outdoor_temp_entity_2",
                )
            )
            else cfg.get("real_outdoor_entity"),
        )

    def _plan_current_identity(self, cfg):
        """Do not label restored or absent sources as the current identity."""
        if any(
            self._temperature_value(cfg, key) is None
            for key in ("indoor_temp_entity", "real_outdoor_entity")
        ):
            return None
        return self._plan_source_identity(cfg)

    def _observe_plan_outcomes(self, cfg, now):
        if not self._plan_audit.plan_pending:
            return
        identity = self._plan_current_identity(cfg)
        if identity is None or identity != self._plan_cycle_identity:
            self._plan_audit.cancel_plans("source_changed_during_cycle")
            return
        source = cfg.get("indoor_temp_entity")
        self._plan_audit.observe_plan(
            now,
            self._temperature_state(cfg, "indoor_temp_entity"),
            source,
            identity,
            reported_after=self._temperature_reported_after,
        )

    def _capture_replay_context(self, cfg, now, aggression):
        """Freeze existing HA inputs for an isolated passive control replay."""
        import copy
        from homeassistant.helpers import entity_registry as er
        from .control_replay import ControlReplayState
        from .holiday import _get_entity_ids, _get_datetime
        from .pilot_passive import PilotPassive

        price_context = self._pilot_price_context
        if not price_context:
            return None, ()
        categories, active_prices, interval, slot = price_context
        known = True
        holiday_ids = ()
        preheat_id = None
        start = end = None
        boolean_on = False
        try:
            holiday_ids = _get_entity_ids(self.hass, self._config_entry.entry_id)
            boolean_id, start_id, end_id = holiday_ids
            for entity_id in holiday_ids:
                if entity_id:
                    state = self.hass.states.get(entity_id)
                    if (
                        state is None
                        or getattr(state, "state", None)
                        in (None, "unknown", "unavailable", "")
                        or (getattr(state, "attributes", {}) or {}).get("restored")
                    ):
                        known = False
            if boolean_id:
                boolean = self.hass.states.get(boolean_id)
                boolean_on = getattr(boolean, "state", None) == "on"
                if getattr(boolean, "state", None) not in ("on", "off"):
                    known = False
            start = _get_datetime(self.hass, start_id)
            end = _get_datetime(self.hass, end_id)
            dates_valid = start is not None and end is not None and end > start
            expected_holiday = start <= now < end if dates_valid else boolean_on
            known = known and expected_holiday == self._pilot_holiday_current
            registry = er.async_get(self.hass)
            preheat_id = registry.async_get_entity_id(
                "switch", DOMAIN, f"{self._config_entry.entry_id}_preheat_enabled"
            )
            if preheat_id:
                state = self.hass.states.get(preheat_id)
                if getattr(state, "state", None) not in ("on", "off") or (
                    getattr(state, "attributes", {}) or {}
                ).get("restored"):
                    known = False
        except (AttributeError, TypeError, ValueError, KeyError):
            known = False

        qualified = True
        for key in ("indoor_temp_entity", "real_outdoor_entity"):
            source = self._temperature_state(cfg, key)
            report = PilotPassive._report(
                source,
                now,
                temperature=True,
                reported_after=self._temperature_reported_after,
            )
            changed = getattr(source, "last_changed", None)
            try:
                if (
                    report is None
                    or changed is None
                    or changed.tzinfo is None
                    or changed > report[0]
                ):
                    qualified = False
            except (TypeError, AttributeError):
                qualified = False

        return {
            "state": ControlReplayState.from_sensor(self),
            "cfg": copy.deepcopy(cfg),
            "categories": categories,
            "active_prices": active_prices,
            "price_entity_id": cfg.get("electricity_price_entity"),
            "cached_price_date": self._price_thresholds_cached_date,
            "cached_price_entity_id": self._price_thresholds_entity_id,
            "cached_p30": self._cached_p30,
            "cached_p80": self._cached_p80,
            "interval_minutes": interval,
            "current_slot": slot,
            "summer_threshold": self._helper_value_with_fallback(
                helper_key="summer_threshold",
                cfg=cfg,
                default=float(DEFAULT_SUMMER_THRESHOLD),
            ),
            "configured_target": self._helper_value_with_fallback(
                helper_key="target_temperature",
                cfg=cfg,
                default=float(DEFAULT_TARGET_TEMP),
            ),
            "aggressiveness": aggression,
            "house_inertia": self._house_inertia(cfg),
            "preheat_enabled": self._preheat_enabled(cfg),
            "calendar_tz": now.tzinfo,
            "holiday_context_known": known,
            "holiday_start": start,
            "holiday_end": end,
            "holiday_boolean_on": boolean_on,
            "inputs_qualified": qualified,
        }, tuple(holiday_ids or ()) + (preheat_id,)

    def _unavailable_pilot_plan(self, reason):
        """Keep the last issued comparison as history, never as a current plan."""
        previous = self._pilot_plan
        comparison = previous.get("comparison", {})
        historical = comparison.get("historical_reference")
        if (
            historical is None
            and previous.get("planning_status")
            in {"validated_passive", "hypothetical_unvalidated"}
            and previous.get("forecast_start")
        ):
            historical = {
                **comparison,
                "status": "historical",
                "forecast_start": previous["forecast_start"],
                "issued_at": previous.get("issued_at"),
                "pilot_plan": {
                    **{
                        key: previous[key]
                        for key in (
                            "planning_status",
                            "reason",
                            "horizon_minutes",
                            "forecast_coverage",
                            "cost_kind",
                            "response_status",
                            "duties",
                            "temperatures",
                            "comfort_status",
                            "validation",
                            "assumed_model",
                            "forecast_quality",
                            "monitoring_forecast",
                            "initial_context",
                            "plan_outcome_revision",
                            "plan_outcome_status",
                            "plan_outcome_reason",
                        )
                        if key in previous
                    },
                    "outcome_evaluation": "not_evaluated",
                },
            }
        result = {"planning_status": "unavailable", "reason": reason}
        if historical is not None:
            result["comparison"] = {
                "baseline": "unavailable",
                "baseline_reason": reason,
                "shared_control_status": "unavailable",
                "savings": "unverified",
                "recovery": "unknown",
                "heat_storage": "unknown",
                "historical_reference": historical,
            }
        return result

    async def _plan_pilot(self, cfg, now, extra, command):
        """Run a bounded diagnostic only; it never changes the control output."""
        if self._pilot_store_read_failed:
            self._pilot_plan = self._unavailable_pilot_plan("history_unavailable")
            return
        price_id = cfg.get("electricity_price_entity")
        tomorrow_id = cfg.get("price_tomorrow_entity")
        weather_id = cfg.get("weather_entity")
        weather_state = self.hass.states.get(weather_id) if weather_id else None
        weather_attrs = getattr(weather_state, "attributes", {}) or {}
        unit = weather_attrs.get("temperature_unit")
        if getattr(weather_state, "state", None) in (None, "unknown", "unavailable"):
            unit = None
        from .pilot_forecast import price_window, source_is_current
        from .pilot_runtime_plan import _weather_intervals

        if not source_is_current(weather_state, now):
            unit = None

        price_state = self.hass.states.get(price_id) if price_id else None
        tomorrow_state = (
            self.hass.states.get(tomorrow_id)
            if tomorrow_id and tomorrow_id != price_id
            else None
        )
        prices = price_window((price_state, tomorrow_state), now, timedelta(hours=2))
        weather = (
            _weather_intervals(
                self._pilot_weather_raw,
                now,
                current_outdoor=extra.get("outdoor_temperature"),
            )
            if unit == "°C"
            else None
        )
        if prices is None or weather is None:
            self._pilot_plan = self._unavailable_pilot_plan(
                "price_forecast_unavailable"
                if prices is None
                else "weather_forecast_unavailable"
            )
            self._pilot_plan_next = None
            self._pilot_plan_signature = None
            return
        target = extra.get("target_temperature")
        aggression = extra.get("aggressiveness")
        replay_context, replay_entities = self._capture_replay_context(
            cfg, now, aggression
        )

        def replay_signature():
            if replay_context is None:
                return None
            inside = self._temperature_state(cfg, "indoor_temp_entity")
            outside = self._temperature_state(cfg, "real_outdoor_entity")
            inside_attrs = getattr(inside, "attributes", {}) or {}
            outside_attrs = getattr(outside, "attributes", {}) or {}
            components = inside_attrs.get("source_temperatures_used")
            components = (
                tuple(sorted(components))
                if isinstance(components, dict)
                and all(isinstance(key, str) for key in components)
                else None
            )
            entities = []
            for entity_id in replay_entities:
                entity = self.hass.states.get(entity_id) if entity_id else None
                entities.append(
                    (
                        getattr(entity, "state", None),
                        getattr(entity, "last_changed", None),
                        (getattr(entity, "attributes", {}) or {}).get("restored"),
                    )
                )
            return (
                replay_context["summer_threshold"],
                replay_context["configured_target"],
                replay_context["house_inertia"],
                replay_context["preheat_enabled"],
                replay_context["holiday_context_known"],
                replay_context["holiday_start"],
                replay_context["holiday_end"],
                replay_context["holiday_boolean_on"],
                self._pilot_holiday_current,
                replay_context["inputs_qualified"],
                replay_context["cached_price_date"],
                replay_context["cached_price_entity_id"],
                replay_context["cached_p30"],
                replay_context["cached_p80"],
                components,
                outside_attrs.get("selected_source"),
                tuple(entities),
            )

        def pi_result_signature(result):
            return (
                (result.offset, result.p_term, result.i_term, result.error)
                if result is not None
                else None
            )

        replay_state = replay_context["state"] if replay_context is not None else None
        replay_state_signature = (
            (
                replay_state.pi._integral,
                replay_state.pi._last_time,
                replay_state.pi._last_error,
                replay_state.pi_precool_paused,
                pi_result_signature(replay_state.last_pi_result),
                replay_state.brake_ramp,
                replay_state.preheat_ramp,
                replay_state.brake_last_t,
                replay_state.brake_last_expensive_t,
                replay_state.preheat_last_t,
                replay_state.was_braking_last_cycle,
                replay_state.prev_aggressiveness,
                replay_state.bridge_short_dip_active,
                replay_state.thermal_pending_samples,
                replay_state.thermal_should_refit,
            )
            if replay_state is not None
            else None
        )
        signature = (
            repr(getattr(price_state, "attributes", None)),
            repr(getattr(tomorrow_state, "attributes", None)),
            repr(self._pilot_weather_raw),
            unit,
            extra.get("indoor_temperature"),
            extra.get("outdoor_temperature"),
            command,
            target,
            aggression,
            cfg.get("pid_output_clamp", PID_OUTPUT_CLAMP),
            replay_signature(),
        )
        if self._pilot_plan_next is not None and now < self._pilot_plan_next:
            if signature != self._pilot_plan_signature:
                self._pilot_plan = self._unavailable_pilot_plan(
                    "context_changed_waiting_for_replan"
                )
            elif (
                replay_state_signature != self._pilot_replay_state_signature
                and self._pilot_plan.get("planning_status")
                in {"validated_passive", "hypothetical_unvalidated"}
            ):
                # A cached reference was issued for another PI/ramp state. Do
                # not rerun the optimizer on every control cycle or expose the
                # stale modeled path as a current comparison.
                self._pilot_plan = self._unavailable_pilot_plan(
                    "control_state_changed_waiting_for_replan"
                )
            return
        self._pilot_plan_next = now + timedelta(minutes=30)
        self._pilot_plan_signature = signature
        self._pilot_replay_state_signature = replay_state_signature
        if (
            not isinstance(target, (int, float))
            or aggression not in COMFORT_FLOOR_BY_AGGRESSIVENESS
        ):
            self._pilot_plan = {
                "planning_status": "unavailable",
                "reason": "comfort_context_unavailable",
            }
            return
        from .pilot_runtime_plan import hypothetical_plan

        from functools import partial

        response_context = None
        response_scope = None
        pilot = self._pilot
        pilot_sources = self._pilot_sources
        if pilot is not None and pilot.manager.pump.ready:
            initial_response = pilot.manager.response_state(now.timestamp())
            if initial_response is not None:
                response_context = (pilot.manager.pump.parameters, initial_response)
                observed = pilot.manager.pump.history[:-16]
                if observed:
                    response_scope = {
                        "request_min": min(row[1] for row in observed),
                        "request_max": max(row[1] for row in observed),
                        "heat_min": min(row[2] for row in observed),
                        "heat_max": max(row[2] for row in observed),
                    }
        pi_reference = None
        if (
            self._attributes.get("mode") == MODE_PI
            and self._last_pi_result is not None
            and self._brake_ramp == 0
            and self._preheat_ramp == 0
            and (aggression == 0 or self._pilot_price_context is not None)
        ):
            import copy

            pi_reference = {
                "controller": copy.deepcopy(self._pi),
                "kp": safe_float(cfg.get("pid_kp", PID_KP)),
                "ki": safe_float(cfg.get("pid_ki", PID_KI)),
                "kd": safe_float(cfg.get("pid_kd", PID_KD)),
                "integral_clamp": safe_float(
                    cfg.get("pid_integral_clamp", PID_INTEGRAL_CLAMP)
                ),
                "summer_threshold": self._helper_value_with_fallback(
                    helper_key="summer_threshold",
                    cfg=cfg,
                    default=float(DEFAULT_SUMMER_THRESHOLD),
                ),
                "calendar_tz": now.tzinfo,
            }
            if aggression != 0:
                categories, active_prices, interval, slot = self._pilot_price_context
                pi_reference.update(
                    categories=categories,
                    active_prices=active_prices,
                    interval_minutes=interval,
                    current_slot=slot,
                    price_lookahead_minutes=PRICE_LOOKAHEAD_HOURS * 60,
                )
        task = partial(
            hypothetical_plan,
            now=now,
            indoor=extra.get("indoor_temperature"),
            outdoor=extra.get("outdoor_temperature"),
            command=command,
            target=target,
            floor=self._comfort_floor(target, aggression),
            ceiling=target + self._preheat_headroom(aggression),
            price_states=(price_state, tomorrow_state),
            weather_rows=self._pilot_weather_raw,
            weather_unit=unit,
            output_clamp=safe_float(cfg.get("pid_output_clamp", PID_OUTPUT_CLAMP)),
            entity_min=MIN_FAKE_TEMP,
            entity_max=MAX_FAKE_TEMP,
            response_context=response_context,
            response_scope=response_scope,
            pi_reference=pi_reference,
            replay_context=replay_context,
        )
        import asyncio
        from .ohmigo import _switch_entity_id

        state_ids = [
            cfg.get(key)
            for key in (
                "indoor_temp_entity",
                "indoor_temp_entity_2",
                "indoor_temp_entity_3",
                "real_outdoor_entity",
                "outdoor_temp_entity_2",
                "electricity_price_entity",
                "price_tomorrow_entity",
                "weather_entity",
                "thermal_heat_activity_entity",
                "thermal_supply_temp_entity",
                "thermal_heating_mode_entity",
                "thermal_hot_water_mode_entity",
                "thermal_compressor_entity",
                "thermal_return_temp_entity",
                "thermal_power_entity",
                "ohmigo_entity",
                "ohmigo_relay_entity",
            )
        ]
        state_ids.extend(
            (
                _switch_entity_id(self.hass, self._config_entry.entry_id),
                self._number_entity_id("target_temperature"),
                self._number_entity_id("aggressiveness"),
                self._number_entity_id("summer_threshold"),
                self._number_entity_id("house_inertia"),
            )
        )
        state_ids.extend(replay_entities)

        def state_snapshot():
            values = []
            for entity_id in state_ids:
                state = self.hass.states.get(entity_id) if entity_id else None
                values.append(
                    (
                        getattr(state, "state", None),
                        getattr(state, "last_reported", None),
                        getattr(state, "last_changed", None),
                        repr(getattr(state, "attributes", None)),
                    )
                )
            return tuple(values)

        def control_snapshot():
            return (
                self._pi._integral,
                self._pi._last_time,
                self._pi._last_error,
                self._pi_precool_paused,
                pi_result_signature(self._last_pi_result),
                self._brake_ramp,
                self._preheat_ramp,
                self._brake_last_t,
                self._brake_last_expensive_t,
                self._preheat_last_t,
                self._was_braking_last_cycle,
                self._prev_aggressiveness,
                self._bridge_short_dip_active,
                self._thermal_model.pending_samples,
            )

        snapshot = state_snapshot()
        control_before = control_snapshot()
        config_snapshot = dict(cfg)

        cached_house = (
            self._pilot_house_candidate
            if self._pilot_house_candidate_owner is pilot
            and self._pilot_house_candidate_at is not None
            and timedelta(0)
            <= now - self._pilot_house_candidate_at
            <= timedelta(hours=3)
            else None
        )

        def evaluate():
            fresh_house = (
                pilot.validated_house(now.timestamp())
                if response_context is not None
                and cfg.get("external_heat_sources") is not True
                else None
            )
            house = (cached_house or fresh_house) if fresh_house is not None else None
            return task(house_context=house), house

        result, house = await asyncio.to_thread(evaluate)
        expected = {
            "indoor_temp_entity": extra.get("indoor_temperature"),
            "real_outdoor_entity": extra.get("outdoor_temperature"),
        }
        if (
            self._cfg().get("thermal_intelligence_enabled") is not True
            or self._pilot is not pilot
            or self._pilot_sources != pilot_sources
            or self._cfg() != config_snapshot
            or state_snapshot() != snapshot
            or control_snapshot() != control_before
            or not self._temperature_inputs_match(cfg, expected)
        ):
            self._pilot_grid_marginal = {
                "status": "unavailable",
                "reason": "inputs_changed_during_planning",
            }
            self._pilot_plan = {
                "planning_status": "unavailable",
                "reason": "inputs_changed_during_planning",
            }
            self._pilot_plan_next = None
            return
        issued_at = dt_util.now()
        if issued_at < now or issued_at - now > timedelta(minutes=1):
            self._pilot_grid_marginal = {
                "status": "unavailable",
                "reason": "planning_result_delayed",
            }
            self._pilot_plan = {
                "planning_status": "unavailable",
                "reason": "planning_result_delayed",
            }
            self._pilot_plan_next = None
            return
        if house is not None and pilot is not None:
            if cached_house is None:
                self._pilot_house_candidate = dict(house)
                self._pilot_house_candidate_at = now
                self._pilot_house_candidate_owner = pilot
            import hashlib

            raw = "pilot-house-v1:" + ":".join(
                format(house[key], ".17g") for key in ("loss", "gain", "background")
            )
            revision = hashlib.sha256(raw.encode()).hexdigest()[:16]
            quality = pilot.audit.summary(revision)
            result["forecast_quality"] = quality
            validated = result.get("planning_status") == "validated_passive"
            degraded = quality["status"] == "degraded"
            if degraded:
                result = {
                    "planning_status": "unavailable",
                    "reason": "forecast_quality_degraded",
                    "forecast_quality": quality,
                }
            if (
                validated
                and pilot.last_indoor_report is not None
                and timedelta(0)
                <= issued_at - pilot.last_indoor_report
                <= timedelta(minutes=5)
                and pilot.manager.recent
            ):
                tick, _, observed_heat = pilot.manager.recent[-1]
                if 0 <= issued_at.timestamp() - tick <= 60:
                    if pilot.audit.issue(
                        issued_at,
                        extra.get("indoor_temperature"),
                        extra.get("outdoor_temperature"),
                        observed_heat,
                        house["loss"],
                        house["gain"],
                        house["background"],
                        revision,
                        source_identity=pilot.source_identity,
                        source_reported=pilot.last_indoor_report,
                    ):
                        if not degraded:
                            result["monitoring_forecast"] = {
                                "kind": "conditional_unchanged_heat_and_outdoor",
                                "horizon_minutes": 15,
                                "temperature": round(
                                    pilot.audit.pending["forecast"], 2
                                ),
                                "initial_temperature": pilot.audit.pending["indoor"],
                                "rate_c_per_hour": pilot.audit.pending["rate"],
                                "valid_horizon_minutes": [10, 20],
                                "source_reported_at": pilot.audit.pending[
                                    "source_reported"
                                ].isoformat(),
                                "physical_measurement_time": "unknown",
                                "model_revision": revision,
                            }
                        await self._save_pilot_store()
        result["forecast_start"] = now.isoformat()
        result["issued_at"] = issued_at.isoformat()
        self._pilot_plan = result
        if result.get("planning_status") in {
            "validated_passive",
            "hypothetical_unvalidated",
        }:
            from .pilot_passive import PilotPassive
            import hashlib

            source = cfg.get("indoor_temp_entity")
            report = PilotPassive._report(
                self._temperature_state(cfg, "indoor_temp_entity"),
                issued_at,
                temperature=True,
                reported_after=self._temperature_reported_after,
            )
            result["plan_outcome_status"] = "unverifiable"
            result["plan_outcome_reason"] = "initial_temperature_report_unavailable"
            identity = self._plan_current_identity(cfg)
            if identity is None or identity != self._plan_cycle_identity:
                result["plan_outcome_reason"] = "source_changed_during_cycle"
            elif result.get("initial_context") is None:
                result["plan_outcome_reason"] = "initial_context_unavailable"
            elif report is not None:
                result["plan_outcome_reason"] = "invalid_plan_metadata"
                model = tuple(
                    (house or {}).get(key) for key in ("loss", "gain", "background")
                )
                response = (
                    tuple(sorted(vars(response_context[0]).items()))
                    if response_context
                    else None
                )
                revision = hashlib.sha256(
                    repr(
                        ("pilot-plan-v1", result["planning_status"], model, response)
                    ).encode()
                ).hexdigest()[:16]
                if self._plan_audit.issue_plan(
                    forecast_start=now,
                    issued_at=issued_at,
                    temperatures=result.get("temperatures"),
                    revision=revision,
                    status=result["planning_status"],
                    source_entity=source,
                    source_identity=identity,
                    source_reported=report[0],
                    initial_context=result.get("initial_context"),
                ):
                    result["plan_outcome_revision"] = revision
                    result["plan_outcome_status"] = "awaiting_reports"
                    result.pop("plan_outcome_reason", None)
                    await self._save_pilot_store()

    def _observe_pilot(self, cfg, now, mode):
        """Passive learning from explicitly selected delivered-house-heat state."""
        self._pilot_operating_mode = "unknown"
        self._pilot_operating_quality = "insufficient_evidence"
        self._heat_operation_detail = {}
        if cfg.get("thermal_intelligence_enabled") is not True:
            self._pilot = None
            self._pilot_sources = None
            self._heat_operation = None
            self._heat_operation_signature = None
            return
        price_state = self.hass.states.get(cfg.get("electricity_price_entity"))
        from .pilot_forecast import price_window, source_is_current

        tomorrow = cfg.get("price_tomorrow_entity")
        tomorrow_state = (
            self.hass.states.get(tomorrow)
            if tomorrow and tomorrow != cfg.get("electricity_price_entity")
            else None
        )
        from .pilot_marginal import grid_import_forecast

        price_intervals = price_window(
            (price_state, tomorrow_state), now, timedelta(hours=2)
        )
        self._pilot_price_forecast = price_intervals is not None
        self._pilot_grid_marginal = (
            grid_import_forecast(
                (price_state, tomorrow_state),
                now,
                timedelta(hours=2),
                intervals=price_intervals,
            )
            if price_intervals is not None
            else {"status": "unavailable", "reason": "price_forecast_unavailable"}
        )
        price_unit = (getattr(price_state, "attributes", {}) or {}).get(
            "unit_of_measurement"
        )
        self._pilot_price_available = (
            self._current_price is not None
            and source_is_current(price_state, now)
            and isinstance(price_unit, str)
            and bool(price_unit.strip())
        )
        self._pilot_forecast_available = self._last_outlook is not None and (
            self._last_outlook.night_min_temp is not None
            or self._last_outlook.day_max_temp is not None
        )
        if self._pilot_store_read_failed:
            self._pilot_status = "history_unavailable"
            return None
        method = cfg.get("thermal_detection_method")
        heat_id = cfg.get("thermal_heat_activity_entity")
        aliased_heat = bool(
            method in {"automatic", "verified"}
            and heat_id
            and heat_id
            in {
                cfg.get("thermal_compressor_entity"),
                cfg.get("thermal_heating_mode_entity"),
                cfg.get("thermal_hot_water_mode_entity"),
            }
        )
        source = None if aliased_heat else heat_id
        if method == "manual":
            source = None
        supply = cfg.get("thermal_supply_temp_entity")
        radiator_mode = cfg.get("thermal_heating_mode_entity")
        hot_water_mode = cfg.get("thermal_hot_water_mode_entity")
        threshold = safe_float(cfg.get("thermal_supply_threshold", 30))
        use_supply = (
            not source
            and method not in {"automatic", "verified"}
            and supply
            and radiator_mode
            and threshold is not None
            and 0 <= threshold <= 60
        )
        if use_supply:
            source = supply
        signature = (
            cfg.get("indoor_temp_entity"),
            cfg.get("real_outdoor_entity"),
            source,
            cfg.get("external_heat_sources") is True,
        )
        if use_supply:
            signature += (
                radiator_mode,
                threshold,
                cfg.get("thermal_heating_mode_inverted") is True,
            )
        if hot_water_mode:
            signature += ("hot_water_mode", hot_water_mode)
        if method == "automatic":
            signature += ("detection_method", method)
            signature += tuple(
                cfg.get(k)
                for k in (
                    "thermal_compressor_entity",
                    "thermal_return_temp_entity",
                    "thermal_power_entity",
                    "thermal_heating_mode_entity",
                    "thermal_heating_mode_inverted",
                    "thermal_supply_temp_entity",
                )
            )
        signature += (
            cfg.get("ohmigo_entity"),
            cfg.get("ohmigo_relay_entity"),
            safe_float(cfg.get("pid_output_clamp", PID_OUTPUT_CLAMP)),
        )
        if any(
            cfg.get(k)
            for k in (
                "indoor_temp_entity_2",
                "indoor_temp_entity_3",
                "outdoor_temp_entity_2",
            )
        ):
            signature += (repr(self._temperature_identity(cfg)),)
        if signature != self._pilot_sources:
            self._pilot = None
            self._pilot_detector = None
            self._pilot_sources = signature
            self._pilot_hot_water_checked_at = None
            self._pilot_operation_checked_at = None
        self._pilot_heat_source = (
            "supply_temperature_indicator"
            if use_supply
            else "binary_heat_indicator"
            if isinstance(source, str) and source.startswith("binary_sensor.")
            else "unavailable"
        )
        operation = None
        if method == "automatic":
            from .heat_operation import HeatOperation

            operation_signature = tuple(
                cfg.get(k)
                for k in (
                    "thermal_heat_activity_entity",
                    "thermal_compressor_entity",
                    "thermal_heating_mode_entity",
                    "thermal_heating_mode_inverted",
                    "thermal_hot_water_mode_entity",
                    "thermal_power_entity",
                    "thermal_supply_temp_entity",
                    "thermal_return_temp_entity",
                )
            )
            if (
                self._heat_operation is None
                or operation_signature != self._heat_operation_signature
            ):
                self._heat_operation = HeatOperation()
                self._heat_operation_signature = operation_signature
                saved = (self._pilot_persisted or {}).get("heat_operation")
                if isinstance(saved, dict) and saved.get("sources") == list(
                    operation_signature
                ):
                    self._heat_operation.restore(saved.get("samples"))

            def source_state(key):
                from types import SimpleNamespace

                entity = cfg.get(key)
                if not entity:
                    return None
                return self.hass.states.get(entity) or SimpleNamespace(
                    state="unavailable", attributes={}
                )

            radiator = self._heating_mode_state(cfg)
            if radiator is None and cfg.get("thermal_heating_mode_entity"):
                radiator = source_state("thermal_heating_mode_entity")
            operation = self._heat_operation.observe(
                now,
                heat=None
                if aliased_heat
                else source_state("thermal_heat_activity_entity"),
                compressor=source_state("thermal_compressor_entity"),
                radiator=radiator,
                water=source_state("thermal_hot_water_mode_entity"),
                power=source_state("thermal_power_entity"),
                supply=source_state("thermal_supply_temp_entity"),
                return_temp=source_state("thermal_return_temp_entity"),
                reported_after=self._temperature_reported_after,
            )
            if aliased_heat:
                operation["reason"] = "heat_signal_aliases_operating_mode"
            self._heat_operation_detail = operation
            self._pilot_operating_mode = operation["mode"]
            self._pilot_operating_quality = operation["quality"]
            # Independent operating transitions can happen between controller
            # cycles even if the verified heat indicator repeats unchanged.
            from .pilot_forecast import _timestamp
            from .pilot_passive import PilotPassive

            role_states = [
                source_state(key)
                for key in ("thermal_compressor_entity", "thermal_heating_mode_entity")
                if cfg.get(key)
            ]
            checked = self._pilot_operation_checked_at
            instant = _timestamp(now)
            valid = True
            changed_since = False
            for state in role_states:
                report = PilotPassive._report(
                    state, now, reported_after=self._temperature_reported_after
                )
                changed = _timestamp(getattr(state, "last_changed", None))
                if report is None or changed is None or changed > _timestamp(report[0]):
                    valid = False
                    break
                if checked is not None and changed > checked:
                    changed_since = True
            if not valid:
                operation["learning_qualified"] = False
                operation["learning_reason"] = "operating_context_unavailable"
            if checked is not None and (
                not valid or changed_since or instant < checked
            ):
                if self._pilot is not None:
                    self._pilot.reset_interval()
                if self._pilot_plan.get("planning_status") == "validated_passive":
                    self._pilot_plan = {
                        "planning_status": "unavailable",
                        "reason": "operation_context_changed",
                    }
            self._pilot_operation_checked_at = (
                instant if valid and role_states else None
            )
        if not use_supply and (
            not isinstance(source, str) or not source.startswith("binary_sensor.")
        ):
            if operation is not None:
                if self._pilot is None:
                    from .pilot_passive import PilotPassive

                    self._pilot = PilotPassive()
                self._pilot.observe_indicated(
                    now,
                    self._temperature_state(cfg, "indoor_temp_entity"),
                    self._temperature_state(cfg, "real_outdoor_entity"),
                    operation,
                    reported_after=self._temperature_reported_after,
                )
                operation["temperature_observation_status"] = (
                    self._pilot.indicated_house.last_status
                )
                operation["indicated_observation_intervals"] = len(
                    self._pilot.indicated_observations
                )
                operation["latest_indicated_interval"] = (
                    self._pilot.indicated_observations[-1]
                    if self._pilot.indicated_observations
                    else None
                )
            if hot_water_mode:
                from .pilot_forecast import _timestamp
                from .pilot_passive import PilotPassive

                state = self.hass.states.get(hot_water_mode)
                report = PilotPassive._report(
                    state, now, reported_after=self._temperature_reported_after
                )
                changed = _timestamp(getattr(state, "last_changed", None))
                if (
                    report is not None
                    and changed is not None
                    and changed <= _timestamp(report[0])
                    and report[1] == 1.0
                ):
                    self._pilot_operating_mode = "hot_water"
                    self._pilot_operating_quality = (
                        "reported_mode_not_physical_confirmation"
                    )
            self._pilot_status = "heat_signal_not_configured"
            return
        try:
            heat_state = self.hass.states.get(source)
            hot_water_status = None
            if hot_water_mode:
                from .pilot_forecast import _timestamp
                from .pilot_passive import PilotPassive

                state = self.hass.states.get(hot_water_mode)
                report = PilotPassive._report(
                    state, now, reported_after=self._temperature_reported_after
                )
                changed = _timestamp(getattr(state, "last_changed", None))
                if report is None or changed is None or changed > _timestamp(report[0]):
                    hot_water_status = "hot_water_mode_unavailable"
                    self._pilot_hot_water_checked_at = None
                elif report[1] != 0.0:
                    hot_water_status = "hot_water_active"
                if hot_water_status is None:
                    previous = self._pilot_hot_water_checked_at
                    if previous is not None and (
                        changed > previous or _timestamp(now) < previous
                    ):
                        if self._pilot is not None:
                            self._pilot.reset_interval()
                        self._pilot_detector = None
                        if (
                            self._pilot_plan.get("planning_status")
                            == "validated_passive"
                        ):
                            self._pilot_plan = {
                                "planning_status": "unavailable",
                                "reason": "hot_water_mode_changed",
                            }
                    self._pilot_hot_water_checked_at = _timestamp(now)
                else:
                    self._pilot_hot_water_checked_at = None
            if hot_water_status is not None:
                # A conflicting or unknown water mode cannot establish
                # radiator heat; reset the supply detector as well.
                self._pilot_detector = None
                heat_state = None
                if self._pilot_plan.get("planning_status") == "validated_passive":
                    self._pilot_plan = {
                        "planning_status": "unavailable",
                        "reason": hot_water_status,
                    }
            elif use_supply:
                from types import SimpleNamespace
                from .pilot_passive import SupplyHeatDetector

                if self._pilot_detector is None:
                    self._pilot_detector = SupplyHeatDetector(threshold)
                after = self._temperature_reported_after
                if self._pilot_detection_after is not None and (
                    after is None or self._pilot_detection_after > after
                ):
                    after = self._pilot_detection_after
                detected = self._pilot_detector.observe(
                    now,
                    heat_state,
                    self._heating_mode_state(cfg),
                    reported_after=after,
                )
                heat_state = (
                    None
                    if detected is None
                    else SimpleNamespace(
                        state="on" if detected else "off",
                        last_reported=self._pilot_detector.last_report,
                        last_changed=self._pilot_detector.changed_at,
                        attributes={},
                    )
                )
            if operation is not None and not operation["learning_qualified"]:
                heat_state = None
            if operation is not None:
                pass
            elif hot_water_status == "hot_water_active":
                self._pilot_operating_mode = "hot_water"
                self._pilot_operating_quality = (
                    "reported_mode_not_physical_confirmation"
                )
            elif hot_water_status is None and heat_state is not None:
                from .pilot_passive import PilotPassive

                observation = PilotPassive._report(
                    heat_state, now, reported_after=self._temperature_reported_after
                )
                if observation is not None:
                    if observation[1]:
                        self._pilot_operating_mode = "radiator_heating"
                        self._pilot_operating_quality = (
                            "heat_indicator_not_measured_output"
                        )
                    else:
                        self._pilot_operating_quality = (
                            "no_heat_detected_idle_unverified"
                        )
            if self._pilot is None:
                from .pilot_passive import PilotPassive

                self._pilot = PilotPassive()
                self._restore_pilot_history(self._pilot, signature, now)
            if operation is not None:
                self._pilot.observe_indicated(
                    now,
                    self._temperature_state(cfg, "indoor_temp_entity"),
                    self._temperature_state(cfg, "real_outdoor_entity"),
                    operation,
                    reported_after=self._temperature_reported_after,
                )
                operation["temperature_observation_status"] = (
                    self._pilot.indicated_house.last_status
                )
                operation["indicated_observation_intervals"] = len(
                    self._pilot.indicated_observations
                )
                operation["latest_indicated_interval"] = (
                    self._pilot.indicated_observations[-1]
                    if self._pilot.indicated_observations
                    else None
                )
            if mode in {"normal", "preheating", "pre_braking", "braking", "brake_hold"}:
                paired = self._pilot.audit.observe(
                    now,
                    self._temperature_state(cfg, "indoor_temp_entity"),
                    self._temperature_state(cfg, "real_outdoor_entity"),
                    heat_state,
                )
                if paired:
                    revision = self._pilot.audit.records[-1]["revision"]
                    quality = self._pilot.audit.summary(revision)
                    if quality["status"] == "degraded":
                        self._pilot_plan = {
                            "planning_status": "unavailable",
                            "reason": "forecast_quality_degraded",
                            "forecast_quality": quality,
                        }
                        self._pilot_plan_next = None
            else:
                self._pilot.audit.pending = None
            self._pilot.observe(
                now,
                self._temperature_state(cfg, "indoor_temp_entity"),
                self._temperature_state(cfg, "real_outdoor_entity"),
                heat_state,
                mode,
                reported_after=self._temperature_reported_after,
            )
            command_entity = cfg.get("ohmigo_entity")
            from .ohmigo import _switch_entity_id

            switch_id = _switch_entity_id(self.hass, self._config_entry.entry_id)
            relay_id = cfg.get("ohmigo_relay_entity")
            switch_state = self.hass.states.get(switch_id) if switch_id else None
            relay_state = self.hass.states.get(relay_id) if relay_id else None
            # HA states indicate configured Push and Active, not physical receipt.
            command_allowed = self._pilot._report(
                switch_state, now, reported_after=self._temperature_reported_after
            ) == (
                getattr(switch_state, "last_reported", None),
                1.0,
            ) and self._pilot._report(
                relay_state, now, reported_after=self._temperature_reported_after
            ) == (getattr(relay_state, "last_reported", None), 1.0)
            sample = self._pilot.observe_response(
                now,
                self._temperature_state(cfg, "real_outdoor_entity"),
                heat_state,
                self.hass.states.get(command_entity)
                if command_entity and command_allowed
                else None,
                mode,
                output_clamp=safe_float(cfg.get("pid_output_clamp", PID_OUTPUT_CLAMP)),
                reported_after=self._temperature_reported_after,
                defer_fit=True,
            )
            self._pilot_status = hot_water_status or self._pilot.status
            return sample
        except Exception:
            _LOGGER.exception(
                "Passive thermal intelligence failed; standard control continues"
            )
            self._pilot_operating_mode = "unknown"
            self._pilot_operating_quality = "insufficient_evidence"
            self._pilot = None
            self._pilot_status = "learning_error"


class PumpSteerThermalStatusSensor(SensorEntity):
    """Compact passive status, updated only when its summary changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "thermal_intelligence"
    _attr_icon = "mdi:brain"
    _attr_device_class = "enum"
    _attr_options = [
        "disabled",
        "waiting",
        "learning",
        "hypothetical_plan",
        "validated_plan",
        "unavailable",
    ]
    _attr_native_value = None

    def __init__(self, controller: PumpSteerSensor) -> None:
        self._controller = controller
        self._attr_unique_id = f"{controller.unique_id}_thermal_intelligence"
        self._attr_device_info = controller._attr_device_info
        self._summary = {}

    @property
    def extra_state_attributes(self):
        return self._summary

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._controller._input_temperature_signal,
                self._async_update_from_controller,
            )
        )
        if not self._controller._update_in_progress:
            self._async_update_from_controller()

    @callback
    def _async_update_from_controller(self):
        controller = self._controller
        cfg = controller._cfg()
        enabled = cfg.get("thermal_intelligence_enabled") is True
        attrs = controller.extra_state_attributes
        plan = attrs.get("thermal_intelligence_plan_detail") or {}
        planning_status = plan.get("planning_status", "unavailable")
        reason = attrs.get("thermal_intelligence_reason")
        if not enabled:
            state = "disabled"
        elif controller._temperature_unloaded or attrs.get("mode") == "safe_mode":
            state = "unavailable"
        elif planning_status == "hypothetical_unvalidated":
            state = "hypothetical_plan"
        elif planning_status == "validated_passive":
            state = "validated_plan"
        elif reason in ("learning_error", "update_error", "history_unavailable"):
            state = "unavailable"
        elif attrs.get("thermal_intelligence_learning_intervals", 0) and attrs.get(
            "thermal_intelligence_status"
        ) in {
            "learning",
            "insufficient_history",
            "insufficient_weather_variation",
            "gain_frozen",
            "ill_conditioned",
        }:
            state = "learning"
        else:
            state = "waiting"
        summary = {
            "enabled": enabled,
            "passive": True,
            "planning_status": planning_status if enabled else "unavailable",
            "reason": reason if enabled else None,
            "plan_reason": plan.get("reason") if enabled else None,
            "price_forecast": attrs.get("thermal_intelligence_price_forecast"),
            "heat_source": attrs.get("thermal_intelligence_heat_source"),
            "operating_mode": attrs.get(
                "thermal_intelligence_operating_mode", "unknown"
            ),
            "operating_quality": attrs.get(
                "thermal_intelligence_operating_quality", "insufficient_evidence"
            ),
            "operation_detail": attrs.get("thermal_intelligence_operation_detail", {}),
            "heat_entity": cfg.get("thermal_heat_activity_entity")
            or cfg.get("thermal_supply_temp_entity"),
            "radiator_mode_entity": cfg.get("thermal_heating_mode_entity"),
            "hot_water_entity": cfg.get("thermal_hot_water_mode_entity"),
            "learning_intervals": attrs.get(
                "thermal_intelligence_learning_intervals", 0
            ),
            "model": attrs.get("thermal_intelligence_model"),
        }
        if getattr(controller, "_zone_diagnostics", None):
            summary["extra_temperature_zones"] = controller._zone_diagnostics
        if state != self._attr_native_value or summary != self._summary:
            self._attr_native_value = state
            self._summary = summary
            self.async_write_ha_state()


class PumpSteerInputTemperatureSensor(SensorEntity):
    """Expose one qualified control input without reading its source again."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "°C"
    _attr_device_class = "temperature"
    _attr_state_class = "measurement"
    _attr_native_value = None

    def __init__(self, controller: PumpSteerSensor, key: str) -> None:
        self._controller = controller
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{controller.unique_id}_{key}"
        self._attr_device_info = controller._attr_device_info

    @property
    def available(self) -> bool:
        return self._attr_native_value is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._controller._input_temperature_signal,
                self._async_update_from_controller,
            )
        )
        if not self._controller._update_in_progress:
            self._async_update_from_controller()

    @callback
    def _async_update_from_controller(self) -> None:
        value = self._controller._input_temperatures.get(self._key)
        if value != self._attr_native_value:
            self._attr_native_value = value
            self.async_write_ha_state()


class ThermalOutlookSensor(SensorEntity):
    """Exposes ThermalOutlook as a HA sensor for visualization and debugging."""

    _attr_has_entity_name = True
    _attr_name = "Thermal Outlook"
    _attr_icon = "mdi:weather-partly-cloudy"
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        self.hass = hass
        self._config_entry = config_entry
        self._attr_unique_id = f"{config_entry.entry_id}_thermal_outlook"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, config_entry.entry_id)},
            name="PumpSteer",
            manufacturer="PumpSteer",
            model="Heat Pump Controller",
            sw_version=SW_VERSION,
        )
        from .forecast import ThermalOutlook

        self._outlook: Optional[ThermalOutlook] = ThermalOutlook(
            night_min_temp=None,
            day_max_temp=None,
            hours_below_threshold=0,
            effective_temp_now=None,
            warming_trend=False,
            cooling_trend=False,
            precool_risk=False,
            preheat_worthwhile=False,
            preheat_strength=0.0,
        )

    @property
    def state(self) -> str:
        if self._outlook is None:
            return "unavailable"
        if self._outlook.preheat_worthwhile:
            return "preheat"
        if self._outlook.precool_risk:
            return "precool"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict:
        if self._outlook is None:
            return {}
        o = self._outlook
        return {
            "night_min_temp": o.night_min_temp,
            "day_max_temp": o.day_max_temp,
            "hours_below_threshold": o.hours_below_threshold,
            "effective_temp_now": (
                round(o.effective_temp_now, 1)
                if o.effective_temp_now is not None
                else None
            ),
            "warming_trend": o.warming_trend,
            "cooling_trend": o.cooling_trend,
            "precool_risk": o.precool_risk,
            "preheat_worthwhile": o.preheat_worthwhile,
            "preheat_strength": round(o.preheat_strength, 2),
        }

    async def async_update(self) -> None:
        """Fetch forecast and compute ThermalOutlook."""
        from .forecast import async_build_forecast, analyze_thermal_outlook

        cfg = {**self._config_entry.data, **self._config_entry.options}
        weather_entity = cfg.get("weather_entity")
        price_entity = cfg.get("electricity_price_entity")
        summer_threshold = float(cfg.get("summer_threshold", DEFAULT_SUMMER_THRESHOLD))

        if not weather_entity or not price_entity:
            return
        try:
            points = await async_build_forecast(
                self.hass,
                price_entity_id=price_entity,
                weather_entity_id=weather_entity,
                horizon_hours=24,
            )
            self._outlook = analyze_thermal_outlook(
                points,
                summer_threshold=summer_threshold,
                now_hour=dt_util.now().hour,
            )
        except Exception as err:
            _LOGGER.debug("ThermalOutlookSensor update failed: %s", err)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    from .pump_log import setup_pump_log

    await hass.async_add_executor_job(setup_pump_log)
    sensor = PumpSteerSensor(hass, config_entry)
    outlook_sensor = ThermalOutlookSensor(hass, config_entry)
    async_add_entities(
        [
            sensor,
            outlook_sensor,
            PumpSteerInputTemperatureSensor(sensor, "indoor_temperature"),
            PumpSteerInputTemperatureSensor(sensor, "outdoor_temperature"),
            PumpSteerThermalStatusSensor(sensor),
        ],
        update_before_add=False,
    )
