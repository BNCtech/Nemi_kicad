"""Step 4c — promote local label-connectivity to direct wires.

The default router emits a label at every pin on every named signal net.
That's electrically valid (same-name labels merge) but visually wrong for
SHORT-RANGE connections — humans expect a direct wire between adjacent
pins, not a label-pair floating in space. This pass closes that gap:

  for every label-named net:
    - find every pin endpoint named by this net
    - if max pairwise distance < `max_distance_mm`:
        replace labels with a Manhattan wire chain connecting the pins
        (router._pick_l_corner provides body-avoidance);
        labels for this net are dropped from routed.net_labels.
    - else (long-distance net): leave the labels untouched.

The threshold defaults to 80 mm — about a third of an A4 sheet width.
Anything beyond that is genuinely a global net (3V3 / GND substitute /
debug rail) where the label-pair convention is the right call.

Power nets (treated separately by the router via power-port symbols)
are untouched.

Wire chain is sequential (pin_0 → pin_1 → pin_2 → ...) sorted by an
axis-aware ordering so the rendered chain follows a natural left-right
or top-bottom flow when possible. Each segment uses router._pick_l_corner
for body avoidance.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple


def _pin_pairwise_max_distance(pts: List[Tuple[float, float]]) -> float:
    if len(pts) < 2:
        return 0.0
    best = 0.0
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            d = math.hypot(a[0] - b[0], a[1] - b[1])
            if d > best:
                best = d
    return best


def _order_pins_for_chain(
    pts: List[Tuple[float, float]],
    forced_axis: Optional[str] = None,
) -> List[Tuple[float, float]]:
    """Sort pins so the wire chain follows a natural axis direction.

    Heuristic: compute the bbox aspect. If span_x > span_y the chain
    follows X (left → right); otherwise Y (top → bottom). Ties default
    to X. Keeps the rendered wire visually linear instead of zigzagging
    across the cluster.

    P14.3 — `forced_axis` ("x" / "y") overrides the heuristic. The
    bus-aware promotion path uses this to enforce that every member of
    a bus chains along the SAME axis, so the rendered bus runs as
    parallel lanes instead of randomly-oriented chains."""
    if len(pts) <= 2:
        return list(pts)
    if forced_axis == "x":
        return sorted(pts, key=lambda p: (p[0], p[1]))
    if forced_axis == "y":
        return sorted(pts, key=lambda p: (p[1], p[0]))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    if span_x >= span_y:
        return sorted(pts, key=lambda p: (p[0], p[1]))
    return sorted(pts, key=lambda p: (p[1], p[0]))


def promote_local_labels_to_wires(
    routed: Dict[str, Any],
    placement: Dict[str, Any],
    schematic_path,
    max_distance_mm: float = 80.0,
    min_pins_per_net: int = 2,
) -> Dict[str, Any]:
    """Mutates `routed` in place. Returns stats dict.

    Logic per label-named signal net:
      1. Collect every (x_mm, y_mm) pin endpoint named by this net
         (using the router's net_labels — every label entry carries
         pin_x_mm / pin_y_mm via label_placer).
      2. If max pairwise distance > max_distance_mm: skip (long-haul
         net — keep labels).
      3. Otherwise: order the pins along the dominant axis, generate
         Manhattan L-corner wires connecting consecutive pins (with
         body-avoidance via router._pick_l_corner), and remove the
         labels for this net from routed.net_labels.
    """
    # Local imports to avoid circular load.
    try:
        from .router import _pick_l_corner
        from .label_placer import _build_body_bbox_by_ref
    except ImportError:
        from ai_backend.kicad_claude.layout.router import _pick_l_corner  # type: ignore
        from ai_backend.kicad_claude.layout.label_placer import _build_body_bbox_by_ref  # type: ignore

    try:
        body_bboxes = _build_body_bbox_by_ref(placement, schematic_path)
    except Exception:
        body_bboxes = {}

    net_labels = routed.get("net_labels", []) or []
    # Group labels by net text. Each label entry carries pin_x_mm / pin_y_mm
    # populated by label_placer; if missing, fall back to x_mm / y_mm (which
    # is the LABEL position after collision resolution — close enough for
    # short-haul distance check, but the wire chain prefers pin coords).
    by_net: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for lb in net_labels:
        # Don't promote labels already marked as hierarchical (cross-sheet).
        if lb.get("hierarchical"):
            continue
        text = (lb.get("text") or "").strip()
        if not text:
            continue
        by_net[text].append(lb)

    # P14.3 — pre-compute bus membership for the nets we're about to
    # promote. Same-bus members will be forced onto a single chain axis
    # so the rendered wires read as parallel lanes (not random chains).
    bus_axis_by_net: Dict[str, str] = {}
    try:
        from . import bus_semantics as _bus
        synth_nets = [{"name": n, "members": []} for n in by_net.keys()]
        detected_buses = _bus.detect_buses(synth_nets)
        for b in detected_buses:
            if b["width"] < 2:
                continue
            # Compute the union pin-bbox across every member's labels
            # to pick a single axis for the whole bus.
            all_pts: List[Tuple[float, float]] = []
            for m in b["members"]:
                nm = m["net"]
                for lb in by_net.get(nm, []):
                    px = lb.get("pin_x_mm") if lb.get("pin_x_mm") is not None else lb.get("x_mm")
                    py = lb.get("pin_y_mm") if lb.get("pin_y_mm") is not None else lb.get("y_mm")
                    if px is not None and py is not None:
                        all_pts.append((float(px), float(py)))
            if len(all_pts) < 2:
                continue
            xs = [p[0] for p in all_pts]
            ys = [p[1] for p in all_pts]
            axis = "x" if (max(xs) - min(xs)) >= (max(ys) - min(ys)) else "y"
            for m in b["members"]:
                bus_axis_by_net[m["net"]] = axis
    except Exception:
        bus_axis_by_net = {}

    nets_promoted = 0
    nets_kept = 0
    wires_added: List[Dict[str, Any]] = []
    labels_to_remove_ids: set = set()
    max_promoted_distance = 0.0
    bus_lanes_aligned = 0

    for text, lbls in by_net.items():
        if len(lbls) < min_pins_per_net:
            nets_kept += 1
            continue
        pin_pts: List[Tuple[float, float]] = []
        for lb in lbls:
            px = lb.get("pin_x_mm")
            py = lb.get("pin_y_mm")
            if px is None or py is None:
                # Label_placer didn't run — fall back to label anchor.
                px, py = lb.get("x_mm"), lb.get("y_mm")
            if px is None or py is None:
                continue
            pin_pts.append((float(px), float(py)))
        if len(pin_pts) < min_pins_per_net:
            nets_kept += 1
            continue

        max_d = _pin_pairwise_max_distance(pin_pts)
        if max_d > max_distance_mm:
            nets_kept += 1
            continue

        # Dedupe identical pin coords (multi-unit ICs may have the same
        # net on multiple instances at the same coord — one wire per
        # endpoint, not per label).
        seen_pts: set = set()
        unique_pts: List[Tuple[float, float]] = []
        for p in pin_pts:
            key = (round(p[0], 2), round(p[1], 2))
            if key in seen_pts:
                continue
            seen_pts.add(key)
            unique_pts.append(p)
        if len(unique_pts) < 2:
            nets_kept += 1
            continue

        forced_axis = bus_axis_by_net.get(text)
        ordered = _order_pins_for_chain(unique_pts, forced_axis=forced_axis)
        if forced_axis:
            bus_lanes_aligned += 1
        # Connect pin_i → pin_{i+1} with Manhattan L-corner segments.
        for i in range(len(ordered) - 1):
            x1, y1 = ordered[i]
            x2, y2 = ordered[i + 1]
            if abs(x1 - x2) < 0.01 or abs(y1 - y2) < 0.01:
                wires_added.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                     "_net": text, "_local_promoted": True})
                continue
            cx, cy = _pick_l_corner(x1, y1, x2, y2, body_bboxes)
            wires_added.append({"x1": x1, "y1": y1, "x2": cx, "y2": cy,
                                 "_net": text, "_local_promoted": True})
            wires_added.append({"x1": cx, "y1": cy, "x2": x2, "y2": y2,
                                 "_net": text, "_local_promoted": True})
        # Mark these labels for removal — the wires replace them.
        for lb in lbls:
            labels_to_remove_ids.add(id(lb))
        nets_promoted += 1
        if max_d > max_promoted_distance:
            max_promoted_distance = max_d

    # Filter labels (don't mutate the original list while iterating elsewhere).
    routed["net_labels"] = [lb for lb in net_labels
                             if id(lb) not in labels_to_remove_ids]

    # Append promoted wires to stub_wires so the emitter writes them.
    stub_wires = list(routed.get("stub_wires", []) or [])
    stub_wires.extend(wires_added)
    routed["stub_wires"] = stub_wires

    return {
        "nets_promoted":        nets_promoted,
        "nets_kept_long_haul":  nets_kept,
        "wires_added":          len(wires_added),
        "labels_removed":       len(labels_to_remove_ids),
        "max_promoted_distance_mm": round(max_promoted_distance, 2),
        "bus_lanes_aligned":    bus_lanes_aligned,
    }
