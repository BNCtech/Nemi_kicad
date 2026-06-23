"""layout/place_refine.py — placement quality post-pass for a parsed .kicad_pcb.

The three placement modes in ``tools/auto_place_pcb.py`` (block-zone, IC-anchor,
prefix-grid) drop footprints on grids / radii **without ever checking whether two
footprints physically overlap** and without trying to shorten the connections.
A big QFN sub-grid cell can be smaller than the part; two decouplers can land on
the same anchor-radius slot; an edge connector and a passive can collide. The
result is courtyard-overlap DRC violations and an unroutable board.

This module is the missing *refinement* stage the research called for
("rule-driven placement + light SA/force-directed refinement"):

  1. **De-collision** — compute each footprint's real bounding box (its
     ``*.CrtYd`` courtyard when present, else its pad extents + a margin) and
     iteratively push overlapping pairs apart along their minimum-translation
     axis until every pair clears ``clearance_mm``. Parts whose refdes prefix is
     in ``fixed_ref_prefixes`` (mounting holes, fiducials) never move; everything
     else moves as little as possible.

  2. **Wirelength refine** (optional, conservative) — a few force-directed sweeps
     that nudge each movable part toward the centroid of the pads it connects to,
     accepting a move ONLY when it strictly reduces that part's net half-perimeter
     wirelength AND introduces no new overlap. This is a hill-climb, never an
     anneal that can get worse, so it is safe to leave on by default.

Operates **in place** on the ``sexpdata``-parsed ``(kicad_pcb ...)`` root (mutates
each footprint's outer ``(at ...)`` clause) and returns a report dict. It never
raises on a malformed node — bad footprints are skipped and counted.

Config: ``layout_config.json:auto_place_pcb.refine`` (all keys optional). With
``enabled=false`` the caller skips this pass entirely, so output stays byte-stable
with the pre-refine behaviour.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

# Axis-aligned bounding box in board mm: (xmin, ymin, xmax, ymax).
BBox = Tuple[float, float, float, float]


# --------------------------------------------------------------------------- #
# s-expr helpers (self-contained — matches the per-tool convention)
# --------------------------------------------------------------------------- #

def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _children(node: list, name: str) -> List[list]:
    return [c for c in node[1:]
            if isinstance(c, list) and _head(c) == name] if isinstance(node, list) else []


def _child(node: list, name: str) -> Optional[list]:
    for c in _children(node, name):
        return c
    return None


def _ref_of_footprint(fp: list) -> Optional[str]:
    for child in fp[1:]:
        if (isinstance(child, list) and _head(child) == "property"
                and len(child) >= 3 and str(child[1]) == "Reference"):
            return str(child[2])
        if (isinstance(child, list) and _head(child) == "fp_text"
                and len(child) >= 3
                and isinstance(child[1], sexpdata.Symbol)
                and child[1].value() == "reference"):
            return str(child[2])
    return None


def _outer_at(fp: list) -> Optional[list]:
    for child in fp[1:]:
        if isinstance(child, list) and _head(child) == "at":
            return child
    return None


def _at_xyr(node: list) -> Tuple[float, float, float]:
    at = _outer_at(node)
    if at is None:
        return (0.0, 0.0, 0.0)
    try:
        x = float(at[1]); y = float(at[2])
        r = float(at[3]) if len(at) > 3 else 0.0
        return (x, y, r)
    except (IndexError, TypeError, ValueError):
        return (0.0, 0.0, 0.0)


def _set_outer_at(fp: list, x: float, y: float) -> bool:
    at = _outer_at(fp)
    if at is None:
        return False
    at[1] = round(x / 0.001) * 0.001
    at[2] = round(y / 0.001) * 0.001
    if len(at) == 3:
        at.append(0.0)            # keep rotation slot present
    return True


def _prefix_of_ref(ref: str) -> str:
    i = len(ref) - 1
    while i >= 0 and ref[i].isdigit():
        i -= 1
    return ref[: i + 1] if i >= 0 else ref


def _rotate(dx: float, dy: float, rot_deg: float) -> Tuple[float, float]:
    if not rot_deg:
        return dx, dy
    a = math.radians(rot_deg)
    c, s = math.cos(a), math.sin(a)
    return c * dx - s * dy, s * dx + c * dy


# --------------------------------------------------------------------------- #
# Footprint geometry — local bbox (independent of board position)
# --------------------------------------------------------------------------- #

def _pad_local_at(pad: list) -> Tuple[float, float, float]:
    for child in pad[1:]:
        if isinstance(child, list) and _head(child) == "at":
            try:
                x = float(child[1])
                y = float(child[2]) if len(child) >= 3 else 0.0
                r = float(child[3]) if len(child) >= 4 else 0.0
                return (x, y, r)
            except (TypeError, ValueError):
                return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def _pad_size(pad: list) -> Tuple[float, float]:
    sz = _child(pad, "size")
    if sz and len(sz) >= 3:
        try:
            return (float(sz[1]), float(sz[2]))
        except (TypeError, ValueError):
            return (0.0, 0.0)
    return (0.0, 0.0)


def _courtyard_points(fp: list) -> List[Tuple[float, float]]:
    """Local-coordinate points of any ``*.CrtYd`` courtyard graphics."""
    pts: List[Tuple[float, float]] = []
    for child in fp[1:]:
        if not isinstance(child, list):
            continue
        tag = _head(child)
        layer = _child(child, "layer")
        lname = str(layer[1]) if layer and len(layer) >= 2 else ""
        if "CrtYd" not in lname:
            continue
        if tag in ("fp_line", "fp_rect"):
            for end in ("start", "end"):
                n = _child(child, end)
                if n and len(n) >= 3:
                    try:
                        pts.append((float(n[1]), float(n[2])))
                    except (TypeError, ValueError):
                        pass
        elif tag in ("fp_poly", "fp_circle"):
            ptsnode = _child(child, "pts")
            if ptsnode:
                for xy in _children(ptsnode, "xy"):
                    if len(xy) >= 3:
                        try:
                            pts.append((float(xy[1]), float(xy[2])))
                        except (TypeError, ValueError):
                            pass
            cnode = _child(child, "center")
            enode = _child(child, "end")
            if cnode and enode and len(cnode) >= 3 and len(enode) >= 3:
                try:
                    cx, cy = float(cnode[1]), float(cnode[2])
                    ex, ey = float(enode[1]), float(enode[2])
                    rad = math.hypot(ex - cx, ey - cy)
                    pts += [(cx - rad, cy - rad), (cx + rad, cy + rad)]
                except (TypeError, ValueError):
                    pass
    return pts


def _local_bbox(fp: list, use_courtyard: bool,
                pad_margin: float) -> Optional[Tuple[float, float, float, float]]:
    """Footprint bbox in *local* (pre-rotation, pre-translation) coords.

    Prefers the courtyard outline (what KiCad's own courtyard-overlap DRC uses);
    falls back to the union of pad extents inflated by ``pad_margin``. Returns
    None when the footprint has neither (e.g. a pure graphic) — caller skips it.
    """
    if use_courtyard:
        cpts = _courtyard_points(fp)
        if cpts:
            xs = [p[0] for p in cpts]; ys = [p[1] for p in cpts]
            return (min(xs), min(ys), max(xs), max(ys))
    xs2: List[float] = []
    ys2: List[float] = []
    for pad in _children(fp, "pad"):
        px, py, _pr = _pad_local_at(pad)
        pw, ph = _pad_size(pad)
        if pw <= 0 and ph <= 0:
            continue
        xs2 += [px - pw / 2 - pad_margin, px + pw / 2 + pad_margin]
        ys2 += [py - ph / 2 - pad_margin, py + ph / 2 + pad_margin]
    if not xs2:
        return None
    return (min(xs2), min(ys2), max(xs2), max(ys2))


def _board_bbox(local: Tuple[float, float, float, float],
                x: float, y: float, rot: float) -> BBox:
    """Axis-aligned board-coordinate bbox of a local bbox placed at (x,y,rot)."""
    x0, y0, x1, y1 = local
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    rx = []; ry = []
    for cx, cy in corners:
        dx, dy = _rotate(cx, cy, rot)
        rx.append(x + dx); ry.append(y + dy)
    return (min(rx), min(ry), max(rx), max(ry))


# --------------------------------------------------------------------------- #
# Overlap math
# --------------------------------------------------------------------------- #

def _overlap_amounts(a: BBox, b: BBox, clearance: float
                     ) -> Tuple[float, float]:
    """Signed overlap on each axis after expanding by ``clearance``. Positive =
    overlapping; <=0 = clear. (ox, oy) where the parts overlap by ox in X and
    oy in Y simultaneously to be a real collision."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ox = min(ax1, bx1) - max(ax0, bx0) + clearance
    oy = min(ay1, by1) - max(ay0, by0) + clearance
    return (ox, oy)


def _centre(b: BBox) -> Tuple[float, float]:
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


# --------------------------------------------------------------------------- #
# Net / pad model for the wirelength refine
# --------------------------------------------------------------------------- #

def _pad_net_id(pad: list) -> int:
    n = _child(pad, "net")
    if n and len(n) >= 2:
        try:
            return int(n[1])
        except (TypeError, ValueError):
            return 0
    return 0


def _pad_board_xy(fp_x: float, fp_y: float, fp_rot: float,
                  pad: list) -> Tuple[float, float]:
    lx, ly, _r = _pad_local_at(pad)
    dx, dy = _rotate(lx, ly, fp_rot)
    return (fp_x + dx, fp_y + dy)


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #

def refine_placement(root: list, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate the parsed ``(kicad_pcb ...)`` root to remove footprint overlaps
    and (optionally) shorten connections. Returns a report dict.

    ``cfg`` is the ``auto_place_pcb.refine`` sub-config. Never raises.
    """
    clearance     = float(cfg.get("clearance_mm", 0.5))
    use_courtyard = bool(cfg.get("use_courtyard", True))
    pad_margin    = float(cfg.get("pad_bbox_margin_mm", 0.25))
    max_iters     = int(cfg.get("max_overlap_iters", 60))
    push_step_cap = float(cfg.get("max_push_mm", 25.0))
    fixed_prefixes = set(str(p).upper() for p in cfg.get(
        "fixed_ref_prefixes", ["H", "MH", "FID", "MK", "NT", "REF"]))
    do_wl         = bool(cfg.get("wirelength_refine", True))
    wl_iters      = int(cfg.get("wl_iters", 3))
    wl_max_step   = float(cfg.get("wl_max_step_mm", 5.0))
    # Connectors are edge-snapped by the placement mode (and by the user's
    # connectors-at-edge rule). The wirelength pull would drag them inward
    # toward the parts they feed, so they are de-collided but never wl-moved.
    wl_skip_prefixes = set(str(p).upper() for p in cfg.get(
        "wl_skip_ref_prefixes", ["J", "P"]))

    # ---- Collect movable footprints + their local bboxes ----
    fps: List[list] = [c for c in root[1:]
                       if isinstance(c, list) and _head(c) == "footprint"]
    items: List[Dict[str, Any]] = []
    skipped_no_bbox = 0
    for fp in fps:
        ref = _ref_of_footprint(fp) or ""
        local = _local_bbox(fp, use_courtyard, pad_margin)
        if local is None:
            skipped_no_bbox += 1
            continue
        x, y, rot = _at_xyr(fp)
        fixed = _prefix_of_ref(ref).upper() in fixed_prefixes
        items.append({"fp": fp, "ref": ref, "local": local,
                      "x": x, "y": y, "rot": rot, "fixed": fixed})

    if len(items) < 2:
        return {"ok": True, "refined": False, "overlaps_before": 0,
                "overlaps_after": 0, "moved": 0,
                "note": "fewer than 2 footprints with geometry"}

    def bbox_of(it: Dict[str, Any]) -> BBox:
        return _board_bbox(it["local"], it["x"], it["y"], it["rot"])

    def count_overlaps() -> int:
        n = 0
        boxes = [bbox_of(it) for it in items]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                ox, oy = _overlap_amounts(boxes[i], boxes[j], clearance)
                if ox > 1e-6 and oy > 1e-6:
                    n += 1
        return n

    overlaps_before = count_overlaps()

    # ---- Phase 1: iterative pairwise de-collision ----
    moved_refs: set = set()
    for _ in range(max_iters):
        boxes = [bbox_of(it) for it in items]
        any_fix = False
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = boxes[i], boxes[j]
                ox, oy = _overlap_amounts(a, b, clearance)
                if ox <= 1e-6 or oy <= 1e-6:
                    continue
                any_fix = True
                ia, ib = items[i], items[j]
                # Push along the axis of *least* penetration (minimum translation).
                acx, acy = _centre(a); bcx, bcy = _centre(b)
                if ox <= oy:
                    push = min(ox, push_step_cap)
                    dirn = 1.0 if bcx >= acx else -1.0
                    dxa, dya, dxb, dyb = -dirn * push, 0.0, dirn * push, 0.0
                else:
                    push = min(oy, push_step_cap)
                    dirn = 1.0 if bcy >= acy else -1.0
                    dxa, dya, dxb, dyb = 0.0, -dirn * push, 0.0, dirn * push
                # Distribute the push: both move half; if one is fixed the other
                # takes the full push so the fixed part stays put.
                if ia["fixed"] and ib["fixed"]:
                    continue                       # can't separate two fixed parts
                elif ia["fixed"]:
                    ib["x"] += dxb * 2; ib["y"] += dyb * 2
                    moved_refs.add(ib["ref"])
                elif ib["fixed"]:
                    ia["x"] += dxa * 2; ia["y"] += dya * 2
                    moved_refs.add(ia["ref"])
                else:
                    ia["x"] += dxa; ia["y"] += dya
                    ib["x"] += dxb; ib["y"] += dyb
                    moved_refs.add(ia["ref"]); moved_refs.add(ib["ref"])
                boxes[i] = bbox_of(ia); boxes[j] = bbox_of(ib)
        if not any_fix:
            break

    # ---- Phase 2: conservative wirelength hill-climb ----
    if do_wl:
        # Build net -> list of (item_index, pad_board_xy) using CURRENT positions
        # for fixed parts (anchors) and live positions for movable ones.
        # Pads of movable parts are recomputed each move from the footprint's
        # local pad offsets.
        net_pads: Dict[int, List[Tuple[int, list]]] = {}
        for idx, it in enumerate(items):
            for pad in _children(it["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid > 0:
                    net_pads.setdefault(nid, []).append((idx, pad))

        def item_net_hpwl(idx: int) -> float:
            """Sum of half-perimeter wirelength of every net this item touches,
            using current item positions."""
            it = items[idx]
            seen: set = set()
            total = 0.0
            for pad in _children(it["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid <= 0 or nid in seen:
                    continue
                seen.add(nid)
                pts = []
                for (oidx, opad) in net_pads.get(nid, []):
                    oit = items[oidx]
                    pts.append(_pad_board_xy(oit["x"], oit["y"], oit["rot"], opad))
                if len(pts) >= 2:
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    total += (max(xs) - min(xs)) + (max(ys) - min(ys))
            return total

        def item_centroid_pull(idx: int) -> Optional[Tuple[float, float]]:
            """Centroid of all OTHER pads on the nets this item connects to."""
            it = items[idx]
            xs: List[float] = []; ys: List[float] = []
            for pad in _children(it["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid <= 0:
                    continue
                for (oidx, opad) in net_pads.get(nid, []):
                    if oidx == idx:
                        continue
                    ox, oy = _pad_board_xy(items[oidx]["x"], items[oidx]["y"],
                                           items[oidx]["rot"], opad)
                    xs.append(ox); ys.append(oy)
            if not xs:
                return None
            return (sum(xs) / len(xs), sum(ys) / len(ys))

        def creates_overlap(idx: int) -> bool:
            a = bbox_of(items[idx])
            for j in range(len(items)):
                if j == idx:
                    continue
                ox, oy = _overlap_amounts(a, bbox_of(items[j]), clearance)
                if ox > 1e-6 and oy > 1e-6:
                    return True
            return False

        for _sweep in range(wl_iters):
            improved = False
            for idx, it in enumerate(items):
                if it["fixed"]:
                    continue
                if _prefix_of_ref(it["ref"]).upper() in wl_skip_prefixes:
                    continue                       # keep edge-snapped connectors put
                pull = item_centroid_pull(idx)
                if pull is None:
                    continue
                before = item_net_hpwl(idx)
                ox, oy = it["x"], it["y"]
                vx, vy = pull[0] - ox, pull[1] - oy
                dist = math.hypot(vx, vy)
                if dist < 1e-6:
                    continue
                step = min(wl_max_step, dist)
                it["x"] = ox + vx / dist * step
                it["y"] = oy + vy / dist * step
                after = item_net_hpwl(idx)
                # Accept only if strictly better AND no new collision.
                if after < before - 1e-6 and not creates_overlap(idx):
                    moved_refs.add(it["ref"])
                    improved = True
                else:
                    it["x"], it["y"] = ox, oy      # revert
            if not improved:
                break

    # ---- Write positions back into the footprint nodes ----
    applied = 0
    for it in items:
        if _set_outer_at(it["fp"], it["x"], it["y"]):
            applied += 1

    overlaps_after = count_overlaps()
    return {
        "ok": True,
        "refined": True,
        "overlaps_before": overlaps_before,
        "overlaps_after": overlaps_after,
        "moved": len(moved_refs),
        "footprints": len(items),
        "skipped_no_geometry": skipped_no_bbox,
        "clearance_mm": clearance,
        "wirelength_refine": do_wl,
    }
