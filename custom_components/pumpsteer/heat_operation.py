"""Conservative, passive heat-operation evidence and bounded calibration.

Electrical input is never converted to heat output; inferred modes cannot
qualify a Pilot learning interval. HA report time is not a physical timestamp.
"""

from datetime import timedelta
import math
from statistics import median

from .pilot_forecast import _timestamp
from .pilot_passive import PilotPassive
from .pilot_power_ledger import PowerLedger


class HeatOperation:
    """Learn power bands only from independently reported operating labels."""

    def __init__(self):
        self.samples = {"idle": [], "heating": []}
        self.temperatures = {"supply": [], "return": []}
        self.last_sample = None
        self.sample_count = 0
        self.last_temperature_samples = {"supply": None, "return": None}
        self.fresh_labels = {"idle", "heating"}
        self.fresh_temperatures = {"supply", "return"}
        self.repeated_sources = set()

    def export(self):
        return {
            key: list(values)
            for key, values in {**self.samples, **self.temperatures}.items()
        }

    def restore(self, saved):
        if not isinstance(saved, dict):
            return False
        for key in ("idle", "heating"):
            values = saved.get(key)
            if (
                not isinstance(values, list)
                or len(values) > 24
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or not 0 <= v <= 100
                    for v in values
                )
            ):
                return False
        temperatures = {}
        for key in ("supply", "return"):
            values = saved.get(key, [])
            if (
                not isinstance(values, list)
                or len(values) > 24
                or any(
                    isinstance(v, bool)
                    or not isinstance(v, (int, float))
                    or not math.isfinite(v)
                    or not -50 <= v <= 60
                    for v in values
                )
            ):
                return False
            temperatures[key] = list(values)
        self.samples = {key: list(saved[key]) for key in ("idle", "heating")}
        self.temperatures = temperatures
        self.last_sample = None
        self.fresh_labels = set()
        self.fresh_temperatures = set()
        return True

    def observe(
        self,
        now,
        *,
        heat=None,
        compressor=None,
        radiator=None,
        water=None,
        power=None,
        supply=None,
        return_temp=None,
        reported_after=None,
    ):
        now = _timestamp(now)
        result = {
            "mode": "unknown",
            "quality": "unknown",
            "signals": [],
            "learning_qualified": False,
            "calibration": "insufficient_evidence",
            "physical_measurement_time": "unknown",
            "delta_t_c": None,
            "delta_t_status": "unavailable",
        }
        if now is None:
            return result

        def binary(state):
            return PilotPassive._report(state, now, reported_after=reported_after)

        h, c, r, w = (binary(state) for state in (heat, compressor, radiator, water))
        if h is not None:
            for mode_state, mode_report in ((radiator, r), (water, w)):
                if mode_state is not None:
                    changed = _timestamp(getattr(mode_state, "last_changed", None))
                    if mode_report is None or changed is None or changed > h[0]:
                        h = None
                        break
        if h is not None and c is not None:
            changed = _timestamp(getattr(compressor, "last_changed", None))
            if changed is None or changed > h[0]:
                h = None
        p = PowerLedger._value(power, now, power=True, started_after=reported_after)
        f = PilotPassive._report(
            supply, now, temperature=True, reported_after=reported_after
        )
        ret = PilotPassive._report(
            return_temp, now, temperature=True, reported_after=reported_after
        )
        # Compare existing Celsius reports only within one controller minute.
        # This bounds report skew, not unknown physical measurement skew.
        paired = (
            f is not None
            and ret is not None
            and abs(f[0] - ret[0]) <= timedelta(seconds=60)
        )
        if paired:
            delta = f[1] - ret[1]
            result["delta_t_c"] = round(delta, 3)
            result["delta_t_status"] = (
                "positive_transfer_indication" if delta > 0 else "no_positive_gradient"
            )
        elif f is not None and ret is not None:
            result["delta_t_status"] = "reports_not_time_aligned"
        result["signals"] = [
            name
            for name, value in (
                ("heat", h),
                ("compressor", c),
                ("radiator", r),
                ("hot_water", w),
                ("power", p),
                ("supply", f),
                ("return", ret),
            )
            if value is not None
        ]
        result["reporting"] = {}
        for name, state in (
            ("heat", heat),
            ("compressor", compressor),
            ("radiator", radiator),
            ("hot_water", water),
            ("power", power),
            ("supply", supply),
            ("return", return_temp),
        ):
            if state is None:
                continue
            if (getattr(state, "attributes", {}) or {}).get("restored"):
                result["reporting"][name] = "restored_unqualified"
                continue
            reported = _timestamp(getattr(state, "last_reported", None))
            updated = _timestamp(getattr(state, "last_updated", None))
            if reported is not None and updated is not None and reported > updated:
                self.repeated_sources.add(name)
            result["reporting"][name] = (
                "repeated_reports_observed"
                if name in self.repeated_sources
                else "unknown"
            )
        water_active = w is not None and w[1] == 1
        conflict = (
            h is not None
            and h[1] == 1
            and ((r is not None and r[1] == 0) or (c is not None and c[1] == 0))
        )
        if water_active:
            # A selected water circuit does not prove the compressor is heating.
            result.update(
                mode="hot_water",
                quality="probable",
                reason="hot_water_mode_report_not_production",
            )
        elif conflict:
            pass
        elif h is not None and (water is None or w is not None):
            if h[1] == 1 and (radiator is None or (r is not None and r[1] == 1)):
                result.update(
                    mode="space_heating", quality="verified", learning_qualified=True
                )
            elif h[1] == 0 and c is not None and c[1] == 0:
                result.update(mode="idle", quality="probable", learning_qualified=True)
            elif h[1] == 0:
                result["learning_qualified"] = True

        # Only a fresh independent heat label and a fresh matching compressor
        # can label power; water status and radiator mode must be explicit.
        if (
            h
            and c
            and r
            and w
            and p
            and w[1] == 0
            and r[1] == 1
            and h[1] == c[1]
            and (
                self.last_sample is None
                or p[0] - self.last_sample >= timedelta(minutes=5)
            )
            and all(
                (changed := _timestamp(getattr(state, "last_changed", None)))
                is not None
                and changed <= p[0] - timedelta(minutes=1)
                for state in (heat, compressor, radiator, water)
            )
        ):
            key = "heating" if h[1] else "idle"
            self.samples[key].append(p[2])
            self.samples[key] = self.samples[key][-24:]
            self.last_sample = p[0]
            self.sample_count += 1
            self.fresh_labels.add(key)
        if (
            result["learning_qualified"]
            and h
            and h[1] == 1
            and (compressor is None or c is not None)
            and (radiator is None or (r and r[1] == 1))
            and (water is None or (w and w[1] == 0))
        ):
            changes = [
                _timestamp(getattr(state, "last_changed", None))
                for state in (heat, compressor, radiator, water)
                if state is not None
            ]
            if all(change is not None for change in changes):
                latest_transition = max(changes)
                for name, reading in (("supply", f), ("return", ret)):
                    previous = self.last_temperature_samples[name]
                    if (
                        reading is not None
                        and reading[0] >= latest_transition + timedelta(minutes=1)
                        and (
                            previous is None
                            or reading[0] - previous >= timedelta(minutes=5)
                        )
                    ):
                        self.temperatures[name].append(reading[1])
                        self.temperatures[name] = self.temperatures[name][-24:]
                        self.last_temperature_samples[name] = reading[0]
                        self.fresh_temperatures.add(name)
                        self.sample_count += 1
        idle, heating = self.samples["idle"], self.samples["heating"]
        if (
            len(idle) >= 3
            and len(heating) >= 3
            and max(idle) < min(heating)
            and self.fresh_labels == {"idle", "heating"}
        ):
            result["calibration"] = "ready"
            threshold = (median(idle) + median(heating)) / 2
            if (
                result["mode"] == "unknown"
                and not conflict
                and not water_active
                and p
                and c
                and r
                and w
                and w[1] == 0
                and r[1] == 1
                and all(
                    (changed := _timestamp(getattr(state, "last_changed", None)))
                    is not None
                    and changed <= p[0] - timedelta(minutes=1)
                    for state in (compressor, radiator, water)
                )
            ):
                if c[1] == 1 and p[2] > threshold and paired and f[1] > ret[1]:
                    bounds = self.temperatures
                    if (
                        all(
                            (
                                changed := _timestamp(
                                    getattr(state, "last_changed", None)
                                )
                            )
                            is not None
                            and changed <= min(f[0], ret[0]) - timedelta(minutes=1)
                            for state in (compressor, radiator, water)
                        )
                        and len(bounds["supply"]) >= 3
                        and len(bounds["return"]) >= 3
                        and self.fresh_temperatures == {"supply", "return"}
                        and min(bounds["supply"]) <= f[1] <= max(bounds["supply"])
                        and min(bounds["return"]) <= ret[1] <= max(bounds["return"])
                    ):
                        result.update(mode="space_heating", quality="probable")
                elif c[1] == 0 and p[2] < threshold:
                    result.update(mode="idle", quality="probable")
        # Record an operating indication for diagnostics when the independent
        # heat label is absent. These coincident signals cannot prove heat
        # delivery and must never qualify a Pilot interval.
        if (
            result["mode"] == "unknown"
            and result["calibration"] == "insufficient_evidence"
            and h is None
            and c is not None
            and c[1] == 1
            and r is not None
            and r[1] == 1
            and w is not None
            and w[1] == 0
            and p is not None
            and paired
            and f[1] > ret[1]
            and all(
                (changed := _timestamp(getattr(state, "last_changed", None)))
                is not None
                and changed
                <= min(c[0], r[0], w[0], p[0], f[0], ret[0])
                - timedelta(minutes=1)
                for state in (compressor, radiator, water)
            )
        ):
            result.update(
                mode="indicated_radiator_heating",
                quality="indication_only",
                evidence_level="operating_indication",
                evidence_uncertainty="heat_delivery_unverified",
            )

        return result
