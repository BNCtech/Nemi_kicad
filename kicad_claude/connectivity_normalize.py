"""Geometric normalization pass — multi-stage repair between Claude's
schematic output and the layout pipeline.

The LLM emits a plausible netlist but commonly gets per-pin geometry
wrong: rotated symbols still wired as if rotation 0, body-edge endpoints
when the convention is pin-tip, off-by-grid drift, wires that nearly
meet but leave a sub-millimetre gap. Each individual error is tiny but
downstream every off-pin / off-pin endpoint breaks
`nets.build_sheet_nets`'s union-find — every pin ends up on its own
single-member net, the connectivity_graph has zero edges, and the
layout pipeline silently ships a dead schematic.

This module repairs that class of failure deterministically, in passes:

  Pass 1 — SNAP    : move wire endpoints onto nearest pin tip (≤ tol)
  Pass 2 — GAP     : extend collinear wires that nearly meet (≤ gap)
  Pass 3 — PRUNE   : drop tiny dead-end stubs that connect nothing
  Pass 4 — SNAP    : second snap, catches opportunities created by 2/3

ROLLBACK SAFETY: each pass is greedy and CAN regress connectivity
(picking the wrong pin on a dense row, prune-ing a short-but-valid
connection). The orchestrator measures `edge_count` before AND after
the whole sequence; if it dropped, the tree is restored to pre-pass
state. Worst case is a no-op, never worse-than-before.

Public:
  normalize_wire_endpoints(doc, tolerance_mm=None, max_gap_mm=None,
                            prune_below_mm=None) -> Dict[str, Any]
      Mutates doc in place. Returns aggregated stats per pass + delta.

Designed to run between schematic-edit apply and the layout pipeline.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._config_loader import load as _load_config
from .schematic_extractor import SchematicExtractor
from .schematic_modifier import (
    SchematicDocument, _head, _sym, _to_str,
)
from . import nets as _nets


# Default tolerances. Override via conventions.json:connectivity_normalize.
#
# tolerance_mm (snap): 5.0 mm — captures the rotation-mismatch class
#     where Claude wires for rotation 0 but the symbol is rotated 90,
#     leaving each endpoint offset by ≈(±3.8, ±2.5) from the real pin
#     tip. Tighter (3.81 mm) loses this class entirely.
# max_gap_mm (gap closure): 3.0 mm — observed gaps in real Claude
#     output are 0.5–2.5 mm. Tighter wouldn't help, looser risks
#     bridging adjacent unrelated nets.
# prune_below_mm (dead-end): 1.5 mm — anything shorter than the grid
#     pitch is almost certainly an editing artefact; longer stubs
#     might be intentional short connections we shouldn't touch.
_DEFAULTS = {
    "tolerance_mm": 5.0,
    "label_tolerance_mm": 8.0,  # labels drift farther than wire endpoints — Claude
                                  # treats them as annotations, not connections; on the
                                  # big-tier histogram 13/29 labels are <5mm from a pin
                                  # but 19/29 are <8mm. The dense-pin-row risk is lower
                                  # for labels than wires (a misplaced label is visible).
    "max_gap_mm": 3.0,
    "prune_below_mm": 1.5,
}


def _cfg(key: str) -> float:
    try:
        cfg = (_load_config("conventions") or {}).get("connectivity_normalize") or {}
        if key in cfg and cfg[key] is not None:
            return float(cfg[key])
    except Exception:
        pass
    return float(_DEFAULTS[key])


# ---------------------------------------------------------------------------
# Shared helpers — pin / junction indexing, wire iteration
# ---------------------------------------------------------------------------

def _build_pin_index(
    extractor: SchematicExtractor,
) -> List[Tuple[float, float, str, str]]:
    """Real world-coord pin tips for every placed component instance, with
    rotation/mirror applied. Returns list of (x, y, ref, pin_number).
    Power-port symbols (#PWR / #FLG) are EXCLUDED — their "pin" is virtual
    (rail merges by name) and snapping wires to them produces visually
    incorrect L-bends through the port symbol body."""
    lib_pins = extractor.lib_symbol_pins()
    out: List[Tuple[float, float, str, str]] = []
    for c in extractor.components():
        ref = c.get("reference") or ""
        if not ref or ref.startswith("#"):
            continue
        lib_id = c.get("lib_id") or ""
        by_unit = lib_pins.get(lib_id) or {}
        unit_no = int(c.get("unit", 1))
        pin_defs: List[Dict[str, Any]] = list(by_unit.get(0, []))
        if unit_no != 0:
            pin_defs.extend(by_unit.get(unit_no, []))
        if not pin_defs:
            continue
        endpoints = _nets.placed_pin_endpoints(c, pin_defs)
        for ep in endpoints:
            out.append((
                float(ep["x"]), float(ep["y"]),
                ep.get("ref") or ref, str(ep.get("number", "")),
            ))
    return out


def _build_junction_set(doc: SchematicDocument) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "junction"):
            continue
        at = next((s for s in child[1:]
                   if isinstance(s, list) and _head(s) == "at"
                   and len(s) >= 3), None)
        if not at:
            continue
        try:
            out.append((float(at[1]), float(at[2])))
        except (TypeError, ValueError):
            continue
    return out


def _wire_segments(doc: SchematicDocument):
    """Yield (wire_node, pts_node, (x1,y1), (x2,y2)) for every 2-point wire.
    Multi-segment wires (>2 points in pts) are reasonably rare in the
    eeschema writer's output but if they appear we just take the first two
    points as the segment for collinearity decisions."""
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        pts_node = next((s for s in child[1:]
                         if isinstance(s, list) and _head(s) == "pts"), None)
        if not pts_node:
            continue
        pts: List[Tuple[float, float]] = []
        for xy in pts_node[1:]:
            if (isinstance(xy, list) and _head(xy) == "xy"
                    and len(xy) >= 3):
                try:
                    pts.append((float(xy[1]), float(xy[2])))
                except (TypeError, ValueError):
                    pass
        if len(pts) >= 2:
            yield child, pts_node, pts[0], pts[1]


def _iter_wire_endpoints(doc: SchematicDocument):
    """Yield (wire_node, pts_node, point_index, x, y) for each (xy ...) sub-
    node of every wire's (pts ...). Caller mutates pts_node[point_index] to
    move the endpoint."""
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        pts_node = next((s for s in child[1:]
                         if isinstance(s, list) and _head(s) == "pts"), None)
        if not pts_node:
            continue
        for i, xy in enumerate(pts_node[1:], start=1):
            if (isinstance(xy, list) and _head(xy) == "xy"
                    and len(xy) >= 3):
                try:
                    x = float(xy[1]); y = float(xy[2])
                except (TypeError, ValueError):
                    continue
                yield child, pts_node, i, x, y


def _close_to(a: Tuple[float, float], b: Tuple[float, float],
              eps: float = 0.05) -> bool:
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def _endpoint_on_pin(
    pt: Tuple[float, float],
    pins: List[Tuple[float, float, str, str]],
    eps: float = 0.05,
) -> bool:
    for px, py, _r, _n in pins:
        if abs(pt[0] - px) < eps and abs(pt[1] - py) < eps:
            return True
    return False


def _segment_contains_interior(
    p1: Tuple[float, float], p2: Tuple[float, float],
    q: Tuple[float, float], eps: float = 0.05,
) -> bool:
    """True iff `q` lies strictly between p1 and p2 on the axis-aligned
    segment p1-p2 (excludes the endpoints themselves). Used by T-junction
    detection: if a wire endpoint sits on the INTERIOR of another wire,
    KiCad needs a junction at that crossing point."""
    qx, qy = q
    x1, y1 = p1
    x2, y2 = p2
    if abs(x1 - x2) < eps:
        if abs(qx - x1) > eps:
            return False
        lo, hi = sorted([y1, y2])
        return lo + eps < qy < hi - eps
    if abs(y1 - y2) < eps:
        if abs(qy - y1) > eps:
            return False
        lo, hi = sorted([x1, x2])
        return lo + eps < qx < hi - eps
    return False  # diagonal segments — KiCad rejects them; nothing to anchor to


# Endpoint classification result codes — used by `_classify_endpoint`. The
# orchestrator can branch on these to pick smart-repair strategies per
# endpoint, instead of running blanket passes that may regress edges.
ENDPOINT_ON_PIN          = "on_pin"
ENDPOINT_ON_JUNCTION     = "on_junction"
ENDPOINT_ON_WIRE_ENDPOINT = "on_wire_endpoint"
ENDPOINT_ON_WIRE_INTERIOR = "on_wire_interior"   # T-junction candidate
ENDPOINT_NEAR_PIN         = "near_pin"           # within snap tolerance
ENDPOINT_DEAD             = "dead"               # touches nothing electrical


def _classify_endpoint(
    pt: Tuple[float, float],
    pins: List[Tuple[float, float, str, str]],
    junctions: List[Tuple[float, float]],
    other_endpoints: Dict[Tuple[float, float], int],
    other_segments: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    snap_tol: float,
) -> str:
    """Categorise a wire endpoint by its electrical anchoring. Priority order
    (highest-validity first):

      on_pin           — sits exactly on a pin tip
      on_junction      — sits exactly on a junction marker
      on_wire_endpoint — shares a coord with another wire's endpoint
      on_wire_interior — sits inside another wire's segment (T-junction)
      near_pin         — within snap_tol of a pin but not exactly on it
      dead             — nothing within tolerance

    Pure data — caller decides what to do per classification. Universal
    across any schematic; no per-circuit rules."""
    if _endpoint_on_pin(pt, pins):
        return ENDPOINT_ON_PIN
    for jx, jy in junctions:
        if abs(jx - pt[0]) < 0.05 and abs(jy - pt[1]) < 0.05:
            return ENDPOINT_ON_JUNCTION
    key = (round(pt[0], 2), round(pt[1], 2))
    if other_endpoints.get(key, 0) >= 1:
        return ENDPOINT_ON_WIRE_ENDPOINT
    for p1, p2 in other_segments:
        if _segment_contains_interior(p1, p2, pt):
            return ENDPOINT_ON_WIRE_INTERIOR
    for px, py, _r, _n in pins:
        if abs(pt[0] - px) <= snap_tol and abs(pt[1] - py) <= snap_tol:
            return ENDPOINT_NEAR_PIN
    return ENDPOINT_DEAD


# ---------------------------------------------------------------------------
# Pass 1 — snap loose wire endpoints onto nearest pin
# ---------------------------------------------------------------------------

def _axis_aware_distance(
    wx: float, wy: float, px: float, py: float,
) -> Tuple[float, float]:
    """Two metrics: (axis_aligned_dist, chebyshev). Axis-aligned distance
    is the canonical-axis offset when the wire endpoint shares one
    coordinate with the pin (the dominant correct case — wire is on the
    right axis, just stops too soon/late). Chebyshev is the tie-breaker."""
    dx = abs(wx - px)
    dy = abs(wy - py)
    cheby = max(dx, dy)
    if dx < 0.05:
        axis = dy
    elif dy < 0.05:
        axis = dx
    else:
        axis = math.hypot(dx, dy)
    return (axis, cheby)


def _pass_snap_to_pins(
    doc: SchematicDocument,
    pins: List[Tuple[float, float, str, str]],
    tol: float,
) -> Dict[str, Any]:
    snapped = 0
    max_delta = 0.0
    wires_modified: Dict[int, int] = {}

    for wire_node, pts_node, i, wx, wy in _iter_wire_endpoints(doc):
        if _endpoint_on_pin((wx, wy), pins):
            continue
        best: Optional[Tuple[float, float]] = None
        best_key: Tuple[float, float] = (tol + 1.0, tol + 1.0)
        for px, py, _r, _n in pins:
            if abs(wx - px) > tol or abs(wy - py) > tol:
                continue
            key = _axis_aware_distance(wx, wy, px, py)
            if key[0] > tol:
                continue
            if key < best_key:
                best_key = key
                best = (px, py)
        if best is None:
            continue
        new_x, new_y = best
        delta = math.hypot(new_x - wx, new_y - wy)
        if delta > max_delta:
            max_delta = delta
        pts_node[i] = [_sym("xy"), float(new_x), float(new_y)]
        snapped += 1
        wires_modified[id(wire_node)] = wires_modified.get(id(wire_node), 0) + 1

    return {
        "snapped": snapped,
        "max_delta_mm": round(max_delta, 3),
        "wires_modified": len(wires_modified),
    }


# ---------------------------------------------------------------------------
# Pass 1b — snap label anchors onto nearest pin
# ---------------------------------------------------------------------------

def _pass_snap_labels_to_pins(
    doc: SchematicDocument,
    pins: List[Tuple[float, float, str, str]],
    tol: float,
) -> Dict[str, Any]:
    """Same idea as wire-endpoint snap, applied to (label ...) anchors.
    KiCad treats a label as electrically attached only when its `(at x y)`
    coincides EXACTLY with a wire endpoint or pin tip. Claude routinely
    drops labels 1–7 mm off the intended pin, so the label-name merge in
    `nets.build_sheet_nets` joins the label's own micro-node but no pin.
    Snapping the label to the nearest pin (within tolerance) anchors the
    name to the right net.

    Only `(label ...)` is snapped — global_label and hierarchical_label
    have their own placement semantics (sheet-pin matching) and may sit
    deliberately off-pin."""
    snapped = 0
    max_delta = 0.0
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "label"):
            continue
        at_node = next((s for s in child[1:]
                        if isinstance(s, list) and _head(s) == "at"
                        and len(s) >= 3), None)
        if not at_node:
            continue
        try:
            lx = float(at_node[1])
            ly = float(at_node[2])
        except (TypeError, ValueError):
            continue
        if _endpoint_on_pin((lx, ly), pins):
            continue
        # Same axis-aware pin search as wire snap.
        best: Optional[Tuple[float, float]] = None
        best_key: Tuple[float, float] = (tol + 1.0, tol + 1.0)
        for px, py, _r, _n in pins:
            if abs(lx - px) > tol or abs(ly - py) > tol:
                continue
            key = _axis_aware_distance(lx, ly, px, py)
            if key[0] > tol:
                continue
            if key < best_key:
                best_key = key
                best = (px, py)
        if best is None:
            continue
        new_x, new_y = best
        delta = math.hypot(new_x - lx, new_y - ly)
        if delta > max_delta:
            max_delta = delta
        # Mutate the (at x y rot) in place — preserve rotation if present.
        at_node[1] = float(new_x)
        at_node[2] = float(new_y)
        snapped += 1
    return {"snapped": snapped, "max_delta_mm": round(max_delta, 3)}


# ---------------------------------------------------------------------------
# Pass 2 — close collinear gaps between wires that nearly meet
# ---------------------------------------------------------------------------

def _pass_close_collinear_gaps(
    doc: SchematicDocument,
    max_gap_mm: float,
    pins: List[Tuple[float, float, str, str]],
) -> Dict[str, Any]:
    """Look at every pair of axis-aligned wires (both horizontal on same Y
    OR both vertical on same X) and check whether their closest endpoints
    are separated by 0 < gap ≤ max_gap_mm. If so, extend the nearer
    wire's endpoint to meet the other.

    Conservative rules (each protects against false-positive extensions):
      - Only fire when at least one of the two endpoints is already on a
        pin — that anchors the extension and prevents merging two stray
        floating wires.
      - Skip if either wire is degenerate (length 0).
      - Don't extend a wire endpoint that's already on a pin (the other
        wire should extend toward us, not us into the pin's body).
    """
    segs: List[Tuple[Any, Any, Tuple[float, float], Tuple[float, float]]] = []
    for w in _wire_segments(doc):
        wire_node, pts_node, p1, p2 = w
        if _close_to(p1, p2):
            continue
        segs.append(w)

    gaps_closed = 0
    max_gap_closed = 0.0

    def _is_horizontal(p1, p2): return abs(p1[1] - p2[1]) < 0.05
    def _is_vertical(p1, p2):   return abs(p1[0] - p2[0]) < 0.05

    for i in range(len(segs)):
        _, pts_a, a1, a2 = segs[i]
        a_horiz = _is_horizontal(a1, a2)
        a_vert  = _is_vertical(a1, a2)
        if not (a_horiz or a_vert):
            continue
        for j in range(i + 1, len(segs)):
            _, pts_b, b1, b2 = segs[j]
            b_horiz = _is_horizontal(b1, b2)
            b_vert  = _is_vertical(b1, b2)
            if a_horiz and b_horiz and abs(a1[1] - b1[1]) < 0.05:
                # Same horizontal line. Sort intervals by x.
                a_lo, a_hi = sorted([a1[0], a2[0]])
                b_lo, b_hi = sorted([b1[0], b2[0]])
                if a_hi < b_lo:
                    gap = b_lo - a_hi
                    if not (0 < gap <= max_gap_mm):
                        continue
                    # extend a's right-end (a_hi) to b_lo OR b's left-end to a_hi
                    target_x = a_hi
                    # Move whichever endpoint is NOT on a pin.
                    a_right_pt = (a_hi, a1[1])
                    b_left_pt  = (b_lo, b1[1])
                    if _endpoint_on_pin(a_right_pt, pins):
                        # extend b's left to a_hi
                        if _endpoint_on_pin(b_left_pt, pins):
                            continue  # both pinned, can't extend
                        _move_endpoint_to(pts_b, b_left_pt, (target_x, b1[1]))
                    else:
                        if not _endpoint_on_pin(b_left_pt, pins):
                            # neither pinned — anchor needed; skip to avoid
                            # bridging unrelated floating wires.
                            continue
                        _move_endpoint_to(pts_a, a_right_pt, (b_lo, a1[1]))
                    gaps_closed += 1
                    if gap > max_gap_closed:
                        max_gap_closed = gap
                elif b_hi < a_lo:
                    gap = a_lo - b_hi
                    if not (0 < gap <= max_gap_mm):
                        continue
                    a_left_pt  = (a_lo, a1[1])
                    b_right_pt = (b_hi, b1[1])
                    if _endpoint_on_pin(a_left_pt, pins):
                        if _endpoint_on_pin(b_right_pt, pins):
                            continue
                        _move_endpoint_to(pts_b, b_right_pt, (a_lo, b1[1]))
                    else:
                        if not _endpoint_on_pin(b_right_pt, pins):
                            continue
                        _move_endpoint_to(pts_a, a_left_pt, (b_hi, a1[1]))
                    gaps_closed += 1
                    if gap > max_gap_closed:
                        max_gap_closed = gap
            elif a_vert and b_vert and abs(a1[0] - b1[0]) < 0.05:
                a_lo, a_hi = sorted([a1[1], a2[1]])
                b_lo, b_hi = sorted([b1[1], b2[1]])
                x_common = a1[0]
                if a_hi < b_lo:
                    gap = b_lo - a_hi
                    if not (0 < gap <= max_gap_mm):
                        continue
                    a_bot_pt = (x_common, a_hi)
                    b_top_pt = (x_common, b_lo)
                    if _endpoint_on_pin(a_bot_pt, pins):
                        if _endpoint_on_pin(b_top_pt, pins):
                            continue
                        _move_endpoint_to(pts_b, b_top_pt, (x_common, a_hi))
                    else:
                        if not _endpoint_on_pin(b_top_pt, pins):
                            continue
                        _move_endpoint_to(pts_a, a_bot_pt, (x_common, b_lo))
                    gaps_closed += 1
                    if gap > max_gap_closed:
                        max_gap_closed = gap
                elif b_hi < a_lo:
                    gap = a_lo - b_hi
                    if not (0 < gap <= max_gap_mm):
                        continue
                    a_top_pt = (x_common, a_lo)
                    b_bot_pt = (x_common, b_hi)
                    if _endpoint_on_pin(a_top_pt, pins):
                        if _endpoint_on_pin(b_bot_pt, pins):
                            continue
                        _move_endpoint_to(pts_b, b_bot_pt, (x_common, a_lo))
                    else:
                        if not _endpoint_on_pin(b_bot_pt, pins):
                            continue
                        _move_endpoint_to(pts_a, a_top_pt, (x_common, b_hi))
                    gaps_closed += 1
                    if gap > max_gap_closed:
                        max_gap_closed = gap

    return {
        "gaps_closed": gaps_closed,
        "max_gap_closed_mm": round(max_gap_closed, 3),
    }


def _move_endpoint_to(
    pts_node: list, old: Tuple[float, float], new: Tuple[float, float],
) -> bool:
    """Find the (xy old.x old.y) sub-node inside pts_node and rewrite it
    to (xy new.x new.y). Returns True if the rewrite happened."""
    for i, xy in enumerate(pts_node[1:], start=1):
        if (isinstance(xy, list) and _head(xy) == "xy"
                and len(xy) >= 3):
            try:
                x = float(xy[1]); y = float(xy[2])
            except (TypeError, ValueError):
                continue
            if abs(x - old[0]) < 0.05 and abs(y - old[1]) < 0.05:
                pts_node[i] = [_sym("xy"), float(new[0]), float(new[1])]
                return True
    return False


# ---------------------------------------------------------------------------
# Pass 2b — T-junction auto-insertion
# ---------------------------------------------------------------------------

def _pass_insert_t_junctions(
    doc: SchematicDocument,
    pins: List[Tuple[float, float, str, str]],
) -> Dict[str, Any]:
    """For each wire endpoint that lies INSIDE (not at the endpoint of)
    another wire's segment, insert a (junction ...) marker at that point.
    KiCad's electrical model treats a wire crossing another wire as
    NOT connected unless a junction is explicitly placed — so a 'T' or
    '+' meeting of three or more wires without a junction is a silent
    disconnection. This pass catches all such cases.

    Conservative: skips when the endpoint already sits on a pin (the pin
    handles the join), already has a junction, or where inserting one
    would create a duplicate junction at the same coord."""
    segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    endpoints: List[Tuple[float, float]] = []
    for _wn, _pn, p1, p2 in _wire_segments(doc):
        segments.append((p1, p2))
        endpoints.append(p1)
        endpoints.append(p2)

    # Existing junctions — never insert a duplicate.
    existing_junctions = _build_junction_set(doc)

    def _existing_junction_at(q: Tuple[float, float]) -> bool:
        for jx, jy in existing_junctions:
            if abs(jx - q[0]) < 0.05 and abs(jy - q[1]) < 0.05:
                return True
        return False

    pin_set = {(round(p[0], 2), round(p[1], 2)) for p, _, _, _ in
               [(p, None, None, None) for p in [(pp[0], pp[1]) for pp in pins]]}
    # Simpler & correct:
    pin_set = {(round(px, 2), round(py, 2)) for px, py, _r, _n in pins}

    inserted = 0
    for ep in endpoints:
        key = (round(ep[0], 2), round(ep[1], 2))
        if key in pin_set:
            continue  # pin handles the join — no junction needed
        if _existing_junction_at(ep):
            continue
        on_interior = False
        for p1, p2 in segments:
            if _segment_contains_interior(p1, p2, ep):
                on_interior = True
                break
        if not on_interior:
            continue
        # Insert (junction (at x y) (diameter 0) (color 0 0 0 0) (uuid ...))
        # — same shape KiCad's writer uses. The diameter=0 + transparent
        # color renders as KiCad's default appearance.
        doc.tree.append([
            _sym("junction"),
            [_sym("at"), float(ep[0]), float(ep[1])],
            [_sym("diameter"), 0],
            [_sym("color"), 0, 0, 0, 0],
            _gen_uuid_node_local(),
        ])
        existing_junctions.append(ep)
        inserted += 1

    return {"inserted": inserted}


def _gen_uuid_node_local() -> list:
    """Local lazy import of the uuid-generator helper used by everything else
    in the project — kept inline so this module doesn't drag a circular
    import via schematic_modifier."""
    from .schematic_modifier import _gen_uuid_node
    return _gen_uuid_node()


# ---------------------------------------------------------------------------
# Pass 4 — auto-no_connect for floating pins (P8.x)
# ---------------------------------------------------------------------------

def _build_full_pin_index(
    extractor: SchematicExtractor,
) -> List[Dict[str, Any]]:
    """Same shape as `_build_pin_index` but also carries `pin_name` and
    `electrical_type` so the auto-NC pass can classify pins safely. Power-
    port symbols still excluded (their pins are virtual)."""
    lib_pins = extractor.lib_symbol_pins()
    out: List[Dict[str, Any]] = []
    for c in extractor.components():
        ref = c.get("reference") or ""
        if not ref or ref.startswith("#"):
            continue
        lib_id = c.get("lib_id") or ""
        by_unit = lib_pins.get(lib_id) or {}
        unit_no = int(c.get("unit", 1))
        pin_defs: List[Dict[str, Any]] = list(by_unit.get(0, []))
        if unit_no != 0:
            pin_defs.extend(by_unit.get(unit_no, []))
        if not pin_defs:
            continue
        endpoints = _nets.placed_pin_endpoints(c, pin_defs)
        for ep in endpoints:
            out.append({
                "x": float(ep["x"]), "y": float(ep["y"]),
                "ref": ep.get("ref") or ref,
                "pin_number": str(ep.get("number", "")),
                "pin_name": str(ep.get("name", "") or ""),
                "electrical_type": str(ep.get("electrical_type", "") or ""),
            })
    return out


def _pin_is_floating(
    pin: Dict[str, Any],
    wire_endpoints: set,
    label_set: set,
    junction_set: set,
    nc_set: set,
    power_set: set,
) -> bool:
    """A pin is floating iff its (x, y) doesn't coincide with ANY of: a
    wire endpoint, a label anchor, a junction, an existing no_connect,
    a power-port symbol anchor."""
    key = (round(pin["x"], 2), round(pin["y"], 2))
    return (key not in wire_endpoints and key not in label_set
            and key not in junction_set and key not in nc_set
            and key not in power_set)


def _nc_classify(
    pin: Dict[str, Any], cfg: Dict[str, Any],
) -> Tuple[str, str]:
    """Return (`tier`, `reason`) for an auto-NC decision.

    Tiers (descending confidence):
      - `tier_1_explicit_nc`: pin's lib electrical type is `unspecified` or
        `no_connect`, OR pin name contains an explicit NC/DNU/RSV substring.
        Add NC without further checks.
      - `tier_2_gpio`: electrical type is `bidirectional` AND pin name
        matches a GPIO regex AND no unsafe substring is present. Add NC —
        this is the unused-MCU-GPIO case.
      - `skip_unsafe`: pin matched an unsafe substring (power / clock /
        reset / comm / analog / differential / feedback). Never auto-NC.
      - `skip_unclassified`: no rule matched. Conservative default — let
        the user / ERC surface it explicitly."""
    import re as _re_nc
    pin_name = (pin.get("pin_name") or "").strip()
    pin_name_strip = pin_name.lstrip("~").upper()
    elec = (pin.get("electrical_type") or "").lower()

    unsafe = [s.upper() for s in (cfg.get("unsafe_substrings") or [])]
    for u in unsafe:
        if u and u in pin_name_strip:
            return ("skip_unsafe", f"matched unsafe pattern '{u}'")

    safe_subs = [s.upper() for s in (cfg.get("safe_substrings") or [])]
    for s in safe_subs:
        if s and s in pin_name_strip:
            return ("tier_1_explicit_nc",
                    f"name contains '{s}' (explicit unused)")

    safe_types = set(cfg.get("safe_electrical_types") or [])
    if elec in safe_types:
        return ("tier_1_explicit_nc",
                f"electrical_type='{elec}' (declared NC-compatible)")

    gpio_patterns = cfg.get("gpio_name_regex") or []
    tier_2_types = set(cfg.get("tier_2_gpio_electrical_types") or [])
    if elec in tier_2_types and pin_name_strip:
        for rex in gpio_patterns:
            try:
                if _re_nc.match(rex, pin_name_strip):
                    return ("tier_2_gpio",
                            f"GPIO name '{pin_name_strip}' + "
                            f"electrical_type='{elec}'")
            except _re_nc.error:
                continue

    return ("skip_unclassified", "no rule matched")


def _pass_auto_no_connect(
    doc: SchematicDocument,
    extractor: SchematicExtractor,
) -> Dict[str, Any]:
    """Insert (no_connect ...) markers on floating pins that classify as
    safe via `_nc_classify`. Skips pins already connected via wire /
    label / junction / existing NC / power-port. Updates doc.tree in
    place; returns counters per tier."""
    cfg = _load_config("conventions").get("auto_no_connect") or {}
    if not cfg.get("enabled", True):
        return {"inserted": 0, "skipped_unsafe": 0,
                "skipped_unclassified": 0, "skipped_already": 0,
                "by_tier": {}}

    full_pins = _build_full_pin_index(extractor)

    # Build anchor sets — coincidence with ANY of these means the pin
    # already has connectivity and shouldn't get an NC.
    wire_endpoints: set = set()
    for _wn, _pn, p1, p2 in _wire_segments(doc):
        for p in (p1, p2):
            wire_endpoints.add((round(p[0], 2), round(p[1], 2)))
    label_set: set = set()
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) in
                ("label", "global_label", "hierarchical_label")):
            continue
        at = next((s for s in child[1:]
                   if isinstance(s, list) and _head(s) == "at"
                   and len(s) >= 3), None)
        if not at:
            continue
        try:
            label_set.add((round(float(at[1]), 2), round(float(at[2]), 2)))
        except (TypeError, ValueError):
            continue
    junction_set: set = set(
        (round(j[0], 2), round(j[1], 2)) for j in _build_junction_set(doc)
    )
    nc_set: set = set()
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "no_connect"):
            continue
        at = next((s for s in child[1:]
                   if isinstance(s, list) and _head(s) == "at"
                   and len(s) >= 3), None)
        if not at:
            continue
        try:
            nc_set.add((round(float(at[1]), 2), round(float(at[2]), 2)))
        except (TypeError, ValueError):
            continue
    power_set: set = set()
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "symbol"):
            continue
        lib_id_node = next((s for s in child[1:]
                            if isinstance(s, list) and _head(s) == "lib_id"
                            and len(s) >= 2), None)
        lib_id = _to_str(lib_id_node[1]) if lib_id_node else ""
        at_node = next((s for s in child[1:]
                        if isinstance(s, list) and _head(s) == "at"
                        and len(s) >= 3), None)
        if not at_node or not lib_id.startswith("power:"):
            continue
        try:
            power_set.add((round(float(at_node[1]), 2),
                            round(float(at_node[2]), 2)))
        except (TypeError, ValueError):
            pass

    max_inserts = int(cfg.get("max_inserts_per_run", 256))
    inserted = 0
    skipped_unsafe = 0
    skipped_unclassified = 0
    skipped_already = 0
    by_tier: Dict[str, int] = {}

    for pin in full_pins:
        if not _pin_is_floating(pin, wire_endpoints, label_set,
                                  junction_set, nc_set, power_set):
            skipped_already += 1
            continue
        tier, _reason = _nc_classify(pin, cfg)
        by_tier[tier] = by_tier.get(tier, 0) + 1
        if tier == "skip_unsafe":
            skipped_unsafe += 1
            continue
        if tier == "skip_unclassified":
            skipped_unclassified += 1
            continue
        if inserted >= max_inserts:
            break
        # add_no_connect handles duplicate-coord dedup internally; mutating
        # via doc keeps the snapshot history intact in case the global
        # rollback fires.
        result = doc.add_no_connect(pin["x"], pin["y"])
        if result.get("ok"):
            # Mark the coord so a duplicate pin (multi-unit IC sharing pin)
            # at the same place doesn't try again.
            nc_set.add((round(pin["x"], 2), round(pin["y"], 2)))
            inserted += 1

    return {
        "inserted": inserted,
        "skipped_unsafe": skipped_unsafe,
        "skipped_unclassified": skipped_unclassified,
        "skipped_already": skipped_already,
        "by_tier": by_tier,
    }


# ---------------------------------------------------------------------------
# Pass 3 — prune dead-end stubs (two tiers)
# ---------------------------------------------------------------------------

def _pass_prune_dead_ends(
    doc: SchematicDocument,
    pins: List[Tuple[float, float, str, str]],
    junctions: List[Tuple[float, float]],
    min_length_mm: float,
) -> Dict[str, Any]:
    """Two-tier wire pruning, each tier protected by the global edge-delta
    rollback:

      Tier 1 (short stub): wire length < `min_length_mm` AND AT LEAST ONE
        endpoint is not anchored to anything electrical. Catches the
        snap-leftover sub-mm stubs.
      Tier 2 (both ends dead): regardless of length, wires whose BOTH
        endpoints are unanchored. These are Claude's "I drew a wire
        in space" mistakes — long enough to not match Tier 1 but
        electrically meaningless. Aggressive but safe — a real signal
        wire always has at least one anchored end.

    Anchoring sources: pin tip, junction, another wire's endpoint, label
    anchor, OR a power-port symbol. The label/power-port sources were
    missing in v1 — without them, a wire from a pin to a label was
    pruned because the label's anchor wasn't recognised, which silently
    disconnected the pin (and edge-delta rollback didn't fire because
    the broken connection went label → pin, not graph-edge inter-component)."""
    # Build other-wire endpoint set.
    other_endpoints: Dict[Tuple[float, float], int] = {}
    for w in _wire_segments(doc):
        _, _, p1, p2 = w
        for p in (p1, p2):
            key = (round(p[0], 2), round(p[1], 2))
            other_endpoints[key] = other_endpoints.get(key, 0) + 1

    # Label anchors — every (label/global_label/hierarchical_label) (at x y).
    # A wire endpoint coinciding with a label position IS anchored (the
    # label binds the net name to that coord).
    label_set: set = set()
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) in
                ("label", "global_label", "hierarchical_label")):
            continue
        at = next((s for s in child[1:]
                   if isinstance(s, list) and _head(s) == "at"
                   and len(s) >= 3), None)
        if not at:
            continue
        try:
            label_set.add((round(float(at[1]), 2), round(float(at[2]), 2)))
        except (TypeError, ValueError):
            continue

    # Power-port symbol anchors — power-port (#PWR / #FLG) (at x y) marks
    # an electrical join point that wires terminate against. Excluded
    # from pins list (those are pin tips of REAL components only).
    power_set: set = set()
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "symbol"):
            continue
        lib_id_node = next((s for s in child[1:]
                            if isinstance(s, list) and _head(s) == "lib_id"
                            and len(s) >= 2), None)
        lib_id = _to_str(lib_id_node[1]) if lib_id_node else ""
        at_node = next((s for s in child[1:]
                        if isinstance(s, list) and _head(s) == "at"
                        and len(s) >= 3), None)
        if not at_node:
            continue
        # Use the symbol's anchor point. Power-port symbols connect at
        # their anchor (the small triangle/arrow at the lib-symbol origin).
        if lib_id.startswith("power:"):
            try:
                power_set.add((round(float(at_node[1]), 2),
                                round(float(at_node[2]), 2)))
            except (TypeError, ValueError):
                pass

    def _anchored(p: Tuple[float, float]) -> bool:
        if _endpoint_on_pin(p, pins):
            return True
        for jx, jy in junctions:
            if abs(jx - p[0]) < 0.05 and abs(jy - p[1]) < 0.05:
                return True
        key = (round(p[0], 2), round(p[1], 2))
        if other_endpoints.get(key, 0) >= 2:
            return True
        if key in label_set:
            return True
        if key in power_set:
            return True
        return False

    pruned_short = 0
    pruned_both_dead = 0
    to_remove: List[int] = []
    for idx, child in enumerate(doc.tree[1:], start=1):
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        pts_node = next((s for s in child[1:]
                         if isinstance(s, list) and _head(s) == "pts"), None)
        if not pts_node:
            continue
        pts: List[Tuple[float, float]] = []
        for xy in pts_node[1:]:
            if (isinstance(xy, list) and _head(xy) == "xy"
                    and len(xy) >= 3):
                try:
                    pts.append((float(xy[1]), float(xy[2])))
                except (TypeError, ValueError):
                    pass
        if len(pts) < 2:
            continue
        p1, p2 = pts[0], pts[1]
        length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        a1 = _anchored(p1)
        a2 = _anchored(p2)
        if length < min_length_mm and (not a1 or not a2):
            to_remove.append(idx)
            pruned_short += 1
        elif (not a1) and (not a2):
            # Both endpoints dead — wire connects nothing, regardless of length.
            to_remove.append(idx)
            pruned_both_dead += 1

    for idx in sorted(to_remove, reverse=True):
        del doc.tree[idx]

    return {
        "pruned": pruned_short + pruned_both_dead,
        "pruned_short": pruned_short,
        "pruned_both_dead": pruned_both_dead,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _build_graph_edges(sch_path: str) -> int:
    try:
        from .layout.connectivity_graph import build_graph
        return build_graph(sch_path).number_of_edges()
    except Exception:
        return -1


def normalize_wire_endpoints(
    doc: SchematicDocument,
    tolerance_mm: Optional[float] = None,
    label_tolerance_mm: Optional[float] = None,
    max_gap_mm: Optional[float] = None,
    prune_below_mm: Optional[float] = None,
) -> Dict[str, Any]:
    """Run all repair passes in order, measuring edge_count globally and
    rolling back the whole batch if the net effect regressed.

    Returns a flat stats dict with per-pass counters and the global delta.
    """
    tol = float(tolerance_mm) if tolerance_mm is not None else _cfg("tolerance_mm")
    lbl_tol = (float(label_tolerance_mm) if label_tolerance_mm is not None
               else _cfg("label_tolerance_mm"))
    gap = float(max_gap_mm) if max_gap_mm is not None else _cfg("max_gap_mm")
    prune = float(prune_below_mm) if prune_below_mm is not None else _cfg("prune_below_mm")

    sch_path = str(getattr(doc, "path", ""))
    if not sch_path:
        return {
            "error": "doc has no path; cannot normalize",
            "tolerance_mm": tol, "max_gap_mm": gap, "prune_below_mm": prune,
            "edges_before": -1, "edges_after": -1,
        }

    try:
        doc.save()
    except Exception:
        pass

    edges_before = _build_graph_edges(sch_path)
    pre_tree = copy.deepcopy(doc.tree)

    # Rebuild pin / junction indices once — they don't change during the
    # passes (snap/gap/prune only touch wires).
    extractor = SchematicExtractor(sch_path)
    pins = _build_pin_index(extractor)
    junctions = _build_junction_set(doc)

    pass_stats: Dict[str, Any] = {}
    pass_stats["snap_1"] = _pass_snap_to_pins(doc, pins, tol)
    pass_stats["label_snap"] = _pass_snap_labels_to_pins(doc, pins, lbl_tol)
    pass_stats["gap"] = _pass_close_collinear_gaps(doc, gap, pins)
    # T-junction auto-insertion runs BEFORE pruning so a wire that ends on
    # another wire's interior is recognised as anchored (via the new junction)
    # instead of being pruned as a dead-end.
    pass_stats["t_junctions"] = _pass_insert_t_junctions(doc, pins)
    # Junctions changed — rebuild the index used by prune.
    junctions = _build_junction_set(doc)
    pass_stats["prune"] = _pass_prune_dead_ends(doc, pins, junctions, prune)
    # Second snap — gap closure may have created new opportunities for
    # the other wire's loose endpoint to snap; cheap to re-run.
    pass_stats["snap_2"] = _pass_snap_to_pins(doc, pins, tol)
    # Auto-no_connect runs LAST so it only sees pins that survived every
    # repair pass (snap, gap, t-junction, prune, snap_2). A pin that's
    # floating at this point is genuinely unused — safe candidate for NC.
    pass_stats["auto_nc"] = _pass_auto_no_connect(doc, extractor)

    try:
        doc.save()
    except Exception:
        pass

    edges_after = _build_graph_edges(sch_path)

    rolled_back = False
    if (edges_before >= 0 and edges_after >= 0
            and edges_after < edges_before):
        doc.tree[:] = pre_tree
        try:
            doc.save()
        except Exception:
            pass
        edges_after = edges_before
        rolled_back = True
        for k in pass_stats:
            pass_stats[k] = {
                **{kk: 0 for kk in pass_stats[k]},
                "rolled_back": True,
            }

    total_snapped = (pass_stats["snap_1"].get("snapped", 0)
                     + pass_stats["snap_2"].get("snapped", 0))
    return {
        "tolerance_mm": tol,
        "max_gap_mm": gap,
        "prune_below_mm": prune,
        "endpoints_snapped": total_snapped,
        "labels_snapped": pass_stats["label_snap"].get("snapped", 0),
        "wires_modified": (pass_stats["snap_1"].get("wires_modified", 0)
                            + pass_stats["snap_2"].get("wires_modified", 0)),
        "max_delta_mm": max(pass_stats["snap_1"].get("max_delta_mm", 0.0),
                             pass_stats["snap_2"].get("max_delta_mm", 0.0)),
        "gaps_closed": pass_stats["gap"].get("gaps_closed", 0),
        "max_gap_closed_mm": pass_stats["gap"].get("max_gap_closed_mm", 0.0),
        "t_junctions_inserted": pass_stats["t_junctions"].get("inserted", 0),
        "wires_pruned": pass_stats["prune"].get("pruned", 0),
        "wires_pruned_both_dead": pass_stats["prune"].get("pruned_both_dead", 0),
        "auto_nc_inserted": pass_stats["auto_nc"].get("inserted", 0),
        "auto_nc_by_tier": pass_stats["auto_nc"].get("by_tier", {}),
        "edges_before": edges_before,
        "edges_after": edges_after,
        "edges_recovered": (edges_after - edges_before
                             if (edges_before >= 0 and edges_after >= 0)
                             else None),
        "rolled_back": rolled_back,
        "passes": pass_stats,
    }
