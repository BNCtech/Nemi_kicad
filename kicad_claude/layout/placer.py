"""Step 3 of the universal layout engine: block-based placer.

Consumes the classified.json artifact (Step 2 output) and assigns every
component an (x, y, rotation) on a KiCad sheet. The strategy is intentionally
simple and JSON-driven:

  1. Group nodes by role into blocks (one block per role per sheet).
  2. Assign each block to a sheet zone via layout_config.role_zone_map; on
     zone conflict, fall back through layout_config.fallback_zones.order.
  3. Compute each block's bbox by packing its components into a grid using
     per-class cell sizes (IC / passive / connector) from spacing.* config.
  4. If the total sheet area is tight, auto-promote sheet size A4 -> A3.
  5. Snap every coordinate to the KiCad grid.

Output is a placement.json artifact consumed by Step 4 (router):

  {
    "sheet":  {"size": "A4_landscape", "width_mm": 297, "height_mm": 210},
    "main_controller": "U1" | null,
    "blocks": [{role, zone, x_mm, y_mm, width_mm, height_mm, members}, ...],
    "components": [
      {ref, role, x_mm, y_mm, rotation, mirror, block_zone}, ...
    ],
    "skipped": [{ref, reason}, ...]
  }

Multi-unit parts collapse to one position for now (unit_count carried
through from Step 2). Step 4 / Step 5 will fan units out — placing them
here would force a half-baked per-unit refactor that doesn't pay off
until the router knows how to wire them.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import load_config


CellClass = str  # "ic" | "passive" | "connector"


_IC_ROLES = {"MAIN_CONTROLLER", "MEMORY", "WIRELESS", "DISPLAY",
             "SENSOR", "MOTOR", "POWER_REGULATOR", "ANALOG", "DEBUG"}


def _label_keepout_pad(cfg_root: Dict[str, Any]) -> float:
    """Derive the per-cell label keepout pad from existing label_format
    settings. No new config knob — `font_height * 1.5 + label_gap` is the
    minimum room a single label row needs to sit BETWEEN two cells without
    touching either. Any edit to font size or label gap propagates here
    automatically."""
    fmt = cfg_root.get("label_format") or {}
    font_h = float(fmt.get("fixed_font_mm", 1.0))
    label_gap = float(fmt.get("label_gap_mm", 2.0))
    return font_h * 1.5 + label_gap


def _cell_size(
    node: Dict[str, Any],
    spacing: Dict[str, Any],
) -> Tuple[float, float, CellClass]:
    """Approximate footprint per component, by class. Used for block-bbox
    estimation only — the router re-derives exact symbol bboxes later.

    The cell is padded uniformly by `spacing["_label_pad"]` on all sides;
    that value is stashed by `place()` from `_label_keepout_pad(cfg)`
    once per run so every call site picks up the same derived figure
    without needing a new signature."""
    pins = int(node.get("pin_count", 0))
    role = node.get("role", "GENERIC")
    ow = float(node.get("outline_w", 0.0)) or float(node.get("body_w", 0.0))
    oh = float(node.get("outline_h", 0.0)) or float(node.get("body_h", 0.0))
    has_size = ow > 0.0 and oh > 0.0
    label_pad = float(spacing.get("_label_pad", 0.0))
    pad2 = 2 * label_pad

    if role == "CONNECTOR":
        if has_size:
            return (ow + pad2, oh + pad2, "connector")
        size = float(spacing["connector_cell_mm"]) + max(0, pins - 4) * 2.54
        return (float(spacing["connector_cell_mm"]) + pad2,
                size + pad2, "connector")

    if role in _IC_ROLES or pins >= 4:
        if has_size:
            return (ow + pad2, oh + pad2, "ic")
        base = float(spacing["ic_cell_mm"])
        scaled = max(base, base + (pins - 8) * 1.5)
        return (scaled + pad2, scaled + pad2, "ic")

    if has_size:
        return (ow + pad2, oh + pad2, "passive")
    p = float(spacing["passive_cell_mm"])
    return (p + pad2, p * 2.0 + pad2, "passive")


_GND_TOKENS = ("GND", "VSS", "DGND", "AGND", "PGND", "SGND", "EGND")
_PASSIVE_PREFIXES = ("C", "R", "L", "D", "Y", "FB", "TVS")
_IC_PREFIXES = ("U", "X", "IC")


_ANCHOR_ROLES_3PIN = {
    "POWER_REGULATOR",  # LM317 / 78xx / AMS1117 / MIC5219 / LP2950 — 3-pin TO-220 family
    "ANALOG",           # discrete BJT/MOSFET stages where the active device anchors its bias network
    "MOTOR",            # half-bridge / single-FET driver chips
    "SENSOR",           # small analog sensors (TMP36, LM35) that anchor their decoupling cap
}


def _is_ic_node(node: Dict[str, Any]) -> bool:
    """True if node should act as an IC-anchor for passive co-location.

    Default: ≥4-pin U*/X*/IC*. Plus: 3-pin parts whose role marks them as the
    functional anchor of a sub-circuit (regulators, single-transistor stages
    etc.) — without this, LM317 + Cin + Cout + R1 + R2 fragment into separate
    POWER and GENERIC blocks because the 3-pin reg fails the 4-pin gate, and
    every bias passive ends up with zero IC anchors."""
    ref = node.get("ref") or ""
    if not ref.startswith(_IC_PREFIXES):
        return False
    pins = int(node.get("pin_count", 0))
    if pins >= 4:
        return True
    if pins >= 3 and (node.get("role") or "").upper() in _ANCHOR_ROLES_3PIN:
        return True
    return False


def _colocate_passives(classified: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Generalised net-adjacency colocation: any 2-pin passive (C/R/L/D/Y)
    whose nets collectively touch EXACTLY ONE IC pin gets reassigned to
    that IC's role.

    Covers every typical support topology with a single rule:
      - Decoupling cap (RAIL + GND, both on IC's VCC pin)
      - Crystal load cap (XTAL_x + GND, on IC's XTAL pin)
      - Pull-up / pull-down (signal + RAIL/GND, on IC's signal pin)
      - Biasing R, divider tap, feedback R (signal + signal, both touching
        the same IC)
      - Output coupling cap (output net + something, single IC touched)

    Universal — works for any IC family, any passive value, any net name.
    Pure graph topology; the rule "this passive talks to exactly one IC"
    captures the human intuition "this passive belongs to that IC".

    Returns (reassign_map, ic_ref_by_passive):
      reassign_map = passive_ref → ic_target_role (for block grouping)
      ic_ref_by_passive = passive_ref → ic_ref (for block-split + title use)"""
    edges = classified.get("edges") or []
    nodes = classified.get("nodes") or []
    if not edges or not nodes:
        return {}, {}

    node_by_ref = {n.get("ref"): n for n in nodes}
    net_to_refs: Dict[str, set] = defaultdict(set)
    ref_to_nets: Dict[str, set] = defaultdict(set)
    for e in edges:
        for net in (e.get("nets") or []):
            if not net:
                continue
            for r in (e.get("a"), e.get("b")):
                if not r:
                    continue
                net_to_refs[net].add(r)
                ref_to_nets[r].add(net)

    reassign: Dict[str, str] = {}
    ic_owner: Dict[str, str] = {}

    for n in nodes:
        ref = n.get("ref") or ""
        if not ref.startswith(_PASSIVE_PREFIXES):
            continue
        if int(n.get("pin_count", 0)) != 2:
            continue
        nets = list(ref_to_nets.get(ref, ()))
        if not nets:
            continue
        # ICs touching ANY of this passive's nets.
        touched_ics: set = set()
        for net in nets:
            for other_ref in net_to_refs.get(net, set()):
                if other_ref == ref or not other_ref:
                    continue
                other = node_by_ref.get(other_ref)
                if other and _is_ic_node(other):
                    touched_ics.add(other_ref)
        if len(touched_ics) == 1:
            ic_ref = next(iter(touched_ics))
            ic = node_by_ref[ic_ref]
            reassign[ref] = ic.get("role") or "GENERIC"
            ic_owner[ref] = ic_ref
    return reassign, ic_owner


# Back-compat alias for older callers (none currently, but reserved).
_colocate_decoupling = _colocate_passives


def _split_role_by_primary_ic(
    role: str, members: List[Dict[str, Any]],
    ic_owner: Dict[str, str],
) -> List[Dict[str, Any]]:
    """When a single role bucket holds >1 IC, split into sub-blocks: one
    per IC plus a remainder bucket for orphans. Each IC takes the passives
    that were colocated TO IT (per ic_owner map).

    Universal — works for any role with any IC count. Single-IC role
    buckets pass through unchanged. Roles with no IC at all are unchanged.

    Returned blocks all keep the original role string so zone resolution
    still routes them to the SAME sheet zone (placer then sub-grids them
    next to each other)."""
    ics_in_bucket = [m for m in members if _is_ic_node(m)]
    if len(ics_in_bucket) <= 1:
        return [{"role": role, "members": members}]

    # Each IC -> list of refs that colocate to it (i.e. passives owned).
    owned_by_ic: Dict[str, List[str]] = defaultdict(list)
    for passive_ref, ic_ref in ic_owner.items():
        owned_by_ic[ic_ref].append(passive_ref)

    member_by_ref = {m.get("ref"): m for m in members}
    accounted: set = set()
    sub_blocks: List[Dict[str, Any]] = []
    for ic in ics_in_bucket:
        ic_ref = ic.get("ref")
        sub_members = [ic]
        accounted.add(ic_ref)
        for pref in owned_by_ic.get(ic_ref, []):
            if pref in member_by_ref and pref not in accounted:
                sub_members.append(member_by_ref[pref])
                accounted.add(pref)
        sub_blocks.append({"role": role, "members": sub_members,
                            "primary_ic": ic_ref})

    # Orphan members (no IC ownership) go to a remainder block.
    orphan = [m for m in members if m.get("ref") not in accounted]
    if orphan:
        sub_blocks.append({"role": role, "members": orphan,
                            "primary_ic": None})
    return sub_blocks


def _group_into_blocks(classified: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Bucket nodes by role, then split mixed-IC buckets so each IC gets
    its own visual block. Two-pass:
      1. Net-adjacency colocation reassigns 2-pin passives to their parent
         IC's role (decoupling, crystal-cap, pull-up, divider, etc.).
      2. After role bucketing, any bucket with multiple ICs is split per
         IC via _split_role_by_primary_ic.

    Order of returned blocks matters — earlier blocks claim zones first,
    so MAIN_CONTROLLER goes first to lock in `center` before peripherals
    fight over it."""
    reassign, ic_owner = _colocate_passives(classified)
    by_role: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for n in classified["nodes"]:
        ref = n.get("ref")
        new_role = reassign.get(ref)
        if new_role and new_role != n.get("role"):
            n = {**n, "role": new_role,
                 "heuristic_role_pre_colocate": n.get("role")}
        by_role[n["role"]].append(n)

    priority_first = ["MAIN_CONTROLLER", "POWER", "POWER_REGULATOR"]
    seen: set = set()
    blocks: List[Dict[str, Any]] = []
    for role in priority_first:
        if role in by_role:
            for b in _split_role_by_primary_ic(role, by_role[role], ic_owner):
                blocks.append(b)
            seen.add(role)
    for role, members in by_role.items():
        if role not in seen:
            for b in _split_role_by_primary_ic(role, members, ic_owner):
                blocks.append(b)
    return blocks


def _split_oversized_blocks(
    blocks: List[Dict[str, Any]], max_size: int,
) -> List[Dict[str, Any]]:
    """Split any block with more members than `max_size` into sequential
    sub-blocks named `{role}_1`, `{role}_2`, etc. Sub-blocks keep the
    original components' roles intact (components only know their classifier
    role, not which sub-block they got chunked into); _resolve_overlaps and
    Pass C downstream match by member-ref lists, not by role string.

    Future: chunk by connectivity (group connected components into the same
    sub-block) so signal traces don't cross sub-block boundaries. For now,
    sequential chunking — same total density, just split into manageable
    pieces with breathing room between them."""
    if max_size <= 0:
        return blocks
    out: List[Dict[str, Any]] = []
    for block in blocks:
        members = block.get("members") or []
        if len(members) <= max_size:
            out.append(block)
            continue
        n_chunks = math.ceil(len(members) / max_size)
        chunk_size = math.ceil(len(members) / n_chunks)
        for i in range(n_chunks):
            chunk = members[i * chunk_size : (i + 1) * chunk_size]
            if not chunk:
                continue
            out.append({
                "role": f"{block['role']}_{i + 1}",
                "members": chunk,
            })
    return out


def _resolve_zones(
    blocks: List[Dict[str, Any]],
    role_zone_map: Dict[str, str],
    fallback_zones: List[str],
) -> None:
    """Stamp each block with a zone name. First block to request a zone wins
    it; subsequent claimants are pushed through fallback_zones in order.
    Mutates blocks in place."""
    used: set = set()
    fb_idx = 0
    for block in blocks:
        wanted = role_zone_map.get(block["role"], "bottom_center")
        if wanted not in used:
            block["zone"] = wanted
            used.add(wanted)
            continue
        while fb_idx < len(fallback_zones) and fallback_zones[fb_idx] in used:
            fb_idx += 1
        if fb_idx < len(fallback_zones):
            block["zone"] = fallback_zones[fb_idx]
            used.add(block["zone"])
            fb_idx += 1
        else:
            block["zone"] = wanted  # ran out — overlap, router will warn


def _layout_crystal(
    members: List[Dict[str, Any]],
    spacing: Dict[str, Any],
) -> Optional[Tuple[List[Dict[str, Any]], float, float]]:
    """C-Y-C symmetric row (or Y alone / Y+C if only one cap). Returns None
    when the block doesn't match the crystal pattern, signaling a fall-back
    to the generic grid layout.

    Reference shape:
        C_left ─── Y ─── C_right
          |               |
         GND             GND
    """
    crystal = next((m for m in members if m["ref"].startswith("Y")), None)
    caps = [m for m in members if m["ref"].startswith("C")]
    other = [m for m in members
             if m is not crystal and not m["ref"].startswith("C")]
    if other or crystal is None or len(caps) > 2:
        return None

    cell_pitch = float(spacing["component_pitch_mm"])
    cy_w, cy_h, _ = _cell_size(crystal, spacing)
    h_gap = cell_pitch * 0.6

    def _comp(member: Dict[str, Any], rel_x: float, rel_y: float,
              cw: float, ch: float) -> Dict[str, Any]:
        return {
            "ref": member["ref"],
            "role": member["role"],
            "rel_xys": [(rel_x, rel_y)],
            "cell_w": cw,
            "cell_h": ch,
            "rotation": 0,
            "mirror": False,
        }

    if not caps:
        return [_comp(crystal, cy_w / 2, cy_h / 2, cy_w, cy_h)], cy_w, cy_h

    cap_w, cap_h, _ = _cell_size(caps[0], spacing)
    total_h = max(cy_h, cap_h)
    cy_y = total_h / 2
    cap_y = total_h / 2

    if len(caps) == 1:
        # Y on the left, C on the right
        positioned = [
            _comp(crystal, cy_w / 2, cy_y, cy_w, cy_h),
            _comp(caps[0], cy_w + h_gap + cap_w / 2, cap_y, cap_w, cap_h),
        ]
        return positioned, cy_w + h_gap + cap_w, total_h

    # Two caps: C₁ — Y — C₂ symmetric
    cap2_w, cap2_h, _ = _cell_size(caps[1], spacing)
    total_h = max(total_h, cap2_h)
    positioned = [
        _comp(caps[0], cap_w / 2, total_h / 2, cap_w, cap_h),
        _comp(crystal, cap_w + h_gap + cy_w / 2, total_h / 2, cy_w, cy_h),
        _comp(caps[1], cap_w + h_gap + cy_w + h_gap + cap2_w / 2, total_h / 2,
              cap2_w, cap2_h),
    ]
    total_w = cap_w + h_gap + cy_w + h_gap + cap2_w
    return positioned, total_w, total_h


def _layout_single_row(
    members: List[Dict[str, Any]],
    spacing: Dict[str, Any],
) -> Optional[Tuple[List[Dict[str, Any]], float, float]]:
    """All components in a single horizontal row. Reference pattern for
    POWER (C1 C2 side-by-side under VDD) and DECOUPLING (Cx Cy row).
    Bails out when the row would be wider than ~5 component cells — beyond
    that, grid is more compact."""
    n = len(members)
    if n == 0 or n > 5:
        return None
    cells = [_cell_size(m, spacing) for m in members]
    cell_pitch = float(spacing["component_pitch_mm"])
    h_gap = cell_pitch * 0.8
    max_h = max(ch for _, ch, _ in cells)
    positioned: List[Dict[str, Any]] = []
    x = 0.0
    for (cw, ch, _), member in zip(cells, members):
        positioned.append({
            "ref": member["ref"],
            "role": member["role"],
            "rel_xys": [(x + cw / 2, max_h / 2)],
            "cell_w": cw,
            "cell_h": ch,
            "rotation": 0,
            "mirror": False,
        })
        x += cw + h_gap
    return positioned, x - h_gap, max_h


def _layout_vertical_stack(
    members: List[Dict[str, Any]],
    spacing: Dict[str, Any],
) -> Optional[Tuple[List[Dict[str, Any]], float, float]]:
    """All components in a single vertical column. Reference pattern for
    RESET / BOOT chains (R pullup, then SW, then C filter — top to bottom).
    Bails out when too many members — fall back to grid."""
    n = len(members)
    if n == 0 or n > 5:
        return None
    cells = [_cell_size(m, spacing) for m in members]
    cell_pitch = float(spacing["component_pitch_mm"])
    v_gap = cell_pitch * 0.6
    max_w = max(cw for cw, _, _ in cells)
    positioned: List[Dict[str, Any]] = []
    y = 0.0
    for (cw, ch, _), member in zip(cells, members):
        positioned.append({
            "ref": member["ref"],
            "role": member["role"],
            "rel_xys": [(max_w / 2, y + ch / 2)],
            "cell_w": cw,
            "cell_h": ch,
            "rotation": 0,
            "mirror": False,
        })
        y += ch + v_gap
    return positioned, max_w, y - v_gap


# Role -> layout function dispatch table. Add new patterns here once the
# function is implemented; the dispatcher falls back to grid if a role has
# no specific layout or the layout returns None (member shape didn't match).
_ROLE_LAYOUTS = {
    "CRYSTAL": _layout_crystal,
    "POWER":   _layout_single_row,
    "RESET":   _layout_vertical_stack,
    "BOOT":    _layout_vertical_stack,
}


def _pack_block(
    members: List[Dict[str, Any]],
    spacing: Dict[str, Any],
    role: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], float, float]:
    """Dispatcher: try a role-specific layout (CRYSTAL symmetric, POWER row,
    RESET/BOOT vertical stack) first; fall back to grid if the role doesn't
    map to a specialized layout or the layout returns None."""
    layout_fn = _ROLE_LAYOUTS.get(role) if role else None
    if layout_fn:
        result = layout_fn(members, spacing)
        if result is not None:
            return result
    return _pack_block_grid(members, spacing)


def _pack_block_grid(
    members: List[Dict[str, Any]],
    spacing: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], float, float]:
    """Lay out a block's members in a grid. Returns (positioned_members,
    block_width, block_height). Positions are relative to the block's
    top-left corner BEFORE padding (added by caller). Components are
    packed left-to-right, top-to-bottom; row height is the tallest cell
    in that row."""
    if not members:
        return [], 0.0, 0.0

    cells = [(_cell_size(m, spacing), m) for m in members]
    cell_pitch = float(spacing["component_pitch_mm"])
    n = len(members)

    # Column count: square-ish for small blocks, wider for big trash buckets so
    # they don't grow taller than the sheet. Cap rows at MAX_ROWS_PER_BLOCK
    # — once a block needs more rows than that, prefer growing horizontally
    # (Phase 3b will split it across fallback zones instead).
    MAX_ROWS_PER_BLOCK = 10
    cols = max(1, int(math.ceil(math.sqrt(n))))
    if n <= 4:
        cols = min(cols, n)
    rows_at_sqrt = math.ceil(n / cols)
    if rows_at_sqrt > MAX_ROWS_PER_BLOCK:
        cols = max(cols, math.ceil(n / MAX_ROWS_PER_BLOCK))

    # Inter-component gap inside a block. cell_pitch * 0.4 (=3 mm) was too
    # tight — KiCad renders Reference (R3) and Value (100k) property text
    # 2.5 mm above/right of the symbol body, with ~6 mm of horizontal extent.
    # Adjacent cells at 3 mm gap let the value text crash into the next
    # component's body. cell_pitch * 1.0 leaves clear room for typical
    # 4-6 char value strings + the 2.54 mm property offset KiCad uses.
    h_gap = cell_pitch * 1.0
    v_gap = cell_pitch * 0.75

    # Sub-grid for multi-unit ICs (74LS125 quad buffer, LM358 dual op-amp, etc).
    # Each unit gets its own cell inside a clustered super-cell that occupies
    # the parent block's grid like a single member. units_grid_cols=2 keeps
    # 2-4 unit ICs as a 2x2 box; >4 units shifts to ceil(sqrt(N)).
    unit_h_gap = h_gap * 0.5
    unit_v_gap = v_gap * 0.5

    positioned: List[Dict[str, Any]] = []
    x = 0.0
    y = 0.0
    row_h = 0.0
    col_idx = 0
    block_w = 0.0
    for (cw, ch, _), member in cells:
        n_units = max(1, int(member.get("unit_count", 1)))
        if n_units > 1:
            u_cols = 2 if n_units <= 4 else int(math.ceil(math.sqrt(n_units)))
            u_rows = int(math.ceil(n_units / u_cols))
            super_w = u_cols * cw + (u_cols - 1) * unit_h_gap
            super_h = u_rows * ch + (u_rows - 1) * unit_v_gap
            rel_xys: List[Tuple[float, float]] = []
            for u in range(n_units):
                ur, uc = divmod(u, u_cols)
                ux = x + uc * (cw + unit_h_gap) + cw / 2.0
                uy = y + ur * (ch + unit_v_gap) + ch / 2.0
                rel_xys.append((ux, uy))
            positioned.append({
                "ref": member["ref"],
                "role": member["role"],
                "rel_xys": rel_xys,
                "cell_w": super_w,
                "cell_h": super_h,
                "rotation": 0,
                "mirror": False,
            })
            row_h = max(row_h, super_h)
            x += super_w + h_gap
        else:
            positioned.append({
                "ref": member["ref"],
                "role": member["role"],
                "rel_xys": [(x + cw / 2.0, y + ch / 2.0)],
                "cell_w": cw,
                "cell_h": ch,
                "rotation": 0,
                "mirror": False,
            })
            row_h = max(row_h, ch)
            x += cw + h_gap
        col_idx += 1
        block_w = max(block_w, x)
        if col_idx >= cols:
            col_idx = 0
            x = 0.0
            y += row_h + v_gap
            row_h = 0.0
    block_h = y + row_h
    return positioned, block_w, block_h


def _snap(value: float, grid: float) -> float:
    return round(value / grid) * grid


def _resolve_overlaps(
    blocks: List[Dict[str, Any]],
    components: List[Dict[str, Any]],
    sheet_w: float,
    sheet_h: float,
    margin: float,
    grid: float,
    block_gap: float,
    max_iter: int = 60,
    margin_bottom: Optional[float] = None,
) -> List[Tuple[str, str]]:
    """Iteratively push overlapping blocks apart along the axis of smallest
    overlap. The smaller-area block is the victim; the anchor stays put.
    Components inside the victim block shift by the same delta. Returns the
    list of unresolved (role_a, role_b) pairs after max_iter — empty list
    means convergence."""
    if len(blocks) < 2:
        return []

    def overlap(a, b):
        ax2 = a["x_mm"] + a["width_mm"]
        ay2 = a["y_mm"] + a["height_mm"]
        bx2 = b["x_mm"] + b["width_mm"]
        by2 = b["y_mm"] + b["height_mm"]
        ox = min(ax2, bx2) - max(a["x_mm"], b["x_mm"])
        oy = min(ay2, by2) - max(a["y_mm"], b["y_mm"])
        if ox <= 0 or oy <= 0:
            return None
        return ox, oy

    def shift_block(block: Dict[str, Any], dx: float, dy: float) -> None:
        # Match by member-ref set, not by role: sub-blocks created by
        # _split_oversized_blocks have synthetic roles (`GENERIC_1`, `GENERIC_2`)
        # but their components retain the original classifier role (`GENERIC`).
        block["x_mm"] += dx
        block["y_mm"] += dy
        member_set = set(block.get("members") or [])
        for c in components:
            if c["ref"] in member_set:
                c["x_mm"] += dx
                c["y_mm"] += dy

    for _ in range(max_iter):
        moved = False
        for i, a in enumerate(blocks):
            for b in blocks[i + 1:]:
                ov = overlap(a, b)
                if not ov:
                    continue
                ox, oy = ov
                area_a = a["width_mm"] * a["height_mm"]
                area_b = b["width_mm"] * b["height_mm"]
                victim, anchor = (a, b) if area_a <= area_b else (b, a)

                if ox < oy:
                    sign = 1.0 if (victim["x_mm"] + victim["width_mm"] / 2
                                   >= anchor["x_mm"] + anchor["width_mm"] / 2) else -1.0
                    raw = victim["x_mm"] + sign * (ox + block_gap)
                    clamped = max(margin, min(sheet_w - margin - victim["width_mm"], raw))
                    dx = _snap(clamped, grid) - victim["x_mm"]
                    if abs(dx) < grid * 0.5:
                        continue
                    shift_block(victim, dx, 0.0)
                else:
                    sign = 1.0 if (victim["y_mm"] + victim["height_mm"] / 2
                                   >= anchor["y_mm"] + anchor["height_mm"] / 2) else -1.0
                    raw = victim["y_mm"] + sign * (oy + block_gap)
                    m_bot = margin if margin_bottom is None else float(margin_bottom)
                    clamped = max(margin, min(sheet_h - m_bot - victim["height_mm"], raw))
                    dy = _snap(clamped, grid) - victim["y_mm"]
                    if abs(dy) < grid * 0.5:
                        continue
                    shift_block(victim, 0.0, dy)
                moved = True
        if not moved:
            break

    unresolved: List[Tuple[str, str]] = []
    for i, a in enumerate(blocks):
        for b in blocks[i + 1:]:
            if overlap(a, b):
                unresolved.append((a["role"], b["role"]))
    return unresolved


def _mcu_relative_anchor(
    zone: str,
    mcu_bbox: Tuple[float, float, float, float],
    block_w: float,
    block_h: float,
    gap: float,
) -> Optional[Tuple[float, float]]:
    """Position a satellite block relative to the MCU's bbox. Returns the
    block's top-left (x, y). Zone names map to a 12-slot compass around the
    MCU (top/bottom/left/right + start/center/end alignment) plus center.

    This replaces the sheet-fractional position from layout_config.zones for
    every block except the MAIN_CONTROLLER itself — fractions don't know how
    big the MCU is, so a 99 mm wide nRF54 eats the central region and the
    right_top / right_center zones land INSIDE the MCU's bbox. With this,
    satellite zones always sit `gap` mm outside the MCU's edges, regardless
    of MCU size."""
    mx0, my0, mx1, my1 = mcu_bbox  # left, top, right, bottom (Y-down: top < bottom)
    cx = (mx0 + mx1) / 2
    cy = (my0 + my1) / 2

    if zone == "center":
        return (cx - block_w / 2, cy - block_h / 2)

    # Decompose zone name into (side, alignment).
    side: Optional[str] = None
    align: Optional[str] = None
    for s in ("top", "bottom", "left", "right"):
        prefix = s + "_"
        if zone.startswith(prefix):
            side = s
            align = zone[len(prefix):]
            break
    if side is None or align is None:
        return None

    if side == "top":
        by = my0 - gap - block_h
        if align == "left":
            bx = mx0
        elif align == "right":
            bx = mx1 - block_w
        else:  # center
            bx = cx - block_w / 2
    elif side == "bottom":
        by = my1 + gap
        if align == "left":
            bx = mx0
        elif align == "right":
            bx = mx1 - block_w
        else:
            bx = cx - block_w / 2
    elif side == "left":
        bx = mx0 - gap - block_w
        if align == "top":
            by = my0
        elif align == "bottom":
            by = my1 - block_h
        else:  # center
            by = cy - block_h / 2
    else:  # right
        bx = mx1 + gap
        if align == "top":
            by = my0
        elif align == "bottom":
            by = my1 - block_h
        else:
            by = cy - block_h / 2
    return (bx, by)


def _choose_sheet(
    packed: List[Dict[str, Any]],
    sheet_cfg: Dict[str, Any],
    margin: float,
    block_gap: float,
    margin_bottom: Optional[float] = None,
) -> Tuple[str, float, float]:
    """Pick the SMALLEST sheet whose usable area can hold every packed block,
    with a `density_factor` multiplier to leave room for inter-block routing
    and net labels.

    Usable height uses asymmetric margins when `margin_bottom` is supplied
    (the KiCad title block strip lives at the bottom, ~30 mm tall on every
    paper size). Falls back to symmetric `margin` on top/sides when
    margin_bottom is None — preserves back-compat with old callers."""
    sizes = sheet_cfg["sizes_mm"]
    default = sheet_cfg.get("default_size", "A4_landscape")
    density_factor = float(sheet_cfg.get("density_factor", 1.4))
    m_bot = margin if margin_bottom is None else float(margin_bottom)

    total_area = 0.0
    max_block_w = 0.0
    max_block_h = 0.0
    for p in packed:
        w = float(p["block_w"]) + block_gap
        h = float(p["block_h"]) + block_gap
        total_area += w * h
        max_block_w = max(max_block_w, w)
        max_block_h = max(max_block_h, h)

    needed_area = total_area * density_factor

    # Smallest-fits algorithm: walk every configured size sorted by area,
    # take the first one whose usable area AND biggest-block dimensions
    # both fit. The configured default is consulted only if it ties on
    # area with another candidate (deterministic tie-break).
    candidates = sorted(
        sizes.items(), key=lambda kv: kv[1][0] * kv[1][1],
    )
    for name, (w, h) in candidates:
        usable_w = float(w) - 2 * margin
        usable_h = float(h) - margin - m_bot
        if usable_w <= 0 or usable_h <= 0:
            continue
        if usable_w < max_block_w or usable_h < max_block_h:
            continue
        if usable_w * usable_h < needed_area:
            continue
        return name, float(w), float(h)

    # Nothing fits — use the largest available so the overlap resolver
    # has the most room to work with.
    if candidates:
        name, (w, h) = candidates[-1]
        return name, float(w), float(h)
    # Final fallback: A4 landscape if config is empty.
    return default, 297.0, 210.0


def place(classified: Dict[str, Any]) -> Dict[str, Any]:
    """Main entry point. Returns the placement.json structure."""
    cfg = load_config("layout_config")
    grid = float(cfg["grid_mm"])
    spacing = dict(cfg["spacing"])  # local copy — we stash the derived pad
    spacing["_label_pad"] = _label_keepout_pad(cfg)
    zones_cfg = cfg["zones"]

    # Layer 5: detect circuit type from the role distribution, then pick the
    # matching zone map. Falls back to the legacy single role_zone_map when
    # role_zone_maps is absent (back-compat with older config files).
    from . import circuit_type as _circuit_type
    circuit_type = _circuit_type.detect_circuit_type(
        classified, cfg.get("circuit_detection") or {},
    )
    role_zone_map = _circuit_type.pick_zone_map(
        circuit_type,
        cfg.get("role_zone_maps") or {},
        fallback=cfg.get("role_zone_map") or {},
    )

    fallback_zones = cfg["fallback_zones"]["order"]
    margin = float(cfg["sheet"]["margin_mm"])
    # Bottom margin is bigger by default so the KiCad title block strip in
    # the bottom-right corner (~30 mm tall on every paper size) stays clear
    # of placed components. Override with `margin_bottom_mm` in config.
    margin_bottom = float(cfg["sheet"].get("margin_bottom_mm", max(margin, 30.0)))

    blocks = _group_into_blocks(classified)

    # Split oversized buckets (GENERIC trash bucket primarily) BEFORE zone
    # resolution. Each sub-block gets its own zone via fallback_zones.
    split_cfg = cfg.get("block_split") or {}
    if split_cfg.get("enabled", True):
        blocks = _split_oversized_blocks(
            blocks, int(split_cfg.get("max_block_size", 15))
        )

    _resolve_zones(blocks, role_zone_map, fallback_zones)

    pad = float(spacing["block_padding_mm"])

    block_gap = float(spacing["block_gap_mm"])

    # Pass A: pack each block + compute its (w, h). Positions come later.
    # Sheet size is chosen AFTER packing because we need the actual block
    # dimensions to pick the smallest paper that fits.
    packed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for block in blocks:
        members = [m for m in block["members"] if m.get("pin_count", 0) > 0 or m["role"] == "CONNECTOR"]
        for m in block["members"]:
            if m.get("pin_count", 0) == 0 and m["role"] != "CONNECTOR":
                skipped.append({"ref": m["ref"], "reason": "no pins (mounting/annotation)"})
        if not members:
            continue
        positioned, w, h = _pack_block(members, spacing, role=block["role"])
        packed.append({
            "role": block["role"],
            "zone": block.get("zone") or "bottom_center",
            "members": members,
            "positioned": positioned,
            "block_w": w + 2 * pad,
            "block_h": h + 2 * pad,
        })

    # Pick the smallest sheet that accommodates every packed block (area +
    # density factor), now that we know each block's real dimensions.
    sheet_name, sheet_w, sheet_h = _choose_sheet(packed, cfg["sheet"], margin, block_gap,
                                                    margin_bottom=margin_bottom)
    usable_w = sheet_w - 2 * margin
    usable_h = sheet_h - margin - margin_bottom

    # Dynamic block gap: when blocks fill less of the sheet, spread them out
    # to use the available space. Linear interpolation between low/high fill
    # ratios. Universal — sparse circuits (16 components on A4) get wide gaps
    # that fill the sheet; dense circuits (198 components) keep tight gaps.
    block_areas = sum(p["block_w"] * p["block_h"] for p in packed)
    fill_ratio = block_areas / max(1.0, usable_w * usable_h)
    dyn_max = float(spacing.get("dynamic_gap_max_mm", 30.0))
    low = float(spacing.get("dynamic_gap_low_fill_ratio", 0.30))
    high = float(spacing.get("dynamic_gap_high_fill_ratio", 0.70))
    if fill_ratio <= low:
        block_gap = dyn_max
    elif fill_ratio >= high:
        block_gap = float(spacing["block_gap_mm"])  # tight
    else:
        # Linear interpolation
        t = (high - fill_ratio) / (high - low)
        block_gap = float(spacing["block_gap_mm"]) + t * (dyn_max - float(spacing["block_gap_mm"]))

    # Pass B: position each block. When a MAIN_CONTROLLER block exists, anchor
    # the MCU at sheet center and place every other block relative to its
    # bbox + a gap — sheet-fractional positions don't account for MCU size
    # and lead to right_top zones landing INSIDE a 99 mm wide MCU's bbox.
    mcu_block = next((p for p in packed if p["role"] == "MAIN_CONTROLLER"), None)
    mcu_bbox: Optional[Tuple[float, float, float, float]] = None
    if mcu_block is not None:
        mcu_w = mcu_block["block_w"]
        mcu_h = mcu_block["block_h"]

        # Position MCU so satellites above + below fit between MCU edges and
        # sheet edges. Without this, a 99 mm tall MCU centered on a 210 mm
        # sheet leaves only ~46 mm above and below — a 48 mm bottom block
        # gets clamped against the sheet edge, overlapping the MCU.
        def _max_h(side: str) -> float:
            return max((p["block_h"] for p in packed if p is not mcu_block
                        and p["zone"].startswith(side + "_")), default=0.0)
        def _max_w(side: str) -> float:
            return max((p["block_w"] for p in packed if p is not mcu_block
                        and p["zone"].startswith(side + "_")), default=0.0)

        top_h = _max_h("top")
        bottom_h = _max_h("bottom")
        left_w = _max_w("left")
        right_w = _max_w("right")

        # Vertical: center MCU in the band left over after reserving top/bottom.
        v_above = top_h + (block_gap if top_h > 0 else 0)
        v_below = bottom_h + (block_gap if bottom_h > 0 else 0)
        v_band = usable_h - v_above - v_below
        if v_band >= mcu_h:
            mcu_y = margin + v_above + (v_band - mcu_h) / 2
        else:
            mcu_y = margin + (usable_h - mcu_h) / 2

        # Horizontal: same idea for left/right satellites.
        h_left = left_w + (block_gap if left_w > 0 else 0)
        h_right = right_w + (block_gap if right_w > 0 else 0)
        h_band = usable_w - h_left - h_right
        if h_band >= mcu_w:
            mcu_x = margin + h_left + (h_band - mcu_w) / 2
        else:
            mcu_x = margin + (usable_w - mcu_w) / 2

        mcu_x = _snap(mcu_x, grid)
        mcu_y = _snap(mcu_y, grid)
        mcu_block["x_mm"] = mcu_x
        mcu_block["y_mm"] = mcu_y
        mcu_bbox = (mcu_x, mcu_y, mcu_x + mcu_w, mcu_y + mcu_h)

    for p in packed:
        if p is mcu_block:
            continue
        bw, bh, zone = p["block_w"], p["block_h"], p["zone"]
        anchor: Optional[Tuple[float, float]] = None
        if mcu_bbox is not None:
            anchor = _mcu_relative_anchor(zone, mcu_bbox, bw, bh, block_gap)
        if anchor is None:
            # No MCU on this sheet (power-only) OR zone name doesn't map —
            # fall back to the original sheet-fractional anchor.
            frac = zones_cfg.get(zone, [0.5, 0.5])
            anchor = (margin + float(frac[0]) * usable_w,
                      margin + float(frac[1]) * usable_h)
        bx, by = anchor
        bx = _snap(max(margin, min(sheet_w - margin - bw, bx)), grid)
        by = _snap(max(margin, min(sheet_h - margin_bottom - bh, by)), grid)
        p["x_mm"] = bx
        p["y_mm"] = by

    # Pass C: emit components inside their now-positioned blocks. Multi-unit
    # ICs emit N entries (one per unit), all sharing the same `ref` but with
    # distinct `unit` numbers and per-unit (x_mm, y_mm). Downstream router /
    # emitter / label_placer fan-out each unit as a separate placed instance.
    placed_components: List[Dict[str, Any]] = []
    placed_blocks: List[Dict[str, Any]] = []
    for p in packed:
        bx, by = p["x_mm"], p["y_mm"]
        zone = p["zone"]
        # Build ref→node lookup so each placed component carries its
        # pin_count downstream. Hierarchical should_split uses pin_count to
        # identify MCU-like chips even when role-classification put them
        # in non-MAIN_CONTROLLER buckets (ESP32 → WIRELESS by lib_id).
        member_by_ref = {m.get("ref"): m for m in p.get("members") or []}
        for pc in p["positioned"]:
            rel_xys = pc.get("rel_xys") or [(pc.get("rel_x", 0.0), pc.get("rel_y", 0.0))]
            for unit_idx, (rx, ry) in enumerate(rel_xys, start=1):
                src = member_by_ref.get(pc["ref"], {})
                placed_components.append({
                    "ref": pc["ref"],
                    "unit": unit_idx,
                    "role": pc["role"],
                    "pin_count": int(src.get("pin_count", 0)),
                    "x_mm": _snap(bx + pad + rx, grid),
                    "y_mm": _snap(by + pad + ry, grid),
                    "rotation": pc["rotation"],
                    "mirror": pc["mirror"],
                    "block_zone": zone,
                })
        placed_blocks.append({
            "role": p["role"],
            "zone": zone,
            "x_mm": bx,
            "y_mm": by,
            "width_mm": _snap(p["block_w"], grid),
            "height_mm": _snap(p["block_h"], grid),
            "members": [m["ref"] for m in p["members"]],
        })

    # Pass D: replace each block's estimated bbox with the UNION of its
    # placed components' outline AABBs + dynamic padding. The estimated
    # bbox used cell sizes that under-report a symbol's true footprint
    # (pin overhangs + name labels). Measure-after-place guarantees the
    # zone border drawn by the emitter actually wraps every component
    # it claims to contain.
    pad_cfg = cfg.get("bbox_padding") or {}
    pad_base = float(pad_cfg.get("base_mm", 4.0))
    pad_per = float(pad_cfg.get("per_component_mm", 0.4))
    pad_min = float(pad_cfg.get("min_mm", 3.0))
    pad_max = float(pad_cfg.get("max_mm", 12.0))

    members_by_role = {b["role"]: b["members"] for b in placed_blocks}
    outlines = {n["ref"]: (float(n.get("outline_w") or n.get("body_w") or 0.0),
                            float(n.get("outline_h") or n.get("body_h") or 0.0))
                for n in classified["nodes"]}

    components_by_ref: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in placed_components:
        components_by_ref[c["ref"]].append(c)

    for block in placed_blocks:
        members = block["members"]
        if not members:
            continue
        bbox = None
        unit_instance_count = 0
        for ref in members:
            instances = components_by_ref.get(ref, [])
            if not instances:
                continue
            ow, oh = outlines.get(ref, (0.0, 0.0))
            if ow <= 0 or oh <= 0:
                ow = oh = 2.54
            for inst in instances:
                unit_instance_count += 1
                cx, cy = inst["x_mm"], inst["y_mm"]
                cb = (cx - ow / 2, cy - oh / 2, cx + ow / 2, cy + oh / 2)
                if bbox is None:
                    bbox = cb
                else:
                    bbox = (min(bbox[0], cb[0]), min(bbox[1], cb[1]),
                            max(bbox[2], cb[2]), max(bbox[3], cb[3]))
        if bbox is None:
            continue
        dyn_pad = max(pad_min, min(pad_max, pad_base + unit_instance_count * pad_per))
        block["x_mm"] = _snap(bbox[0] - dyn_pad, grid)
        block["y_mm"] = _snap(bbox[1] - dyn_pad, grid)
        block["width_mm"] = _snap((bbox[2] - bbox[0]) + 2 * dyn_pad, grid)
        block["height_mm"] = _snap((bbox[3] - bbox[1]) + 2 * dyn_pad, grid)

    unresolved = _resolve_overlaps(
        placed_blocks, placed_components, sheet_w, sheet_h, margin, grid, block_gap,
        margin_bottom=margin_bottom,
    )

    return {
        "sheet": {"size": sheet_name, "width_mm": sheet_w, "height_mm": sheet_h},
        "main_controller": classified.get("main_controller"),
        "circuit_type": circuit_type,
        "blocks": placed_blocks,
        "components": placed_components,
        "skipped": skipped,
        "unresolved_overlaps": [list(p) for p in unresolved],
    }


def _input_to_classified(in_path: Path) -> Dict[str, Any]:
    """CLI helper: accept either a .kicad_sch (run Steps 1+2 in memory) or a
    pre-built classified.json."""
    if in_path.suffix == ".kicad_sch":
        from .connectivity_graph import build_graph, graph_to_dict
        from .classifier import classify
        return classify(graph_to_dict(build_graph(in_path)))
    with open(in_path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.placer")
    ap.add_argument("input", help="classified.json (Step 2 output) or a .kicad_sch")
    ap.add_argument("--out", help="write placement JSON here (default: stdout)")
    ap.add_argument("--summary", action="store_true", help="print per-block summary to stderr")
    args = ap.parse_args(argv)

    classified = _input_to_classified(Path(args.input))
    placement = place(classified)
    payload = json.dumps(placement, indent=2)

    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(
            f"wrote {args.out}  sheet={placement['sheet']['size']}  "
            f"blocks={len(placement['blocks'])}  "
            f"components={len(placement['components'])}  "
            f"skipped={len(placement['skipped'])}  "
            f"main={placement['main_controller']}"
        )
    else:
        sys.stdout.write(payload + "\n")

    if args.summary:
        sys.stderr.write(f"Sheet: {placement['sheet']['size']} "
                         f"{placement['sheet']['width_mm']:.0f}x{placement['sheet']['height_mm']:.0f} mm\n")
        for b in placement["blocks"]:
            sys.stderr.write(
                f"  [{b['role']:16s}] zone={b['zone']:14s} "
                f"@({b['x_mm']:6.2f}, {b['y_mm']:6.2f}) "
                f"{b['width_mm']:5.1f}x{b['height_mm']:5.1f} mm  "
                f"members={','.join(b['members'][:6])}{'...' if len(b['members']) > 6 else ''}\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
