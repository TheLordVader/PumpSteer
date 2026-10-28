"""Causal audit of issued, conditional one-step temperature forecasts.

Inspired by Heat Pump Optimizer's forecast-versus-outcome accuracy records.
The issued forecast is a function of elapsed time, assuming unchanged measured
heat indicator and outside temperature. HA report time is not physical time.
"""

from datetime import timedelta
import math


def _plan_context(value):
    """Keep only bounded numeric inputs actually published with the plan."""
    if not isinstance(value, dict):
        return None
    keys = (
        "indoor_temperature",
        "outdoor_temperature",
        "target_temperature",
        "comfort_floor",
        "comfort_ceiling",
        "virtual_temperature_command",
    )
    if any(
        isinstance(value.get(key), bool)
        or not isinstance(value.get(key), (int, float))
        or not math.isfinite(value[key])
        or not -200 <= value[key] <= 200
        for key in keys
    ):
        return None
    result = {
        **{key: value[key] for key in keys},
        "physical_measurement_time": "unknown",
    }
    if "controller" not in value:
        return result
    state = value["controller"]
    if not isinstance(state, dict):
        return None
    numbers = (
        "pi_integral",
        "pi_last_error",
        "brake_ramp",
        "preheat_ramp",
        "previous_aggressiveness",
    )
    times = (
        "pi_last_time",
        "brake_last_time",
        "brake_last_expensive_time",
        "preheat_last_time",
    )
    flags = ("pi_precool_paused", "was_braking_last_cycle", "bridge_short_dip_active")
    if any(key not in state for key in (*numbers, *times, *flags)):
        return None
    if any(
        state[key] is not None
        and (
            isinstance(state[key], bool)
            or not isinstance(state[key], (int, float))
            or not math.isfinite(state[key])
            or abs(state[key]) > 1_000_000
        )
        for key in numbers
    ) or any(not isinstance(state[key], bool) for key in flags):
        return None
    from .pilot_forecast import _timestamp

    if any(state[key] is not None and _timestamp(state[key]) is None for key in times):
        return None
    result["controller"] = {
        **{key: state[key] for key in (*numbers, *flags)},
        **{
            key: _timestamp(state[key]).isoformat() if state[key] is not None else None
            for key in times
        },
    }
    return result


def _plan_identity(value):
    """Normalize the indoor contributors and selected outdoor source."""
    if not isinstance(value, (tuple, list)) or len(value) not in (2, 3):
        return None
    components, outdoor = value[:2]
    outdoor_entity = value[2] if len(value) == 3 else None
    if components is not None:
        if not isinstance(components, (tuple, list)) or not all(
            isinstance(item, str) and item for item in components
        ):
            return None
        components = tuple(sorted(components))
    if outdoor is not None and not isinstance(outdoor, str):
        return None
    if outdoor_entity is not None and not isinstance(outdoor_entity, str):
        return None
    return components, outdoor, outdoor_entity


def _plan_context_causal(value, start):
    from .pilot_forecast import _timestamp

    controller = (value or {}).get("controller", {})
    return all(
        controller.get(key) is None or _timestamp(controller[key]) <= start
        for key in (
            "pi_last_time",
            "brake_last_time",
            "brake_last_expensive_time",
            "preheat_last_time",
        )
    )


class ForecastAudit:
    def __init__(self):
        self.fixed_pending = None
        self.fixed_records = []
        self.fixed_next = None
        self.pending = None
        self.records = []
        # Passive plan residuals are separate from model-validation evidence.
        self.plan_pending = []
        self.plan_records = []
        self.plan_unverifiable = 0
        self.plan_last_reason = None
        self._plan_last_report = None

    def issue_fixed(self, at, indoor, outdoor, k, identity):
        """Freeze three conditional forecasts; no heat-production assumption.

        Common-outcome comparison follows EMHASS forecast_calibration (MIT,
        179cbc2b); horizon-specific evidence follows ThermoSmart ForecastModel
        (MIT, 76f5fe17). No external implementation/dependencies imported.
        """
        from .pilot_forecast import _timestamp

        at = _timestamp(at)
        if (
            at is None
            or self.fixed_pending
            or (self.fixed_next and at < self.fixed_next)
        ):
            return False
        if (
            not all(
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and math.isfinite(v)
                for v in (indoor, outdoor, k)
            )
            or not 0.005 < k < 0.5
        ):
            return False
        delta = max(0.0, indoor - outdoor)
        self.fixed_pending = {
            "issued": at,
            "identity": tuple(identity),
            "predictions": {
                h: [
                    indoor - k * delta * h / 60,
                    indoor + delta * math.expm1(-k * h / 60),
                    indoor,
                ]
                for h in (30, 60, 120)
            },
        }
        # Non-overlapping 120-minute episodes plus endpoint tolerance.
        self.fixed_next = at + timedelta(minutes=125)
        return True

    def observe_fixed(self, now, indoor, identity):
        from .pilot_forecast import _timestamp
        from .pilot_passive import PilotPassive

        now = _timestamp(now)
        pending = self.fixed_pending
        if pending is None:
            return
        if (
            now is None
            or now < pending["issued"]
            or tuple(identity) != pending["identity"]
        ):
            self.fixed_pending = None
            return
        reading = PilotPassive._report(indoor, now, temperature=True, max_age=None)
        for horizon in list(pending["predictions"]):
            due = pending["issued"] + timedelta(minutes=horizon)
            if reading and due <= reading[0] <= due + timedelta(minutes=5):
                self.fixed_records.append(
                    {
                        "issued": pending["issued"].isoformat(),
                        "observed": reading[0].isoformat(),
                        "horizon": horizon,
                        "identity": list(identity),
                        "predictions": pending["predictions"].pop(horizon),
                        "actual": reading[1],
                    }
                )
                self.fixed_records = self.fixed_records[-96:]
            elif now > due + timedelta(minutes=5):
                pending["predictions"].pop(horizon)
        if not pending["predictions"]:
            self.fixed_pending = None

    def fixed_summary(self, identity=None):
        """Diagnostic heuristics, never authorization for active control."""
        result = {}
        for horizon in (30, 60, 120):
            rows = [
                r
                for r in self.fixed_records
                if r["horizon"] == horizon
                and (identity is None or tuple(r["identity"]) == tuple(identity))
            ]
            # Common actual endpoints for all three methods; one row per episode.
            unique = {r["issued"]: r for r in rows}
            rows = list(unique.values())[-24:]
            mae = (
                [
                    sum(abs(r["predictions"][i] - r["actual"]) for r in rows)
                    / len(rows)
                    for i in range(3)
                ]
                if rows
                else None
            )
            status = "insufficient_evidence"
            skill = None
            if mae and mae[2] >= 0.02:
                skill = 1 - mae[1] / mae[2]
                if len(rows) >= 12 and len({r["issued"][:10] for r in rows}) >= 3:
                    halves = (rows[: len(rows) // 2], rows[len(rows) // 2 :])
                    improved = all(
                        sum(abs(r["predictions"][1] - r["actual"]) for r in part)
                        <= 0.9
                        * sum(abs(r["predictions"][2] - r["actual"]) for r in part)
                        for part in halves
                    )
                    status = (
                        "improved"
                        if skill >= 0.1 and improved
                        else "worse_than_persistence"
                        if skill < -0.1
                        else "insufficient_evidence"
                        if skill >= 0.1
                        else "comparable"
                    )
            result[str(horizon)] = {
                "count": len(rows),
                "model_order": ["linear", "bounded", "persistence"],
                "mae": mae,
                "skill": skill,
                "status": status,
                "passive_only": True,
                "assumption": "constant_outdoor_net_response_under_actual_control",
            }
        return result

    def issue(
        self,
        at,
        indoor,
        outdoor,
        heat,
        loss,
        gain,
        background,
        revision,
        *,
        source_identity=(None, None),
        source_reported=None,
    ):
        if self.pending is not None or not isinstance(revision, str):
            return False
        values = (indoor, outdoor, heat, loss, gain, background)
        if (
            at.tzinfo is None
            or any(
                not isinstance(x, (int, float))
                or isinstance(x, bool)
                or not math.isfinite(x)
                for x in values
            )
            or heat not in (0.0, 1.0)
        ):
            return False
        source_reported = at if source_reported is None else source_reported
        if source_reported.tzinfo is None or not timedelta(
            0
        ) <= at - source_reported <= timedelta(minutes=5):
            return False
        rate = loss * (outdoor - indoor) + gain * heat + background
        self.pending = {
            "issued": at,
            "source_reported": source_reported,
            "last": at,
            "indoor": indoor,
            "outdoor": outdoor,
            "heat": heat,
            "rate": rate,
            "forecast": indoor + rate * 0.25,
            "revision": revision,
            "source_identity": source_identity,
        }
        return True

    def observe(self, now, indoor, outdoor, heat):
        from .pilot_passive import PilotPassive

        pending = self.pending
        if pending is None:
            return False
        t_out = PilotPassive._report(outdoor, now, temperature=True)
        t_heat = PilotPassive._report(heat, now)
        t_in = PilotPassive._report(indoor, now, temperature=True, max_age=None)
        inside_attrs = getattr(indoor, "attributes", {}) or {}
        outside_attrs = getattr(outdoor, "attributes", {}) or {}
        components = inside_attrs.get("source_temperatures_used")
        components = (
            tuple(sorted(components))
            if isinstance(components, dict)
            and all(isinstance(k, str) for k in components)
            else None
        )
        selected = outside_attrs.get("selected_source")
        identity = (components, selected if isinstance(selected, str) else None)
        if (
            identity != pending["source_identity"]
            or now < pending["last"]
            or now - pending["last"] > timedelta(minutes=5)
            or t_out is None
            or t_heat is None
            or t_in is None
            or t_out[1] != pending["outdoor"]
            or t_heat[1] != pending["heat"]
            or any(
                getattr(s, "last_changed", None) is None
                or s.last_changed.tzinfo is None
                or s.last_changed > pending["issued"]
                for s in (outdoor, heat)
            )
        ):
            self.pending = None
            return False
        pending["last"] = now
        elapsed = (t_in[0] - pending["issued"]).total_seconds()
        if elapsed < 600:
            if now - pending["issued"] > timedelta(minutes=20):
                self.pending = None
            return False
        self.pending = None
        if elapsed > 1200:
            return False
        forecast = pending["indoor"] + pending["rate"] * elapsed / 3600
        self.records.append(
            {
                "revision": pending["revision"],
                "issued": pending["issued"].isoformat(),
                "source_reported": pending["source_reported"].isoformat(),
                "observed": t_in[0].isoformat(),
                "horizon_seconds": elapsed,
                "rate_per_hour": pending["rate"],
                "predicted": forecast,
                "actual": t_in[1],
                "initial": pending["indoor"],
                "error": forecast - t_in[1],
                "persistence_error": pending["indoor"] - t_in[1],
            }
        )
        self.records = self.records[-24:]
        return True

    def export_state(self):
        # Preserve issued evidence, but never resume its pending outcome after restart.
        pending = (
            None
            if self.pending is None
            else {
                key: (value.isoformat() if hasattr(value, "isoformat") else value)
                for key, value in self.pending.items()
            }
        )
        return {"records": list(self.records), "pending": pending}

    def restore(self, payload, now):
        from .pilot_forecast import _timestamp

        self.pending = None
        self.records = []
        rows = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 24:
            return
        candidates = {}
        conflicting = set()
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("revision"), str)
                or len(row["revision"]) > 80
            ):
                continue
            issued, observed, source = (
                _timestamp(row.get("issued")),
                _timestamp(row.get("observed")),
                _timestamp(row.get("source_reported")),
            )
            values = (
                row.get("predicted"),
                row.get("actual"),
                row.get("initial"),
                row.get("rate_per_hour"),
            )
            if (
                issued is None
                or observed is None
                or source is None
                or observed > now
                or not 600 <= (observed - issued).total_seconds() <= 1200
                or not 0 <= (issued - source).total_seconds() <= 300
            ):
                continue
            if any(
                not isinstance(x, (int, float))
                or isinstance(x, bool)
                or not math.isfinite(x)
                or abs(x) > 50
                for x in values
            ):
                continue
            item = dict(row)
            item["horizon_seconds"] = (observed - issued).total_seconds()
            if (
                abs(
                    item["predicted"]
                    - (
                        item["initial"]
                        + item["rate_per_hour"] * item["horizon_seconds"] / 3600
                    )
                )
                > 1e-6
            ):
                continue
            item["error"] = item["predicted"] - item["actual"]
            item["persistence_error"] = item["initial"] - item["actual"]
            # A live audit has only one pending forecast at a time. Repeated
            # copies are one piece of evidence; contradictory versions of
            # the same issuance must not be counted at all.
            signature = (row["revision"], observed, source, *values)
            if issued in conflicting:
                continue
            prior = candidates.get(issued)
            if prior is not None and prior[0] != signature:
                del candidates[issued]
                conflicting.add(issued)
            elif prior is None:
                candidates[issued] = (signature, observed, item)

        ordered = sorted(candidates.items())
        overlapping = set()
        for index, (issued, (_, observed, _)) in enumerate(ordered):
            for next_issued, _ in ordered[index + 1 :]:
                if next_issued >= observed:
                    break
                overlapping.update((issued, next_issued))
        self.records = [
            item for issued, (_, _, item) in ordered if issued not in overlapping
        ]

    def summary(self, revision):
        rows = [r for r in self.records if r["revision"] == revision][-8:]
        if not rows:
            return {"count": 0, "status": "insufficient_outcomes"}
        mae = sum(abs(r["error"]) for r in rows) / len(rows)
        baseline = sum(abs(r["persistence_error"]) for r in rows) / len(rows)
        bias = sum(r["error"] for r in rows) / len(rows)
        bad = len(rows) >= 5 and mae >= 0.02 and mae > baseline * 1.1
        return {
            "count": len(rows),
            "mae": round(mae, 3),
            "persistence_mae": round(baseline, 3),
            "bias": round(bias, 3),
            "horizon_min_seconds": min(r["horizon_seconds"] for r in rows),
            "horizon_max_seconds": max(r["horizon_seconds"] for r in rows),
            "status": "degraded"
            if bad
            else "insufficient_outcomes"
            if len(rows) < 5
            else "monitoring",
        }

    def issue_plan(
        self,
        *,
        forecast_start,
        issued_at,
        temperatures,
        revision,
        status,
        source_entity,
        source_identity,
        source_reported,
        initial_context=None,
    ):
        """Freeze issued grid points; the planned commands are never executed."""
        from .pilot_forecast import _timestamp

        start, issued, reported = map(
            _timestamp, (forecast_start, issued_at, source_reported)
        )
        if (
            start is None
            or issued is None
            or reported is None
            or not 0 <= (issued - start).total_seconds() <= 60
            or not 0 <= (issued - reported).total_seconds() <= 300
            or status not in {"validated_passive", "hypothetical_unvalidated"}
            or not isinstance(revision, str)
            or not revision
            or len(revision) > 80
            or not isinstance(source_entity, str)
            or not source_entity
            or _plan_identity(source_identity) is None
            or (initial_context is not None and _plan_context(initial_context) is None)
            or not isinstance(temperatures, (list, tuple))
            or not 3 <= len(temperatures) <= 49
            or any(
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                or not -50 <= v <= 60
                for v in temperatures
            )
        ):
            return False
        if (
            initial_context is not None
            and abs(initial_context["indoor_temperature"] - temperatures[0])
            > 0.005000001
        ):
            return False
        if not _plan_context_causal(initial_context, start):
            return False
        horizons = {
            h: temperatures[h // 15]
            for h in (30, 60, 120)
            if h // 15 < len(temperatures)
        }
        if not horizons or any(item["issued"] == issued for item in self.plan_pending):
            return False
        if len(self.plan_pending) >= 5:
            self._discard_plan(self.plan_pending.pop(0), "capacity")
        self.plan_pending.append(
            {
                "start": start,
                "issued": issued,
                "source_reported": reported,
                "source_entity": source_entity,
                "identity": _plan_identity(source_identity),
                "revision": revision,
                "status": status,
                "initial": temperatures[0],
                "initial_context": _plan_context(initial_context),
                "horizons": horizons,
                "last": issued,
            }
        )
        return True

    def _discard_plan(self, plan, reason):
        self.plan_unverifiable += len(plan["horizons"])
        self.plan_last_reason = reason

    def cancel_plans(self, reason):
        self.fixed_pending = None
        for plan in self.plan_pending:
            self._discard_plan(plan, reason)
        self.plan_pending = []

    def observe_plan(
        self, now, indoor, source_entity, source_identity, *, reported_after=None
    ):
        """Pair each issued forecast with a real report, which may be shared."""
        from .pilot_forecast import _timestamp
        from .pilot_passive import PilotPassive

        now = _timestamp(now)
        if now is None:
            self.cancel_plans("invalid_clock")
            return False
        report = PilotPassive._report(
            indoor, now, temperature=True, reported_after=reported_after, max_age=None
        )
        changed = _timestamp(getattr(indoor, "last_changed", None))
        if report is None or changed is None:
            self.cancel_plans("sensor_unavailable")
            return False
        at, value = _timestamp(report[0]), report[1]
        if at is None or changed > at:
            self.cancel_plans("invalid_report")
            return False
        if self._plan_last_report is not None and at < self._plan_last_report:
            self.cancel_plans("report_reordered")
            return False
        new = self._plan_last_report is None or at > self._plan_last_report
        self._plan_last_report = at
        for plan in list(self.plan_pending):
            if (
                plan["source_entity"] != source_entity
                or plan["identity"] != _plan_identity(source_identity)
                or now < plan["last"]
                or now - plan["last"] > timedelta(minutes=5)
            ):
                self._discard_plan(plan, "source_change_or_gap")
                self.plan_pending.remove(plan)
            else:
                plan["last"] = now
        paired = False
        for plan in list(self.plan_pending):
            for horizon, predicted in list(plan["horizons"].items()):
                target = plan["start"] + timedelta(minutes=horizon)
                offset = (at - target).total_seconds()
                if (
                    new
                    and at > plan["issued"]
                    and at > plan["source_reported"]
                    and abs(offset) <= 60
                    and now - at <= timedelta(minutes=5)
                ):
                    self.plan_records.append(
                        {
                            "forecast_start": plan["start"].isoformat(),
                            "issued_at": plan["issued"].isoformat(),
                            "source_reported": plan["source_reported"].isoformat(),
                            "forecast_at": target.isoformat(),
                            "observed": at.isoformat(),
                            "horizon_minutes": horizon,
                            "lead_seconds": (at - plan["issued"]).total_seconds(),
                            "time_error_seconds": offset,
                            "predicted": predicted,
                            "actual": value,
                            "initial": plan["initial"],
                            "initial_context": plan["initial_context"],
                            "error": predicted - value,
                            "persistence_error": plan["initial"] - value,
                            "revision": plan["revision"],
                            "status": plan["status"],
                            "source_entity": source_entity,
                            "source_identity": plan["identity"],
                            "kind": "unexecuted_scenario_residual",
                        }
                    )
                    self.plan_records = self.plan_records[-48:]
                    del plan["horizons"][horizon]
                    paired = True
                elif now > target + timedelta(seconds=60):
                    del plan["horizons"][horizon]
                    self.plan_unverifiable += 1
                    self.plan_last_reason = "no_distinct_report_near_forecast_time"
            if not plan["horizons"]:
                self.plan_pending.remove(plan)
        return paired

    def plan_summary(self, source_entity=None, source_identity=None):
        identity = _plan_identity(source_identity)
        records = self.plan_records
        pending = self.plan_pending
        if source_entity is not None:
            records = [
                row
                for row in records
                if identity is not None
                and row["source_entity"] == source_entity
                and row["source_identity"] == identity
            ]
            pending = [
                plan
                for plan in pending
                if identity is not None
                and plan["source_entity"] == source_entity
                and plan["identity"] == identity
            ]
        groups = {}
        for row in records:
            key = (
                row["revision"],
                row["status"],
                row["source_entity"],
                row["source_identity"],
                row["horizon_minutes"],
            )
            groups.setdefault(key, []).append(row)
        # Only the newest model/status is summarized to keep attributes compact.
        latest = records[-1] if records else None
        summaries = {}
        if latest is not None:
            for (revision, status, source, identity, horizon), rows in groups.items():
                if (revision, status, source, identity) != (
                    latest["revision"],
                    latest["status"],
                    latest["source_entity"],
                    latest["source_identity"],
                ):
                    continue
                summaries[str(horizon)] = {
                    "count": len(rows),
                    "mae": round(sum(abs(r["error"]) for r in rows) / len(rows), 3),
                    "bias": round(sum(r["error"] for r in rows) / len(rows), 3),
                    "persistence_mae": round(
                        sum(abs(r["persistence_error"]) for r in rows) / len(rows), 3
                    ),
                }
        return {
            "kind": "unexecuted_scenario_residual",
            "physical_measurement_time": "unknown",
            "interpretation": "plan_not_executed_not_model_validation_or_savings",
            "count_semantics": "forecast_outcome_pairs_not_independent_reports",
            "pending": sum(len(p["horizons"]) for p in pending),
            "unverifiable": self.plan_unverifiable,
            "last_reason": self.plan_last_reason,
            "model_revision": latest["revision"] if latest else None,
            "planning_status": latest["status"] if latest else None,
            "horizons_minutes": summaries,
            "latest_pair": {
                key: latest[key]
                for key in (
                    "forecast_start",
                    "issued_at",
                    "forecast_at",
                    "observed",
                    "horizon_minutes",
                    "lead_seconds",
                    "time_error_seconds",
                    "predicted",
                    "actual",
                    "initial",
                    "initial_context",
                )
            }
            if latest
            else None,
        }

    def export_plan(self):
        return {
            "fixed_v1": list(self.fixed_records),
            "records": list(self.plan_records),
            "pending_count": sum(len(p["horizons"]) for p in self.plan_pending),
            "unverifiable": self.plan_unverifiable,
            "last_reason": self.plan_last_reason,
        }

    def restore_plan(self, payload, now):
        """Restore completed pairs only; observation gaps cannot be bridged."""
        from .pilot_forecast import _timestamp

        self.fixed_pending = None
        self.fixed_records = []
        self.fixed_next = None
        self.plan_pending = []
        self.plan_records = []
        self._plan_last_report = None
        now = _timestamp(now)
        if not isinstance(payload, dict) or now is None:
            return
        fixed = payload.get("fixed_v1", [])
        if isinstance(fixed, list) and len(fixed) <= 96:
            seen = set()
            for row in fixed:
                if not isinstance(row, dict):
                    continue
                issued, observed = (
                    _timestamp(row.get("issued")),
                    _timestamp(row.get("observed")),
                )
                h = row.get("horizon")
                pred, actual = row.get("predictions"), row.get("actual")
                identity = row.get("identity")
                if (
                    issued is None
                    or observed is None
                    or h not in (30, 60, 120)
                    or not issued + timedelta(minutes=h)
                    <= observed
                    <= issued + timedelta(minutes=h + 5)
                    or observed > now
                    or not isinstance(pred, list)
                    or len(pred) != 3
                    or not isinstance(identity, list)
                    or len(identity) != 2
                    or not all(isinstance(x, str) and len(x) <= 2048 for x in identity)
                    or not all(
                        isinstance(x, (int, float))
                        and not isinstance(x, bool)
                        and math.isfinite(x)
                        and -100 <= x <= 100
                        for x in [*pred, actual]
                    )
                ):
                    continue
                key = (issued, h)
                if key in seen:
                    continue
                seen.add(key)
                self.fixed_records.append(dict(row))
            self.fixed_records.sort(key=lambda r: r["issued"])
            if self.fixed_records:
                self.fixed_next = max(
                    _timestamp(r["issued"]) for r in self.fixed_records
                ) + timedelta(minutes=125)
        count = payload.get("unverifiable", 0)
        pending = payload.get("pending_count", 0)
        if (
            isinstance(count, int)
            and not isinstance(count, bool)
            and 0 <= count <= 1_000_000
        ):
            self.plan_unverifiable = count
        if (
            isinstance(pending, int)
            and not isinstance(pending, bool)
            and 0 <= pending <= 15
        ):
            self.plan_unverifiable += pending
            if pending:
                self.plan_last_reason = "restart"
        rows = payload.get("records")
        if not isinstance(rows, list) or len(rows) > 48:
            return
        candidates = {}
        conflicting = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            source_entity = row.get("source_entity")
            start, issued, source, target, observed = (
                _timestamp(row.get(k))
                for k in (
                    "forecast_start",
                    "issued_at",
                    "source_reported",
                    "forecast_at",
                    "observed",
                )
            )
            horizon = row.get("horizon_minutes")
            values = (row.get("predicted"), row.get("actual"), row.get("initial"))
            revision, status = row.get("revision"), row.get("status")
            identity = _plan_identity(row.get("source_identity"))
            context = row.get("initial_context")
            if (
                any(v is None for v in (start, issued, source, target, observed))
                or horizon not in (30, 60, 120)
                or not isinstance(revision, str)
                or not revision
                or len(revision) > 80
                or not isinstance(source_entity, str)
                or not source_entity
                or identity is None
                or (context is not None and _plan_context(context) is None)
                or status not in {"validated_passive", "hypothetical_unvalidated"}
                or observed > now
                or not 0 <= (issued - start).total_seconds() <= 60
                or not 0 <= (issued - source).total_seconds() <= 300
                or target != start + timedelta(minutes=horizon)
                or abs((observed - target).total_seconds()) > 60
                or observed <= issued
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or not -50 <= v <= 60
                    for v in values
                )
            ):
                continue
            if (
                context is not None
                and abs(context["indoor_temperature"] - values[2]) > 0.005000001
            ):
                continue
            if not _plan_context_causal(context, start):
                continue
            predicted, actual, initial = values
            pair_key = (start, issued, horizon)
            item = {
                "forecast_start": start.isoformat(),
                "issued_at": issued.isoformat(),
                "source_reported": source.isoformat(),
                "forecast_at": target.isoformat(),
                "observed": observed.isoformat(),
                "horizon_minutes": horizon,
                "lead_seconds": (observed - issued).total_seconds(),
                "time_error_seconds": (observed - target).total_seconds(),
                "predicted": predicted,
                "actual": actual,
                "initial": initial,
                "initial_context": _plan_context(context),
                "error": predicted - actual,
                "persistence_error": initial - actual,
                "revision": revision,
                "status": status,
                "source_entity": source_entity,
                "source_identity": identity,
                "kind": "unexecuted_scenario_residual",
            }
            if pair_key in conflicting:
                continue
            prior = candidates.get(pair_key)
            if prior is not None and prior != item:
                del candidates[pair_key]
                conflicting.add(pair_key)
            elif prior is None:
                candidates[pair_key] = item
        self.plan_records = list(candidates.values())
