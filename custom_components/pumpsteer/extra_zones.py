"""Passive zone observations. HA report time is not physical measurement time."""

from datetime import datetime, timedelta

from .utils import safe_float

_EVENTS = ("temperature", "forecast", "heat_shortage", "sensor_fault")
_GAP = timedelta(minutes=5)


def _fresh(state, now, max_age=_GAP):
    attrs = getattr(state, "attributes", {}) or {}
    try:
        return (
            state is not None
            and getattr(state, "state", None) not in (None, "unknown", "unavailable")
            and not attrs.get("restored")
            and timedelta(0) <= now - state.last_reported <= max_age
        )
    except (TypeError, AttributeError, OverflowError):
        return False


def _temperature(state, now, max_age=_GAP):
    if (
        _fresh(state, now, max_age)
        and (getattr(state, "attributes", {}) or {}).get("unit_of_measurement") == "°C"
    ):
        return safe_float(state.state)
    return None


def _trend(samples):
    """Least squares over continuous reports; expose fit before extrapolating."""
    if len(samples) < 7 or samples[-1][0] - samples[0][0] < timedelta(minutes=30):
        return None, None
    xs = [(s[0] - samples[0][0]).total_seconds() / 3600 for s in samples]
    ys = [s[1] for s in samples]
    xmean, ymean = sum(xs) / len(xs), sum(ys) / len(ys)
    xx = sum((x - xmean) ** 2 for x in xs)
    yy = sum((y - ymean) ** 2 for y in ys)
    xy = sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys))
    slope = xy / xx
    fit = min(1.0, max(0.0, xy * xy / (xx * yy))) if yy > 1e-12 else 1.0
    return slope, fit


class ExtraZones:
    def __init__(self, restored=None):
        self._zones = {}
        self._restored = restored if isinstance(restored, dict) else {}

    def mark_sent(self, slot, event="temperature"):
        self._zones[slot]["sent"][event] = True
        if event == "recovery":
            self._zones[slot]["recovery_pending"] = False

    def notification_current(self, alert):
        zone = self._zones.get(alert["slot"])
        if zone is None or zone["generation"] is not alert["generation"]:
            return False
        event = alert["event"]
        return (
            zone["recovery_pending"]
            if event == "recovery"
            else zone["events"].get(event, False)
        )

    def observe(self, now, configs, states, outdoor=None, outdoor_max_age_minutes=5):
        result, alerts = {}, []
        active = set()
        outdoor_value = _temperature(
            outdoor, now, timedelta(minutes=outdoor_max_age_minutes)
        )
        for slot, cfg in enumerate(configs, 1):
            entity = cfg.get("entity")
            if not entity:
                continue
            active.add(slot)
            profile = cfg.get("profile") or "maintenance"
            threshold = safe_float(
                cfg.get("comfort_threshold" if profile == "comfort" else "threshold")
            )
            delay = safe_float(cfg.get("alarm_delay_minutes"))
            delay = min(240, max(1, delay if delay is not None else 15))
            signature = (
                entity,
                threshold,
                bool(cfg.get("enabled")),
                profile,
                cfg.get("climate"),
                delay,
            )
            zone = self._zones.get(slot)
            if zone is None or zone["signature"] != signature:
                saved = self._restored.pop(str(slot), {})
                same = isinstance(saved, dict) and (
                    saved.get("entity") == entity
                    and saved.get("threshold") == threshold
                    and saved.get("profile", "maintenance") == profile
                    and bool(saved.get("enabled")) == bool(cfg.get("enabled"))
                    and saved.get("climate") == cfg.get("climate")
                    and saved.get("alarm_delay_minutes", 15) == delay
                )
                events = {e: False for e in _EVENTS}
                sent = {}
                if same:
                    saved_events = saved.get("events", {})
                    if isinstance(saved_events, dict):
                        events.update({e: saved_events.get(e) is True for e in _EVENTS})
                    events["temperature"] |= saved.get("alarm_active") is True
                    saved_sent = saved.get("notifications_sent", {})
                    if isinstance(saved_sent, dict):
                        sent = {
                            e: saved_sent.get(e) is True for e in (*_EVENTS, "recovery")
                        }
                    sent["temperature"] = (
                        sent.get("temperature", False)
                        or saved.get("notification_sent") is True
                    )
                zone = {
                    "signature": signature,
                    "generation": object(),
                    "since": None,
                    "last": None,
                    "report": None,
                    "samples": [],
                    "heat_since": None,
                    "climate_report": None,
                    "events": events,
                    "sent": sent,
                    "episode": same
                    and (saved.get("episode_active") is True or any(events.values())),
                    "recovery_pending": same and saved.get("recovery_pending") is True,
                    "last_event": saved.get("last_event") if same else None,
                    "last_event_at": saved.get("last_event_at") if same else None,
                }
                self._zones[slot] = zone
            state = states.get(entity)
            value = _temperature(state, now)
            valid = value is not None and threshold is not None
            report = getattr(state, "last_reported", None)
            climate = states.get(cfg.get("climate")) if cfg.get("climate") else None
            climate_fresh = _fresh(climate, now)
            climate_attrs = getattr(climate, "attributes", {}) or {}
            demand, valve = None, None
            if climate_fresh:
                action = climate_attrs.get("hvac_action")
                for key in ("pi_heating_demand", "valve_position", "heating_demand"):
                    valve = safe_float(climate_attrs.get(key))
                    if valve is not None and 0 <= valve <= 100:
                        break
                    valve = None
                if action == "heating":
                    demand = True
                elif action in ("idle", "off", "cooling", "drying", "fan"):
                    demand = False
                elif valve is not None:
                    demand = valve > 0
            climate_report = getattr(climate, "last_reported", None)
            if demand is True and zone["climate_report"] is not None:
                if not timedelta(0) <= climate_report - zone["climate_report"] <= _GAP:
                    zone["heat_since"] = None
            zone["climate_report"] = climate_report if climate_fresh else None
            previous = zone["events"].copy()
            enabled = bool(cfg.get("enabled"))
            continuous = (
                (
                    zone["last"] is not None
                    and timedelta(0) <= now - zone["last"] <= _GAP
                    and zone["report"] is not None
                    and isinstance(report, datetime)
                    and timedelta(0) <= report - zone["report"] <= _GAP
                )
                if valid
                else False
            )
            if not continuous or not valid:
                zone["since"] = None
                zone["heat_since"] = None
                zone["samples"].clear()
            if valid:
                if report != zone["report"]:
                    zone["samples"].append((report, value, outdoor_value))
                zone["samples"] = [
                    s for s in zone["samples"] if now - s[0] <= timedelta(hours=1)
                ]
            if valid and enabled:
                if value < threshold:
                    if zone["since"] is None:
                        zone["since"] = now
                    if now - zone["since"] >= timedelta(minutes=delay):
                        zone["events"]["temperature"] = True
                else:
                    zone["since"] = None
                    if value >= threshold + 0.5:
                        zone["events"]["temperature"] = False
                if demand is True:
                    if zone["heat_since"] is None:
                        zone["heat_since"] = now
                else:
                    zone["heat_since"] = None
            zone["last"] = now if valid else None
            zone["report"] = report if valid else None
            samples = zone["samples"]
            trend, fit = _trend(samples)
            relative_samples = [
                (s[0], s[1] - s[2]) for s in samples if s[2] is not None
            ]
            # Missing outdoor reports must not masquerade as continuous coverage.
            relative, _ = (
                _trend(relative_samples)
                if len(relative_samples) == len(samples)
                else (None, None)
            )
            eta = None
            if (
                valid
                and trend is not None
                and fit >= 0.8
                and trend < -0.1
                and value > threshold
            ):
                eta = (value - threshold) / -trend
            consistent = True
            if trend is not None:
                mean_hours = sum(
                    (s[0] - samples[0][0]).total_seconds() / 3600 for s in samples
                ) / len(samples)
                last_hours = (samples[-1][0] - samples[0][0]).total_seconds() / 3600
                predicted_last = sum(s[1] for s in samples) / len(samples) + trend * (
                    last_hours - mean_hours
                )
                consistent = abs(value - predicted_last) <= 0.3
                if not consistent:
                    eta = None
            events = zone["events"]
            if enabled:
                events["sensor_fault"] = not valid
                if valid:
                    if eta is not None and eta <= 2:
                        events["forecast"] = True
                    elif trend is not None and (
                        trend >= -0.1 or (eta is not None and eta > 3)
                    ):
                        events["forecast"] = False
                    elif (
                        zone["episode"] and value >= threshold + 0.5 and not continuous
                    ):
                        # A fresh healthy report after a gap can resolve an old episode.
                        events["forecast"] = False
                    if value <= threshold or (
                        not consistent
                        and value > predicted_last + 0.3
                        and value >= threshold + 0.5
                    ):
                        events["forecast"] = False
                heat_samples = [
                    s
                    for s in samples
                    if zone["heat_since"] is not None and s[0] >= zone["heat_since"]
                ]
                heat_trend, heat_fit = _trend(heat_samples)
                if demand is False:
                    events["heat_shortage"] = False
                elif valid and value >= threshold + 0.5:
                    events["heat_shortage"] = False
                elif heat_trend is not None and heat_fit >= 0.8:
                    events["heat_shortage"] = heat_trend <= 0.1 and value < threshold
                # Unknown telemetry neither proves failure nor proves recovery.
            else:
                events.update({e: False for e in _EVENTS})
                zone["episode"] = False
                zone["recovery_pending"] = False
                zone["sent"].clear()
            if zone["recovery_pending"] and (not valid or value < threshold + 0.5):
                zone["recovery_pending"] = False
                zone["episode"] = enabled
            current = any(events.values())
            if enabled and current:
                zone["episode"] = True
                zone["recovery_pending"] = False
                zone["sent"]["recovery"] = False
            recovered = (
                enabled
                and valid
                and value >= threshold + 0.5
                and not current
                and zone["episode"]
            )
            if recovered:
                zone["episode"] = False
                zone["recovery_pending"] = True
                zone["last_event"] = "recovery"
                zone["last_event_at"] = now.isoformat()
            for event in _EVENTS:
                if not events[event]:
                    zone["sent"][event] = False
            for event in (*_EVENTS, "recovery"):
                raised = (
                    events.get(event, False)
                    if event != "recovery"
                    else zone["recovery_pending"]
                )
                if event in ("temperature", "forecast", "heat_shortage") and not valid:
                    raised = False
                if event == "forecast" and eta is None:
                    raised = False
                if event == "heat_shortage" and demand is not True:
                    raised = False
                if raised and event != "recovery" and not previous[event]:
                    zone["last_event"] = event
                    zone["last_event_at"] = now.isoformat()
                if raised and not zone["sent"].get(event):
                    alerts.append(
                        {
                            "generation": zone["generation"],
                            "slot": slot,
                            "name": cfg.get("name") or entity,
                            "event": event,
                            "temperature": value if valid else None,
                            "threshold": threshold,
                            "estimated_hours": eta,
                        }
                    )
            status = (
                "disabled"
                if not enabled
                else "unavailable"
                if not valid
                else "alarm"
                if events["temperature"]
                else "heat_shortage"
                if events["heat_shortage"]
                else "forecast"
                if events["forecast"]
                else "pending"
                if zone["since"]
                else "normal"
            )
            result[str(slot)] = {
                "name": cfg.get("name") or entity,
                "entity": entity,
                "temperature": value if valid else None,
                "status": status,
                "last_reported": report.isoformat()
                if isinstance(report, datetime)
                else None,
                "report_fresh": valid,
                "physical_measurement_time": "unknown",
                "trend_c_per_hour": trend,
                "trend_fit": fit,
                "relative_trend_c_per_hour": relative,
                "estimated_hours_to_threshold": eta,
                "trend_quality": "observational"
                if trend is not None and fit >= 0.8
                else "noisy"
                if trend is not None
                else "insufficient_data",
                "forecast_uncertainty": "linear_extrapolation_not_verified"
                if eta is not None
                else "insufficient_evidence",
                "threshold": threshold,
                "profile": profile,
                "enabled": cfg.get("enabled"),
                "alarm_delay_minutes": delay,
                "events": dict(events),
                "alarm_active": events["temperature"],
                "notification_sent": zone["sent"].get("temperature", False),
                "notifications_sent": dict(zone["sent"]),
                "episode_active": zone["episode"],
                "recovery_pending": zone["recovery_pending"],
                "last_event": zone["last_event"],
                "last_event_at": zone["last_event_at"],
                "climate": cfg.get("climate"),
                "climate_status": "current"
                if climate_fresh
                else "unavailable"
                if cfg.get("climate")
                else "not_configured",
                "climate_setpoint": safe_float(climate_attrs.get("temperature"))
                if climate_fresh
                else None,
                "climate_temperature_unit": climate_attrs.get("temperature_unit")
                if climate_fresh
                else None,
                "climate_action": climate_attrs.get("hvac_action")
                if climate_fresh
                else None,
                "valve_demand_percent": valve,
                "heat_demand": demand,
                "heat_response_c_per_hour": heat_trend
                if enabled and demand is True
                else None,
                "heat_response": "insufficient_data"
                if demand is None
                else "suspected_insufficient"
                if events["heat_shortage"]
                else "observational",
            }
        for slot in set(self._zones) - active:
            del self._zones[slot]
        return result, alerts
