"""Passive evidence capture and shadow evaluation for the RTM prototype."""

from __future__ import annotations

import math
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from .rtm_core import ModelConfig, RCThermalModel, Sample
from .rtm_core.quality import step_temperature

_STATE_VERSION = 1
_MAX_RECORDS = 2048
_MAX_GAP_MINUTES = 60.0
_OUTDOOR_MAX_AGE = timedelta(hours=3)
_AUDIT_HORIZON = timedelta(hours=1)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result.astimezone(UTC)


def command_coordinate(outdoor: float, virtual_outdoor: float, span: float) -> float:
    """Map a setpoint request to a bounded, ordinal command coordinate.

    One means the coldest request (nominally most heating); zero means the warmest.
    This is a coordinate for the RTM input term, not heating effort or delivered heat.
    """
    if not all(math.isfinite(value) for value in (outdoor, virtual_outdoor, span)):
        raise ValueError("command coordinate inputs must be finite")
    if span <= 0:
        raise ValueError("command span must be positive")
    return min(1.0, max(0.0, (outdoor + span - virtual_outdoor) / (2.0 * span)))


class ThermalIntelligence:
    """Bounded, restart-safe passive data path; never controls PumpSteer outputs."""

    def __init__(self) -> None:
        self.model = RCThermalModel(
            ModelConfig(
                step_minutes=15.0,
                half_life_days=21.0,
                max_dead_time_minutes=180.0,
                max_gap_minutes=_MAX_GAP_MINUTES,
                max_step_delta_k=1.5,
                history_days=30.0,
                outdoor_max_gap_minutes=180.0,
            )
        )
        self._source_signature: tuple = ()
        self._source_values: dict[str, tuple[float, datetime]] = {}
        self._last_command: dict[str, Any] | None = None
        self._active_cycle: dict[str, Any] | None = None
        self._pending_forecast: dict[str, Any] | None = None
        self._last_forecast_at: datetime | None = None
        self._observations: deque[dict[str, Any]] = deque(maxlen=_MAX_RECORDS)
        self._outputs: deque[dict[str, Any]] = deque(maxlen=_MAX_RECORDS)
        self._scores: deque[dict[str, Any]] = deque(maxlen=96)
        self._interruptions: deque[dict[str, Any]] = deque(maxlen=96)
        self._last_interruption: str | None = None
        self._status = "collecting"
        self._samples = 0
        self._last_training_at: datetime | None = None
        self._braking_episode_active = False
        self._braking_episode_forecasted = False

    @property
    def status(self) -> str:
        if self.model.parameters() is None:
            return self._status
        return "candidate_unvalidated"

    @property
    def store_signature(self) -> tuple:
        return (
            self._observations[-1].get("at") if self._observations else None,
            self._outputs[-1].get("at") if self._outputs else None,
            self._scores[-1].get("observed") if self._scores else None,
            len(self._observations),
            len(self._outputs),
            self._samples,
            self._interruptions[-1].get("at") if self._interruptions else None,
            len(self._interruptions),
        )

    @property
    def has_evidence(self) -> bool:
        return bool(
            self._observations or self._outputs or self._scores or self._interruptions
        )

    def begin_cycle(self, cycle: dict[str, Any], *, use_ohmigo_input: bool) -> None:
        """Record one decision context and add only value-advancing observations."""
        self._active_cycle = None
        at = _timestamp(cycle.get("at"))
        indoor = _number(cycle.get("indoor"))
        outdoor = _number(cycle.get("outdoor"))
        outdoor_at = _timestamp(cycle.get("outdoor_reported_at"))
        sources = cycle.get("sources")
        mode = cycle.get("mode")
        indoor_signature = (
            tuple(
                sorted(
                    (
                        row.get("entity"),
                        _number(row.get("weight")),
                    )
                    for row in sources
                    if isinstance(row, dict) and isinstance(row.get("entity"), str)
                )
            )
            if isinstance(sources, list)
            else ()
        )
        source_signature = (
            indoor_signature,
            cycle.get("outdoor_source"),
            cycle.get("command_source"),
        )
        stable_braking = (
            mode == "braking" and (_number(cycle.get("brake_factor")) or 0.0) >= 0.98
        )
        if not stable_braking or not self._braking_episode_active:
            self._braking_episode_forecasted = False
        self._braking_episode_active = stable_braking

        if (
            source_signature
            and self._source_signature
            and source_signature != self._source_signature
        ):
            self._interrupt("source_or_transport_changed", at)
            self.model = RCThermalModel(self.model.config)
            self._source_values.clear()
            self._last_command = None
            self._samples = 0
            self._last_training_at = None
            self._last_forecast_at = None
        if source_signature:
            self._source_signature = source_signature
        pending = self._pending_forecast
        if pending is not None and (
            tuple(pending["identity"]) != self._source_signature
            or mode != pending["mode"]
            or abs(
                (_number(cycle.get("brake_factor")) or 0.0)
                - (_number(pending.get("brake_factor")) or 0.0)
            )
            > 0.02
            or (
                _number(cycle.get("outdoor")) is not None
                and abs(_number(cycle.get("outdoor")) - pending["outdoor"]) > 1e-9
            )
        ):
            self._interrupt("forecast_context_changed", at)

        changed = False
        source_rows = []
        if isinstance(sources, list):
            for row in sources:
                if not isinstance(row, dict) or not isinstance(row.get("entity"), str):
                    continue
                value = _number(row.get("value"))
                reported = _timestamp(row.get("reported_at"))
                entity = row["entity"]
                previous = self._source_values.get(entity)
                advanced = bool(
                    value is not None
                    and reported is not None
                    and (previous is None or reported > previous[1])
                    and (previous is None or value != previous[0])
                )
                if value is not None and reported is not None:
                    self._source_values[entity] = (value, reported)
                changed = changed or advanced
                source_rows.append(
                    {
                        "entity": entity,
                        "weight": _number(row.get("weight")),
                        "value": value,
                        "reported_at": reported.isoformat() if reported else None,
                        "value_advanced": advanced,
                    }
                )

        if at is None or indoor is None or outdoor is None:
            self._interrupt("required_temperature_missing", at)
            self._status = "waiting_for_temperatures"
            return

        indoor_at = _timestamp(cycle.get("indoor_reported_at"))
        if (
            not changed
            or indoor_at is None
            or indoor_at > at
            or at - indoor_at > timedelta(minutes=_MAX_GAP_MINUTES)
        ):
            self._status = "waiting_for_new_temperature_value"
            return
        if outdoor_at is None or at - outdoor_at > _OUTDOOR_MAX_AGE or outdoor_at > at:
            self._interrupt("outdoor_observation_stale", at)
            self._status = "waiting_for_fresh_outdoor"
            return

        command = self._last_command if use_ohmigo_input else None
        command_value = _number(command.get("requested")) if command else None
        command_at = _timestamp(command.get("at")) if command else None
        command_status = command.get("status") if command else None
        coordinate = None
        if (
            command_value is not None
            and command_at is not None
            and command_status == "service_completed_unverified"
            and command_at <= indoor_at
            and outdoor_at <= indoor_at
        ):
            coordinate = command_coordinate(
                outdoor,
                command_value,
                _number(cycle.get("command_span")) or 20.0,
            )
            if (
                mode == "braking"
                and (_number(cycle.get("brake_factor")) or 0.0) >= 0.98
                and (
                    self._last_training_at is None or indoor_at > self._last_training_at
                )
            ):
                self.model.add_sample(
                    Sample(
                        ts=indoor_at,
                        indoor_c=indoor,
                        outdoor_c=outdoor,
                        effort=coordinate,
                    )
                )
                self._last_training_at = indoor_at
                self._samples += 1
                if self.model.fit_due(at):
                    summary = self.model.fit(at)
                    self._status = summary.get("status", "candidate_fit_failed")
                else:
                    self._status = "candidate_learning"

        record = {
            "at": at.isoformat(),
            "indoor_reported_at": indoor_at.isoformat(),
            "indoor_c": indoor,
            "outdoor_reported_at": outdoor_at.isoformat(),
            "outdoor_c": outdoor,
            "outdoor_source": cycle.get("outdoor_source"),
            "sources": source_rows,
            "requested_virtual_outdoor_c": _number(
                cycle.get("requested_virtual_outdoor")
            ),
            "mode": mode,
            "brake_factor": _number(cycle.get("brake_factor")),
            "pi_demand_c": _number(cycle.get("pi_demand_c")),
            "command_coordinate": coordinate,
            "command_status": command_status,
            "command_at": command_at.isoformat() if command_at else None,
            "interruption": self._last_interruption,
        }
        self._last_interruption = None
        self._observations.append(record)
        self._active_cycle = record

        self._score_pending(at, indoor, indoor_at, outdoor, mode, coordinate)
        self._maybe_issue(record, outdoor, command_value, coordinate)

    def record_output(self, event: dict[str, Any]) -> None:
        """Capture transport-level status; never claim physical application."""
        row = dict(event)
        at = _timestamp(row.get("at"))
        row["at"] = at.isoformat() if at else None
        row["requested"] = _number(row.get("requested"))
        self._outputs.append(row)
        if (
            row.get("transport") == "ohmigo"
            and row.get("status") == "service_completed_unverified"
            and row["requested"] is not None
            and at is not None
        ):
            previous = self._last_command
            if (
                previous is not None
                and _number(previous.get("requested")) != row["requested"]
            ):
                self._interrupt("ohmigo_command_changed", at)
            self._last_command = {
                "requested": row["requested"],
                "at": at.isoformat(),
                "status": row["status"],
            }
        elif row.get("transport") == "ohmigo" and row.get("status") in {
            "dispatch_failed",
            "skipped",
            "disabled",
            "not_configured",
            "invalid_configuration",
        }:
            self._last_command = None
            self._interrupt(f"ohmigo_{row['status']}", at)
        if self._active_cycle is not None:
            self._active_cycle.setdefault("outputs", []).append(row)

    def _maybe_issue(
        self,
        record: dict[str, Any],
        outdoor: float,
        command_value: float | None,
        coordinate: float | None,
    ) -> None:
        params = self.model.parameters()
        at = _timestamp(record["at"])
        command_at = _timestamp(record.get("command_at"))
        command_warmup = (
            at is not None
            and command_at is not None
            and at - command_at >= timedelta(minutes=max(0.0, params.dead_time_minutes))
            if params is not None
            else False
        )
        if self._pending_forecast is not None and (
            tuple(self._pending_forecast["identity"]) != self._source_signature
            or record.get("mode") != self._pending_forecast["mode"]
            or abs(
                (_number(record.get("brake_factor")) or 0.0)
                - (_number(self._pending_forecast.get("brake_factor")) or 0.0)
            )
            > 0.02
            or abs(outdoor - self._pending_forecast["outdoor"]) > 1e-9
            or coordinate is None
            or abs(coordinate - self._pending_forecast["coordinate"]) > 1e-6
        ):
            self._interrupt("forecast_context_changed", at)
        if (
            params is None
            or at is None
            or record.get("mode") != "braking"
            or (_number(record.get("brake_factor")) or 0.0) < 0.98
            or command_value is None
            or coordinate is None
            or not command_warmup
            or self._pending_forecast is not None
            or self._braking_episode_forecasted
        ):
            return
        prediction = self._predict(record["indoor_c"], outdoor, coordinate, params)
        if prediction is None:
            return
        self._pending_forecast = {
            "issued": at.isoformat(),
            "due": (at + _AUDIT_HORIZON).isoformat(),
            "identity": self._source_signature,
            "mode": record["mode"],
            "brake_factor": record["brake_factor"],
            "indoor": record["indoor_c"],
            "outdoor": outdoor,
            "coordinate": coordinate,
            "model": self.model.to_dict(),
            "predicted": prediction,
            "invalidated": False,
        }
        self._braking_episode_forecasted = True
        self._last_forecast_at = at

    @staticmethod
    def _predict(indoor, outdoor, coordinate, params) -> float | None:
        """Forecast a frozen, constant command/outdoor scenario for one hour."""
        values = (
            params.heating_rate_k_per_h,
            params.loss_rate_per_h,
            params.gains_k_per_h,
        )
        if not all(math.isfinite(value) for value in values):
            return None
        step_h = 0.25
        temp = indoor
        effort = coordinate
        for index in range(4):
            temp = step_temperature(
                temp,
                effort,
                outdoor,
                params.heating_rate_k_per_h,
                params.loss_rate_per_h,
                params.gains_k_per_h,
                step_h,
            )
            if not math.isfinite(temp):
                return None
        return temp

    def _score_pending(self, at, indoor, indoor_at, outdoor, mode, coordinate):
        pending = self._pending_forecast
        if pending is None:
            return
        issued = _timestamp(pending["issued"])
        due = _timestamp(pending["due"])
        if (
            issued is None
            or due is None
            or pending["invalidated"]
            or tuple(pending["identity"]) != self._source_signature
            or mode != pending["mode"]
            or indoor_at < due
            or indoor_at > due + timedelta(minutes=5)
        ):
            if at > (due + timedelta(minutes=5) if due else at):
                self._pending_forecast = None
                self._interrupt("forecast_outcome_unavailable", at)
            return
        if coordinate is None or abs(coordinate - pending["coordinate"]) > 1e-6:
            self._pending_forecast = None
            self._interrupt("forecast_command_unverified_or_changed", at)
            return
        elapsed = (indoor_at - issued).total_seconds() / 3600.0
        if not 0 < elapsed <= 65 / 60:
            self._pending_forecast = None
            self._interrupt("forecast_endpoint_out_of_window", at)
            return
        error = abs(pending["predicted"] - indoor)
        persistence_error = abs(pending["indoor"] - indoor)
        self._scores.append(
            {
                "issued": pending["issued"],
                "observed": indoor_at.isoformat(),
                "horizon_minutes": round(elapsed * 60, 2),
                "model_error_c": error,
                "persistence_error_c": persistence_error,
                "model": "rtm_command_v1",
                "independent_unit": "non_overlapping_braking_episode",
                "command_application": "service_completed_unverified",
                "identity": repr(self._source_signature),
            }
        )
        self._pending_forecast = None

    def _interrupt(self, reason: str, at: datetime | None = None) -> None:
        if reason != self._last_interruption:
            self._interruptions.append(
                {
                    "at": (at or datetime.now(UTC)).isoformat(),
                    "reason": reason,
                }
            )
        self._last_interruption = reason
        if self._active_cycle is not None:
            self._active_cycle["interruption"] = reason
        if self._pending_forecast is not None:
            self._pending_forecast = None

    def export_state(self) -> dict[str, Any]:
        """Return JSON-compatible bounded state; open predictions never resume."""
        return {
            "version": _STATE_VERSION,
            "model": self.model.to_dict(),
            "source_signature": {
                "indoor": [list(row) for row in self._source_signature[0]]
                if self._source_signature
                else [],
                "outdoor": self._source_signature[1]
                if self._source_signature
                else None,
                "command_source": self._source_signature[2]
                if self._source_signature
                else None,
            },
            "observations": list(self._observations),
            "outputs": list(self._outputs),
            "scores": list(self._scores),
            "interruptions": list(self._interruptions),
            "samples": self._samples,
            "last_training_at": (
                self._last_training_at.isoformat() if self._last_training_at else None
            ),
            "braking_episode_active": self._braking_episode_active,
            "braking_episode_forecasted": self._braking_episode_forecasted,
            "last_forecast_at": (
                self._last_forecast_at.isoformat() if self._last_forecast_at else None
            ),
        }

    def restore_state(self, data: Any) -> bool:
        """Restore completed history; require a new command after every restart."""
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
            return False
        try:
            model = RCThermalModel.from_dict(data["model"], self.model.config)
            rows = data.get("observations", [])
            outputs = data.get("outputs", [])
            scores = data.get("scores", [])
            if not all(isinstance(value, list) for value in (rows, outputs, scores)):
                return False
            self.model = model
            self._observations = deque(rows[-_MAX_RECORDS:], maxlen=_MAX_RECORDS)
            self._outputs = deque(outputs[-_MAX_RECORDS:], maxlen=_MAX_RECORDS)
            self._scores = deque(scores[-96:], maxlen=96)
            interruptions = data.get("interruptions", [])
            if not isinstance(interruptions, list):
                return False
            self._interruptions = deque(interruptions[-96:], maxlen=96)
            self._last_interruption = None
            self._samples = max(0, int(data.get("samples", 0)))
            self._last_training_at = _timestamp(data.get("last_training_at"))
            self._braking_episode_active = bool(
                data.get("braking_episode_active", False)
            )
            self._braking_episode_forecasted = bool(
                data.get("braking_episode_forecasted", False)
            )
            raw_signature = data.get("source_signature", {})
            if not isinstance(raw_signature, dict):
                return False
            raw_indoor = raw_signature.get("indoor", [])
            if not isinstance(raw_indoor, list):
                return False
            indoor_signature = tuple(
                (row[0], _number(row[1]))
                for row in raw_indoor
                if isinstance(row, list) and len(row) == 2 and isinstance(row[0], str)
            )
            self._source_signature = (
                indoor_signature,
                raw_signature.get("outdoor"),
                raw_signature.get("command_source"),
            )
            self._source_values.clear()
            observations = list(self._observations)
            if observations:
                for row in observations[-1].get("sources", []):
                    if not isinstance(row, dict):
                        continue
                    value = _number(row.get("value"))
                    reported = _timestamp(row.get("reported_at"))
                    entity = row.get("entity")
                    if (
                        isinstance(entity, str)
                        and value is not None
                        and reported is not None
                    ):
                        self._source_values[entity] = (value, reported)
            self._last_command = None
            self._pending_forecast = None
            self._last_forecast_at = _timestamp(data.get("last_forecast_at"))
            self._active_cycle = None
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        return True

    def summary(self) -> dict[str, Any]:
        params = self.model.parameters()
        scores = [
            row
            for row in self._scores
            if row.get("identity") == repr(self._source_signature)
        ]
        model_mae = (
            sum(row["model_error_c"] for row in scores) / len(scores)
            if scores
            else None
        )
        persistence_mae = (
            sum(row["persistence_error_c"] for row in scores) / len(scores)
            if scores
            else None
        )
        return {
            "status": self.status,
            "model": "adaptive_preheat_rtm_empirical_command_response",
            "samples": self._samples,
            "fit_parameters_available": params is not None,
            "fit_trusted": False,
            "forecast_count": len(scores),
            "forecast_mae_c": model_mae,
            "persistence_mae_c": persistence_mae,
            "forecast_skill": (
                1.0 - model_mae / persistence_mae
                if model_mae is not None
                and persistence_mae is not None
                and persistence_mae > 0
                else None
            ),
            "command_semantics": "service_request_unverified_not_thermal_power",
            "active_control": False,
            "last_interruption": (
                self._interruptions[-1]["reason"] if self._interruptions else None
            ),
            "reason": "passive_diagnostics_only",
        }
