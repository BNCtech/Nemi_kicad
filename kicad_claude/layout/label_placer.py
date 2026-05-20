"""Step 4b: dynamic label position resolver (v2).

Takes routed.json (labels at raw pin tips) + placement.json + source
schematic, finds a collision-free position for each label by trying the
pin's outward direction first, then ±90° perpendiculars, then the back
direction; ray-casting outward from the pin tip within each angle. A
stub wire from the original pin tip to the new label anchor keeps the
connection electrical.

The collision space is indexed by a grid spatial hash so candidate
positions are tested in O(1) average instead of O(N) per query. Every
placed component (with its ref/value text margin) AND every already-
positioned label registers a bbox into the hash; new label candidates
query the hash for intersections before being placed.

Stub wires are always orthogonal (KiCad requires it). Diagonal angles
are intentionally NOT among the candidates.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import load_config

try:
    from ai_backend.kicad_claude.schematic_extractor import SchematicExtractor
except ImportError:
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore


BBox = Tuple[float, float, float, float]  # (min_x, min_y, max_x, max_y)


def _direction_vec(angle_deg: int) -> Tuple[float, float]:
    """KiCad screen direction. Y is +down in schematic data:
      0   -> +X (right), 90 -> +Y (down on screen),
      180 -> -X (left),  270 -> -Y (up on screen)."""
    a = angle_deg % 360
    table = {0: (1.0, 0.0), 90: (0.0, 1.0), 180: (-1.0, 0.0), 270: (0.0, -1.0)}
    return table.get(a, (math.cos(math.radians(a)), math.sin(math.radians(a))))


def _label_bbox(x: float, y: float, text: str, angle: int, cfg: Dict[str, Any]) -> BBox:
    """AABB the label's text occupies in world coords. Anchor at (x, y) with
    justify-left-bottom; text extends in the angle direction by
    len(text)*char_width."""
    char_w = float(cfg["char_width_mm"])
    font_h = float(cfg["font_height_mm"])
    pad = float(cfg["collision_pad_mm"])
    width = len(text) * char_w
    height = font_h

    dx, dy = _direction_vec(angle)
    cx = x + 0.5 * width * dx
    cy = y + 0.5 * width * dy

    if angle % 180 == 0:
        half_w = width / 2 + pad
        half_h = height / 2 + pad
    else:
        half_w = height / 2 + pad
        half_h = width / 2 + pad
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _bbox_overlap(a: BBox, b: BBox) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


class _SpatialHash:
    """Grid-bucket spatial index. Cell size is configured; every bbox is
    inserted into every cell it touches. Lookup queries the same cells and
    dedupes results. Avg O(1) per query for evenly-spread input, O(N) worst
    case (all bboxes in one cell). For our schematics — A4-sized sheets with
    a few hundred items — perfectly adequate and zero dependencies."""

    __slots__ = ("cell", "buckets")

    def __init__(self, cell_mm: float):
        self.cell = float(cell_mm)
        self.buckets: Dict[Tuple[int, int], List[BBox]] = defaultdict(list)

    def _keys(self, bbox: BBox) -> Iterable[Tuple[int, int]]:
        c = self.cell
        i0, i1 = int(math.floor(bbox[0] / c)), int(math.floor(bbox[2] / c))
        j0, j1 = int(math.floor(bbox[1] / c)), int(math.floor(bbox[3] / c))
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                yield (i, j)

    def add(self, bbox: BBox) -> None:
        for k in self._keys(bbox):
            self.buckets[k].append(bbox)

    def overlaps_any(self, bbox: BBox) -> bool:
        seen: Set[int] = set()
        for k in self._keys(bbox):
            bucket = self.buckets.get(k)
            if not bucket:
                continue
            for b in bucket:
                bid = id(b)
                if bid in seen:
                    continue
                seen.add(bid)
                if _bbox_overlap(bbox, b):
                    return True
        return False


def _compute_hybrid_offsets(
    routed: Dict[str, Any],
    cfg_label: Dict[str, Any],
    lane_cfg: Dict[str, Any],
    col_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[int, float]:
    """Hybrid per (ref, unit, outward_angle) group:
      - If perpendicular-axis pin pitch >= (font_height + label_gap),
        COLUMN ALIGN every label in the group to one X (or Y).
      - Else (tight pitch), fall back to SWIM LANES — greedy lane assignment
        with lane offsets at lane_width_mm.

    Column alignment is the cleaner KiCad-standard look but only works when
    adjacent labels have enough vertical (or horizontal) gap to not overlap
    when stacked at the same X. nRF54 BM15x has 2.54 mm pin pitch + 1.27 mm
    label font + arrow-wedge padding — labels in a single column read as
    crammed. Swim lanes split them into 2-3 X positions per side, restoring
    visual breathing room at the cost of a less-clean look."""
    groups: Dict[Tuple[str, int, int], List[Dict[str, Any]]] = defaultdict(list)
    for lb in routed.get("net_labels", []):
        groups[(lb.get("ref", ""), int(lb.get("unit", 1)), int(lb.get("angle", 0)))].append(lb)

    font_h = float(cfg_label.get("font_height_mm", 1.27))
    label_gap = float(lane_cfg.get("label_gap_mm", 1.5))
    lane_width = float(lane_cfg.get("lane_width_mm", 6.0))
    max_lanes = int(lane_cfg.get("max_lanes", 4))
    min_pitch_for_column = font_h + label_gap
    max_column_labels = int((col_cfg or {}).get("max_column_labels", 12))

    extras: Dict[int, float] = {}
    for (_, _, angle), labels in groups.items():
        if len(labels) <= 1:
            continue
        horiz_outward = angle % 180 == 0
        perp_key = (lambda lb: lb["y_mm"]) if horiz_outward else (lambda lb: lb["x_mm"])
        sorted_labels = sorted(labels, key=perp_key)
        coords = [perp_key(lb) for lb in sorted_labels]
        min_pitch = (min(coords[i + 1] - coords[i] for i in range(len(coords) - 1))
                     if len(coords) >= 2 else float("inf"))

        # Column path only if pitch is loose AND label count is manageable.
        # 26 labels in one column still reads crammed even at sufficient pitch.
        use_column = (min_pitch >= min_pitch_for_column
                      and len(labels) <= max_column_labels)
        force_swim = len(labels) > max_column_labels
        if use_column:
            # Column alignment path
            if angle == 180:
                ref = min(lb["x_mm"] for lb in labels)
                for lb in labels:
                    d = lb["x_mm"] - ref
                    if d > 0:
                        extras[id(lb)] = d
            elif angle == 0:
                ref = max(lb["x_mm"] for lb in labels)
                for lb in labels:
                    d = ref - lb["x_mm"]
                    if d > 0:
                        extras[id(lb)] = d
            elif angle == 270:
                ref = min(lb["y_mm"] for lb in labels)
                for lb in labels:
                    d = lb["y_mm"] - ref
                    if d > 0:
                        extras[id(lb)] = d
            elif angle == 90:
                ref = max(lb["y_mm"] for lb in labels)
                for lb in labels:
                    d = ref - lb["y_mm"]
                    if d > 0:
                        extras[id(lb)] = d
        elif force_swim:
            # Force tier-mod assignment (every Nth label to lane N). Greedy
            # would put everyone in lane 0 if pitch barely meets clearance —
            # which defeats the purpose when the user asked for staggering
            # because count is too high.
            tiers = 2
            for i, lb in enumerate(sorted_labels):
                tier = i % tiers
                if tier > 0:
                    extras[id(lb)] = tier * lane_width
        else:
            # Swim-lane path: tight pitch — split into lanes for breathing room
            lane_last: List[float] = []
            for lb in sorted_labels:
                c = perp_key(lb)
                assigned = -1
                for i, last_c in enumerate(lane_last):
                    if c - last_c >= min_pitch_for_column:
                        lane_last[i] = c
                        assigned = i
                        break
                if assigned < 0:
                    if len(lane_last) < max_lanes:
                        lane_last.append(c)
                        assigned = len(lane_last) - 1
                    else:
                        assigned = min(range(max_lanes), key=lambda i: c - lane_last[i])
                        lane_last[assigned] = c
                if assigned > 0:
                    extras[id(lb)] = assigned * lane_width
    return extras


def _compute_swim_lane_offsets(
    routed: Dict[str, Any], lane_cfg: Dict[str, Any], lp_cfg: Dict[str, Any]
) -> Dict[int, float]:
    """Greedy lane assignment per (component_ref, outward_angle) group.

    Lane width is AUTO-COMPUTED per group from the actual max label text width
    in that group, NOT a fixed config value. A fixed 6 mm lane width meant
    adjacent lanes' text overlapped by 2 mm whenever any label was longer
    than 6 mm (e.g. `NPM1300.SCL` is 8.4 mm). Per-group auto-width fixes
    that — `lane_width = max_label_width + label_gap_mm`. The config
    `lane_width_mm` is kept as a MINIMUM floor.

    Same per-lane vertical-clearance logic: place each label in the first
    lane whose previous occupant is at least `label_height + label_gap`
    away on the perpendicular axis. Opens new lanes up to max_lanes."""
    if not lane_cfg.get("enabled", True):
        return {}
    lane_width_floor = float(lane_cfg["lane_width_mm"])
    label_gap = float(lane_cfg["label_gap_mm"])
    max_lanes = int(lane_cfg["max_lanes"])
    font_h = float(lp_cfg.get("font_height_mm", 1.27))
    char_w = float(lp_cfg.get("char_width_mm", 0.7))
    min_clearance = font_h + label_gap

    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for lb in routed.get("net_labels", []):
        groups[(lb.get("ref", ""), int(lb.get("angle", 0)))].append(lb)

    out: Dict[int, float] = {}
    for (_, angle), labels in groups.items():
        if len(labels) <= 1:
            continue
        # Auto-compute lane width: longest label in the group sets the lane
        # pitch, with a floor from config and a small horizontal gap.
        max_text = max(len(lb.get("text", "")) for lb in labels)
        lane_width = max(lane_width_floor, max_text * char_w + label_gap)

        horiz_outward = angle % 180 == 0
        coord = (lambda lb: lb["y_mm"]) if horiz_outward else (lambda lb: lb["x_mm"])
        lane_last: List[float] = []
        for lb in sorted(labels, key=coord):
            c = coord(lb)
            assigned = -1
            for i, last_c in enumerate(lane_last):
                if c - last_c >= min_clearance:
                    lane_last[i] = c
                    assigned = i
                    break
            if assigned < 0:
                if len(lane_last) < max_lanes:
                    lane_last.append(c)
                    assigned = len(lane_last) - 1
                else:
                    assigned = min(range(max_lanes), key=lambda i: c - lane_last[i])
                    lane_last[assigned] = c
            if assigned > 0:
                out[id(lb)] = assigned * lane_width
    return out


def _build_body_bbox_by_ref(
    placement: Dict[str, Any], schematic_path,
) -> Dict[str, BBox]:
    """Per-ref body bbox in world coords, NO padding. Used as the
    stub-keepout map: we want to know whether a stub from one component's
    pin would cross ANOTHER component's body, not its own."""
    ext = SchematicExtractor(schematic_path)
    bodies = ext.lib_symbol_bodies()
    lib_pins = ext.lib_symbol_pins()
    lib_id_by_ref: Dict[str, str] = {}
    for orig in ext.components():
        ref = orig.get("reference") or ""
        if ref and ref not in lib_id_by_ref:
            lib_id_by_ref[ref] = orig.get("lib_id", "")
    out: Dict[str, BBox] = {}
    for p in placement.get("components", []):
        ref = p.get("ref") or ""
        if not ref:
            continue
        lib_id = lib_id_by_ref.get(ref, "")
        body = bodies.get(lib_id)
        if not body:
            xs: List[float] = []
            ys: List[float] = []
            for unit_pins in (lib_pins.get(lib_id) or {}).values():
                for pin in unit_pins:
                    xs.append(float(pin["x"]))
                    ys.append(float(pin["y"]))
            if not xs:
                continue
            body = (min(xs), min(ys), max(xs), max(ys))
        cx = float(p["x_mm"])
        cy = float(p["y_mm"])
        out[ref] = (
            cx + body[0],
            cy - body[3],
            cx + body[2],
            cy - body[1],
        )
    return out


def _build_component_bboxes(
    placement: Dict[str, Any], schematic_path, margin: float,
    prop_cfg: Optional[Dict[str, Any]] = None,
) -> List[BBox]:
    """World-coord collision bboxes per placed real component. Three rects
    per part: the BODY (padded by `margin`), plus the REFERENCE-text rect
    (above the body for rot 0/180, to the LEFT for rot 90/270) and the
    VALUE-text rect (below / RIGHT). Without the property-text rects, net
    labels happily land on top of `SW1` / `R1` / `0.1uF` strings — the
    exact overlap visible in the user's tactile-switch screenshot
    (SW1 / GND / RESET stacked on the same cell).

    Body fallback (when the lib_symbol has no graphical body — some
    custom libs draw via pins only): use the pin-tip bounding box.
    Without this such parts were SKIPPED entirely.

    Property-text rects mirror what `_normalize_property_positions` in
    emitter.py actually writes — so the collision index reserves exactly
    the cells the emitter will occupy."""
    ext = SchematicExtractor(schematic_path)
    bodies = ext.lib_symbol_bodies()
    lib_pins = ext.lib_symbol_pins()
    lib_id_by_ref: Dict[str, str] = {}
    value_by_ref: Dict[str, str] = {}
    for orig in ext.components():
        ref = orig.get("reference") or ""
        if ref and ref not in lib_id_by_ref:
            lib_id_by_ref[ref] = orig.get("lib_id", "")
            value_by_ref[ref] = orig.get("value", "") or ""

    prop_cfg = prop_cfg or {}
    if prop_cfg.get("enabled", True):
        ref_margin = float(prop_cfg.get("refdes_margin_mm", 1.27))
        val_margin = float(prop_cfg.get("value_margin_mm", 1.27))
    else:
        ref_margin = val_margin = 0.0
    # KiCad's default schematic property font is 1.27 mm. Char width ~0.6 mm.
    prop_font_h = 1.27
    prop_char_w = 0.6
    # Half-height padding around the text rect so a label "kissing" the
    # property text from below still counts as a collision.
    text_pad = 0.5

    out: List[BBox] = []
    for p in placement.get("components", []):
        ref = p.get("ref") or ""
        lib_id = lib_id_by_ref.get(ref, "")
        body = bodies.get(lib_id)
        if not body:
            xs: List[float] = []
            ys: List[float] = []
            for unit_pins in (lib_pins.get(lib_id) or {}).values():
                for pin in unit_pins:
                    xs.append(float(pin["x"]))
                    ys.append(float(pin["y"]))
            if not xs:
                continue
            body = (min(xs), min(ys), max(xs), max(ys))
        cx = float(p["x_mm"])
        cy = float(p["y_mm"])
        out.append((
            cx + body[0] - margin,
            cy - body[3] - margin,
            cx + body[2] + margin,
            cy - body[1] + margin,
        ))

        if not prop_cfg.get("enabled", True):
            continue
        # Property-text rects — mirror emitter._normalize_property_positions.
        rot_q = int(round(float(p.get("rotation", 0.0)) / 90.0)) % 4
        ref_str = ref or "U?"
        val_str = value_by_ref.get(ref, "") or ""
        ref_w = max(2, len(ref_str)) * prop_char_w
        val_w = max(2, len(val_str)) * prop_char_w
        if rot_q in (1, 3):
            # Horizontal body — Reference LEFT of body, Value RIGHT.
            world_left  = cx - body[3]
            world_right = cx - body[1]
            # Reference centered at (world_left - ref_margin, cy), extends LEFT
            ref_cx = world_left - ref_margin - ref_w / 2
            ref_cy = cy
            val_cx = world_right + val_margin + val_w / 2
            val_cy = cy
        else:
            # Vertical body — Reference ABOVE, Value BELOW (rotation 0 or 180).
            body_top_world = cy - body[3]
            body_bot_world = cy - body[1]
            ref_cx = cx
            ref_cy = body_top_world - ref_margin - prop_font_h / 2
            val_cx = cx
            val_cy = body_bot_world + val_margin + prop_font_h / 2

        out.append((
            ref_cx - ref_w / 2 - text_pad,
            ref_cy - prop_font_h / 2 - text_pad,
            ref_cx + ref_w / 2 + text_pad,
            ref_cy + prop_font_h / 2 + text_pad,
        ))
        if val_str:
            out.append((
                val_cx - val_w / 2 - text_pad,
                val_cy - prop_font_h / 2 - text_pad,
                val_cx + val_w / 2 + text_pad,
                val_cy + prop_font_h / 2 + text_pad,
            ))
    return out


def _segment_crosses_bbox(
    x1: float, y1: float, x2: float, y2: float, bbox: BBox,
) -> bool:
    """True iff the axis-aligned segment ((x1,y1)→(x2,y2)) passes through
    the interior of bbox (xmin, ymin, xmax, ymax). Endpoints touching the
    edge don't count — we only flag genuine through-crossings so the
    pin-tip itself sitting on the owner body's edge is allowed."""
    xmin, ymin, xmax, ymax = bbox
    if abs(x1 - x2) < 1e-3:  # vertical segment
        x = x1
        if x <= xmin + 1e-3 or x >= xmax - 1e-3:
            return False
        lo, hi = min(y1, y2), max(y1, y2)
        return not (hi <= ymin + 1e-3 or lo >= ymax - 1e-3)
    if abs(y1 - y2) < 1e-3:  # horizontal segment
        y = y1
        if y <= ymin + 1e-3 or y >= ymax - 1e-3:
            return False
        lo, hi = min(x1, x2), max(x1, x2)
        return not (hi <= xmin + 1e-3 or lo >= xmax - 1e-3)
    return False  # diagonal — not emitted by ray-cast


def _stub_clear_of_other_bodies(
    pin_x: float, pin_y: float, lx: float, ly: float,
    other_body_bboxes: List[BBox],
) -> bool:
    """True iff the straight stub (pin → label) avoids every body bbox in
    `other_body_bboxes`. The owner's own body is intentionally NOT in this
    list — the pin tip starts ON the owner's body edge, so an overlap check
    against the owner is always a false positive."""
    if abs(lx - pin_x) < 0.01 and abs(ly - pin_y) < 0.01:
        return True
    for bb in other_body_bboxes:
        if _segment_crosses_bbox(pin_x, pin_y, lx, ly, bb):
            return False
    return True


def _try_place(
    pin_x: float, pin_y: float, angle: int, text: str,
    spatial: _SpatialHash, cfg: Dict[str, Any],
    other_body_bboxes: Optional[List[BBox]] = None,
) -> Optional[Tuple[float, float, BBox]]:
    """Ray-cast outward along `angle`. Returns (lx, ly, bbox) for the first
    position where the label's bbox doesn't overlap anything in `spatial`
    AND the straight stub from pin to that position doesn't cross any
    OTHER component's body bbox. The body-clearance check is what stops
    a stub wire from passing through a neighbour's symbol body —
    universal, no part-number knowledge."""
    min_off = float(cfg["min_offset_mm"])
    max_off = float(cfg["max_offset_mm"])
    step = float(cfg["step_mm"])
    dx, dy = _direction_vec(angle)
    offset = min_off
    others = other_body_bboxes or []
    while offset <= max_off + 1e-6:
        lx = pin_x + dx * offset
        ly = pin_y + dy * offset
        bb = _label_bbox(lx, ly, text, angle, cfg)
        if not spatial.overlaps_any(bb):
            if _stub_clear_of_other_bodies(pin_x, pin_y, lx, ly, others):
                return (lx, ly, bb)
        offset += step
    return None


def place_labels(
    routed: Dict[str, Any], placement: Dict[str, Any], schematic_path
) -> Dict[str, Any]:
    """Resolve label positions. Mutates routed in place; safe to re-run
    (stub_wires are regenerated from scratch each pass)."""
    cfg_all = load_config("layout_config")
    cfg = dict(cfg_all["label_placement"])  # copy — runtime sync below
    if not cfg.get("enabled", True):
        return routed

    # Live-sync font height from label_format.fixed_font_mm so the collision
    # bbox math (char widths × height) matches what the emitter actually
    # writes. Without this sync, raising fixed_font_mm doesn't grow the
    # reserved label bbox and adjacent labels start overlapping again.
    fmt_cfg = cfg_all.get("label_format") or {}
    fixed_font = fmt_cfg.get("fixed_font_mm")
    if fixed_font is not None:
        cfg["font_height_mm"] = float(fixed_font)
    # label_gap_mm in label_format becomes the minimum outward offset, so
    # every column lands exactly `label_gap_mm` outside the body edge.
    label_gap = fmt_cfg.get("label_gap_mm")
    if label_gap is not None:
        cfg["min_offset_mm"] = max(float(cfg.get("min_offset_mm", 1.27)),
                                    float(label_gap))

    body_margin = float(cfg.get("component_body_margin_mm", 2.5))
    hash_cell = float(cfg.get("spatial_hash_cell_mm", 20.0))
    angle_offsets = list(cfg.get("angle_candidates_offset_deg", [0, 90, -90, 180]))
    grid = float(cfg_all.get("grid_mm", 1.27))

    def _snap(v: float) -> float:
        return round(v / grid) * grid
    # Hybrid column-align / swim-lane: column when pin pitch is loose enough,
    # swim-lanes when tight. Decided per (ref, unit, side) group.
    col_cfg = cfg_all.get("column_alignment") or {}
    lane_cfg = cfg_all.get("swim_lanes") or {}
    stagger_extras: Dict[int, float] = {}
    if col_cfg.get("enabled", True):
        stagger_extras = _compute_hybrid_offsets(routed, cfg, lane_cfg, col_cfg)
    elif lane_cfg.get("enabled", True):
        stagger_extras = _compute_swim_lane_offsets(routed, lane_cfg, cfg)

    spatial = _SpatialHash(hash_cell)
    prop_cfg = cfg_all.get("property_normalize") or {}
    # Per-ref body bboxes (no padding) for stub-wire keepout.
    body_bbox_by_ref = _build_body_bbox_by_ref(placement, schematic_path)
    for cb in _build_component_bboxes(
            placement, schematic_path, body_margin, prop_cfg=prop_cfg):
        spatial.add(cb)

    # Preserve any wires the router already wrote (auto-named-net wire
    # chains) — those carry the only electrical connection for nets
    # whose labels were intentionally suppressed; wiping them would
    # leave the corresponding pins dangling.
    stub_wires: List[Dict[str, Any]] = list(routed.get("stub_wires") or [])
    primary_clear = 0
    perpendicular = 0
    forced = 0
    staggered = 0

    for lb in routed.get("net_labels", []):
        pin_x = float(lb["x_mm"])
        pin_y = float(lb["y_mm"])
        base_angle = int(lb.get("angle", 0))
        text = lb.get("text", "")

        # Density-tier stagger: bump the starting outward offset so adjacent
        # labels in a dense column don't all stack at the same X (or Y).
        extra = stagger_extras.get(id(lb), 0.0)
        if extra > 0:
            staggered += 1
        cfg_eff = (
            {**cfg, "min_offset_mm": float(cfg["min_offset_mm"]) + extra}
            if extra > 0 else cfg
        )

        # Body-keepout list for stub wires: every OTHER component's body.
        # The owner's own body is excluded because the pin tip is on its
        # edge — a self-overlap would block every position.
        owner_ref = lb.get("ref") or ""
        other_bodies = [bb for r, bb in body_bbox_by_ref.items()
                        if r != owner_ref]

        placed: Optional[Tuple[float, float, int, BBox]] = None
        for i, off in enumerate(angle_offsets):
            cand = (base_angle + int(off)) % 360
            # Only axis-aligned wires are legal in KiCad. If the offset is
            # not orthogonal (e.g. someone added 45 to the config), skip it
            # — would produce a diagonal stub.
            if cand % 90 != 0:
                continue
            res = _try_place(pin_x, pin_y, cand, text, spatial, cfg_eff,
                              other_body_bboxes=other_bodies)
            if res:
                lx, ly, bb = res
                placed = (lx, ly, cand, bb)
                if i == 0:
                    primary_clear += 1
                else:
                    perpendicular += 1
                break

        if placed is None:
            # All candidates blocked. Place at max_offset along primary anyway.
            dx, dy = _direction_vec(base_angle)
            max_off = float(cfg["max_offset_mm"])
            lx = pin_x + dx * max_off
            ly = pin_y + dy * max_off
            bb = _label_bbox(lx, ly, text, base_angle, cfg)
            placed = (lx, ly, base_angle, bb)
            forced += 1

        lx, ly, ang, bb = placed
        # Snap to grid BEFORE storing so the label anchor and the stub wire's
        # downstream-emitter snap produce identical coords. Without this,
        # 119.07 mm (lane-width derived) snaps to 119.38 in both paths, but
        # subtle float-rounding can put a label at one grid cell and its stub
        # endpoint at the adjacent cell — KiCad then reports the label as
        # sitting on the wire mid-segment instead of at its endpoint.
        lx = _snap(lx)
        ly = _snap(ly)
        spatial.add(bb)
        lb["pin_x_mm"] = pin_x
        lb["pin_y_mm"] = pin_y
        lb["x_mm"] = lx
        lb["y_mm"] = ly
        lb["angle"] = ang

        if abs(lx - pin_x) >= 0.01 or abs(ly - pin_y) >= 0.01:
            stub_wires.append({"x1": pin_x, "y1": pin_y, "x2": lx, "y2": ly, "_net": text})

    # Merge same-net collinear stubs into one max-extent wire each. Multi-unit
    # clusters have multiple pins on the same net at the same Y (row) or X
    # (column); each pin emits its own stub, and these stubs partially overlap.
    # Merging by (net, axis, axis_value) folds them into a single segment from
    # leftmost to rightmost (or topmost to bottommost) endpoint — every pin
    # along that line is on the same net so they remain electrically connected.
    # Different-net stubs are left alone, no risk of cross-net shorts.
    grouped: Dict[Tuple[str, str, float], List[Tuple[float, float]]] = defaultdict(list)
    other_stubs: List[Dict[str, Any]] = []
    for w in stub_wires:
        x1, y1, x2, y2 = w["x1"], w["y1"], w["x2"], w["y2"]
        net = w.get("_net", "")
        if not net:
            other_stubs.append(w)
            continue
        if abs(y1 - y2) < 0.01:  # horizontal
            grouped[(net, "h", round(y1, 2))].append((min(x1, x2), max(x1, x2)))
        elif abs(x1 - x2) < 0.01:  # vertical
            grouped[(net, "v", round(x1, 2))].append((min(y1, y2), max(y1, y2)))
        else:
            other_stubs.append(w)  # diagonal — shouldn't happen, pass through

    merged_stubs: List[Dict[str, Any]] = list(other_stubs)
    for (net, axis, axis_val), segments in grouped.items():
        lo = min(s[0] for s in segments)
        hi = max(s[1] for s in segments)
        if axis == "h":
            merged_stubs.append({"x1": lo, "y1": axis_val, "x2": hi, "y2": axis_val})
        else:
            merged_stubs.append({"x1": axis_val, "y1": lo, "x2": axis_val, "y2": hi})
    stub_wires = merged_stubs

    routed["stub_wires"] = stub_wires
    routed["_label_placement_stats"] = {
        "labels_processed": len(routed.get("net_labels", [])),
        "primary_clear": primary_clear,
        "perpendicular_fallback": perpendicular,
        "forced_at_max_offset": forced,
        "staggered_labels": staggered,
        "stub_wires_added": len(stub_wires),
    }
    return routed


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.label_placer")
    ap.add_argument("placement", help="placement.json from Step 3")
    ap.add_argument("routed", help="routed.json from Step 4 (router)")
    ap.add_argument("schematic", help="source .kicad_sch (for body bboxes)")
    ap.add_argument("--out", help="write resolved routed JSON (default: overwrite routed)")
    args = ap.parse_args(argv)

    placement = json.loads(Path(args.placement).read_text(encoding="utf-8"))
    routed = json.loads(Path(args.routed).read_text(encoding="utf-8"))

    refined = place_labels(routed, placement, args.schematic)

    out_path = Path(args.out) if args.out else Path(args.routed)
    out_path.write_text(json.dumps(refined, indent=2), encoding="utf-8")
    s = refined.get("_label_placement_stats") or {}
    print(
        f"wrote {out_path}  "
        f"labels={s.get('labels_processed', 0)}  "
        f"primary={s.get('primary_clear', 0)}  "
        f"perp={s.get('perpendicular_fallback', 0)}  "
        f"forced={s.get('forced_at_max_offset', 0)}  "
        f"stubs={s.get('stub_wires_added', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
