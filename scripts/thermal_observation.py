"""Finite passive HA collector. No HA services, MQTT, entity writes or Recorder I/O.

Filtered state-change subscription plus bounded reads of actual last_reported.
Snapshots may miss intermediate reports; they never manufacture report times.
"""

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
from dataclasses import asdict
import importlib
import json
import math
import os
from pathlib import Path
import resource
import shlex
import signal
import sys
import time
from types import ModuleType, SimpleNamespace
import urllib.error
import urllib.request
import uuid
import zlib

TEMPERATURES = (
    "sensor.indoor_weighted",
    "sensor.indoor_primary",
    "sensor.indoor_room_b",
    "sensor.indoor_room_c",
    "sensor.outdoor_selected",
    "sensor.outdoor_primary_raw",
    "sensor.outdoor_primary",
    "sensor.outdoor_backup",
)
CONTROL = "sensor.pumpsteer"
ENTITIES = (*TEMPERATURES, CONTROL, "sensor.pumpsteer_electricity_price")
ATTRIBUTES = {
    "unit_of_measurement",
    "restored",
    "source_temperatures_used",
    "selected_source",
    "selection_reason",
    "freshness",
    "mode",
    "brake_factor",
    "aggressiveness",
    "target_temperature",
    "indoor_temperature",
    "outdoor_temperature",
    "fake_outdoor_temperature",
    "current_price",
    "price_category",
    "thermal_k",
    "thermal_k_valid",
    "thermal_k_samples",
    "raw_today", "raw_tomorrow", "prices", "forecast",
    "preheat_enabled", "comfort_floor_c", "preheat_ceiling_c",
    "pi_i_term", "pi_p_term", "pi_error_c", "preheat_boost_c",
}


def stamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def now():
    return datetime.now(timezone.utc)


def new_report(previous, current, received_at):
    at = stamp(current.get("last_reported"))
    before = stamp(previous.get("last_reported")) if previous else None
    return at is not None and at <= received_at and (before is None or at > before)


def trigger_reading(message):
    trigger = message.get("event", {}).get("variables", {}).get("trigger", {})
    if "to_state" not in trigger:
        return None
    if trigger["to_state"] is not None:
        return trigger["to_state"]
    return {"entity_id": trigger.get("entity_id"), "state": None, "attributes": {}}


def accept_reading(latest, admitted, raw, received_at, *, entities=ENTITIES):
    """Keep admission order separate from invalid evidence and read duplicates."""
    entity = raw.get("entity_id")
    if entity not in entities:
        return "ignored", raw
    value = {
        k: raw.get(k)
        for k in ("entity_id", "state", "last_reported", "last_updated", "last_changed")
    }
    value["attributes"] = {
        k: v for k, v in raw.get("attributes", {}).items() if k in ATTRIBUTES
    }
    value["received_at"] = received_at.isoformat()
    previous = admitted.get(entity)
    at = stamp(value.get("last_reported"))
    before = stamp(previous.get("last_reported")) if previous else None
    rejected=latest.get(entity,{})
    if rejected.get('_quarantined') and at is not None and at==stamp(rejected.get('last_reported')):
        # A reread of the same rejected report never becomes new evidence merely
        # because local time caught up. A different valid report recovers normally.
        return 'duplicate', value
    if at is not None and before is not None and at < before:
        return "reordered", value
    if new_report(previous, value, received_at):
        admitted[entity] = value
        latest[entity] = value
        return "report", value
    if previous and at == before:
        return "duplicate", value
    latest[entity] = {**value, "_quarantined": True}
    return "unqualified", value


def read_token(path, variable):
    """Read only the explicitly selected existing credential; no discovery/eval."""
    for line in Path(path).read_text().splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == variable:
            values = shlex.split(value, comments=True)
            if len(values) == 1 and values[0] and "\n" not in values[0]:
                return values[0]
    raise ValueError("Configured HA credential unavailable")


def read_state(base, token, entity, opener=urllib.request.urlopen):
    request = urllib.request.Request(
        base + "/api/states/" + entity, headers={"Authorization": "Bearer " + token}
    )
    try:
        with opener(request, timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        return {"entity_id": entity, "state": None, "attributes": {}}


def read_weather_forecast(base, token, entity, opener=urllib.request.urlopen):
    """Read-only weather.get_forecasts, never a caller-selected HA action."""
    if not isinstance(entity,str) or not entity.startswith('weather.'):
        raise ValueError('A weather entity is required')
    request=urllib.request.Request(base+'/api/services/weather/get_forecasts?return_response',
        data=json.dumps({'entity_id':entity,'type':'hourly'}).encode(),
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
    with opener(request,timeout=5) as response:
        result=json.load(response)
    try:
        rows=result['service_response'][entity]['forecast']
    except (TypeError,KeyError):
        raise ValueError('Malformed weather response') from None
    if not isinstance(rows,list) or len(rows)>512 or not all(isinstance(row,dict) for row in rows):
        raise ValueError('Malformed weather forecast rows')
    return rows


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False))
    os.chmod(temporary, 0o600)
    temporary.replace(path)


class Journal:
    """Bounded append-only gzip members; incomplete final member is recoverable."""

    def __init__(self, directory, limit=64 * 1024 * 1024):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.limit = limit
        self.buffer = []
        self.run_id = f"{time.time_ns():020d}"

    @property
    def size(self):
        return sum(p.stat().st_size for p in self.directory.glob("*.jsonl.gz"))

    def add(self, value):
        self.buffer.append(json.dumps(value, allow_nan=False, separators=(",", ":")))
        if len(self.buffer) >= 64:
            self.flush()

    def flush(self):
        if not self.buffer:
            return
        content = gzip.compress(("\n".join(self.buffer) + "\n").encode())
        if self.size + len(content) > self.limit:
            raise RuntimeError("observation_storage_limit")
        path = self.directory / (
            now().strftime("%Y-%m-%d") + "-" + self.run_id + ".jsonl.gz"
        )
        with path.open("ab") as stream:
            os.chmod(path, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        self.buffer.clear()


def recover_records(path):
    """Ignore a torn final member, never promote its partial observations."""
    decoder = zlib.decompressobj(31)
    member = bytearray()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(65536):
            while chunk:
                try:
                    member.extend(decoder.decompress(chunk))
                except zlib.error:
                    return
                chunk = decoder.unused_data
                if not decoder.eof:
                    break
                # Emit only after this member's CRC/trailer is verified. Never
                # promote partial observations from a torn final gzip member.
                for line in member.splitlines():
                    yield json.loads(line)
                member.clear()
                decoder = zlib.decompressobj(31)


class Shadow:
    """Reuse frozen ThermalModel/Observer. No controller or output dependency."""

    def __init__(self, model_dir, *, entities=None, emit=None, operational=False,
                 external_heat_sources=False, execution_basis="live_shadow", pi_config=None):
        self.operational = operational is True
        self.pi_config=({key:value for key,value in pi_config.items()
                         if key in ('pid_kp','pid_ki','pid_kd','pid_integral_clamp','pid_output_clamp','brake_delta_c')}
                        if isinstance(pi_config,dict) else None)
        self.external_heat_sources = external_heat_sources is True
        self.execution_basis = execution_basis
        self.forecast = None
        self.forecast_snapshot_id = None
        self._last_recommendation = None
        self.entities = entities or (TEMPERATURES[0], TEMPERATURES[4], CONTROL)
        self.emit = emit
        self.run_id = str(uuid.uuid4())
        self.segment = 0
        self.calibration_origin = "unfitted"
        self._resolved = set()
        self._last_issue = self._last_decision = self._last_evidence = None
        self.code_sha256 = hashlib.sha256(
            b"".join(
                (model_dir / file).read_bytes()
                for file in (
                    "thermal_model.py", "thermal_observer.py", "thermal_intelligence.py",
                    *(("forecast.py", "thermal_assist.py", "settings.py", "control.py") if self.operational else ()),
                )
            )
        ).hexdigest()
        name = (
            "_pumpsteer_observation_"
            + hashlib.sha256(
                (str(model_dir.resolve()) + self.code_sha256).encode()
            ).hexdigest()
        )
        package = ModuleType(name)
        package.__path__ = [str(model_dir)]
        sys.modules[name] = package
        model = importlib.import_module(name + ".thermal_model").ThermalModel()
        self.observer = importlib.import_module(
            name + ".thermal_observer"
        ).ThermalObserver(model)
        if self.operational:
            self.forecast_module = importlib.import_module(name + ".forecast")
            self.forecast_module_PI = importlib.import_module(name + ".thermal_intelligence").PIProjection
            self.planner = importlib.import_module(name + ".thermal_assist").assess_assistance
            self.settings = importlib.import_module(name + ".settings")
        self.was_braking = False

    def set_forecast(self, snapshot, at):
        if snapshot is None or snapshot.received_at>at:
            self.forecast = self.forecast_snapshot_id = None
            return False
        data=asdict(snapshot)
        ident=hashlib.sha256(json.dumps(data,default=lambda x:x.isoformat(),sort_keys=True).encode()).hexdigest()
        self.forecast=snapshot
        self.forecast_snapshot_id=ident
        self._record("forecast_inputs",at,snapshot_id=ident,snapshot=data)
        return True

    def _record(self, kind, at, **value):
        if self.emit:
            # Freeze nested dictionaries at issuance; later callbacks cannot
            # rewrite original evidence even when a test sink retains objects.
            self.emit(
                json.loads(
                    json.dumps(
                        dict(
                            schema=1,
                            execution_basis=self.execution_basis,
                            kind=kind,
                            received_at=at.isoformat(),
                            run_id=self.run_id,
                            segment=self.segment,
                            code_sha256=self.code_sha256,
                            **value,
                        ),
                        default=lambda v: v.isoformat(),
                        allow_nan=False,
                    )
                )
            )

    def _id(self, prediction):
        return f"{self.run_id}:{self.segment}:{prediction.issued_at.isoformat()}:{prediction.horizon_minutes}"

    @staticmethod
    def provenance(raw, at):
        raw = raw or {}
        ha = stamp(raw.get("last_reported"))
        received = stamp(raw.get("received_at"))
        attrs = raw.get("attributes", {})
        return dict(
            entity_id=raw.get("entity_id"),
            value=raw.get("state"),
            ha_last_reported=ha,
            received_at=received,
            ha_report_age_seconds=(at - ha).total_seconds() if ha else None,
            reception_delay_seconds=(received - ha).total_seconds()
            if received and ha
            else None,
            physical_measured_at=None,
            physical_freshness_verified=False,
            quarantined=bool(raw.get("_quarantined")),
            attributes=attrs,
        )

    def _capture_resolutions(self, at, latest):
        current = set()
        for comparison in self.observer.diagnostics.evaluation_history:
            ident = self._id(comparison.prediction)
            current.add(ident)
            if ident in self._resolved:
                continue
            data = asdict(comparison)
            data.pop("prediction")
            actual = comparison.actual_temperature_c
            self._record(
                "resolution",
                at,
                forecast_id=ident,
                **data,
                model_error_c=comparison.error_c,
                persistence_error_c=actual - comparison.prediction.indoor_temperature_c
                if actual is not None
                else None,
                actual_horizon_minutes=(
                    comparison.actual_at - comparison.prediction.issued_at
                ).total_seconds()
                / 60
                if comparison.actual_at
                else None,
                actual_input=self.provenance(latest.get(self.entities[0]), at)
                if actual is not None
                else None,
            )
        self._resolved = current  # bounded by existing 3x64 history, not whole run

    def boundary(self, at, reason="reconnect"):
        self.observer.diagnostics.cancel_pending(at, reason)
        self._capture_resolutions(at, {})
        self.was_braking = False
        self.observer.start(at)
        self.segment += 1
        self._resolved.clear()
        self._last_issue = self._last_decision = self._last_evidence = None
        self._record("shadow_boundary", at, reason=reason)

    def finish(self, at):
        self.observer.diagnostics.cancel_pending(at, "right_censored")
        self._capture_resolutions(at, {})

    def update(self, latest, at):
        control = latest.get(self.entities[2], {})
        attrs = control.get("attributes", {})
        reported = stamp(control.get("last_reported"))
        try:
            numeric_control = not isinstance(
                control.get("state"), bool
            ) and math.isfinite(float(control.get("state")))
        except (TypeError, ValueError):
            numeric_control = False
        valid_control = (
            numeric_control
            and not control.get("_quarantined")
            and not attrs.get("restored")
            and reported is not None
            and 0 <= (at - reported).total_seconds() <= 120
        )
        mode = attrs.get("mode") if valid_control else None
        if (
            mode in (None, "safe_mode", "summer_mode")
            or attrs.get("aggressiveness") == 0
        ):
            self.was_braking = False
        elif mode in ("braking", "brake_hold"):
            self.was_braking = True
        elif (
            mode in ("normal", "holiday")
            and attrs.get("brake_factor") == 0
            and self.was_braking
        ):
            if not self.operational and self.observer.model.pending_samples >= 20:
                self.observer.model.fit()
                if self.observer.model.rate_noise is not None:
                    self.calibration_origin = "shadow_fit"
            self.was_braking = False

        def state(entity):
            raw = latest.get(entity)
            if raw and raw.get("_quarantined"):
                return None
            return (
                SimpleNamespace(
                    entity_id=entity,
                    state=raw["state"],
                    last_reported=stamp(raw.get("last_reported")),
                    attributes=raw.get("attributes", {}),
                )
                if raw
                else None
            )

        before_response = self.observer.model.response
        pi_projection=(self.forecast_module_PI.from_control(self.pi_config,attrs,reported)
                       if self.operational and valid_control else None)
        self.observer.observe(
            at,
            state(self.entities[0]),
            state(self.entities[1]),
            braking=valid_control and mode in ("braking", "brake_hold"),
            control_context=(mode, attrs.get("brake_factor")),
            operation={"mode": mode,"brake_factor":attrs.get("brake_factor",0),
                       "fake_outdoor_temperature":float(control['state']) if valid_control else None,
                       "external_heat_sources":self.external_heat_sources,
                       "pi_projection":pi_projection} if self.operational else None,
            forecast=self.forecast if self.operational else None,
        )
        if self.operational and self.observer.model.response is not None and self.observer.model.response is not before_response:
            self.calibration_origin="shadow_fit"
        result = self.observer.attributes()
        inputs = {
            name: self.provenance(latest.get(entity), at)
            for name, entity in zip(("indoor", "outdoor", "control"), self.entities)
        }
        context = {
            "mode": mode or "unknown",
            "brake_factor": attrs.get("brake_factor") if valid_control else None,
        }
        predictions = self.observer.diagnostics.predictions
        if predictions and predictions[0].issued_at != self._last_issue:
            for prediction in predictions:
                self._record(
                    "forecast",
                    at,
                    forecast_id=self._id(prediction),
                    prediction=asdict(prediction),
                    inputs=inputs,
                    context=context,
                    baseline_confirmed=result["heat_evidence"].get(
                        "baseline_confirmed", False
                    ),
                    calibration_samples=self.observer.model.response.sample_count if self.operational and self.observer.model.response else self.observer.model.sample_count,
                    forecast_snapshot_id=self.forecast_snapshot_id if self.operational else None,
                    calibration_origin=self.calibration_origin,
                )
            self._last_issue = predictions[0].issued_at
        self._capture_resolutions(at, latest)
        raw = latest.get(self.entities[0], {})
        # One decision per observed HA report identity, not per timer callback.
        decision = (
            raw.get("last_reported"),
            raw.get("state"),
            bool(raw.get("_quarantined")),
        )
        reported=stamp(raw.get('last_reported'))
        after=self.observer._after
        new_source_report=reported is not None and reported<=at and (after is None or reported>after)
        if raw and new_source_report and decision != self._last_decision:
            reason = result["reason"]
            if mode == "safe_mode":
                reason = "safe_context"
            elif not valid_control or mode is None:
                reason = "unknown_context"
            elif not self.operational and mode not in ("braking", "brake_hold"):
                reason = "outside_braking"
            self._record(
                "decision",
                at,
                context=context,
                inputs=inputs,
                issued=bool(predictions and predictions[0].issued_at == at),
                reason=reason,
                model_version=self.observer.diagnostics.model_version,
            )
            self._last_decision = decision
        if self.operational:
            def number(raw):
                try:
                    v=float(raw)
                    return v if math.isfinite(v) else None
                except (TypeError,ValueError):return None
            indoor=number(latest.get(self.entities[0],{}).get('state'))
            outdoor=number(latest.get(self.entities[1],{}).get('state'))
            target=number(attrs.get('target_temperature'))
            level=attrs.get('aggressiveness')
            level=level if type(level) is int and 0<=level<=5 else None
            floor=target-self.settings.COMFORT_FLOOR_BY_AGGRESSIVENESS[level] if target is not None and level is not None else None
            ceiling=target+self.settings.PREHEAT_HEADROOM_BY_AGGRESSIVENESS[level] if target is not None and level is not None else None
            recommendation=self.planner(True,result,now=at,forecast=self.forecast,
                indoor=indoor,outdoor=outdoor,target=target,comfort_floor=floor,ceiling=ceiling,
                mode=mode,control_signal=-float(control['state']) if valid_control else None,
                response=self.observer.model.response,preheat_enabled=attrs.get('preheat_enabled',False) is True,
                pi_projection=pi_projection)
            rec_key=(recommendation['reason'],recommendation['action'],mode)
            if (recommendation['eligible'] or decision!=getattr(self,'_recommendation_decision',None)
                or rec_key!=self._last_recommendation):
                self._record('recommendation',at,recommendation=recommendation,inputs=inputs,
                             context=context,forecast_snapshot_id=self.forecast_snapshot_id)
                self._last_recommendation=rec_key
                self._recommendation_decision=decision
            result['recommendation']=recommendation
        evidence = result["heat_evidence"]
        window = evidence.get("last_window")
        key = (evidence["status"], window.get("ended_at") if window else None)
        if key != self._last_evidence:
            self._record(
                "heat_diagnostic",
                at,
                evidence=evidence,
                context=context,
                verified=False,
                source_identifiable=False,
            )
            self._last_evidence = key
        return result


def source_signature(latest, indoor_id=TEMPERATURES[0], outdoor_id=TEMPERATURES[4]):
    if not all(entity in latest for entity in (indoor_id, outdoor_id)):
        return None
    indoor = latest[indoor_id].get("attributes", {}).get("source_temperatures_used", {})
    keys = tuple(sorted(indoor)) if isinstance(indoor, dict) else ()
    return keys, latest[outdoor_id].get("attributes", {}).get("selected_source")


def restore_checkpoint(shadow, path):
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if shadow.operational:
        if (isinstance(saved,dict) and saved.get('code_sha256')==shadow.code_sha256
            and saved.get('entities')==list(shadow.entities)
            and shadow.observer.model.restore_response(saved.get('response_fit'))
            and shadow.observer.restore_response_identity(saved.get('response_identity'))):
            shadow.calibration_origin='restored_checkpoint'
            return True
        return False
    if (
        isinstance(saved, dict)
        and saved.get("valid") is True
        and saved.get("code_sha256") == shadow.code_sha256
        and saved.get("entities") == list(shadow.entities)
        and type(saved.get("sample_count")) is int
        and saved["sample_count"] >= 20
    ):
        shadow.observer.model.restore_k(saved.get("k"), saved["sample_count"])
        if shadow.observer.model.is_valid:
            shadow.calibration_origin = "restored_checkpoint"
            return True
        return False
    return False


def selected_entities(config):
    selected = config.get("entities")
    if selected is None:
        return (TEMPERATURES[0], TEMPERATURES[4], CONTROL), ENTITIES
    if not isinstance(selected, dict) or set(selected) != {
        "indoor",
        "outdoor",
        "control",
    }:
        raise ValueError("Select exactly indoor, outdoor and control entities")
    entities = tuple(selected[k] for k in ("indoor", "outdoor", "control"))
    if len(set(entities)) != 3 or any(
        not isinstance(e, str) or not e.startswith("sensor.") for e in entities
    ):
        raise ValueError("Select three distinct sensor entity IDs")
    return entities, entities


def collect(config, credential, variable):
    # Imported only in the isolated deployed process; no host package installation.
    import websocket

    directory = Path(config["data_dir"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    end = stamp(config["end"])
    if end is None or now() >= end:
        return
    token = read_token(credential, variable)
    base = config["ha_url"].rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("Invalid configured HA endpoint")
    ws_url = base.replace("http", "ws", 1) + "/api/websocket"
    selected, entities = selected_entities(config)
    journal = Journal(directory / "raw")
    shadow = Shadow(Path(config["model_dir"]), entities=selected, emit=journal.add,
                    operational=config.get("thermal_intelligence",False),
                    external_heat_sources=config.get("external_heat_sources",False),
                    pi_config=config.get("pi_config"))
    for entity in (config.get('price_entity'),config.get('price_tomorrow_entity')):
        if entity and entity not in entities:
            if not isinstance(entity,str) or not entity.startswith('sensor.'):
                raise ValueError('Invalid price source')
            entities=(*entities,entity)
    status = {
        "started": config["start"],
        "end": config["end"],
        "reports": 0,
        "identical_value_reports": 0,
        "duplicates": 0,
        "segments": 0,
        "effective_assistance": False,
        "transport": "filtered_state_trigger_and_30s_last_reported_snapshots",
    }
    latest, admitted = {}, {}
    segment_start, signature = None, None
    stopped = False
    reconnect = False

    def request_reconnect(signum, frame):
        nonlocal reconnect
        reconnect = True

    def request_stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGHUP, request_reconnect)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    boot = time.monotonic()
    maximum = min(7 * 86400, max(0, (end - now()).total_seconds()))
    prior_clock = now()
    try:
        prior_status = json.loads((directory / "status.json").read_text())
        recorded_clock = stamp(prior_status.get("updated_at"))
        if recorded_clock and recorded_clock > prior_clock:
            prior_clock = recorded_clock
    except (OSError, ValueError):
        pass

    def api(entity):
        return read_state(base, token, entity)

    def ingest(raw, source):
        nonlocal signature
        at = now()
        if at >= end:
            return
        entity = raw.get("entity_id")
        if entity not in entities:
            return
        previous = admitted.get(entity)
        kind, value = accept_reading(latest, admitted, raw, at, entities=entities)
        if (
            kind == "report"
            and source == "connection_snapshot"
            and stamp(value.get("last_reported")) <= segment_start
        ):
            kind = "seed"
            status["seed_snapshots"] = status.get("seed_snapshots", 0) + 1
        elif kind == "report":
            status["reports"] += 1
            status["identical_value_reports"] += bool(
                previous and previous["state"] == value["state"]
            )
        elif kind == "duplicate":
            status["duplicates"] += 1
            return
        elif kind == "ignored":
            return
        updated_signature = source_signature(latest, selected[0], selected[1])
        if signature is not None and updated_signature != signature:
            shadow.boundary(at, "temperature_source_changed")
            journal.add(
                {
                    "kind": "boundary",
                    "received_at": at.isoformat(),
                    "reason": "temperature_source_changed",
                }
            )
        signature = updated_signature
        journal.add(
            {
                "kind": kind,
                "received_at": at.isoformat(),
                "source": source,
                "value": value,
            }
        )

    last_forecast_read = None

    def future_inputs():
        nonlocal last_forecast_read
        entity=config.get('weather_entity')
        if not shadow.operational or not entity or (last_forecast_read is not None and time.monotonic()-last_forecast_read<900):
            return
        last_forecast_read=time.monotonic()
        try:
            weather=api(entity)
            rows=read_weather_forecast(base,token,entity)
            if not isinstance(rows,list) or len(rows)>512:
                raise ValueError("Invalid or oversized weather forecast")
            module=shadow.forecast_module
            points=[]
            for row in rows[:512]:
                at=stamp(row.get('datetime'))
                if at is None:raise ValueError('Invalid weather time')
                points.append(module.ForecastPoint(at,None,row.get('temperature'),None,None))
            received=now()
            def price_state(source):
                raw=latest.get(source)
                return SimpleNamespace(state=raw.get('state'),attributes=raw.get('attributes',{}),
                    last_reported=stamp(raw.get('last_reported'))) if raw and not raw.get('_quarantined') else None
            today=config.get('price_entity');tomorrow=config.get('price_tomorrow_entity')
            snapshot=module.make_thermal_forecast(points,received_at=received,
                weather_source=entity,price_source=today or '',
                price_state=price_state(today),tomorrow_state=price_state(tomorrow) if tomorrow and tomorrow!=today else None,
                weather_unit=weather.get('attributes',{}).get('temperature_unit'))
            shadow.set_forecast(snapshot,received)
        except (OSError,ValueError,TypeError,KeyError,RuntimeError):
            shadow.set_forecast(None,now())
            journal.add({'kind':'future_input_gap','received_at':now().isoformat()})

    def tick():
        at = now()
        journal.add({"kind": "tick", "received_at": at.isoformat()})
        return shadow.update(latest, at)

    last_status_save = None
    last_checkpoint = None

    def save(observe=True):
        nonlocal last_status_save, last_checkpoint
        if observe:
            future_inputs()
        status.update(
            {
                "updated_at": now().isoformat(),
                "pid": os.getpid(),
                "cpu_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_utime
                + resource.getrusage(resource.RUSAGE_SELF).ru_stime,
                "peak_memory_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                / 1024,
                "compressed_bytes": journal.size,
                "shadow": tick() if observe else shadow.observer.attributes(),
            }
        )
        journal.flush()
        if (
            not observe
            or last_status_save is None
            or time.monotonic() - last_status_save >= 300
        ):
            atomic_json(directory / "status.json", status)
            last_status_save = time.monotonic()
        model = shadow.observer.model
        checkpoint = {
            "valid": model.is_valid,
            "k": model.k,
            "sample_count": model.sample_count,
            "code_sha256": shadow.code_sha256,
            "entities": list(shadow.entities),
            "response_fit": model.response_state() if shadow.operational else None,
            "response_identity": shadow.observer._response_identity if shadow.operational else None,
        }
        if checkpoint != last_checkpoint:
            atomic_json(directory / "model-checkpoint.json", checkpoint)
            last_checkpoint = checkpoint

    status["checkpoint_restored"] = restore_checkpoint(
        shadow, directory / "model-checkpoint.json"
    )
    while not stopped and now() < end and time.monotonic() - boot < maximum:
        # After a backward clock step, resume only beyond the previous watermark.
        # No observation is manufactured or admitted on the overlapping timeline.
        if now() < prior_clock:
            time.sleep(min(15, max(0, (end - now()).total_seconds())))
            continue
        prior_clock = now()
        ws = None
        try:
            reconnect = False
            ws = websocket.create_connection(
                ws_url, timeout=5, enable_multithread=False
            )
            if json.loads(ws.recv()).get("type") != "auth_required":
                raise RuntimeError("Unexpected HA handshake")
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            if json.loads(ws.recv()).get("type") != "auth_ok":
                raise RuntimeError("HA authentication failed")
            ws.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": "subscribe_trigger",
                        "trigger": {"platform": "state", "entity_id": list(entities)},
                    }
                )
            )
            reply = json.loads(ws.recv())
            if reply.get("success") is not True:
                raise RuntimeError("Filtered subscription failed")
            at = now()
            segment_start, signature = at, None
            status["segments"] += 1
            latest.clear()
            admitted.clear()
            shadow.boundary(at)
            journal.add(
                {
                    "kind": "boundary",
                    "received_at": at.isoformat(),
                    "segment": str(uuid.uuid4()),
                    "reason": "connect",
                }
            )
            for entity in entities:
                ingest(api(entity), "connection_snapshot")
            ws.settimeout(1)
            next_snapshot = time.monotonic() + 30
            while not stopped and now() < end and time.monotonic() - boot < maximum:
                clock = now()
                if reconnect:
                    raise RuntimeError("requested_reconnect")
                if clock < prior_clock:
                    raise RuntimeError("clock_reversal")
                prior_clock = clock
                try:
                    message = json.loads(ws.recv())
                    raw = trigger_reading(message)
                    if raw:
                        ingest(raw, "filtered_state_change")
                        tick()
                except websocket.WebSocketTimeoutException:
                    pass
                if time.monotonic() >= next_snapshot:
                    for entity in entities:
                        ingest(api(entity), "last_reported_snapshot")
                    save()
                    next_snapshot = time.monotonic() + 30
        except (
            OSError,
            ValueError,
            RuntimeError,
            websocket.WebSocketException,
        ) as error:
            journal.add(
                {
                    "kind": "gap",
                    "received_at": max(now(), prior_clock).isoformat(),
                    "detected_at": now().isoformat(),
                    "reason": str(error)
                    if str(error) == "clock_reversal"
                    else type(error).__name__,
                }
            )
            latest.clear()
            admitted.clear()
            shadow.boundary(now())
            if str(error) == "clock_reversal":
                status["updated_at"] = prior_clock.isoformat()
                atomic_json(directory / "status.json", status)
                journal.flush()
            else:
                prior_clock = now()
                save()
            if str(error) == "observation_storage_limit":
                raise
            time.sleep(min(15, max(0, (end - now()).total_seconds())))
        finally:
            if ws:
                ws.close()
    shadow.finish(now())
    journal.add(
        {
            "kind": "end",
            "received_at": now().isoformat(),
            "reason": status.get(
                "stop_reason", "service_stopped" if stopped else "deadline"
            ),
        }
    )
    save(observe=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--credential-env", type=Path, required=True)
    parser.add_argument("--credential-variable", required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    try:
        collect(config, args.credential_env, args.credential_variable)
    except RuntimeError as error:
        if str(error) != "observation_storage_limit":
            raise
        atomic_json(
            Path(config["data_dir"]) / "stop.json",
            {"reason": "storage_limit", "at": now().isoformat()},
        )
        raise SystemExit(78) from None


if __name__ == "__main__":
    main()
