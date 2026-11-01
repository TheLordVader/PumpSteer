"""Passive binary heat-pump fault notifications with restart-safe deduplication."""

from datetime import timedelta


class HeatPumpAlarm:
    def __init__(self, entity, restored=None, started_at=None):
        saved = (
            restored
            if isinstance(restored, dict) and restored.get("entity") == entity
            else {}
        )
        self.entity = entity
        self.active = saved.get("active") is True
        self.sent = saved.get("sent") is True
        self.recovery_pending = saved.get("recovery_pending") is True
        self.generation = 0
        self.started_at = started_at if saved else None
        self.status = "unknown"

    def observe(self, now, state):
        attrs = getattr(state, "attributes", {}) or {}
        report = getattr(state, "last_reported", None)
        try:
            valid = (
                state is not None
                and state.state in ("on", "off")
                and not attrs.get("restored")
                and report is not None
                and report.utcoffset() is not None
                and 0
                <= now.timestamp() - report.timestamp()
                <= timedelta(minutes=60).total_seconds()
                and (
                    self.started_at is None
                    or report.timestamp() > self.started_at.timestamp()
                )
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            valid = False
        event = None
        self.status = "unknown"
        if valid:
            self.started_at = None
            self.status = "alarm" if state.state == "on" else "normal"
            if state.state == "on":
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
        return {
            "entity": self.entity,
            "status": self.status,
            "active": self.active,
            "sent": self.sent,
            "recovery_pending": self.recovery_pending,
        }, event

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
