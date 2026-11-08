"""Passive zone observations. HA report time is not physical measurement time."""

from datetime import datetime, timedelta, timezone

from .utils import safe_float, temperature_from_state

_EVENTS = ("temperature", "forecast", "heat_shortage", "sensor_fault")
_GAP = timedelta(minutes=35)
_MAX_AGE = timedelta(minutes=60)
_CLIMATE_GAP = timedelta(minutes=5)


def _fresh(state, now, max_age=_MAX_AGE):
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


def _temperature(state, now, max_age=_MAX_AGE):
    if (
        _fresh(state, now, max_age)
        and (getattr(state, "attributes", {}) or {}).get("unit_of_measurement") == "°C"
    ):
        return temperature_from_state(state)
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


def zone_notification_message(alert, language="sv"):
    """Explain a passive zone alert using only qualified observation fields."""
    swedish = str(language).lower().split("-")[0] in ("sv", "se")

    def number(value):
        text = f"{value:.1f}"
        return text.replace(".", ",") if swedish else text

    event = alert["event"]
    value, threshold = alert.get("temperature"), alert.get("threshold")
    source = alert.get("heat_demand_source")
    if swedish:
        reasons = {
            "temperature": "Temperaturlarm",
            "forecast": "Förvarning",
            "heat_shortage": "Misstänkt värmebrist",
            "sensor_fault": "Givarfel: temperaturdata saknas eller är för gamla",
            "recovery": "Återhämtning: zonens larm har upphört",
        }
        parts = [f"{alert['name']}: {reasons[event]}."]
        if value is not None:
            detail = f"Temperaturen är {number(value)} °C"
            if threshold is not None:
                detail += (
                    f", under larmgränsen {number(threshold)} °C"
                    if event in ("temperature", "heat_shortage") and value < threshold
                    else f", larmgränsen är {number(threshold)} °C"
                )
            parts.append(detail + ".")
        if event == "forecast" and alert.get("estimated_hours") is not None:
            parts.append(
                f"Gränsen kan nås inom {number(alert['estimated_hours'])} timmar enligt trenden."
            )
        if alert.get("trend_c_per_hour") is not None:
            parts.append(
                f"Observerad trend: {number(alert['trend_c_per_hour'])} °C/timme."
            )
        if alert.get("climate_setpoint") is not None and alert.get(
            "climate_temperature_unit"
        ) in ("°C", "°F"):
            parts.append(
                f"Termostatens börvärde är {number(alert['climate_setpoint'])} {alert['climate_temperature_unit']}."
            )
        if event in ("temperature", "heat_shortage", "forecast"):
            parts.append(
                "Termostaten rapporterar värmedrift."
                if source == "hvac_action" and alert.get("heat_demand") is True
                else "Termostaten rapporterar ingen pågående värmedrift."
                if source == "hvac_action" and alert.get("heat_demand") is False
                else "Termostaten rapporterar att den begär värme."
                if source in ("heating_demand", "pi_heating_demand")
                and alert.get("heat_demand") is True
                else "Termostaten rapporterar att den inte begär värme."
                if source in ("heating_demand", "pi_heating_demand")
                and alert.get("heat_demand") is False
                else "Termostaten rapporterar ett ventilläge; värmebegäran kan inte fastställas enbart från detta."
                if source == "valve_position"
                else "Termostatens värmebegäran är okänd."
            )
        if alert.get("heat_response_qualified"):
            parts.append(
                "Temperaturen visar ingen tydlig återhämtning enligt de tillgängliga mätningarna."
            )
        parts.append(
            "PumpSteers zonfunktion har endast övervakat; den har inte ändrat termostaten eller central styrning utifrån zonen."
        )
        if event == "sensor_fault":
            parts.append("Kontrollera temperaturgivaren och dess rapportering.")
        elif event in ("temperature", "heat_shortage", "forecast"):
            parts.append(
                "Kontrollera radiator och värmetillförsel."
                if alert.get("heat_demand") is True
                else "Kontrollera termostatens inställning och läge."
                if alert.get("heat_demand") is False
                else "Kontrollera rumsgivare, termostat och värmetillförsel."
            )
        return " ".join(parts)

    reasons = {
        "temperature": "Low temperature alarm",
        "forecast": "Early warning",
        "heat_shortage": "Possible insufficient heat",
        "sensor_fault": "Sensor fault: temperature data is missing or stale",
        "recovery": "Recovery: the zone alarm has cleared",
    }
    parts = [f"{alert['name']}: {reasons[event]}."]
    if value is not None:
        detail = f"Temperature is {number(value)} °C"
        if threshold is not None:
            detail += (
                f", below the alarm limit of {number(threshold)} °C"
                if event in ("temperature", "heat_shortage") and value < threshold
                else f", the alarm limit is {number(threshold)} °C"
            )
        parts.append(detail + ".")
    if event == "forecast" and alert.get("estimated_hours") is not None:
        parts.append(
            f"The trend suggests the limit may be reached within {number(alert['estimated_hours'])} hours."
        )
    if alert.get("trend_c_per_hour") is not None:
        parts.append(f"Observed trend: {number(alert['trend_c_per_hour'])} °C/hour.")
    if alert.get("climate_setpoint") is not None and alert.get(
        "climate_temperature_unit"
    ) in ("°C", "°F"):
        parts.append(
            f"Thermostat setpoint is {number(alert['climate_setpoint'])} {alert['climate_temperature_unit']}."
        )
    if event in ("temperature", "heat_shortage", "forecast"):
        parts.append(
            "The thermostat reports heating operation."
            if source == "hvac_action" and alert.get("heat_demand") is True
            else "The thermostat reports no ongoing heating operation."
            if source == "hvac_action" and alert.get("heat_demand") is False
            else "The thermostat reports a heat request."
            if source in ("heating_demand", "pi_heating_demand")
            and alert.get("heat_demand") is True
            else "The thermostat reports no heat request."
            if source in ("heating_demand", "pi_heating_demand")
            and alert.get("heat_demand") is False
            else "The thermostat reports a valve position; a heat request cannot be verified from this alone."
            if source == "valve_position"
            else "The thermostat's heat request is unknown."
        )
    if alert.get("heat_response_qualified"):
        parts.append("The available measurements show no clear temperature recovery.")
    parts.append(
        "PumpSteer's zone function has only monitored; it has not changed the thermostat or central control based on this zone."
    )
    if event == "sensor_fault":
        parts.append("Check the temperature sensor and its reports.")
    elif event in ("temperature", "heat_shortage", "forecast"):
        parts.append(
            "Check the radiator and heat supply."
            if alert.get("heat_demand") is True
            else "Check the thermostat setpoint and mode."
            if alert.get("heat_demand") is False
            else "Check the room sensor, thermostat and heat supply."
        )
    return " ".join(parts)


class ExtraZones:
    def __init__(self, restored=None, started_at=None):
        self._started_at = started_at
        self._zones = {}
        self._restored = restored if isinstance(restored, dict) else {}

    def mark_sent(self, slot, event="temperature"):
        self._zones[slot]["sent"][event] = True
        if event == "recovery":
            self._zones[slot]["recovery_pending"] = False
            self._zones[slot]["episode_notified"] = False
            self._zones[slot]["resolved"] = False
        else:
            self._zones[slot]["episode_notified"] = True

    def mark_delivered(self, alert):
        """Acknowledge delivery even when the same episode resolved in flight."""
        zone = self._zones.get(alert["slot"])
        if (
            zone is None
            or zone["generation"] is not alert["generation"]
            or zone["episode_serial"] != alert.get("episode_serial")
        ):
            return None
        event = alert["event"]
        if event == "recovery":
            if not zone["recovery_pending"]:
                return None
            self.mark_sent(alert["slot"], event)
        else:
            zone["episode_notified"] = True
            if zone["events"].get(event):
                zone["sent"][event] = True
            if not zone["episode"] and zone["resolved"]:
                zone["recovery_pending"] = True
            elif not zone["episode"]:
                # The observation became uncertain or fell again while a
                # successful delivery was in flight. Keep the notified episode
                # open until a new report actually confirms recovery.
                zone["episode"] = True
        return {
            "notifications_sent": dict(zone["sent"]),
            "episode_active": zone["episode"],
            "episode_notified": zone["episode_notified"],
            "recovery_pending": zone["recovery_pending"],
        }

    def notification_current(self, alert):
        zone = self._zones.get(alert["slot"])
        if (
            zone is None
            or zone["generation"] is not alert["generation"]
            or zone["episode_serial"] != alert.get("episode_serial")
        ):
            return False
        event = alert["event"]
        return (
            zone["recovery_pending"]
            if event == "recovery"
            else zone["events"].get(event, False)
        )

    def notification_payload(self, alert, now, states):
        """Recheck the observations before an asynchronously queued message."""
        if not self.notification_current(alert):
            return None
        now = now.astimezone(timezone.utc)
        zone = self._zones[alert["slot"]]
        entity, threshold, _, _, climate_entity, _ = zone["signature"]
        state = states.get(entity)
        value = _temperature(state, now)
        event = alert["event"]
        if event == "sensor_fault":
            if value is not None:
                return None
        else:
            if (
                value is None
                or value != alert.get("temperature")
                or getattr(state, "last_reported", None) != zone["report"]
            ):
                return None
            if event == "recovery" and (threshold is None or value < threshold + 0.5):
                return None
            if event in ("forecast", "heat_shortage") and now - zone["report"] > _GAP:
                return None
        payload = dict(alert)
        climate = states.get(climate_entity) if climate_entity else None
        if (
            not _fresh(climate, now, _CLIMATE_GAP)
            or getattr(climate, "last_reported", None) != zone["climate_report"]
        ):
            if event == "heat_shortage":
                return None
            payload.update(
                heat_demand=None,
                heat_demand_source=None,
                climate_setpoint=None,
                heat_response_qualified=False,
            )
            payload.pop("related_events", None)
        return payload

    def observe(
        self,
        now,
        configs,
        states,
        outdoor=None,
        outdoor_max_age_minutes=5,
        climate_temperature_unit=None,
    ):
        now = now.astimezone(timezone.utc)
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
                episode_notified = bool(
                    same
                    and (
                        saved.get("episode_notified") is True
                        if "episode_notified" in saved
                        else any(sent.get(event) is True for event in _EVENTS)
                    )
                )
                zone = {
                    "signature": signature,
                    "generation": object(),
                    "since": None,
                    "started": now,
                    "restored_active": bool(
                        same
                        and (
                            any(events.values())
                            or saved.get("episode_active")
                            or saved.get("recovery_pending")
                        )
                    ),
                    "seen": False,
                    "last": None,
                    "report": None,
                    "samples": [],
                    "heat_since": None,
                    "climate_report": None,
                    "events": events,
                    "sent": sent,
                    "episode": same
                    and (saved.get("episode_active") is True or any(events.values())),
                    "episode_serial": int(
                        same
                        and (
                            saved.get("episode_active") is True
                            or any(events.values())
                            or saved.get("recovery_pending") is True
                        )
                    ),
                    "episode_notified": episode_notified,
                    "resolved": same and saved.get("recovery_pending") is True,
                    "recovery_pending": same
                    and saved.get("recovery_pending") is True
                    and episode_notified,
                    "last_event": saved.get("last_event") if same else None,
                    "last_event_at": saved.get("last_event_at") if same else None,
                }
                self._zones[slot] = zone
            state = states.get(entity)
            value = _temperature(state, now)
            valid = value is not None and threshold is not None
            report = getattr(state, "last_reported", None)
            if isinstance(report, datetime) and report.utcoffset() is not None:
                report = report.astimezone(timezone.utc)
            if valid and zone["restored_active"] and self._started_at is not None:
                if report <= self._started_at:
                    valid = False
                else:
                    zone["restored_active"] = False
            climate = states.get(cfg.get("climate")) if cfg.get("climate") else None
            climate_fresh = _fresh(climate, now, _CLIMATE_GAP)
            climate_attrs = getattr(climate, "attributes", {}) or {}
            climate_unit = (
                climate_attrs.get("temperature_unit") or climate_temperature_unit
            )
            raw_setpoint = climate_attrs.get("temperature")
            setpoint = (
                safe_float(raw_setpoint, -100, 200)
                if climate_fresh and not isinstance(raw_setpoint, bool)
                else None
            )
            demand, valve, demand_source, valve_source = None, None, None, None
            if climate_fresh:
                action = climate_attrs.get("hvac_action")
                explicit = []
                for key in ("pi_heating_demand", "valve_position", "heating_demand"):
                    raw = climate_attrs.get(key)
                    reading = None if isinstance(raw, bool) else safe_float(raw)
                    if reading is not None and 0 <= reading <= 100:
                        if valve is None:
                            valve, valve_source = reading, key
                        if key != "valve_position":
                            explicit.append((key, reading > 0))
                if action == "heating":
                    demand = True
                    demand_source = "hvac_action"
                elif action in ("idle", "off", "cooling", "drying", "fan"):
                    demand = False
                    demand_source = "hvac_action"
                elif valve is not None:
                    demand = valve > 0
                    demand_source = valve_source
                if explicit:
                    explicit_demand = explicit[0][1]
                    if any(value != explicit_demand for _, value in explicit) or (
                        demand_source == "hvac_action" and demand != explicit_demand
                    ):
                        demand, demand_source = None, None
                    else:
                        demand, demand_source = explicit_demand, explicit[0][0]
            climate_report = getattr(climate, "last_reported", None)
            if (
                isinstance(climate_report, datetime)
                and climate_report.utcoffset() is not None
            ):
                climate_report = climate_report.astimezone(timezone.utc)
            if demand is True and zone["climate_report"] is not None:
                if (
                    not timedelta(0)
                    <= climate_report - zone["climate_report"]
                    <= _CLIMATE_GAP
                ):
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
                    and now - report <= _GAP
                    and timedelta(0) <= report - zone["report"] <= _GAP
                )
                if valid
                else False
            )
            if not continuous or not valid:
                zone["since"] = None
                zone["heat_since"] = None
                zone["samples"].clear()
            new_report = valid and report != zone["report"]
            if valid:
                zone["seen"] = True
                if new_report and (
                    not zone["samples"]
                    or report - zone["samples"][-1][0] >= timedelta(minutes=1)
                ):
                    zone["samples"].append(
                        (
                            report,
                            value,
                            outdoor_value,
                            getattr(outdoor, "last_reported", None),
                        )
                    )
                zone["samples"] = [
                    s for s in zone["samples"] if now - s[0] <= timedelta(hours=2)
                ][-128:]
            if valid and enabled:
                if value < threshold:
                    if zone["since"] is None:
                        zone["since"] = report
                    if new_report and report - zone["since"] >= timedelta(
                        minutes=delay
                    ):
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
            # Reusing one outdoor reading does not establish outdoor coverage.
            outdoor_reports = {s[3] for s in samples if s[3] is not None}
            outdoor_covered = (
                len(relative_samples) == len(samples)
                and len(outdoor_reports) >= 7
                and all(
                    s[3] is not None and -_GAP <= s[0] - s[3] <= _GAP for s in samples
                )
            )
            relative, _ = _trend(relative_samples) if outdoor_covered else (None, None)
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
                events["sensor_fault"] = not valid and (
                    previous["sensor_fault"]
                    or zone["seen"]
                    or now - zone["started"] >= _MAX_AGE
                )
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
                zone["episode_notified"] = False
                zone["resolved"] = False
                zone["sent"].clear()
            if not valid or value < threshold + 0.5:
                zone["resolved"] = False
            if zone["recovery_pending"] and (not valid or value < threshold + 0.5):
                zone["recovery_pending"] = False
                zone["episode"] = enabled
            current = any(events.values())
            if enabled and current:
                if not zone["episode"] and not zone["recovery_pending"]:
                    zone["episode_serial"] += 1
                    zone["episode_notified"] = False
                zone["resolved"] = False
                zone["episode"] = True
                zone["recovery_pending"] = False
                zone["sent"]["recovery"] = False
            recovered = (
                enabled
                and valid
                and new_report
                and value >= threshold + 0.5
                and not current
                and zone["episode"]
            )
            if recovered:
                zone["episode"] = False
                zone["resolved"] = True
                zone["recovery_pending"] = zone["episode_notified"]
                zone["last_event"] = "recovery"
                zone["last_event_at"] = now.isoformat()
            for event in _EVENTS:
                if not events[event]:
                    zone["sent"][event] = False
            slot_alert_start = len(alerts)
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
                if event == "heat_shortage" and (
                    demand is not True
                    or heat_trend is None
                    or heat_fit is None
                    or heat_fit < 0.8
                ):
                    raised = False
                if raised and event != "recovery" and not previous[event]:
                    zone["last_event"] = event
                    zone["last_event_at"] = now.isoformat()
                if raised and not zone["sent"].get(event):
                    alerts.append(
                        {
                            "generation": zone["generation"],
                            "episode_serial": zone["episode_serial"],
                            "slot": slot,
                            "name": cfg.get("name") or entity,
                            "event": event,
                            "temperature": value if valid else None,
                            "threshold": threshold,
                            "estimated_hours": eta,
                            "trend_c_per_hour": trend
                            if valid and fit is not None and fit >= 0.8 and consistent
                            else None,
                            "climate_setpoint": setpoint,
                            "climate_temperature_unit": climate_unit,
                            "climate_configured": bool(cfg.get("climate")),
                            "heat_demand": demand,
                            "heat_demand_source": demand_source,
                            "heat_response_qualified": bool(
                                events["heat_shortage"]
                                and demand is True
                                and heat_trend is not None
                                and heat_fit is not None
                                and heat_fit >= 0.8
                            ),
                        }
                    )
            slot_alerts = alerts[slot_alert_start:]
            temperature_alert = next(
                (a for a in slot_alerts if a["event"] == "temperature"), None
            )
            heat_alert = next(
                (a for a in slot_alerts if a["event"] == "heat_shortage"), None
            )
            if temperature_alert is not None and heat_alert is not None:
                temperature_alert["related_events"] = ["heat_shortage"]
                alerts.remove(heat_alert)
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
                "episode_notified": zone["episode_notified"],
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
