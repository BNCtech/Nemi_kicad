"""Minimal `.kicad_sch` reader.

First-pass surface for the rebuild: load a schematic, count its top-level
entities, and pull a per-component summary (reference, value, lib_id,
position). Editing operations (apply_op, write back) land in a follow-up
pass — kept out of this file so the read path stays trivially auditable.

Parser is sexpdata, already in requirements.txt. We deliberately do NOT
recreate the 40 KB schematic_modifier.py here; git commit 9191899 has it
for reference if a specific edit primitive needs to be ported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

import sexpdata


def _head(node: Any) -> Optional[str]:
    """Return the leading symbol of an s-expr list, or None."""
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _prop(symbol_node: list, name: str) -> Optional[str]:
    """Read a (property "name" "value" ...) string out of a (symbol ...) node."""
    for child in symbol_node[1:]:
        if isinstance(child, list) and _head(child) == "property":
            if len(child) >= 3 and str(child[1]) == name:
                return str(child[2])
    return None


def _at(symbol_node: list) -> Optional[tuple]:
    """Return (x, y, rot) from the first (at ...) child, or None."""
    for child in symbol_node[1:]:
        if isinstance(child, list) and _head(child) == "at":
            xs = [float(v) for v in child[1:] if isinstance(v, (int, float))]
            if len(xs) >= 2:
                return (xs[0], xs[1], xs[2] if len(xs) > 2 else 0.0)
    return None


@dataclass
class ComponentRow:
    reference: str
    value: str
    lib_id: str
    x: float
    y: float
    rot: float
    # Owning sheet file (basename). Empty for a flat single-file read;
    # populated by the hierarchy-aware deep reader so callers know which
    # child .kicad_sch a component actually lives on. Additive default —
    # nothing that builds a ComponentRow positionally has to change.
    sheet: str = ""


@dataclass
class SchematicSummary:
    """What's on a .kicad_sch sheet, at a glance."""
    path: str
    component_count: int
    wire_count: int
    label_count: int          # plain `label` only
    global_label_count: int
    hierarchical_label_count: int
    components: List[ComponentRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "totals": {
                "components": self.component_count,
                "wires": self.wire_count,
                "labels": self.label_count,
                "global_labels": self.global_label_count,
                "hierarchical_labels": self.hierarchical_label_count,
            },
            "components": [
                {
                    "ref": c.reference,
                    "value": c.value,
                    "lib_id": c.lib_id,
                    "pos": [c.x, c.y, c.rot],
                }
                for c in self.components
            ],
        }


def read_summary(path: str | Path) -> SchematicSummary:
    """Parse a .kicad_sch and return its summary. Raises if the file is
    missing or unparseable — callers (tools) should let it propagate so the
    agent sees a real error instead of an empty result."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    root = sexpdata.loads(text)
    if not isinstance(root, list) or _head(root) != "kicad_sch":
        raise ValueError(f"{p} is not a kicad_sch s-expression root")

    components: List[ComponentRow] = []
    wires = labels = glabels = hlabels = 0

    for node in root[1:]:
        if not isinstance(node, list):
            continue
        h = _head(node)
        if h == "symbol":
            ref = _prop(node, "Reference") or ""
            if ref.startswith("#"):       # power-port stub like #PWR01 — skip
                continue
            val = _prop(node, "Value") or ""
            lib_id = ""
            for child in node[1:]:
                if isinstance(child, list) and _head(child) == "lib_id" and len(child) >= 2:
                    lib_id = str(child[1])
                    break
            pos = _at(node) or (0.0, 0.0, 0.0)
            components.append(ComponentRow(ref, val, lib_id, pos[0], pos[1], pos[2]))
        elif h == "wire":
            wires += 1
        elif h == "label":
            labels += 1
        elif h == "global_label":
            glabels += 1
        elif h == "hierarchical_label":
            hlabels += 1

    return SchematicSummary(
        path=str(p),
        component_count=len(components),
        wire_count=wires,
        label_count=labels,
        global_label_count=glabels,
        hierarchical_label_count=hlabels,
        components=components,
    )
