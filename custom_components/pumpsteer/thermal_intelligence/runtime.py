"""Passive, chronological evidence and model evaluation."""

from __future__ import annotations

import itertools
import math
from collections import deque
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..pilot_passive import PilotPassive
from ..pilot_vendor.adaptive_model import AdaptiveThermalModel
from ..pilot_vendor.learning_interval import LearningInterval
from .rtm_core import ModelConfig, RCThermalModel, Sample
from .rtm_core.quality import step_temperature

_STATE_VERSION = 2
_MAX_RECORDS = 2048
_MAX_GAP_MINUTES = 60.0
_OUTDOOR_MAX_AGE = timedelta(hours=3)
_PILOT_HEAT_MAX_AGE = timedelta(minutes=5)
_PILOT_HISTORY_HOURS = 336
_COUNTER_LIMIT = 2**63 - 1
_COUNTER_KEYS = (
    "source_numeric_changes", "source_change_attempts", "source_changes_accepted",
    "source_changes_rejected", "accepted_indoor_changes", "accepted_outdoor_changes",
    "accepted_changes_physical_time_unknown", "identical_source_rechecks",
    "reject_report_time_missing", "reject_report_time_future", "reject_report_time_stale",
    "reject_report_out_of_order", "aggregate_observations", "command_establishments",
    "command_value_changes", "command_confirmations", "dispatch_failures",
    "interruptions", "restarts", "fit_attempts", "fit_results_applied", "fit_results_stale",
)


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


def classify_regime(mode: str | None, brake_factor: float | None) -> str:
    """Label operating regime for evidence segregation without cross-mode pollution."""
    bf = brake_factor or 0.0
    if mode == "braking" and bf >= 0.98:
        return "braking_stable"
    if mode in ("braking", "pre_braking", "brake_hold"):
        return "braking_transition"
    if mode == "preheating":
        return "preheating"
    if mode in ("normal", "holiday") and bf <= 0.02:
        return "normal_pi"
    if mode == "precool":
        return "precool"
    if mode == "summer_mode":
        return "summer"
    if mode == "safe_mode":
        return "safe_mode"
    return mode or "unknown"


class ThermalIntelligence:
    """One bounded chain for actual observations and empirical service requests."""

    def __init__(self, *, pilot_only: bool = False) -> None:
        self.pilot_only = pilot_only
        self._pilot_key = None
        self._pilot_intervals = deque(maxlen=_PILOT_HISTORY_HOURS)
        self._pilot_inputs = {}
        self._pilot_anchor = None
        self._pilot_window = None
        self._pilot_input_epoch = 0
        self._pilot_fit = {}
        self._pilot_attempted_end = None
        self.model = RCThermalModel(ModelConfig(
            step_minutes=15.0, half_life_days=21.0,
            max_dead_time_minutes=180.0, max_gap_minutes=_MAX_GAP_MINUTES,
            max_step_delta_k=1.5, history_days=30.0,
            outdoor_max_gap_minutes=180.0,
        ))
        self._events: deque[dict[str, Any]] = deque(maxlen=_MAX_RECORDS)
        self._sequence = 0
        self._counters = dict.fromkeys(_COUNTER_KEYS, 0)
        self._counter_revision = 0
        self._counter_started_at: datetime | None = None
        self._source_signature: tuple = ()
        self._source_values: dict[str, tuple[float, datetime | None]] = {}
        self._captured_values: dict[str, tuple] = {}
        self._last_numeric_values: dict[str, float] = {}
        self._observation_values: dict[str, float] = {}
        self._latest_observation: dict[str, Any] | None = None
        self._context_signature: tuple = ()
        self._last_command: dict[str, Any] | None = None
        self._last_command_sample_at: datetime | None = None
        self._active_cycle: dict[str, Any] | None = None
        self._last_output_signature: dict[str, tuple] = {}
        self._last_interruption: str | None = None
        self._last_interruption_event: dict[str, Any] | None = None
        self._status = "collecting"
        self._samples = 0
        self._last_training_at: datetime | None = None
        self._fit_generation = 0
        self._fit_in_progress = False
        self._fit_summary: dict[str, Any] = {}
        self._segment = 0
        self._outdoor_sample_segment: int | None = None
        self._last_cycle_at: datetime | None = None

    @property
    def _observations(self) -> list[dict[str, Any]]:
        return [row for row in self._events if row["kind"] == "observation"]

    @property
    def _outputs(self) -> list[dict[str, Any]]:
        return [row for row in self._events if row["kind"] == "output"]

    @property
    def _interruptions(self) -> list[dict[str, Any]]:
        return [row for row in self._events if row["kind"] == "interruption"]

    @property
    def latest_observation(self) -> dict[str, Any] | None:
        return dict(self._latest_observation) if self._latest_observation else None

    @property
    def context_id(self) -> int:
        return self._segment

    def pilot_response_intervals(self, reports, *, through, output_clamp):
        """Replay qualified source reports against this chain's control boundaries."""
        return PilotPassive.response_intervals(
            self._events, reports, through=through, output_clamp=output_clamp
        )

    @property
    def pilot_context_id(self) -> str:
        return f"{self.context_id}:{self._pilot_input_epoch}"

    def record_pilot_input(self, at, role, source, report) -> None:
        """Record an independent report or loss of input in the common chain."""
        at = _timestamp(at)
        if not self.pilot_only or self._active_cycle is None or at is None or not source:
            return
        source_key = {"heat": "pilot_heat_source", "outdoor": "outdoor_source"}.get(role)
        if source_key is None or source != self._active_cycle.get(source_key):
            return
        age_limit = _PILOT_HEAT_MAX_AGE if role == "heat" else _OUTDOOR_MAX_AGE
        reported, value = report if report is not None else (None, None)
        reported, value = _timestamp(reported), _number(value)
        if (reported is None or not timedelta(0) <= at - reported < age_limit
                or value is None or (role == "heat" and value not in (0, 1))):
            reported, value = None, None
        prior = self._pilot_inputs.get(role)
        if prior is not None:
            previous_report = _timestamp(prior["reported_at"])
            if reported == previous_report and value == prior["value"] and source == prior["entity"]:
                return
            change_at = reported if reported is not None and value != prior["value"] else at
            late = (change_at < at or (self._events and at < _timestamp(self._events[-1]["at"])))
            if late and self._pilot_intervals and self._pilot_intervals[-1].end > change_at.timestamp():
                # An actual earlier edge invalidates intervals that used the old
                # held value. The original reports remain in the evidence chain.
                while self._pilot_intervals and self._pilot_intervals[-1].end > change_at.timestamp():
                    self._pilot_intervals.pop()
                self._pilot_fit = {}
                self._pilot_attempted_end = None
                self._fit_generation += 1
                self._fit_in_progress = False
            if (reported is None or prior["entity"] != source or not prior["qualified"]
                    or _timestamp(prior["valid_until"]) < at or late
                    or (previous_report is not None and reported <= previous_report)):
                self._pilot_input_epoch += 1
                self._pilot_anchor = self._pilot_window = None
                if previous_report is not None and reported is not None and reported <= previous_report:
                    reported, value = None, None
        if reported is None and prior is None:
            return
        fields = {}
        if role == "heat":
            fields = {"semantics": "space_heat_duty", "operation": {
                "learning_qualified": reported is not None,
                "mode": "space_heating" if value == 1 else "idle" if value == 0 else "unknown",
            }}
        self._pilot_inputs[role] = self._append(
            "temperature", at, role=f"pilot_{role}", pilot_role=role, entity=source,
            value=value, reported_at=reported.isoformat() if reported is not None else None,
            valid_until=(reported + age_limit).isoformat() if reported is not None else None,
            qualified=reported is not None, physical_time_known=False, **fields,
        )

    def pilot_inputs_valid(self, now) -> bool:
        cycle = self._active_cycle or {}
        return (cycle.get("regime") in {"normal_pi", "braking_stable", "preheating"}
                and cycle.get("gos_active") is False and bool(cycle.get("pilot_heat_source"))
                and self._pilot_inputs.keys() >= {"outdoor", "heat"}
                and all(row["qualified"] and _timestamp(row["at"]) <= now < _timestamp(row["valid_until"])
                        for row in self._pilot_inputs.values()))

    def collect_pilot_house(self, now):
        """Close at a distinct observed indoor endpoint, using only prior evidence."""
        if not self.pilot_only or not self.pilot_inputs_valid(now):
            return None
        observation = self.latest_observation
        if observation is None or observation["segment"] != self.context_id:
            return None
        at = _timestamp(observation["indoor_reported_at"])
        if at is None or not timedelta(0) <= now - at <= timedelta(minutes=5):
            return None
        end = (at, observation["indoor_c"])
        start = self._pilot_anchor
        if start is not None and at - start[0] < timedelta(hours=1):
            return None
        self._pilot_anchor = end
        self._pilot_window = None
        if start is None:
            return None
        inputs = [dict(row, role=row["pilot_role"], source=row["entity"]) for row in self._events
                  if row["segment"] == self.context_id and "pilot_role" in row]
        if not inputs:
            return None
        # Segment identity establishes context even when its original event
        # has rotated out. A retained report before the start is still needed.
        context = {"kind": "context", "at": inputs[0]["at"], "gos_active": False,
                   "regime": self._active_cycle["regime"]}
        interval = PilotPassive.house_interval([context], inputs, start=start, end=end)
        if interval is None:
            return None
        self._pilot_window = interval
        self._pilot_intervals.append(interval)
        return interval

    def pilot_forecast(self, now):
        """Freeze recent observed heat share as a scenario for natural Base drift."""
        candidate = self._pilot_fit.get("candidate")
        window = self._pilot_window
        observation = self._latest_observation
        if (candidate is None or window is None or observation is None
                or not self.pilot_inputs_valid(now)
                or window.end != _timestamp(observation["indoor_reported_at"]).timestamp()
                or self._pilot_fit["fit_end"] > window.end):
            return None
        return dict(candidate, heat=window.heat, fit_end=self._pilot_fit["fit_end"])

    @property
    def current_command(self) -> dict[str, Any] | None:
        return dict(self._last_command) if self._last_command else None

    @property
    def status(self) -> str:
        if self.pilot_only:
            return "candidate_unvalidated" if self._pilot_fit.get("candidate") else self._status
        if self._status == "candidate_insufficient_excitation":
            return self._status
        return "candidate_unvalidated" if self.model.parameters() else self._status

    @property
    def store_signature(self) -> tuple:
        return (self._sequence, self._fit_generation, self._status, self._samples, self._counter_revision)

    @property
    def has_evidence(self) -> bool:
        return bool(self._events)

    def _count(self, key: str, at: datetime | None = None) -> None:
        self._counters[key] = min(_COUNTER_LIMIT, self._counters[key] + 1)
        self._counter_revision += 1
        if self._counter_started_at is None and at is not None:
            self._counter_started_at = at

    def _append(self, kind: str, at: datetime, **values: Any) -> dict[str, Any]:
        # Wall clock may move backwards; chain order follows capture order, while
        # source timestamps retain their independent, validated provenance.
        previous = _timestamp(self._events[-1]["at"]) if self._events else None
        captured = max(at, previous) if previous else at
        self._sequence += 1
        row = dict(values, kind=kind, at=captured.isoformat(),
                   sequence=self._sequence, segment=self._segment)
        if captured != at:
            row["reported_capture_at"] = at.isoformat()
        self._events.append(row)
        cutoff = captured - timedelta(days=self.model.config.history_days)
        while self._events and _timestamp(self._events[0]["at"]) < cutoff:
            self._events.popleft()
        if kind == "observation":
            self._count("aggregate_observations", at)
            self._latest_observation = row
        elif kind == "interruption":
            self._last_interruption_event = row
        return row

    def fit_due(self, now: datetime) -> bool:
        """True when a refit is worthwhile and no fit is currently in progress."""
        if self._fit_in_progress:
            return False
        if self.pilot_only:
            return (self._pilot_key is not None and bool(self._pilot_intervals)
                    and self._pilot_intervals[-1].end != self._pilot_attempted_end
                    and self._pilot_intervals[-1].end <= now.timestamp()
                    and sum(row.hours for row in self._pilot_intervals
                            if row.end > now.timestamp() - _PILOT_HISTORY_HOURS * 3600) >= 24)
        return self.model.fit_due(now)

    def prepare_fit_job(self, now: datetime) -> tuple[int, dict[str, Any]] | None:
        """Prepare an isolated immutable snapshot for executor fitting."""
        if not self.fit_due(now):
            return None
        self._fit_generation += 1
        self._count("fit_attempts", now)
        if self.pilot_only:
            self._fit_in_progress = True
            self._pilot_attempted_end = self._pilot_intervals[-1].end
            cutoff = now.timestamp() - _PILOT_HISTORY_HOURS * 3600
            return self._fit_generation, {"pilot_house": tuple(row for row in self._pilot_intervals if row.end > cutoff)}
        if hasattr(self.model, "record_fit_attempt"):
            self.model.record_fit_attempt(self.model._grid, now.timestamp())
        self._fit_in_progress = True
        token = self._fit_generation
        snapshot_data = {
            "grid": self.model._grid.snapshot(),
            "config": self.model.config,
            "now_ts": now.timestamp(),
            "group_prior": self.model._group_prior,
        }
        return token, snapshot_data

    @staticmethod
    def static_fit(snapshot_data: dict[str, Any]) -> dict[str, Any]:
        """Pure CPU-bound fitting function safe for asyncio executor execution."""
        if "pilot_house" in snapshot_data:
            rows = snapshot_data["pilot_house"]
            model = AdaptiveThermalModel()
            for row in rows:
                model.add_interval(row)
            result = {"status": model.status, "fit_end": rows[-1].end}
            if model.gain_identified and model.status == "learning":
                result["candidate"] = {"loss": model.heat_loss_coeff, "gain": model.heat_gain_coeff,
                                       "background": model.background_gain}
            return result
        return RCThermalModel.fit_snapshot(
            snapshot_data["grid"],
            snapshot_data["config"],
            snapshot_data["now_ts"],
            snapshot_data.get("group_prior"),
        )

    def apply_fit_result(self, token: int, result: dict[str, Any]) -> bool:
        """Apply fitted results if still current. Discards results from obsolete generations."""
        if token != self._fit_generation:
            self._count("fit_results_stale")
            return False
        self._fit_in_progress = False
        self._count("fit_results_applied")
        if self.pilot_only:
            self._pilot_fit = result
            return True
        self._fit_summary = result
        status = result.get("status")
        if result.get("fit_ts") is not None:
            self.model._last_fit_ts = result["fit_ts"]
        if result.get("data_end_ts") is not None:
            self.model._data_end_at_fit = result["data_end_ts"]
        self.model._summary = result

        if status == "candidate_insufficient_excitation":
            self._status = "candidate_insufficient_excitation"
            self.model._params = None
            self.model._quality = None
            return True
        if status == "ok" and result.get("params"):
            self.model._params = result["params"]
            self.model._quality = result.get("quality")
            self.model._outdoor_ref = result.get("outdoor_ref")
            self._status = "candidate_learning"
            return True
        self._status = status or "candidate_fit_failed"
        self.model._params = None
        self.model._quality = None
        return False

    def fit_sync(self, now: datetime) -> dict[str, Any]:
        """Synchronous fitting for tests and offline harnesses."""
        job = self.prepare_fit_job(now)
        if job is None:
            return {"status": "fit_not_due"}
        token, data = job
        result = self.static_fit(data)
        self.apply_fit_result(token, result)
        return result

    def _boundary(self, at: datetime) -> None:
        self._segment += 1
        self._pilot_anchor = self._pilot_window = None
        self._pilot_inputs.clear()
        if not self.pilot_only:
            if hasattr(self.model, "break_segment"):
                self.model.break_segment(at)
            else:
                self.model._pending.clear()
                self.model.add_sample(Sample(at, None, None, None, excluded=True))
            self._fit_generation += 1
            self._fit_in_progress = False

    def begin_cycle(self, cycle: dict[str, Any], *, use_ohmigo_input: bool) -> None:
        """Capture source changes even without a usable model or output transport."""
        self._active_cycle = None
        at = _timestamp(cycle.get("at"))
        if at is None:
            return
        if self._last_cycle_at is not None and not timedelta(0) <= at - self._last_cycle_at <= timedelta(minutes=_MAX_GAP_MINUTES):
            self.suspend("capture_gap_or_clock_rollback", at)
        self._last_cycle_at = at
        sources = cycle.get("sources")
        sources = [row for row in sources if isinstance(row, dict)] if isinstance(sources, list) else []
        signature = (tuple(sorted((row["entity"], _number(row.get("weight")))
                     for row in sources if isinstance(row.get("entity"), str))),
                     cycle.get("outdoor_source"), cycle.get("command_source"))
        regime = classify_regime(cycle.get("mode"), _number(cycle.get("brake_factor")))
        heat_source = cycle.get("pilot_heat_source") if self.pilot_only else None
        context = (signature, cycle.get("mode"), regime, cycle.get("gos_active"), use_ohmigo_input, heat_source)
        if context != self._context_signature:
            if self._context_signature:
                self._last_interruption = None
                self._interrupt("context_changed", at)
                self._last_command = None
            self._context_signature = context
            self._append("context", at, mode=cycle.get("mode"), regime=regime,
                         sources=[list(row) for row in signature[0]],
                         outdoor_source=signature[1], command_source=signature[2],
                         gos_active=cycle.get("gos_active"),
                         use_ohmigo_input=use_ohmigo_input)
        if self._source_signature and signature != self._source_signature:
            self._source_values.clear()
            self._captured_values.clear()
            self._last_numeric_values.clear()
            self._observation_values.clear()
            if not self.pilot_only:
                self.model = RCThermalModel(self.model.config)
                self._fit_summary.clear()
            self._last_training_at = None
        self._source_signature = signature
        if heat_source:
            key = repr((signature[:2], heat_source))
            if self._pilot_key != key:
                self._pilot_key = key
                self._pilot_intervals.clear()
                self._pilot_fit = {}
                self._pilot_attempted_end = None
                self._fit_generation += 1
                self._fit_in_progress = False
        eligible = regime in {"normal_pi", "braking_stable", "preheating"} and not cycle.get("gos_active")
        indoor = _number(cycle.get("indoor"))
        outdoor = _number(cycle.get("outdoor"))
        source_rows = []
        changed = False
        for raw in sources + [{"entity": cycle.get("outdoor_source") or "outdoor",
                               "value": outdoor,
                               "reported_at": cycle.get("outdoor_reported_at"),
                               "physical_time_known": cycle.get("outdoor_physical_time_known", False),
                               "role": "outdoor"}]:
            entity = raw.get("entity")
            if not isinstance(entity, str):
                continue
            role = raw.get("role", "indoor")
            key = role + ":" + entity
            value = _number(raw.get("value"))
            reported = _timestamp(raw.get("reported_at"))
            previous = self._source_values.get(key)
            age_limit = _OUTDOOR_MAX_AGE if role == "outdoor" else timedelta(minutes=_MAX_GAP_MINUTES)
            valid_time = reported is not None and timedelta(0) <= at - reported <= age_limit
            advanced = value is not None and (previous is None or value != previous[0])
            ordered = previous is None or previous[1] is None or (reported is not None and (reported > previous[1] or (reported == previous[1] and not advanced)))
            accepted = advanced and ordered
            row = {"entity": entity, "weight": _number(raw.get("weight")), "value": value,
                       "reported_at": reported.isoformat() if reported else None,
                       "physical_time_known": raw.get("physical_time_known") is True and valid_time,
                       "timestamp_valid": valid_time and ordered, "value_advanced": accepted,
                       "physical_time_status": "known" if raw.get("physical_time_known") is True and valid_time else "physical_time_unknown"}
            captured = (value, valid_time, ordered)
            numeric_changed = value is not None and self._last_numeric_values.get(key) != value
            captured_change = advanced and self._captured_values.get(key) != captured
            if value is not None:
                self._last_numeric_values[key] = value
            if numeric_changed:
                self._count("source_numeric_changes", at)
            elif not advanced and value is not None and valid_time and ordered:
                self._count("identical_source_rechecks", at)
            if numeric_changed or captured_change:
                self._count("source_change_attempts", at)
                if valid_time and ordered:
                    self._count("source_changes_accepted", at)
                    self._count("accepted_outdoor_changes" if role == "outdoor" else "accepted_indoor_changes", at)
                    if not row["physical_time_known"]:
                        self._count("accepted_changes_physical_time_unknown", at)
                else:
                    self._count("source_changes_rejected", at)
                    if reported is None:
                        rejection = "reject_report_time_missing"
                    elif reported > at:
                        rejection = "reject_report_time_future"
                    elif at - reported > age_limit:
                        rejection = "reject_report_time_stale"
                    else:
                        rejection = "reject_report_out_of_order"
                    self._count(rejection, at)
            if captured_change:
                self._captured_values[key] = captured
                self._append("temperature", at, role=role, **row,
                             quality="valid_report_time" if valid_time and ordered else "invalid_or_unknown_report_time")
            # Invalid times never consume an otherwise usable future observation.
            if accepted and valid_time:
                self._source_values[key] = (value, reported)
            if not self.pilot_only and role == "outdoor" and eligible and value is not None and valid_time and ordered:
                if self._outdoor_sample_segment != self._segment:
                    # Restore held context after a boundary, without asserting a
                    # new weather observation or extending its source age limit.
                    self.model.add_sample(Sample(at, None, value, None))
                    self._outdoor_sample_segment = self._segment
                elif accepted:
                    self.model.add_sample(Sample(reported, None, value, None))
            if role == "indoor":
                source_rows.append(row)
                changed |= valid_time and ordered and value is not None and self._observation_values.get(entity) != value
        self._active_cycle = dict(cycle, regime=regime, use_ohmigo_input=use_ohmigo_input)
        if indoor is None or outdoor is None or not source_rows or any(
            row["value"] is None or not row["timestamp_valid"] for row in source_rows
        ):
            self._interrupt("required_temperature_missing_or_stale", at)
            self._status = "waiting_for_temperatures"
            return
        changed_times = [self._source_values["indoor:" + row["entity"]][1]
                         for row in source_rows
                         if self._observation_values.get(row["entity"]) != row["value"]
                         and "indoor:" + row["entity"] in self._source_values]
        indoor_at = max(changed_times) if changed_times else _timestamp(cycle.get("indoor_reported_at"))
        outdoor_at = _timestamp(cycle.get("outdoor_reported_at"))
        if indoor_at is None or not timedelta(0) <= at - indoor_at <= timedelta(minutes=_MAX_GAP_MINUTES):
            self._interrupt("indoor_report_time_unknown_or_stale", at)
            return
        if outdoor_at is None or not timedelta(0) <= at - outdoor_at <= _OUTDOOR_MAX_AGE:
            self._interrupt("outdoor_observation_stale", at)
            return
        if not changed:
            self._status = "waiting_for_new_temperature_value"
            return
        command = self._last_command if use_ohmigo_input else None
        command_value = _number(command.get("requested")) if command else None
        command_at = _timestamp(command.get("established_at")) if command else None
        coordinate = None
        if command_value is not None and command_at is not None and command_at <= indoor_at:
            coordinate = _number(command.get("coordinate"))
        if not self.pilot_only and eligible and (self._last_training_at is None or indoor_at > self._last_training_at):
            # Independent command/outdoor samples establish held fields in the
            # grid; an indoor report never backdates a newly received command.
            self.model.add_sample(Sample(indoor_at, indoor, None, None))
            self._last_training_at = indoor_at
            if coordinate is not None:
                self._samples += 1
                self._status = "candidate_learning"
        self._observation_values.update({row["entity"]: row["value"] for row in source_rows})
        self._append("observation", at, indoor_reported_at=indoor_at.isoformat(),
                     indoor_c=indoor, outdoor_reported_at=outdoor_at.isoformat(),
                     outdoor_c=outdoor, sources=source_rows, mode=cycle.get("mode"),
                     regime=regime, command_coordinate=coordinate,
                     calculated_virtual_outdoor_c=_number(cycle.get("requested_virtual_outdoor")),
                     dispatched_virtual_outdoor_c=command_value,
                     requested_virtual_outdoor_c=_number(cycle.get("requested_virtual_outdoor")),
                     command_quantization_error_c=(round(command_value - _number(cycle.get("requested_virtual_outdoor")), 4) if command_value is not None and _number(cycle.get("requested_virtual_outdoor")) is not None else None),
                     command_at=command_at.isoformat() if command_at else None,
                     command_status=command.get("status") if command else None,
                     data_quality="empirical_command_unverified" if coordinate is not None else "command_unknown")
        self._last_interruption = None

    def record_output(self, event: dict[str, Any]) -> None:
        """Capture meaningful dispatch changes independently of indoor changes."""
        row = dict(event)
        at = _timestamp(row.pop("at", None))
        if at is None:
            return
        row["requested"] = _number(row.get("requested"))
        transport = str(row.get("transport"))
        status = row.get("status")
        if status in {"not_due", "dispatch_attempted"}:
            return
        signature = (status, row["requested"], row.get("source"), row.get("reason"), self._segment)
        repeated = self._last_output_signature.get(transport) == signature
        self._last_output_signature[transport] = signature
        previous = self._last_command
        success = transport == "ohmigo" and status == "service_completed_unverified" and row["requested"] is not None
        if success:
            same = previous is not None and previous["requested"] == row["requested"]
            self._count("command_confirmations" if same else "command_establishments", at)
            if previous is not None and not same:
                self._count("command_value_changes", at)
            established = previous["established_at"] if same else at.isoformat()
            self._last_command = {"coordinate": previous.get("coordinate") if same else None, "requested": row["requested"], "established_at": established,
                                      "at": established, "last_dispatched_at": at.isoformat(), "status": status}
            if self._active_cycle:
                cycle = self._active_cycle
                outdoor = _number(cycle.get("outdoor"))
                outdoor_at = _timestamp(cycle.get("outdoor_reported_at"))
                if outdoor is not None and outdoor_at is not None and timedelta(0) <= at - outdoor_at <= _OUTDOOR_MAX_AGE and cycle.get("use_ohmigo_input") and cycle["regime"] in {"normal_pi", "braking_stable", "preheating"} and not cycle.get("gos_active"):
                    coordinate = _number(self._last_command.get("coordinate"))
                    if coordinate is None:
                        coordinate = command_coordinate(outdoor, row["requested"], _number(cycle.get("command_span")) or 12.0)
                    self._last_command["coordinate"] = coordinate
                    # Confirmations refresh coverage at most once per grid step;
                    # repeated calls cannot create a dense pending-sample log.
                    if not self.pilot_only and (not same or self._last_command_sample_at is None or at - self._last_command_sample_at >= timedelta(minutes=self.model.config.step_minutes)):
                        self.model.add_sample(Sample(at, None, None, coordinate))
                        self._last_command_sample_at = at
        elif transport == "ohmigo" and status in {"dispatch_failed", "skipped", "disabled", "not_configured", "invalid_configuration"}:
            if status == "dispatch_failed":
                self._count("dispatch_failures", at)
            self._last_command = None
            self._interrupt("ohmigo_" + status, at)
        if not repeated:
            self._append("output", at, **row, physical_application_verified=False)

    @staticmethod
    def _predict(
        indoor: float,
        outdoor: float,
        coordinate: float,
        params: Any,
        duration_hours: float = 1.0,
    ) -> float | None:
        """Forecast a frozen, constant command/outdoor scenario for elapsed duration."""
        if not math.isfinite(duration_hours) or duration_hours < 0:
            return None
        if duration_hours == 0:
            return indoor
        values = (
            params.heating_rate_k_per_h,
            params.loss_rate_per_h,
            params.gains_k_per_h,
        )
        if not all(math.isfinite(value) for value in values):
            return None
        a = params.heating_rate_k_per_h
        b = params.loss_rate_per_h
        c = params.gains_k_per_h
        step_h = 0.25
        temp = indoor
        effort = coordinate
        remaining = duration_hours
        while remaining > 1e-6:
            dt = min(step_h, remaining)
            temp = step_temperature(
                temp,
                effort,
                outdoor,
                a,
                b,
                c,
                dt,
            )
            if not math.isfinite(temp):
                return None
            remaining -= dt
        return temp

    def suspend(self, reason: str, at: datetime) -> None:
        self._interrupt(reason, at)
        self._last_command = None
        self._active_cycle = None
        self._context_signature = ()

    def _interrupt(self, reason: str, at: datetime | None = None) -> None:
        at = at or datetime.now(UTC)
        # A boundary retains completed evidence, never held command continuity.
        self._last_command = None
        self._active_cycle = None
        if reason != self._last_interruption:
            self._count("interruptions", at)
            if reason == "restart":
                self._count("restarts", at)
            self._boundary(at)
            self._append("interruption", at, reason=reason)
        self._last_interruption = reason

    def export_state(self) -> dict[str, Any]:
        """Persist one chain; unfinished command and interpolation never resume."""
        model_state = self.model.to_dict()
        model_state["pending"] = []  # Open interpolation intervals never resume.
        state = {"version": _STATE_VERSION, "model": model_state,
                    "events": list(self._events), "sequence": self._sequence,
                    "samples": self._samples, "status": self._status,
                    "fit_summary": dict(self._fit_summary), "fit_generation": self._fit_generation,
                    "segment": self._segment, "diagnostic_counters": dict(self._counters),
                    "counter_started_at": self._counter_started_at.isoformat() if self._counter_started_at else None}
        if self._pilot_key is not None:
            state["pilot_house"] = {"key": self._pilot_key,
                                    "intervals": [asdict(row) for row in self._pilot_intervals]}
        return deepcopy(state)

    def restore_state(self, data: Any) -> bool:
        restored = ThermalIntelligence(pilot_only=self.pilot_only)
        if not restored._restore_state(data):
            return False
        self.__dict__.update(restored.__dict__)
        return True

    def _restore_state(self, data: Any) -> bool:
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
            return False
        try:
            counters = data.get("diagnostic_counters", {})
            if not isinstance(counters, dict) or any(
                key not in _COUNTER_KEYS or isinstance(value, bool)
                or not isinstance(value, int) or not 0 <= value <= _COUNTER_LIMIT
                for key, value in counters.items()
            ):
                return False
            self._counters.update(counters)
            self._counter_started_at = _timestamp(data.get("counter_started_at"))
            if data.get("counter_started_at") is not None and self._counter_started_at is None:
                return False
            events = deepcopy(data["events"])
            if any(key in data for key in ("scores", "observations", "outputs", "interruptions")):
                return False
            if not isinstance(events, list) or any(not isinstance(row, dict) or _timestamp(row.get("at")) is None or row.get("kind") not in {"temperature", "context", "output", "observation", "interruption"} for row in events):
                return False
            if any(_timestamp(a["at"]) > _timestamp(b["at"]) or int(a["sequence"]) >= int(b["sequence"]) for a, b in itertools.pairwise(events)):
                return False
            model = RCThermalModel.from_dict(data["model"], self.model.config)
            model._pending.clear()
            self.model = model
            cutoff = _timestamp(events[-1]["at"]) - timedelta(days=model.config.history_days) if events else None
            events = [row for row in events if cutoff is None or _timestamp(row["at"]) >= cutoff]
            self._events = deque(events[-_MAX_RECORDS:], maxlen=_MAX_RECORDS)
            self._sequence = max(int(data.get("sequence", 0)), int(events[-1]["sequence"]) if events else 0)
            self._samples = max(0, int(data.get("samples", 0)))
            self._fit_summary = dict(data.get("fit_summary", {}))
            self._fit_generation = int(data.get("fit_generation", 0))
            self._fit_in_progress = False
            self._segment = int(data.get("segment", 0))
            self._status = "collecting"
            self._last_command = None
            self._source_values.clear()
            self._captured_values.clear()
            self._last_numeric_values.clear()
            self._observation_values.clear()
            self._latest_observation = None
            # Reconstruct source values only to suppress identical republishes.
            for row in self._events:
                if row["kind"] == "temperature" and _number(row.get("value")) is not None:
                    self._last_numeric_values[row["role"] + ":" + row["entity"]] = _number(row["value"])
                if row["kind"] == "temperature" and row.get("timestamp_valid"):
                    value = _number(row.get("value"))
                    if value is not None:
                        self._source_values[row["role"] + ":" + row["entity"]] = (value, _timestamp(row.get("reported_at")))
                        self._captured_values[row["role"] + ":" + row["entity"]] = (value, True, True)
            for row in self._events:
                if row["kind"] == "interruption":
                    self._last_interruption_event = row
                if row["kind"] == "observation":
                    self._latest_observation = row
                    self._observation_values = {source["entity"]: source["value"] for source in row.get("sources", [])}
            self._context_signature = ()
            self._source_signature = ()
            for row in reversed(self._events):
                if row["kind"] == "context":
                    self._source_signature = (tuple((source[0], _number(source[1])) for source in row.get("sources", [])), row.get("outdoor_source"), row.get("command_source"))
                    break
            self._active_cycle = None
            self._last_output_signature.clear()
            self._last_training_at = None
            self._last_interruption = None
            if events:
                self._restore_pilot_house(data.get("pilot_house"), _timestamp(events[-1]["at"]))
                self._interrupt("restart", _timestamp(events[-1]["at"]))
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        return True

    def _restore_pilot_house(self, payload, through) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("key"), str):
            return
        values = payload.get("intervals")
        if not isinstance(values, list) or len(values) > _PILOT_HISTORY_HOURS:
            return
        previous = 0.0
        rows = []
        for item in values:
            try:
                row = LearningInterval(**item)
                numeric = (row.end, row.hours, row.start_temp, row.end_temp, row.outdoor, row.heat, row.coverage)
                if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in numeric)
                        or not 1 <= row.hours <= 3 or not 0.95 <= row.coverage <= 1
                        or not 0 <= row.heat <= 1 or not -50 <= row.outdoor <= 60
                        or not -10 < row.start_temp < 50 or not -10 < row.end_temp < 50
                        or row.request is not None or row.end > through.timestamp()
                        or row.end - row.hours * 3600 < previous - 1e-6):
                    return
            except (TypeError, ValueError, OverflowError):
                return
            rows.append(row)
            previous = row.end
        self._pilot_key = payload["key"]
        self._pilot_intervals.extend(rows)

    def summary(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "model_status": "candidate_unvalidated" if self.model.parameters() else "insufficient_evidence",
            "model": "adaptive_preheat_rtm_empirical_command_response",
            "samples": self._samples,
            "evidence_events": len(self._events),
            "evidence_diagnostics": {
                "scope": "since_counter_start_not_retained_event_window",
                "started_at": self._counter_started_at.isoformat() if self._counter_started_at else None,
                "source_rejection_denominator": "numeric_change_validation_attempts_including_quality_retries",
                "accepted_source_semantics": "valid_numeric_report_provenance_not_model_eligibility",
                "source_rejection_fraction": (
                    self._counters["source_changes_rejected"] / self._counters["source_change_attempts"]
                    if self._counters["source_change_attempts"] else None
                ),
                "counts": dict(self._counters),
            },
            "fit_in_progress": self._fit_in_progress,
            "last_fit_status": self._fit_summary.get("status"),
            "last_fit_attempt_epoch": _number(self.model._last_fit_ts),
            "oldest_evidence_at": self._events[0]["at"] if self._events else None,
            "latest_evidence_at": self._events[-1]["at"] if self._events else None,
            "forecast_endpoint_basis": "accepted_numeric_changes_only",
            "fit_parameters_available": self.model.parameters() is not None,
            "fit_trusted": False,
            "excitation_sufficient": self._fit_summary.get("excitation_sufficient"),
            "command_range": self._fit_summary.get("command_range"),
            "command_variance": self._fit_summary.get("command_variance"),
            "command_semantics": "service_request_unverified_not_thermal_power",
            "forecast_audit": "shared_forecast_audit",
            "active_control": False,
            "last_interruption": self._last_interruption_event["reason"] if self._last_interruption_event else None,
            "reason": self._fit_summary.get("reason") or "passive_diagnostics_only",
        }
        if self.pilot_only:
            available = self._pilot_fit.get("candidate") is not None
            reason = "verified_space_heat_unavailable"
            if self._pilot_inputs.get("heat", {}).get("qualified"):
                reason = self._pilot_fit.get("status") or "waiting_for_house_intervals"
            result.update(
                model="heat_pump_pilot_house",
                model_status="candidate_unvalidated" if available else "insufficient_evidence",
                samples=len(self._pilot_intervals),
                pilot_house_intervals=len(self._pilot_intervals),
                pilot_house_hours=sum(row.hours for row in self._pilot_intervals),
                fit_parameters_available=available,
                last_fit_status=self._pilot_fit.get("status"),
                last_fit_attempt_epoch=self._pilot_attempted_end,
                excitation_sufficient=available,
                command_range=None,
                command_variance=None,
                forecast_heat_assumption="mean_heat_in_latest_observed_house_interval",
                reason=reason,
            )
        return result
