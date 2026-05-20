"""Post-emit quality checks for the kicad_layout pipeline.

Runs after Step 5 (emitter) on the generated .kicad_sch + the placement /
routed JSONs. Produces a structured `[Issue]` list with severity codes,
machine-readable detail, and a one-line human summary. The pipeline writes
this to `quality.json` next to the output schematic.

Severity ladder:
  - "error":   would break ERC or render incorrectly in eeschema
  - "warning": real visual issue but won't block the file from loading
  - "info":    informational/diagnostic (component counts, etc.)

Each check is a small pure function over (placement, routed, doc_tree).
New checks compose by appending to the `_CHECKS` registry — no caller
changes needed.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    from ai_backend.kicad_claude.schematic_modifier import (
        SchematicDocument, _head, _to_str,
    )
except ImportError:
    from kicad_claude.schematic_modifier import (  # type: ignore
        SchematicDocument, _head, _to_str,
    )


@dataclass
class Issue:
    severity: str   # "error" | "warning" | "info"
    code: str       # short identifier — LAYOUT_001, WIRE_002, etc.
    message: str    # human-readable
    detail: Dict[str, Any]  # machine-readable context (ref, coords, ...)


def _check_off_grid_components(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
    grid_mm: float = 1.27,
) -> List[Issue]:
    out: List[Issue] = []
    for c in placement.get("components", []):
        x = float(c.get("x_mm", 0.0))
        y = float(c.get("y_mm", 0.0))
        rx = round(x / grid_mm) * grid_mm
        ry = round(y / grid_mm) * grid_mm
        if abs(rx - x) > 0.005 or abs(ry - y) > 0.005:
            out.append(Issue(
                severity="warning", code="LAYOUT_001",
                message=f"{c['ref']} off-grid at ({x:.3f}, {y:.3f})",
                detail={"ref": c["ref"], "x_mm": x, "y_mm": y,
                        "grid_mm": grid_mm},
            ))
    return out


def _check_wire_overlaps(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
) -> List[Issue]:
    """After dedup_collinear_wires has run, any remaining identical wires
    are real duplicates the dedup pass missed (cross-net containment is
    NOT removed by design)."""
    seen: Dict[Tuple, int] = defaultdict(int)
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        pts_node = next((s for s in child[1:]
                         if isinstance(s, list) and _head(s) == "pts"), None)
        if not pts_node:
            continue
        coords = []
        for xy in pts_node[1:]:
            if isinstance(xy, list) and _head(xy) == "xy" and len(xy) >= 3:
                coords.append((round(float(xy[1]), 3), round(float(xy[2]), 3)))
        if len(coords) != 2:
            continue
        key = frozenset(coords)
        seen[key] += 1
    out: List[Issue] = []
    for key, count in seen.items():
        if count > 1:
            out.append(Issue(
                severity="warning", code="WIRE_001",
                message=f"{count} duplicate wires at {sorted(key)}",
                detail={"count": count, "endpoints": sorted(key)},
            ))
    return out


def _check_off_sheet_components(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
) -> List[Issue]:
    sheet = placement.get("sheet") or {}
    w = float(sheet.get("width_mm", 297.0))
    h = float(sheet.get("height_mm", 210.0))
    out: List[Issue] = []
    for c in placement.get("components", []):
        x = float(c.get("x_mm", 0.0))
        y = float(c.get("y_mm", 0.0))
        if x < 0 or x > w or y < 0 or y > h:
            out.append(Issue(
                severity="error", code="LAYOUT_002",
                message=f"{c['ref']} placed off-sheet at ({x:.1f}, {y:.1f}); sheet is {w:.0f}×{h:.0f}",
                detail={"ref": c["ref"], "x_mm": x, "y_mm": y,
                        "sheet_w": w, "sheet_h": h},
            ))
    return out


def _check_wire_through_body(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
) -> List[Issue]:
    """Count emitted wires whose axis-aligned segment passes through ANY
    placed component's body bbox (excluding endpoints sitting on the body
    edge — a pin tip is legitimately ON the body). Surfaces leftover
    crossings the body-aware ray-cast couldn't avoid.

    Universal: scans every wire vs every body. O(W × B), typically
    <500 × <100 = manageable for any sheet size."""
    # Collect body bboxes by ref from placement.
    bboxes: List[Tuple[str, Tuple[float, float, float, float]]] = []
    try:
        from .label_placer import _build_body_bbox_by_ref  # type: ignore
    except Exception:
        return []
    schematic_path = placement.get("_schematic_source") or None
    if schematic_path is None:
        # Fall back to using doc's path if available
        schematic_path = getattr(doc, "path", None)
    if not schematic_path:
        return []
    try:
        by_ref = _build_body_bbox_by_ref(placement, schematic_path)
    except Exception:
        return []
    bboxes = list(by_ref.items())

    crossings = 0
    sample: List[Dict[str, Any]] = []
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        # Extract 2 endpoints
        pts = None
        for s in child[1:]:
            if isinstance(s, list) and _head(s) == "pts":
                pts = s
                break
        if not pts or len(pts) < 3:
            continue
        try:
            x1 = float(pts[1][1]); y1 = float(pts[1][2])
            x2 = float(pts[2][1]); y2 = float(pts[2][2])
        except (IndexError, ValueError, TypeError):
            continue
        for ref, bb in bboxes:
            xmin, ymin, xmax, ymax = bb
            # Skip degenerate bbox (zero-extent symbols)
            if xmax - xmin < 0.1 or ymax - ymin < 0.1:
                continue
            # Vertical
            if abs(x1 - x2) < 0.01:
                if not (xmin + 0.01 < x1 < xmax - 0.01):
                    continue
                lo, hi = min(y1, y2), max(y1, y2)
                if hi <= ymin + 0.01 or lo >= ymax - 0.01:
                    continue
            elif abs(y1 - y2) < 0.01:
                if not (ymin + 0.01 < y1 < ymax - 0.01):
                    continue
                lo, hi = min(x1, x2), max(x1, x2)
                if hi <= xmin + 0.01 or lo >= xmax - 0.01:
                    continue
            else:
                continue
            crossings += 1
            if len(sample) < 5:
                sample.append({"ref_crossed": ref,
                               "wire": [[x1, y1], [x2, y2]]})
            break  # one cross per wire is enough
    if crossings:
        return [Issue(
            severity="warning", code="WIRE_002",
            message=f"{crossings} wire(s) cross component bodies",
            detail={"count": crossings, "sample": sample},
        )]
    return []


def _check_unplaced_pins(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
) -> List[Issue]:
    skipped = routed.get("stats", {}).get("skipped_pins", []) or []
    out: List[Issue] = []
    if skipped:
        out.append(Issue(
            severity="warning", code="ROUTE_001",
            message=f"{len(skipped)} pin(s) skipped by router",
            detail={"count": len(skipped), "sample": skipped[:5]},
        ))
    return out


def _check_component_density(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
) -> List[Issue]:
    comps = placement.get("components", [])
    return [Issue(
        severity="info", code="STATS_001",
        message=f"{len(comps)} components, {len(routed.get('net_labels', []))} labels, "
                f"{len(routed.get('power_ports', []))} power ports",
        detail={
            "components":  len(comps),
            "labels":      len(routed.get("net_labels", [])),
            "power_ports": len(routed.get("power_ports", [])),
            "blocks":      len(placement.get("blocks", [])),
        },
    )]


_CHECKS: List[Callable[..., List[Issue]]] = [
    _check_off_grid_components,
    _check_off_sheet_components,
    _check_wire_overlaps,
    _check_wire_through_body,
    _check_unplaced_pins,
    _check_component_density,
]


def run_quality(
    placement: Dict[str, Any], routed: Dict[str, Any], schematic_path,
) -> Dict[str, Any]:
    """Execute every registered check and return a summary dict suitable
    for JSON serialisation."""
    doc = SchematicDocument(schematic_path)
    issues: List[Issue] = []
    for check in _CHECKS:
        try:
            issues.extend(check(placement, routed, doc))
        except Exception as e:  # never let a check crash the pipeline
            issues.append(Issue(
                severity="warning", code="CHECK_FAILED",
                message=f"{check.__name__} raised {type(e).__name__}: {e}",
                detail={"check": check.__name__, "error": str(e)},
            ))
    by_sev: Dict[str, int] = defaultdict(int)
    for it in issues:
        by_sev[it.severity] += 1
    return {
        "schematic": str(schematic_path),
        "totals": dict(by_sev),
        "issues": [asdict(it) for it in issues],
    }


def write_quality_json(result: Dict[str, Any], out_path) -> str:
    p = Path(out_path)
    p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return str(p)
