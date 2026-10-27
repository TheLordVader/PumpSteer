"""Causal audit of issued, conditional one-step temperature forecasts.

Inspired by Heat Pump Optimizer's forecast-versus-outcome accuracy records.
The issued forecast is a function of elapsed time, assuming unchanged measured
heat indicator and outside temperature. HA report time is not physical time.
"""

from datetime import timedelta
import math


class ForecastAudit:
    def __init__(self):
        self.pending = None
        self.records = []

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
