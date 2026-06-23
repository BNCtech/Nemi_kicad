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


def _all_pin_tips(doc: SchematicDocument) -> List[Tuple[float, float]]:
    """Every world-space pin endpoint across every placed symbol."""
    out: List[Tuple[float, float]] = []
    for child in doc.tree[1:]:
        if isinstance(child, list) and _head(child) == "symbol":
            out.extend(doc._world_pin_positions(child))
    return out


def _wire_endpoints(doc: SchematicDocument) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "pts":
                for xy in sub[1:]:
                    if isinstance(xy, list) and _head(xy) == "xy" and len(xy) >= 3:
                        out.append((float(xy[1]), float(xy[2])))
    return out


def _label_anchors(doc: SchematicDocument) -> List[Tuple[str, str, Tuple[float, float]]]:
    """Returns [(kind, name, (x,y))] for every label/global_label/hierarchical_label."""
    out: List[Tuple[str, str, Tuple[float, float]]] = []
    for child in doc.tree[1:]:
        if not (isinstance(child, list)):
            continue
        kind = _head(child)
        if kind not in ("label", "global_label", "hierarchical_label"):
            continue
        name = _to_str(child[1]) if len(child) > 1 else ""
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                out.append((kind, name, (float(sub[1]), float(sub[2]))))
                break
    return out


def _power_port_anchors(doc: SchematicDocument) -> List[Tuple[str, Tuple[float, float]]]:
    """Power-port instance anchors (lib_id startswith 'power:')."""
    out: List[Tuple[str, Tuple[float, float]]] = []
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "symbol"):
            continue
        lib_id = ""
        at_xy: Optional[Tuple[float, float]] = None
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "lib_id" and len(sub) > 1:
                lib_id = _to_str(sub[1])
            elif isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                at_xy = (float(sub[1]), float(sub[2]))
        if lib_id.startswith("power:") and at_xy is not None:
            out.append((lib_id, at_xy))
    return out


def _near(p: Tuple[float, float], pts: List[Tuple[float, float]], tol: float) -> bool:
    px, py = p
    for (x, y) in pts:
        if abs(x - px) <= tol and abs(y - py) <= tol:
            return True
    return False


def _check_zero_wires(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
) -> List[Issue]:
    """CRITICAL: any non-trivial schematic with 0 wires AND <1 label-per-pin
    cannot be electrically valid. Universal — works for every circuit type."""
    n_comp = sum(1 for c in doc.tree[1:]
                 if isinstance(c, list) and _head(c) == "symbol")
    n_wires = sum(1 for c in doc.tree[1:]
                  if isinstance(c, list) and _head(c) == "wire")
    n_pins = len(_all_pin_tips(doc))
    n_labels = len(_label_anchors(doc))
    # power ports count as connectivity-by-name
    n_pwr = len(_power_port_anchors(doc))
    if n_comp >= 3 and n_wires == 0 and (n_labels + n_pwr) < n_pins * 0.5:
        return [Issue(
            severity="error", code="CONN_001",
            message=f"0 wires emitted for {n_comp} components ({n_pins} pins, "
                    f"{n_labels} labels, {n_pwr} power ports) — schematic is "
                    f"electrically disconnected",
            detail={"components": n_comp, "wires": n_wires,
                    "pins": n_pins, "labels": n_labels, "power_ports": n_pwr},
        )]
    return []


def _check_dangling_labels(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
    tol_mm: float = 0.635,
) -> List[Issue]:
    """Every label / global_label / hierarchical_label MUST sit on a pin tip
    or wire endpoint. Anything else is dangling — KiCad reports it as
    `(no net)` and the net never forms. Universal geometry check."""
    anchors = _all_pin_tips(doc) + _wire_endpoints(doc)
    dangling: List[Dict[str, Any]] = []
    for kind, name, xy in _label_anchors(doc):
        if not _near(xy, anchors, tol_mm):
            dangling.append({"kind": kind, "name": name,
                             "x": xy[0], "y": xy[1]})
    if dangling:
        return [Issue(
            severity="error", code="CONN_002",
            message=f"{len(dangling)} label(s) dangling — not on any pin or "
                    f"wire endpoint (tol {tol_mm} mm)",
            detail={"count": len(dangling), "sample": dangling[:10]},
        )]
    return []


def _check_dangling_power_ports(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
    tol_mm: float = 0.635,
) -> List[Issue]:
    """Same as labels: power-port instances must sit on a pin tip or wire
    endpoint. NE555 had 12 power ports scattered with no connection."""
    anchors = _all_pin_tips(doc) + _wire_endpoints(doc)
    # exclude the power-ports themselves from the "tip" set so they don't
    # validate each other.
    pwr_xys = {xy for _, xy in _power_port_anchors(doc)}
    non_pwr_anchors = [a for a in anchors if a not in pwr_xys]
    dangling: List[Dict[str, Any]] = []
    for lib_id, xy in _power_port_anchors(doc):
        if not _near(xy, non_pwr_anchors, tol_mm):
            dangling.append({"lib_id": lib_id, "x": xy[0], "y": xy[1]})
    if dangling:
        return [Issue(
            severity="error", code="CONN_003",
            message=f"{len(dangling)} power-port(s) dangling — not on any "
                    f"component pin or wire endpoint (tol {tol_mm} mm)",
            detail={"count": len(dangling), "sample": dangling[:10]},
        )]
    return []


def _check_body_overlap(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
    sep_mm: float = 0.0,
) -> List[Issue]:
    """Any two placed symbols whose real bboxes intersect → ERROR.
    Universal geometry — same code for every circuit."""
    try:
        from .label_placer import _build_body_bbox_by_ref  # type: ignore
    except Exception:
        return []
    schematic_path = getattr(doc, "path", None)
    if not schematic_path:
        return []
    try:
        by_ref = _build_body_bbox_by_ref(placement, schematic_path)
    except Exception:
        return []
    refs = list(by_ref.items())
    overlaps: List[Dict[str, Any]] = []
    for i in range(len(refs)):
        ra, (ax0, ay0, ax1, ay1) = refs[i]
        if ax1 - ax0 < 0.1 or ay1 - ay0 < 0.1:
            continue
        for j in range(i + 1, len(refs)):
            rb, (bx0, by0, bx1, by1) = refs[j]
            if bx1 - bx0 < 0.1 or by1 - by0 < 0.1:
                continue
            if (ax0 < bx1 - sep_mm and bx0 < ax1 - sep_mm
                    and ay0 < by1 - sep_mm and by0 < ay1 - sep_mm):
                overlaps.append({"a": ra, "b": rb})
    if overlaps:
        return [Issue(
            severity="error", code="GEOM_001",
            message=f"{len(overlaps)} component body overlap(s)",
            detail={"count": len(overlaps), "sample": overlaps[:10]},
        )]
    return []


def _check_orphan_pins(
    placement: Dict[str, Any], routed: Dict[str, Any], doc: SchematicDocument,
    tol_mm: float = 0.635,
) -> List[Issue]:
    """Every pin tip that is NOT near a wire endpoint, another pin tip,
    a label anchor, a power-port anchor, or a no_connect marker is
    electrically floating. Universal — drives the "every part fully
    connected" hard invariant from feedback memory."""
    pin_tips = _all_pin_tips(doc)
    wire_pts = _wire_endpoints(doc)
    label_pts = [xy for _, _, xy in _label_anchors(doc)]
    pwr_pts = [xy for _, xy in _power_port_anchors(doc)]
    nc_pts: List[Tuple[float, float]] = []
    for child in doc.tree[1:]:
        if isinstance(child, list) and _head(child) == "no_connect":
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                    nc_pts.append((float(sub[1]), float(sub[2])))
                    break
    # multi-set of pin coords so we can detect pin-on-pin connectivity
    pin_set = pin_tips
    universe = wire_pts + label_pts + pwr_pts + nc_pts
    orphans: List[Tuple[float, float]] = []
    for p in pin_tips:
        if _near(p, universe, tol_mm):
            continue
        # pin sitting on another pin (direct touch) is connected
        count = sum(1 for q in pin_set
                    if abs(q[0] - p[0]) <= tol_mm and abs(q[1] - p[1]) <= tol_mm)
        if count >= 2:
            continue
        orphans.append(p)
    if orphans:
        return [Issue(
            severity="error", code="CONN_004",
            message=f"{len(orphans)} pin(s) floating — no wire/label/power/NC "
                    f"and no other pin touching",
            detail={"count": len(orphans),
                    "sample": [{"x": x, "y": y} for x, y in orphans[:10]]},
        )]
    return []


_CHECKS: List[Callable[..., List[Issue]]] = [
    _check_zero_wires,
    _check_dangling_labels,
    _check_dangling_power_ports,
    _check_orphan_pins,
    _check_body_overlap,
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
