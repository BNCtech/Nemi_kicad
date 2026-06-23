"""Minimal `.kicad_pcb` reader.

Mirror of `document.py` (which handles `.kicad_sch`). One pass over the
s-expression tree, returning a small `PCBSummary` dataclass that callers
can serialise for the chat UI.

Counts footprints, tracks, vias, zones; detects whether an Edge.Cuts
outline exists; flags which copper layers carry tracks. Pure read —
no edits, no LLM, no heuristics. Used by the chat backend to produce
the on-open page-summary card.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import sexpdata


def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _prop(footprint_node: list, name: str) -> Optional[str]:
    for child in footprint_node[1:]:
        if (isinstance(child, list) and _head(child) == "property"
                and len(child) >= 3 and str(child[1]) == name):
            return str(child[2])
    return None


def _layer_of(node: list) -> Optional[str]:
    for child in node[1:]:
        if (isinstance(child, list) and _head(child) == "layer"
                and len(child) >= 2):
            return str(child[1])
    return None


def _at(node: list) -> tuple:
    for child in node[1:]:
        if isinstance(child, list) and _head(child) == "at":
            try:
                x = float(child[1]); y = float(child[2])
                r = float(child[3]) if len(child) > 3 else 0.0
                return (x, y, r)
            except (IndexError, TypeError, ValueError):
                return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


@dataclass
class FootprintRow:
    reference: str
    value: str
    library: str
    layer: str
    x: float
    y: float
    rot: float


@dataclass
class PCBSummary:
    """What's on a .kicad_pcb, at a glance."""
    path: str
    footprint_count: int
    track_count: int
    via_count: int
    zone_count: int
    has_edge_cuts: bool
    layers_used: List[str]
    footprints: List[FootprintRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "totals": {
                "footprints": self.footprint_count,
                "tracks":     self.track_count,
                "vias":       self.via_count,
                "zones":      self.zone_count,
            },
            "has_edge_cuts": self.has_edge_cuts,
            "layers_used":   self.layers_used,
            "footprints": [
                {
                    "ref": f.reference, "value": f.value,
                    "library": f.library, "layer": f.layer,
                    "pos": [f.x, f.y, f.rot],
                } for f in self.footprints
            ],
        }


def read_pcb_summary(path: str | Path) -> PCBSummary:
    """Parse a .kicad_pcb and return its summary.

    Raises FileNotFoundError if the file doesn't exist, ValueError if it
    isn't a kicad_pcb root. Callers (chat backend) catch both and present
    a useful empty-state message rather than crashing the WebSocket turn.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    root = sexpdata.loads(text)
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        raise ValueError(f"{p} is not a kicad_pcb s-expression root")

    footprints: List[FootprintRow] = []
    tracks = vias = zones = 0
    has_edge_cuts = False
    layers_used: set = set()

    for node in root[1:]:
        if not isinstance(node, list):
            continue
        h = _head(node)
        if h == "footprint":
            # Footprint library reference is the first leading atom after
            # 'footprint' (e.g. "Resistor_SMD:R_0603_1608Metric").
            lib = ""
            for child in node[1:]:
                if isinstance(child, str):
                    lib = child
                    break
                if (isinstance(child, sexpdata.Symbol)
                        and ":" in child.value()):
                    lib = child.value()
                    break
            ref = _prop(node, "Reference") or ""
            val = _prop(node, "Value") or ""
            layer = _layer_of(node) or ""
            x, y, r = _at(node)
            footprints.append(FootprintRow(ref, val, lib, layer, x, y, r))
        elif h == "segment":
            tracks += 1
            layer = _layer_of(node)
            if layer:
                layers_used.add(layer)
        elif h == "via":
            vias += 1
        elif h == "zone":
            zones += 1
        elif h in ("gr_line", "gr_arc", "gr_rect", "gr_poly", "gr_circle"):
            if _layer_of(node) == "Edge.Cuts":
                has_edge_cuts = True

    return PCBSummary(
        path=str(p),
        footprint_count=len(footprints),
        track_count=tracks,
        via_count=vias,
        zone_count=zones,
        has_edge_cuts=has_edge_cuts,
        layers_used=sorted(layers_used),
        footprints=footprints,
    )
