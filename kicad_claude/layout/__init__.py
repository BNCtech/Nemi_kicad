"""Universal KiCad schematic layout engine.

Sibling to kicad_claude/ (repair/analysis); layout *generation* lives here.
Every threshold, weight, and naming convention is in layout_config.json —
no circuit names or component-specific logic in Python.

Build order (each step is a thin module that consumes the previous step's
artifact):
  1. connectivity_graph  — components + nets -> networkx graph + importance
  2. classifier          — graph -> functional roles per node
  3. placer              — roles + graph -> sheet zones and positions
  4. router              — placements -> wires + net labels
  5. sheet_generator     — large designs -> hierarchical sheets
"""
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

_PKG_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load_config(name: str = "layout_config") -> Dict[str, Any]:
    """Load a JSON config from the package directory by basename (no .json).
    Cached so callers can use it inside hot loops without re-reading disk."""
    with open(_PKG_DIR / f"{name}.json", "r", encoding="utf-8") as f:
        return json.load(f)


def reload_configs() -> None:
    """Drop the cache so edits to layout_config.json take effect without a
    process restart. Used by tests and hot-edit workflows."""
    load_config.cache_clear()
