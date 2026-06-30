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


def _set_outer_at(fp: list, x: float, y: float,
                  rot: Optional[float] = None) -> bool:
    at = _outer_at(fp)
    if at is None:
        return False
    at[1] = round(x / 0.001) * 0.001
    at[2] = round(y / 0.001) * 0.001
    if rot is not None:
        rv = round(rot % 360.0, 3)
        if len(at) >= 4:
            at[3] = rv
        else:
            at.append(rv)
    elif len(at) == 3:
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
    """Axis-aligned board-coordinate bbox of a local bbox placed at (x,y,rot).

    KiCad places a footprint by rotating its geometry CLOCKWISE by the orientation
    (board Y points down), i.e. by ``-rot``. Using ``+rot`` put a rotated part's
    courtyard on the WRONG side, so de-collision missed real overlaps that KiCad's
    courtyard DRC flags (e.g. a 90 deg-rotated LED vs a round cap). rot==0 is
    unchanged (byte-stable). Matches the same fix already applied to the router."""
    x0, y0, x1, y1 = local
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    rx = []; ry = []
    for cx, cy in corners:
        dx, dy = _rotate(cx, cy, -rot)
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
    # KiCad rotates pad offsets CLOCKWISE by the footprint orientation (-fp_rot),
    # board Y down — same convention as the router fix. fp_rot==0 is unchanged.
    lx, ly, _r = _pad_local_at(pad)
    dx, dy = _rotate(lx, ly, -fp_rot)
    return (fp_x + dx, fp_y + dy)


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #

def refine_placement(root: list, cfg: Dict[str, Any],
                     constraints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Mutate the parsed ``(kicad_pcb ...)`` root to remove footprint overlaps
    and (optionally) shorten connections. Returns a report dict.

    ``cfg`` is the ``auto_place_pcb.refine`` sub-config. ``constraints`` is the
    optional per-ref electrical-reasoning map from ``tools/pcb_reasoning``
    (``{ref: {role, characters, score, constraints{...}}}``); when present and
    ``constraint_placement.enabled`` it drives Phase 3 (keep-close / keep-away).
    Never raises.
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
    # Rotation optimizer (Gap 1): try orthogonal re-orientations of each movable
    # part and keep the one that shortens its connections — purely cost-driven,
    # so it generalises to any board with no per-part knowledge. Conservative
    # hill-climb (accept only a strict HPWL win with no new overlap), exactly
    # like the wirelength translate below. Default off keeps output byte-stable;
    # the live config turns it on.
    do_rot        = bool(cfg.get("rotation_optimize", False))
    rot_angles    = [float(a) for a in cfg.get(
        "rotation_angles", [0.0, 90.0, 180.0, 270.0])]
    # Connectors / mechanical parts keep their orientation (edge-snapped, keyed).
    rot_skip_prefixes = set(str(p).upper() for p in cfg.get(
        "rot_skip_ref_prefixes", ["J", "P", "H", "MH", "FID", "MK", "NT", "REF"]))
    # High-fanout nets (GND/VCC rails) connect to many pads, so their centroid
    # sits near the board centre and the attraction toward it cancels the
    # signal-driven pull — leaving parts scattered. The fix is purely topological
    # (net DEGREE, not net names): every attraction/cost pass ignores nets whose
    # pad count exceeds wl_ignore_net_degree, so parts cluster by their SIGNAL
    # connections. 0 = consider every net (byte-stable legacy behaviour).
    ignore_degree    = int(cfg.get("wl_ignore_net_degree", 0))
    # A fixed degree threshold is board-specific (degree-4 is a rail on a 15-part
    # board but a normal signal net on a 100-part board). The dynamic rule scales
    # the cutoff with the part count: a net is a rail when its fanout exceeds
    # max(wl_rail_min_degree, ceil(wl_rail_fraction * num_parts)). Set
    # wl_rail_fraction=0 to fall back to the absolute wl_ignore_net_degree.
    rail_fraction    = float(cfg.get("wl_rail_fraction", 0.0))
    rail_min_degree  = int(cfg.get("wl_rail_min_degree", 4))
    # Phase-0 clustering: a bold force-directed pre-pass that pulls each movable
    # part most of the way to the centroid of its signal-net neighbours BEFORE
    # de-collision. Unlike the strict Phase-2 hill-climb it accepts every move
    # (de-collision cleans up overlaps after), so it can form tight functional
    # clusters that a timid per-part climb starting from a scattered grid cannot.
    do_cluster       = bool(cfg.get("cluster_placement", False))
    cluster_iters    = int(cfg.get("cluster_iters", 8))
    cluster_strength = float(cfg.get("cluster_strength", 0.6))
    cluster_max_step = float(cfg.get("cluster_max_step_mm", 20.0))

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
    moved_refs: set = set()

    # ---- Shared net model: pad membership + per-net degree ----
    net_pads_all: Dict[int, List[Tuple[int, list]]] = {}
    for idx, it in enumerate(items):
        for pad in _children(it["fp"], "pad"):
            nid = _pad_net_id(pad)
            if nid > 0:
                net_pads_all.setdefault(nid, []).append((idx, pad))
    net_degree = {nid: len(v) for nid, v in net_pads_all.items()}
    # Effective rail cutoff: dynamic (scales with part count) when wl_rail_fraction
    # is set, else the absolute wl_ignore_net_degree, else disabled.
    if rail_fraction > 0.0:
        ignore_eff = max(rail_min_degree, math.ceil(rail_fraction * len(items)))
    else:
        ignore_eff = ignore_degree

    def _net_ok(nid: int) -> bool:
        """A net counts toward the attraction unless it is a high-fanout rail."""
        if ignore_eff <= 0:
            return True
        return net_degree.get(nid, 0) <= ignore_eff

    # ---- Phase 0: bold connectivity clustering (signal nets only) ----
    clustered = 0
    if do_cluster:
        def _signal_centroid(idx: int) -> Optional[Tuple[float, float]]:
            it = items[idx]
            xs: List[float] = []; ys: List[float] = []
            for pad in _children(it["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid <= 0 or not _net_ok(nid):
                    continue
                for (oidx, opad) in net_pads_all.get(nid, []):
                    if oidx == idx:
                        continue
                    px, py = _pad_board_xy(items[oidx]["x"], items[oidx]["y"],
                                           items[oidx]["rot"], opad)
                    xs.append(px); ys.append(py)
            if not xs:
                return None
            return (sum(xs) / len(xs), sum(ys) / len(ys))

        cluster_moved: set = set()
        for _ in range(cluster_iters):
            any_move = False
            for idx, it in enumerate(items):
                if it["fixed"]:
                    continue
                if _prefix_of_ref(it["ref"]).upper() in wl_skip_prefixes:
                    continue                       # connectors stay edge-snapped
                c = _signal_centroid(idx)
                if c is None:
                    continue
                vx, vy = c[0] - it["x"], c[1] - it["y"]
                dist = math.hypot(vx, vy)
                if dist < 1e-3:
                    continue
                step = min(cluster_max_step, dist * cluster_strength)
                it["x"] += vx / dist * step
                it["y"] += vy / dist * step
                any_move = True
                cluster_moved.add(it["ref"])
            if not any_move:
                break
        clustered = len(cluster_moved)
        moved_refs |= cluster_moved

    # ---- Phase 1: iterative pairwise de-collision ----
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

    # ---- Phase 1.5: orthogonal rotation optimizer (cost-driven) ----
    # For each movable part, try the configured re-orientations and keep the one
    # that strictly reduces the half-perimeter wirelength of the nets it touches
    # without creating a new courtyard overlap. The cost is measured from the
    # part's own pads, so a 90 deg turn that lines a passive up with its net wins
    # automatically — no NE555/timing-cluster special-casing.
    rotated = 0
    if do_rot:
        net_pads_r: Dict[int, List[Tuple[int, list]]] = {}
        for idx, it in enumerate(items):
            for pad in _children(it["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid > 0:
                    net_pads_r.setdefault(nid, []).append((idx, pad))

        def _rot_hpwl(idx: int) -> float:
            it = items[idx]
            seen: set = set()
            total = 0.0
            for pad in _children(it["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid <= 0 or nid in seen or not _net_ok(nid):
                    continue
                seen.add(nid)
                pts = []
                for (oidx, opad) in net_pads_r.get(nid, []):
                    oit = items[oidx]
                    pts.append(_pad_board_xy(oit["x"], oit["y"], oit["rot"], opad))
                if len(pts) >= 2:
                    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                    total += (max(xs) - min(xs)) + (max(ys) - min(ys))
            return total

        def _rot_overlaps(idx: int) -> bool:
            a = bbox_of(items[idx])
            for j in range(len(items)):
                if j == idx:
                    continue
                ox, oy = _overlap_amounts(a, bbox_of(items[j]), clearance)
                if ox > 1e-6 and oy > 1e-6:
                    return True
            return False

        for idx, it in enumerate(items):
            if it["fixed"]:
                continue
            if _prefix_of_ref(it["ref"]).upper() in rot_skip_prefixes:
                continue
            base = it["rot"]
            best = base
            best_cost = _rot_hpwl(idx)
            for d in rot_angles:
                if d == 0.0:
                    continue
                cand = (base + d) % 360.0
                it["rot"] = cand
                if _rot_overlaps(idx):
                    it["rot"] = base
                    continue
                cost = _rot_hpwl(idx)
                if cost < best_cost - 1e-6:
                    best_cost = cost
                    best = cand
                it["rot"] = base
            if best != base:
                it["rot"] = best
                rotated += 1
                moved_refs.add(it["ref"])

    # ---- Phase 1.6: orientation uniformity (P5, gated) ----
    # Same-class passives should face the same way (assembly / inspection DFM).
    # The wirelength rotation optimizer above can leave a group mixed; snap each
    # minority part of a refdes-prefix group to the group's MAJORITY orthogonal
    # orientation, but only when it creates no new overlap (so it never trades a
    # collision for tidiness). Gated by `orientation_uniformity.enabled` (off ->
    # byte-stable). Connectors / mechanical keep their keyed angle.
    ou_cfg = cfg.get("orientation_uniformity", {})
    if not isinstance(ou_cfg, dict):
        ou_cfg = {}
    oriented = 0
    do_ou = bool(ou_cfg.get("enabled", False))
    if do_ou:
        from collections import Counter
        min_group = int(ou_cfg.get("min_group", 3))

        def _ou_overlaps(idx: int) -> bool:
            a = bbox_of(items[idx])
            for j in range(len(items)):
                if j == idx:
                    continue
                ox, oy = _overlap_amounts(a, bbox_of(items[j]), clearance)
                if ox > 1e-6 and oy > 1e-6:
                    return True
            return False

        groups: Dict[str, List[int]] = {}
        for idx, it in enumerate(items):
            if it["fixed"]:
                continue
            pfx = _prefix_of_ref(it["ref"]).upper()
            if pfx in rot_skip_prefixes:
                continue
            groups.setdefault(pfx, []).append(idx)

        for pfx, idxs in groups.items():
            if len(idxs) < min_group:
                continue
            rots = [round(items[i]["rot"] % 360.0) for i in idxs]
            common = float(Counter(rots).most_common(1)[0][0])
            for i in idxs:
                if abs((items[i]["rot"] % 360.0) - common) < 1e-6:
                    continue
                base = items[i]["rot"]
                items[i]["rot"] = common
                if _ou_overlaps(i):
                    items[i]["rot"] = base          # never trade a collision for tidiness
                else:
                    oriented += 1
                    moved_refs.add(items[i]["ref"])

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
                if nid <= 0 or nid in seen or not _net_ok(nid):
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
                if nid <= 0 or not _net_ok(nid):
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

    # ---- Phase 3: constraint-aware placement (reasoning-driven, gated) ----
    # Consume the Phase-1 electrical-reasoning constraints
    # (tools/pcb_reasoning -> <board>.envil-constraints.json): pull each support
    # part toward the higher-criticality part it shares a net with (decoupling
    # <= max_dist_mm, clock, filter caps), and push domain-conflicting parts apart
    # (analog away from switching, RF keepout) by min_gap_mm. Conservative: every
    # move is reverted if it creates a real courtyard overlap. Gated by
    # `constraint_placement.enabled` (off / constraints=None -> byte-stable).
    cp_cfg = cfg.get("constraint_placement", {})
    if not isinstance(cp_cfg, dict):
        cp_cfg = {}
    kept_close = 0
    kept_apart = 0
    if constraints and cp_cfg.get("enabled", False):
        kc_iters    = int(cp_cfg.get("keep_close_iters", 4))
        ka_iters    = int(cp_cfg.get("keep_away_iters", 4))
        cp_max_step = float(cp_cfg.get("max_step_mm", 5.0))

        def _info(ref: str) -> Dict[str, Any]:
            return constraints.get(ref, {}) or {}

        def _place_cons(ref: str) -> Dict[str, Any]:
            return _info(ref).get("constraints", {}) or {}

        def _labels(idx: int) -> set:
            info = _info(items[idx]["ref"])
            s = set(info.get("characters", []) or [])
            if info.get("role"):
                s.add(info["role"])
            return s

        def _crit_key(idx: int) -> Tuple[int, float, int]:
            ref = items[idx]["ref"]
            return (1 if _place_cons(ref).get("is_anchor") else 0,
                    float(_info(ref).get("score", 0.0) or 0.0),
                    len(_children(items[idx]["fp"], "pad")))

        def _overlaps_idx(idx: int) -> bool:
            a = bbox_of(items[idx])
            for j in range(len(items)):
                if j == idx:
                    continue
                ox, oy = _overlap_amounts(a, bbox_of(items[j]), clearance)
                if ox > 1e-6 and oy > 1e-6:
                    return True
            return False

        def _shared_target(idx: int):
            """The highest-criticality OTHER part sharing a net with items[idx],
            and that part's pad on a shared net NEAREST to items[idx]. Targeting
            the nearest shared pad (not the first) keeps a decoupling cap on the
            IC edge next to its power pin instead of being dragged through the
            body toward a pad on the far side. Returns (target_idx, pad xy) or
            None."""
            it = items[idx]
            my_nets: set = set()
            for pad in _children(it["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid > 0:
                    my_nets.add(nid)
            cand: Dict[int, Tuple[int, float, int]] = {}
            for nid in my_nets:
                for (oidx, _opad) in net_pads_all.get(nid, []):
                    if oidx != idx:
                        cand[oidx] = _crit_key(oidx)
            if not cand:
                return None
            toidx = max(cand, key=lambda k: cand[k])
            best_pad: Optional[Tuple[float, float]] = None
            best_d: Optional[float] = None
            for pad in _children(items[toidx]["fp"], "pad"):
                nid = _pad_net_id(pad)
                if nid <= 0 or nid not in my_nets:
                    continue
                px, py = _pad_board_xy(items[toidx]["x"], items[toidx]["y"],
                                       items[toidx]["rot"], pad)
                dd = math.hypot(px - it["x"], py - it["y"])
                if best_d is None or dd < best_d:
                    best_d = dd
                    best_pad = (px, py)
            if best_pad is None:
                return None
            return toidx, best_pad

        # -- keep-close: a support part hugs the pin it serves --
        for _ in range(kc_iters):
            moved_any = False
            for idx, it in enumerate(items):
                if it["fixed"]:
                    continue
                pc = _place_cons(it["ref"])
                if not pc.get("keep_close_to_shared_pin"):
                    continue
                tgt = _shared_target(idx)
                if tgt is None:
                    continue
                tx, ty = tgt[1]
                d = float(pc.get("max_dist_mm", cp_max_step))
                dist = math.hypot(tx - it["x"], ty - it["y"])
                if dist <= d or dist < 1e-6:
                    continue
                step = min(cp_max_step, dist - d)
                ox, oy = it["x"], it["y"]
                # Move as close as possible: try the full step, then progressively
                # shorter ones, and keep the largest that creates no overlap — so
                # the cap ends up hugging the IC edge (de-collision sets the floor).
                placed = False
                for frac in (1.0, 0.6, 0.3, 0.15):
                    ns = step * frac
                    it["x"] = ox + (tx - ox) / dist * ns
                    it["y"] = oy + (ty - oy) / dist * ns
                    if not _overlaps_idx(idx):
                        moved_refs.add(it["ref"])
                        kept_close += 1
                        moved_any = True
                        placed = True
                        break
                if not placed:
                    it["x"], it["y"] = ox, oy
            if not moved_any:
                break

        # -- keep-away: separate conflicting domains (analog/switching, RF) --
        for _ in range(ka_iters):
            moved_any = False
            for idx, it in enumerate(items):
                if it["fixed"]:
                    continue
                pc = _place_cons(it["ref"])
                ka = set(pc.get("keep_away_from", []) or [])
                if not ka:
                    continue
                g = float(pc.get("min_gap_mm", cp_max_step))
                a = bbox_of(it)
                for j in range(len(items)):
                    if j == idx or not (_labels(j) & ka):
                        continue
                    ox, oy = _overlap_amounts(a, bbox_of(items[j]), g)
                    if ox <= 1e-6 or oy <= 1e-6:
                        continue                       # already clear of gap g
                    acx, acy = _centre(a)
                    bcx, bcy = _centre(bbox_of(items[j]))
                    if ox <= oy:
                        dirn = -1.0 if acx <= bcx else 1.0
                        dx, dy = dirn * min(ox, cp_max_step), 0.0
                    else:
                        dirn = -1.0 if acy <= bcy else 1.0
                        dx, dy = 0.0, dirn * min(oy, cp_max_step)
                    px, py = it["x"], it["y"]
                    it["x"] += dx
                    it["y"] += dy
                    if _overlaps_idx(idx):
                        it["x"], it["y"] = px, py
                    else:
                        a = bbox_of(it)
                        moved_refs.add(it["ref"])
                        kept_apart += 1
                        moved_any = True
            if not moved_any:
                break

    # ---- Write positions back into the footprint nodes ----
    # Rotation is only written when the optimizer ran, so with rotation_optimize
    # off the footprint's `at` clause is untouched on the angle slot (byte-stable).
    applied = 0
    write_rot = do_rot or do_ou
    for it in items:
        if _set_outer_at(it["fp"], it["x"], it["y"],
                         it["rot"] if write_rot else None):
            applied += 1

    overlaps_after = count_overlaps()
    return {
        "ok": True,
        "refined": True,
        "overlaps_before": overlaps_before,
        "overlaps_after": overlaps_after,
        "moved": len(moved_refs),
        "rotated": rotated,
        "oriented": oriented,
        "clustered": clustered,
        "kept_close": kept_close,
        "kept_apart": kept_apart,
        "constraint_placement": bool(constraints and cp_cfg.get("enabled", False)),
        "footprints": len(items),
        "skipped_no_geometry": skipped_no_bbox,
        "clearance_mm": clearance,
        "wirelength_refine": do_wl,
        "rotation_optimize": do_rot,
    }
