"""Prior for a poorly fitted room built from well fitted rooms on the same boiler zone.

Rooms heated by the same boiler zone use similar radiators, so the heating rate ``a`` and
the loss rate ``b`` of a room that has too little data can borrow the median of its
mature peers instead of the generic radiator defaults. The result is only a ridge prior
for :func:`fit_rc` (the room's own data still wins as it accumulates); ``c`` and the dead
time are never shared because they depend on the room's gains and pipework.

Evidence (see the README, "Zone groups"): on real data the shared prior did not measurably
improve the forecast over the default prior, because no room had a trustworthy fit to
share. It is therefore opt-in, and a peer only counts once its own maturity is at least
:data:`MIN_PEER_MATURITY`.

Pure standard library: no Home Assistant imports.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import median
from typing import Any

from .fit import Prior

MIN_PEER_MATURITY = 0.3  # a peer must be at least this mature to lend its parameters
MAX_OWN_MATURITY = 0.3  # a room at or above this keeps learning from its own data only
HOURS_PER_PEER = 12.0  # prior weight (hours of data) per peer ...
MAX_PRIOR_HOURS = 24.0  # ... capped here


@dataclass(frozen=True)
class PeerSummary:
    """Trusted parameter summary of one member of a zone group."""

    name: str
    a: float
    b: float
    maturity: float


@dataclass(frozen=True)
class GroupPrior:
    """Immutable prior borrowed from peers (safe to hand to an executor job)."""

    a: float
    b: float
    hours: float
    peers: tuple[str, ...]

    def as_prior(self) -> Prior:
        """The ridge prior for the fit (``c`` keeps its default)."""
        return Prior(a=self.a, b=self.b, hours=self.hours)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe description recorded in the fit summary."""
        return {
            "source": "group",
            "a": round(self.a, 4),
            "b": round(self.b, 4),
            "hours": self.hours,
            "peers": list(self.peers),
        }


DEFAULT_PRIOR_INFO: dict[str, Any] = {"source": "default", "peers": []}


def is_usable_peer(peer: PeerSummary, min_maturity: float = MIN_PEER_MATURITY) -> bool:
    """A peer lends its parameters when mature enough and physically sensible."""
    return (
        peer.maturity >= min_maturity
        and math.isfinite(peer.a)
        and math.isfinite(peer.b)
        and peer.a > 0.0
        and peer.b > 0.0
    )


def build_group_prior(
    peers: Iterable[PeerSummary], min_maturity: float = MIN_PEER_MATURITY
) -> GroupPrior | None:
    """Median ``a`` and ``b`` of the usable peers, or None when there are none."""
    usable = sorted((p for p in peers if is_usable_peer(p, min_maturity)), key=lambda p: p.name)
    if not usable:
        return None
    return GroupPrior(
        a=float(median(p.a for p in usable)),
        b=float(median(p.b for p in usable)),
        hours=min(MAX_PRIOR_HOURS, HOURS_PER_PEER * len(usable)),
        peers=tuple(p.name for p in usable),
    )
