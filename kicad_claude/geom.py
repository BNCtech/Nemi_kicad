"""Shared geometry helpers. Single source of truth for grid + snap so every
module agrees on the KiCad schematic pitch. The pitch itself comes from
conventions.json — never hardcode it.
"""

from typing import Tuple

from ._config_loader import load as _load_config


def grid_mm() -> float:
    """Schematic grid step in mm (KiCad default 1.27)."""
    return float(_load_config("conventions")["grid"]["schematic_mm"])


def snap(v: float) -> float:
    """Snap a single coordinate to the schematic grid."""
    g = grid_mm()
    return round(float(v) / g) * g


def snap_xy(x: float, y: float) -> Tuple[float, float]:
    """Snap an (x, y) pair to the schematic grid."""
    return snap(x), snap(y)
