"""Bounded observed episode context, isolated from heating control and I/O."""

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone

THERMAL_MODES = (
    "normal",
    "preheating",
    "pre_braking",
    "braking",
    "brake_hold",
    "recovery",
    "precool",
    "safe_mode",
    "sensor_interruption",
    "unknown",
)
HEATING_KINDS = ("known", "estimated", "unknown")


def utc_time(value: datetime) -> datetime | None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        return None
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ThermalEpisode:
    run_id: str
    epoch: int
    number: int
    started_at: datetime
    observed_until: datetime
    ended_at: datetime | None
    mode: str
    source_mode: str
    heating_kind: str
    boundary_reason: str

    def contains(self, at: datetime) -> bool:
        at = utc_time(at)
        return (
            at is not None
            and self.started_at <= at <= self.observed_until
            and (self.ended_at is None or at < self.ended_at)
        )


class EpisodeTracker:
    """Only supplied mode/heat evidence; no inferred power or recovery duration.

    Source timestamps describe observed operating context, not polling substitutes.
    More than five minutes without context creates an explicit interruption.
    Model coefficient updates do not define physical episode boundaries.
    """

    def __init__(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id or len(run_id) > 64:
            raise ValueError("Supply a nonempty run identity of at most 64 characters")
        self._run_id = run_id
        self._epoch = 0
        self._number = 0
        self._history: deque[ThermalEpisode] = deque(maxlen=32)
        self.current: ThermalEpisode | None = None
        self._last: datetime | None = None
        self.reason = "start"

    @property
    def history(self) -> tuple[ThermalEpisode, ...]:
        return tuple(self._history)

    def _close(self, at: datetime) -> None:
        if self.current is not None:
            self._history.append(replace(self.current, ended_at=at))
            self.current = None

    def _start(
        self, at: datetime, mode: str, source: str, kind: str, reason: str
    ) -> ThermalEpisode:
        self._number += 1
        self.current = ThermalEpisode(
            self._run_id,
            self._epoch,
            self._number,
            at,
            at,
            None,
            mode,
            source,
            kind,
            reason,
        )
        self.reason = reason
        return self.current

    def observe(
        self,
        at: datetime,
        mode: str,
        *,
        heating_kind: str = "unknown",
        sensor_valid: bool = True,
        brake_releasing: bool = False,
    ) -> ThermalEpisode | None:
        at = utc_time(at)
        invalid_flags = not isinstance(sensor_valid, bool) or not isinstance(
            brake_releasing, bool
        )
        if at is None or heating_kind not in HEATING_KINDS or invalid_flags:
            if self._last is not None:
                self._close(self._last)
            self._last = None
            self.reason = (
                "invalid_timestamp"
                if at is None
                else ("invalid_context" if invalid_flags else "invalid_heating_kind")
            )
            return None
        source = (
            mode if isinstance(mode, str) and 0 < len(mode) <= 40 else "unrecognized"
        )
        category = source if source in THERMAL_MODES else "unknown"
        if source != "safe_mode" and not sensor_valid:
            category = "sensor_interruption"
        elif source == "normal" and brake_releasing:
            category = "recovery"
        if self._last is not None and at < self._last:
            self._close(self._last)
            self._epoch += 1
            self._last = None
            self.reason = "clock_reset"
            return None
        signature = (category, source, heating_kind)
        previous = (
            (self.current.mode, self.current.source_mode, self.current.heating_kind)
            if self.current
            else None
        )
        if at == self._last:
            if signature == previous:
                return self.current
            self._close(at)
            self.reason = "conflicting_timestamp"
            return None
        gap = self._last is not None and (at - self._last).total_seconds() > 300
        if gap:
            self._close(self._last)
            self._start(
                self._last,
                "sensor_interruption",
                "unobserved",
                "unknown",
                "observation_gap",
            )
            self.current = replace(self.current, observed_until=at)
            self._close(at)
        if gap or previous != signature:
            boundary = (
                "observation_gap"
                if gap
                else (self.reason if self.current is None else "context_changed")
            )
            self._close(at)
            self._start(
                at,
                category,
                source,
                heating_kind,
                boundary,
            )
        elif self.current is None:
            self._start(at, category, source, heating_kind, self.reason)
        else:
            self.current = replace(self.current, observed_until=at)
        self._last = at
        return self.current

    def episode_at(self, at: datetime) -> ThermalEpisode | None:
        at = utc_time(at)
        if at is None:
            return None
        for episode in (self.current, *reversed(self._history)):
            if (
                episode is not None
                and episode.epoch == self._epoch
                and episode.contains(at)
            ):
                return episode
        return None


def episode_exposure(
    origin: ThermalEpisode | None, target: ThermalEpisode | None, at: datetime | None
) -> str:
    """Observed continuity only; missing context and different runs stay unknown."""
    if origin is None or target is None or at is None or not target.contains(at):
        return "unknown"
    if (origin.run_id, origin.epoch) != (target.run_id, target.epoch):
        return "unknown"
    return "observed_same_episode" if origin.number == target.number else "transitioned"
