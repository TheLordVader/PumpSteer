"""Pure-Python thermal model core (stdlib only, no Home Assistant imports)."""

from __future__ import annotations

from .base import (
    ForecastPoint,
    ModelConfig,
    PreheatEstimate,
    QualityReport,
    Sample,
    ThermalModel,
    ThermalParameters,
)
from .model import RCThermalModel
from .shared_prior import GroupPrior, PeerSummary, build_group_prior


def create_model(config: ModelConfig | None = None) -> ThermalModel:
    """Factory used by the integration (single backend for now)."""
    return RCThermalModel(config or ModelConfig())


__all__ = [
    "ForecastPoint",
    "GroupPrior",
    "ModelConfig",
    "PeerSummary",
    "PreheatEstimate",
    "QualityReport",
    "RCThermalModel",
    "Sample",
    "ThermalModel",
    "ThermalParameters",
    "build_group_prior",
    "calibration_factor",
    "create_model",
]
