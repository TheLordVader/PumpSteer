"""Passive heat-pump fault notifications with restart-safe deduplication."""

from datetime import timedelta

from .utils import safe_float


def _numeric(value):
    """Accept finite numeric values only; booleans are not fault codes."""
    return None if isinstance(value, bool) else safe_float(value)


class HeatPumpAlarm:
    def __init__(
        self,
        entity,
        restored=None,
        started_at=None,
        *,
        numeric_verified=False,
        numeric_normal=None,
    ):
        self.numeric = isinstance(entity, str) and entity.startswith("sensor.")
        self.numeric_normal = _numeric(numeric_normal) if self.numeric else None
        self.numeric_verified = (
            self.numeric
            and numeric_verified is True
            and self.numeric_normal is not None
        )
        if not self.numeric_verified:
            self.numeric_normal = None
        saved = (
            restored
            if isinstance(restored, dict) and restored.get("entity") == entity
            else {}
        )
        if self.numeric and not (
            self.numeric_verified
            and saved.get("numeric_verified") is True
            and _numeric(saved.get("numeric_normal")) == self.numeric_normal
        ):
            saved = {}
        self.entity = entity
        self.active = saved.get("active") is True
        self.sent = saved.get("sent") is True
        self.recovery_pending = saved.get("recovery_pending") is True
        self.persistent = saved.get("persistent") is True
        self.generation = 0
        self.started_at = started_at if saved else None
        self.status = "unknown"

    def matches_config(self, entity, *, numeric_verified=False, numeric_normal=None):
        """Reset an episode when its source or verified interpretation changes."""
        if self.entity != entity:
            return False
        if not self.numeric:
            return True
        normal = _numeric(numeric_normal)
        verified = numeric_verified is True and normal is not None
        return verified == self.numeric_verified and (
            not verified or normal == self.numeric_normal
        )

    def observe(self, now, state):
        attrs = getattr(state, "attributes", {}) or {}
        report = getattr(state, "last_reported", None)
        raw = getattr(state, "state", None)
        if self.numeric:
            value = _numeric(raw) if self.numeric_verified else None
            alarm = value != self.numeric_normal if value is not None else None
        else:
            alarm = True if raw == "on" else False if raw == "off" else None
        try:
            valid = (
                state is not None
                and alarm is not None
                and not attrs.get("restored")
                and report is not None
                and report.utcoffset() is not None
                and 0
                <= now.timestamp() - report.timestamp()
                <= timedelta(minutes=60).total_seconds()
                and (
                    self.started_at is None
                    or alarm is True
                    or report.timestamp() > self.started_at.timestamp()
                )
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            valid = False
        event = None
        self.status = "unknown"
        if valid:
            # A pre-start active report may raise/deduplicate an alarm, but it
            # does not qualify a later pre-start value as a recovery.
            if (
                self.started_at is not None
                and report.timestamp() > self.started_at.timestamp()
            ):
                self.started_at = None
            self.status = "alarm" if alarm else "normal"
            if alarm:
                if not self.active:
                    self.generation += 1
                    self.active, self.sent = True, False
                self.recovery_pending = False
                if not self.sent:
                    event = "alarm"
            else:
                if self.active:
                    self.active = False
                    self.recovery_pending = self.sent
                    self.sent = False
                if self.recovery_pending:
                    event = "recovery"
        result = {
            "entity": self.entity,
            "status": self.status,
            "active": self.active,
            "sent": self.sent,
            "persistent": self.persistent,
            "recovery_pending": self.recovery_pending,
        }
        if self.numeric:
            result.update(
                numeric_verified=self.numeric_verified,
                numeric_normal=self.numeric_normal if self.numeric_verified else None,
            )
        return result, event

    def current(self, event):
        return self.status != "unknown" and (
            self.active if event == "alarm" else self.recovery_pending
        )

    def mark_sent(self, event):
        self.mark_delivered(event, self.generation)

    def mark_delivered(self, event, generation):
        """Acknowledge delivery only for the alarm cycle that initiated it."""
        if generation != self.generation:
            return False
        if event == "alarm":
            if self.active:
                self.sent = True
            else:
                self.recovery_pending = True
        elif event == "recovery" and not self.active:
            self.recovery_pending = False
        else:
            return False
        return True
