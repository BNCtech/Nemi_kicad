"""Tool: apply in-place edits to an open .kicad_sch.

Supports a small, focused verb set for the common chat-driven edits:
delete a component, move a component, rotate a component. ADD and
fix_overlaps are NOT in v1 — the user should use build_circuit with a
modified description to add new components (rebuilds the whole file).

Parsing strategy: sexpdata for the root list, then surgical mutation of
the matching `(symbol ...)` node's `(at X Y R)` clause. Serialization
uses a KiCad-style multi-line indented formatter so eeschema reads the
output cleanly. The output is NOT byte-identical to the input — git
diff will show whole-file reformatting — but the schematic is
functionally equivalent and renders identically.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool


# ---------------------------------------------------------------------------
# sexpdata helpers
# ---------------------------------------------------------------------------

def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _ref_of_symbol(symbol_node: list) -> Optional[str]:
    """Return the Reference property of a (symbol ...) node, or None."""
    for child in symbol_node[1:]:
        if isinstance(child, list) and _head(child) == "property":
            if len(child) >= 3 and str(child[1]) == "Reference":
                return str(child[2])
    return None


def _at_of_symbol(symbol_node: list) -> Optional[list]:
    """Return the `(at ...)` child node of a (symbol ...), or None."""
    for child in symbol_node[1:]:
        if isinstance(child, list) and _head(child) == "at":
            return child
    return None


# ---------------------------------------------------------------------------
# KiCad-style multi-line serializer
# ---------------------------------------------------------------------------

def _is_short(node: Any, threshold: int = 50) -> bool:
    """A node should be emitted on one line when its compact form fits in
    `threshold` chars and it contains no nested lists with their own
    sub-lists (so `(at 1 2 0)` stays inline but `(symbol ...)` does not)."""
    if not isinstance(node, list):
        return True
    s = _compact(node)
    if len(s) > threshold:
        return False
    for child in node[1:]:
        if isinstance(child, list):
            for grand in child[1:] if len(child) > 1 else []:
                if isinstance(grand, list):
                    return False
    return True


def _compact(node: Any) -> str:
    """Compact one-line emission — mirrors what _atomize does in engine.py.

    IMPORTANT: preserve the int-vs-float distinction. KiCad's
    `(version 20250114)` MUST stay an integer — promoting it to
    `20250114.0` (`.1f` format) makes the sch loader reject the file
    with 'Failed to load schematic'. Same for layer indices like
    `(0 "F.Cu" signal)` and `(stroke (width 0) ...)`."""
    if isinstance(node, list):
        return "(" + " ".join(_compact(c) for c in node) + ")"
    if isinstance(node, sexpdata.Symbol):
        return node.value()
    if isinstance(node, str):
        s = node.replace("\\", "\\\\").replace('"', '\\"')
        s = s.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return '"' + s + '"'
    if isinstance(node, bool):
        return "yes" if node else "no"
    if isinstance(node, int):
        return str(node)
    if isinstance(node, float):
        if abs(node - round(node)) < 1e-9:
            return f"{node:.1f}"
        return f"{node:.10f}".rstrip("0").rstrip(".")
    return str(node)


def _emit_multiline(node: Any, indent: int = 0,
                     force_inline: bool = False) -> str:
    """Render a sexpdata node tree to KiCad-style text. The ROOT and a
    short whitelist of structural heads stay on their own line; every
    other node emits inline. This matches what the engine produces
    (engine's `_atomize` policy) and what kicad-cli accepts — multi-line
    formatting of `(pin ...)` / `(property ...)` / nested font blocks
    makes the sch loader reject the file with 'Failed to load schematic',
    even when eeschema's GUI accepts the same text.

    Whitelist for newline-per-child: `kicad_sch`, `lib_symbols`,
    `title_block`, `sheet_instances`. Everything else is forced inline."""
    if force_inline:
        return "\t" * indent + _compact(node)
    if not isinstance(node, list):
        return "\t" * indent + _compact(node)
    head_node = node[0] if node else None
    head = _compact(head_node) if head_node is not None else ""
    BLOCK_HEADS = {"kicad_sch", "lib_symbols", "title_block",
                    "sheet_instances"}
    if head not in BLOCK_HEADS:
        return "\t" * indent + _compact(node)
    leading_atoms = []
    rest_start = 1
    for child in node[1:]:
        if isinstance(child, list):
            break
        leading_atoms.append(_compact(child))
        rest_start += 1
    first_line = "\t" * indent + "(" + head
    if leading_atoms:
        first_line += " " + " ".join(leading_atoms)
    lines = [first_line]
    for child in node[rest_start:]:
        lines.append(_emit_multiline(child, indent + 1))
    lines.append("\t" * indent + ")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verb implementations
# ---------------------------------------------------------------------------

def _find_symbol(root: list, ref: str) -> Optional[list]:
    if not isinstance(root, list):
        return None
    for child in root[1:]:
        if isinstance(child, list) and _head(child) == "symbol":
            if _ref_of_symbol(child) == ref:
                return child
    return None


def _pin_abs_coords(symbol_node: list) -> List[Tuple[float, float]]:
    """Resolve every pin's absolute (x, y) for a placed symbol instance.
    Returns [] when the lib_id can't be loaded (unknown symbol) — caller
    treats that as 'no pin coords known, skip orphan sweep'."""
    lib_id = None
    for child in symbol_node[1:]:
        if isinstance(child, list) and _head(child) == "lib_id":
            if len(child) >= 2:
                lib_id = str(child[1])
            break
    at = _at_of_symbol(symbol_node)
    if lib_id is None or at is None or len(at) < 3:
        return []
    try:
        sx = float(at[1]); sy = float(at[2])
        srot = float(at[3]) if len(at) >= 4 else 0.0
    except (TypeError, ValueError):
        return []
    try:
        from ..kicad.symbol_geom import load_symbol, place_pin
    except ImportError:
        return []
    try:
        geom = load_symbol(lib_id)
    except ValueError:
        return []
    out: List[Tuple[float, float]] = []
    for p in geom.pins:
        try:
            abs_pos = place_pin(p, sx, sy, srot)
        except Exception:
            continue
        if abs_pos is None:
            continue
        out.append((round(abs_pos[0], 4), round(abs_pos[1], 4)))
    return out


def _coord_match(a: Tuple[float, float], b: Tuple[float, float],
                  tol: float = 0.01) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _wire_endpoints(wire_node: list) -> List[Tuple[float, float]]:
    """Extract (x, y) pairs from a (wire (pts (xy ...) (xy ...))) node."""
    out: List[Tuple[float, float]] = []
    for child in wire_node[1:]:
        if not (isinstance(child, list) and _head(child) == "pts"):
            continue
        for xy in child[1:]:
            if (isinstance(xy, list) and _head(xy) == "xy"
                    and len(xy) >= 3):
                try:
                    out.append((round(float(xy[1]), 4),
                                 round(float(xy[2]), 4)))
                except (TypeError, ValueError):
                    pass
    return out


def _op_delete(root: list, ref: str) -> Tuple[bool, str]:
    target = _find_symbol(root, ref)
    if target is None:
        return False, f"no symbol with Reference={ref!r} found"
    # Capture pin coords BEFORE removal so we can sweep orphan wires +
    # junctions that terminate at those coords. Skipping the sweep
    # leaves wires pointing into empty space, which (a) draws as a
    # body-piercing red line in eeschema and (b) cascades into new ERC
    # errors ('Pin not connected' on the orphan endpoint + 'Power input
    # pin not driven' on every #PWR/#FLG that lost its rail).
    pin_coords = _pin_abs_coords(target)
    root.remove(target)
    removed_wires = 0
    removed_junctions = 0
    removed_ncs = 0
    if pin_coords:
        # Walk root in reverse so removals don't shift indices.
        for child in list(root[1:]):
            if not isinstance(child, list):
                continue
            head = _head(child)
            if head == "wire":
                eps = _wire_endpoints(child)
                if any(_coord_match(ep, pc) for ep in eps for pc in pin_coords):
                    root.remove(child)
                    removed_wires += 1
            elif head == "junction":
                # (junction (at x y) ...) — match the at clause coord
                jat = None
                for sub in child[1:]:
                    if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                        jat = sub
                        break
                if jat is not None:
                    try:
                        jp = (round(float(jat[1]), 4),
                                round(float(jat[2]), 4))
                    except (TypeError, ValueError):
                        continue
                    if any(_coord_match(jp, pc) for pc in pin_coords):
                        root.remove(child)
                        removed_junctions += 1
            elif head == "no_connect":
                nat = None
                for sub in child[1:]:
                    if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                        nat = sub
                        break
                if nat is not None:
                    try:
                        np_ = (round(float(nat[1]), 4),
                                round(float(nat[2]), 4))
                    except (TypeError, ValueError):
                        continue
                    if any(_coord_match(np_, pc) for pc in pin_coords):
                        root.remove(child)
                        removed_ncs += 1
    extras = []
    if removed_wires:
        extras.append(f"{removed_wires} wire(s)")
    if removed_junctions:
        extras.append(f"{removed_junctions} junction(s)")
    if removed_ncs:
        extras.append(f"{removed_ncs} no_connect(s)")
    suffix = f" (+swept {', '.join(extras)})" if extras else ""
    return True, f"removed symbol {ref}{suffix}"


def _op_move(root: list, ref: str, dx: float, dy: float) -> Tuple[bool, str]:
    target = _find_symbol(root, ref)
    if target is None:
        return False, f"no symbol with Reference={ref!r} found"
    at = _at_of_symbol(target)
    if at is None or len(at) < 3:
        return False, f"symbol {ref} has no (at ...) clause"
    try:
        x = float(at[1]); y = float(at[2])
    except (TypeError, ValueError):
        return False, f"symbol {ref} (at ...) coords not numeric"
    at[1] = x + dx
    at[2] = y + dy
    return True, f"moved {ref} by ({dx}, {dy}) -> ({at[1]:.2f}, {at[2]:.2f})"


def _op_rotate(root: list, ref: str, angle: float) -> Tuple[bool, str]:
    target = _find_symbol(root, ref)
    if target is None:
        return False, f"no symbol with Reference={ref!r} found"
    at = _at_of_symbol(target)
    if at is None:
        return False, f"symbol {ref} has no (at ...) clause"
    if len(at) < 4:
        at.append(0.0)
    try:
        current = float(at[3])
    except (TypeError, ValueError):
        current = 0.0
    at[3] = (current + angle) % 360.0
    return True, f"rotated {ref} by {angle}° -> {at[3]:.1f}°"


# ---------------------------------------------------------------------------
# Phase 6 — property editing (value / footprint / DNP / arbitrary)
# ---------------------------------------------------------------------------

def _set_symbol_property(symbol_node: list, key: str, value: str) -> bool:
    """Set (property "key" "value" ...) on a symbol node. Updates the
    existing property if present, otherwise appends a new minimal one.
    Returns True on success."""
    for child in symbol_node[1:]:
        if not isinstance(child, list):
            continue
        if _head(child) != "property":
            continue
        if len(child) >= 3 and str(child[1]) == key:
            child[2] = value
            return True
    # Property doesn't exist yet — append a minimal one. KiCad accepts
    # `(property "Key" "Value" (at 0 0 0))` as the minimum shape.
    new_prop = [
        sexpdata.Symbol("property"),
        key,
        value,
        [sexpdata.Symbol("at"), 0.0, 0.0, 0.0],
    ]
    symbol_node.append(new_prop)
    return True


def _op_change_value(root: list, ref: str, new_value: str) -> Tuple[bool, str]:
    target = _find_symbol(root, ref)
    if target is None:
        return False, f"no symbol with Reference={ref!r} found"
    # Capture old value for the reply (designer wants to see "10k -> 4k7")
    old = ""
    for child in target[1:]:
        if (isinstance(child, list) and _head(child) == "property"
                and len(child) >= 3 and str(child[1]) == "Value"):
            old = str(child[2])
            break
    _set_symbol_property(target, "Value", new_value)
    return True, f"value of {ref}: {old!r} -> {new_value!r}"


def _op_change_footprint(root: list, ref: str, new_footprint: str) -> Tuple[bool, str]:
    target = _find_symbol(root, ref)
    if target is None:
        return False, f"no symbol with Reference={ref!r} found"
    _set_symbol_property(target, "Footprint", new_footprint)
    return True, f"footprint of {ref} -> {new_footprint!r}"


def _op_set_property(root: list, ref: str, key: str,
                       value: str) -> Tuple[bool, str]:
    """Generic property setter — DNP marker via this is awkward (use
    set_dnp instead); for tolerance / datasheet / part number / etc.
    this is the right verb. Reserved property names that have special
    handling: Reference, Value, Footprint."""
    target = _find_symbol(root, ref)
    if target is None:
        return False, f"no symbol with Reference={ref!r} found"
    if not key:
        return False, "key must be non-empty"
    _set_symbol_property(target, key, value)
    return True, f"{ref}.{key} -> {value!r}"


# ---------------------------------------------------------------------------
# Phase 7 — wire add / delete
# ---------------------------------------------------------------------------

import uuid as _uuid


def _new_uuid() -> str:
    return str(_uuid.uuid4())


def _symbol_bboxes_from_root(root: list,
                               exclude_endpoints: Optional[List[Tuple[float, float]]] = None
                               ) -> List[Tuple[float, float, float, float, str]]:
    """Compute (x1, y1, x2, y2, ref) bbox for every PLACED symbol in
    the schematic. Used by the geometry validator so apply_ops:add_wire
    can reject paths that would pierce a component body.

    Bbox is derived from pin extents in the matching lib_symbol entry,
    transformed by the symbol instance's (at x y rot). Power-port
    symbols (refdes starting with '#') get a generous fixed bbox
    because their tiny pin extent doesn't reflect the visible arrow.

    `exclude_endpoints` is a list of (x, y) — bboxes whose centre is
    within 1 mm of any endpoint are SKIPPED. This is critical: a wire
    LEGITIMATELY touches its own endpoint pins; flagging that as a
    pierce would block every legal wire. The caller passes the wire's
    endpoints so the validator knows which symbols are 'allowed to
    touch'."""
    excluded = exclude_endpoints or []

    # Load lib_symbol pin extents once
    lib_pin_extents: Dict[str, Tuple[float, float, float, float]] = {}
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "lib_symbols"):
            continue
        for sym_def in child[1:]:
            if not (isinstance(sym_def, list) and _head(sym_def) == "symbol"):
                continue
            if len(sym_def) < 2:
                continue
            lib_id = str(sym_def[1])
            xs: List[float] = []
            ys: List[float] = []
            def _walk_for_pins(node):
                if not isinstance(node, list):
                    return
                if _head(node) == "pin":
                    for sub in node[1:]:
                        if (isinstance(sub, list) and _head(sub) == "at"
                                and len(sub) >= 3):
                            try:
                                xs.append(float(sub[1]))
                                ys.append(float(sub[2]))
                            except (TypeError, ValueError):
                                pass
                for child2 in node[1:]:
                    _walk_for_pins(child2)
            _walk_for_pins(sym_def)
            if xs and ys:
                lib_pin_extents[lib_id] = (min(xs), min(ys),
                                            max(xs), max(ys))

    out: List[Tuple[float, float, float, float, str]] = []
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "symbol"):
            continue
        ref = _ref_of_symbol(child) or ""
        # Power-port symbols (#PWR, #FLG) have minimal bbox — wires
        # SHOULD pass through them legitimately to bond to the rail.
        # Skip them entirely from collision check.
        if ref.startswith("#"):
            continue
        lib_id = None
        for c2 in child[1:]:
            if (isinstance(c2, list) and _head(c2) == "lib_id"
                    and len(c2) >= 2):
                lib_id = str(c2[1])
                break
        at = _at_of_symbol(child)
        if lib_id is None or at is None or len(at) < 3:
            continue
        try:
            sx = float(at[1])
            sy = float(at[2])
        except (TypeError, ValueError):
            continue
        extent = lib_pin_extents.get(lib_id)
        if extent is None:
            # Conservative fallback: 8x8 mm centred bbox
            bbox = (sx - 4.0, sy - 4.0, sx + 4.0, sy + 4.0)
        else:
            lx1, ly1, lx2, ly2 = extent
            # KiCad symbol coords are Y-up; schematic is Y-down. Mirror Y.
            bbox = (sx + lx1, sy - ly2, sx + lx2, sy - ly1)
        # Skip if any wire endpoint is at this symbol's pin tip
        skip = False
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        for (ex, ey) in excluded:
            # Endpoint inside expanded bbox = legitimate touch
            if (bbox[0] - 2.54 <= ex <= bbox[2] + 2.54
                    and bbox[1] - 2.54 <= ey <= bbox[3] + 2.54):
                skip = True
                break
        if skip:
            continue
        out.append((bbox[0], bbox[1], bbox[2], bbox[3], ref))
    return out


def _segment_pierces_bbox(x1: float, y1: float, x2: float, y2: float,
                            bbox: Tuple[float, float, float, float],
                            margin: float = 0.5) -> bool:
    """True iff the Manhattan segment (x1,y1)→(x2,y2) passes through
    the INTERIOR of bbox (shrunk by `margin` so edge-touches don't
    count as a pierce)."""
    bx1, by1, bx2, by2 = bbox
    sx1, sy1 = bx1 + margin, by1 + margin
    sx2, sy2 = bx2 - margin, by2 - margin
    if sx1 >= sx2 or sy1 >= sy2:
        return False
    # Horizontal
    if abs(y1 - y2) < 0.01:
        y = y1
        if not (sy1 < y < sy2):
            return False
        lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
        return lo < sx2 and hi > sx1
    # Vertical
    if abs(x1 - x2) < 0.01:
        x = x1
        if not (sx1 < x < sx2):
            return False
        lo, hi = (y1, y2) if y1 <= y2 else (y2, y1)
        return lo < sy2 and hi > sy1
    # Diagonal — wires shouldn't be diagonal, but be defensive:
    # check midpoint inside.
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    return sx1 < mx < sx2 and sy1 < my < sy2


def _wire_piercing_components(x1: float, y1: float,
                                x2: float, y2: float,
                                root: list) -> List[str]:
    """Return list of refdes whose body a straight wire from
    (x1,y1)→(x2,y2) would pierce. Wire endpoints excluded from check
    so a wire legally touching its own endpoint pins isn't flagged."""
    bboxes = _symbol_bboxes_from_root(root,
                                        exclude_endpoints=[(x1, y1),
                                                            (x2, y2)])
    pierced: List[str] = []
    for (bx1, by1, bx2, by2, ref) in bboxes:
        if _segment_pierces_bbox(x1, y1, x2, y2, (bx1, by1, bx2, by2)):
            pierced.append(ref)
    return pierced


def _existing_wire_segments(root: list) -> List[Tuple[float, float, float, float]]:
    """Extract all (x1, y1, x2, y2) wire segments currently in the
    schematic. Used by the wire-to-wire overlap check so a new wire
    op can detect 'going on top of an existing wire' situations
    BEFORE committing."""
    out: List[Tuple[float, float, float, float]] = []
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        pts = None
        for c2 in child[1:]:
            if isinstance(c2, list) and _head(c2) == "pts":
                pts = c2
                break
        if pts is None:
            continue
        xys: List[Tuple[float, float]] = []
        for xy in pts[1:]:
            if isinstance(xy, list) and _head(xy) == "xy" and len(xy) >= 3:
                try:
                    xys.append((float(xy[1]), float(xy[2])))
                except (TypeError, ValueError):
                    pass
        if len(xys) >= 2:
            out.append((xys[0][0], xys[0][1], xys[1][0], xys[1][1]))
    return out


def _manhattan_segments_overlap(a: Tuple[float, float, float, float],
                                 b: Tuple[float, float, float, float],
                                 tol: float = 0.1) -> bool:
    """True iff two Manhattan segments share a NON-ZERO-LENGTH portion
    of the same line. Endpoint-only touches don't count (that's a
    legal T-junction). Diagonal segments are treated as 'overlap if
    any shared point' — but we don't emit diagonals anyway.

    Two horizontal segments at the same Y with x-ranges that
    intersect for more than `tol` mm = overlap. Same logic for
    vertical at same X. Different orientations = no overlap (they
    can cross at a single point — that's a 4-way junction, handled
    elsewhere)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    a_horiz = abs(ay1 - ay2) < 0.01
    a_vert  = abs(ax1 - ax2) < 0.01
    b_horiz = abs(by1 - by2) < 0.01
    b_vert  = abs(bx1 - bx2) < 0.01
    # Horizontal vs horizontal
    if a_horiz and b_horiz:
        if abs(ay1 - by1) > tol:
            return False
        a_lo, a_hi = (ax1, ax2) if ax1 <= ax2 else (ax2, ax1)
        b_lo, b_hi = (bx1, bx2) if bx1 <= bx2 else (bx2, bx1)
        # Non-zero overlap on the x range
        return max(a_lo, b_lo) + tol < min(a_hi, b_hi)
    # Vertical vs vertical
    if a_vert and b_vert:
        if abs(ax1 - bx1) > tol:
            return False
        a_lo, a_hi = (ay1, ay2) if ay1 <= ay2 else (ay2, ay1)
        b_lo, b_hi = (by1, by2) if by1 <= by2 else (by2, by1)
        return max(a_lo, b_lo) + tol < min(a_hi, b_hi)
    return False


def _segment_overlaps_existing(x1: float, y1: float,
                                 x2: float, y2: float,
                                 existing: List[Tuple[float, float, float, float]]
                                 ) -> bool:
    """Convenience wrapper: does the proposed segment run along the
    SAME line as any existing wire for a non-zero length? Returns
    True on first overlap detected — caller treats as 'pick another
    route variant'."""
    new_seg = (x1, y1, x2, y2)
    for ex in existing:
        if _manhattan_segments_overlap(new_seg, ex):
            return True
    return False


def _op_add_wire(root: list, x1: float, y1: float,
                  x2: float, y2: float) -> Tuple[bool, str]:
    """Append a (wire ...) node to the schematic root. Coords in mm,
    snapped to KiCad's 50-mil (1.27 mm) grid so the wire endpoints land
    on legal positions and electrically connect to pin tips.

    GEOMETRY VALIDATOR (2026-05-27): rejects straight wires whose path
    would pierce a non-endpoint component body. The professional rule
    (KLC, IEEE 315): wires never cross component bodies — they connect
    pin tips only. AI-driven wire ops that propose a straight line from
    one side of an IC to the other get REJECTED here so the next try
    can use add_wire_by_pin (L-routed) or split the wire into segments
    that go around the obstacle. Without this check, follow-up edits
    caused 'wire-over-symbol' chaos the user flagged in field tests."""
    # Snap to 1.27 mm grid
    def _snap(v: float) -> float:
        return round(v / 1.27) * 1.27
    x1, y1, x2, y2 = _snap(x1), _snap(y1), _snap(x2), _snap(y2)
    if x1 == x2 and y1 == y2:
        return False, "wire endpoints identical — refusing zero-length wire"
    # Pre-commit geometry check. Diagonal wires aren't legal in KiCad
    # anyway; only L-shaped (Manhattan) paths pass cleanly.
    if abs(x1 - x2) > 0.01 and abs(y1 - y2) > 0.01:
        return False, (f"diagonal wire ({x1:.2f},{y1:.2f})->"
                        f"({x2:.2f},{y2:.2f}) rejected — split into "
                        f"horizontal + vertical segments")
    pierced = _wire_piercing_components(x1, y1, x2, y2, root)
    if pierced:
        return False, (f"wire ({x1:.2f},{y1:.2f})->({x2:.2f},{y2:.2f}) "
                        f"REJECTED: would pierce {', '.join(pierced)} body. "
                        f"Either split into L-shape via two add_wire ops "
                        f"or use add_wire_by_pin which auto-routes around "
                        f"components.")
    # Wire-over-wire rule (#5): reject a new segment that runs along
    # the SAME line as an existing wire. Visual ambiguity (two wires
    # on top of each other) + electrical risk (KiCad bonds them at
    # endpoints regardless of intent). Endpoint-only touches don't
    # count — that's a legal T-junction.
    existing = _existing_wire_segments(root)
    if _segment_overlaps_existing(x1, y1, x2, y2, existing):
        return False, (f"wire ({x1:.2f},{y1:.2f})->({x2:.2f},{y2:.2f}) "
                        f"REJECTED: overlaps an existing wire on the "
                        f"same line. Pick a different route (use "
                        f"add_wire_by_pin which auto-detours).")
    wire = [
        sexpdata.Symbol("wire"),
        [sexpdata.Symbol("pts"),
         [sexpdata.Symbol("xy"), x1, y1],
         [sexpdata.Symbol("xy"), x2, y2]],
        [sexpdata.Symbol("stroke"),
         [sexpdata.Symbol("width"), 0.0],
         [sexpdata.Symbol("type"), sexpdata.Symbol("default")]],
        [sexpdata.Symbol("uuid"), _new_uuid()],
    ]
    root.append(wire)
    return True, f"wire ({x1:.2f},{y1:.2f}) -> ({x2:.2f},{y2:.2f})"


def _op_delete_wire(root: list, x1: float, y1: float,
                     x2: float, y2: float,
                     tol: float = 1.5,
                     match: str = "both") -> Tuple[bool, str]:
    """Find a (wire ...) node whose endpoints match (within `tol` mm)
    and remove it. Either direction matches — (a,b)→(c,d) == (c,d)→(a,b).
    User-supplied coordinates are snapped to the 1.27 mm grid before
    comparison, so callers don't have to manually snap; add_wire and
    delete_wire with the SAME caller coords will always match.

    `match` controls how a wire is selected:
      "both" (default, LEGACY) — a wire is removed only when BOTH its
            endpoints match the two given points. This is the original
            behaviour and is byte-stable for every existing caller.
      "either_end" — a wire is removed when EITHER endpoint is within tol
            of the FIRST point (x1,y1). The ERC `wire_dangling` fix uses
            this: KiCad reports only the dangling END's coord, not the
            whole segment, so a both-endpoint match would never find a
            real (non-degenerate) dangling wire. Safe because the
            dangling end is by definition shared with nothing, so it can
            only hit the dangling wire — and erc_autofix's regression
            guard re-runs ERC and rolls back if the delete made things
            worse."""
    def _snap(v: float) -> float:
        return round(v / 1.27) * 1.27
    x1, y1, x2, y2 = _snap(x1), _snap(y1), _snap(x2), _snap(y2)

    def _close(a: float, b: float) -> bool:
        return abs(a - b) <= tol

    for child in list(root[1:]):
        if not isinstance(child, list) or _head(child) != "wire":
            continue
        # Find the (pts ...) sub-node
        pts = None
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "pts":
                pts = sub
                break
        if pts is None or len(pts) < 3:
            continue
        # pts = [Symbol("pts"), [xy, x1, y1], [xy, x2, y2]]
        try:
            a = pts[1]; b = pts[2]
            ax, ay = float(a[1]), float(a[2])
            bx, by = float(b[1]), float(b[2])
        except (IndexError, TypeError, ValueError):
            continue
        if match == "either_end":
            matches = ((_close(ax, x1) and _close(ay, y1))
                       or (_close(bx, x1) and _close(by, y1)))
        else:
            matches = ((_close(ax, x1) and _close(ay, y1)
                        and _close(bx, x2) and _close(by, y2))
                       or (_close(ax, x2) and _close(ay, y2)
                            and _close(bx, x1) and _close(by, y1)))
        if matches:
            root.remove(child)
            return True, f"deleted wire ({ax:.2f},{ay:.2f})-({bx:.2f},{by:.2f})"
    return False, ("no wire found with endpoints near "
                    f"({x1:.2f},{y1:.2f})-({x2:.2f},{y2:.2f})")


# ---------------------------------------------------------------------------
# ERC safe-auto verbs — add_no_connect / snap_endpoint
#
# These are the executors the ERC taxonomy (config/erc_taxonomy.json) routes
# its `safe_auto` violations to once erc_autofix's confidence layer clears
# them: a genuinely-unused pin gets a No-Connect flag; an off-grid wire end
# gets snapped onto the connection grid. Both are coordinate-driven (the
# ERC report gives the exact pin / endpoint coord) and both are protected by
# erc_autofix's regression guard, which re-runs ERC and rolls back if a fix
# ever introduces a new error. The .kicad_sch grammar matches KiCad's own
# writer:  (no_connect (at X Y) (uuid "..."))  — confirmed against the KiCad
# source (eeschema/erc/erc_item.cpp keys + demo schematics).
# ---------------------------------------------------------------------------

def _op_add_no_connect(root: list, x: float, y: float) -> Tuple[bool, str]:
    """Append a (no_connect (at x y) (uuid ...)) marker at the given coord —
    the canonical fix for a `pin_not_connected` ERC error on a pin that is
    genuinely unused. Coords in mm, snapped to KiCad's 1.27 mm grid (pins
    sit on grid and the ERC coord is already the pin tip).

    Idempotent: if a no_connect already sits within half a grid cell of the
    target, nothing is added — re-running the fix never stacks markers."""
    def _snap(v: float) -> float:
        return round(v / 1.27) * 1.27
    x, y = _snap(x), _snap(y)
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "no_connect"):
            continue
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                try:
                    ax, ay = float(sub[1]), float(sub[2])
                except (TypeError, ValueError):
                    continue
                if abs(ax - x) <= 0.635 and abs(ay - y) <= 0.635:
                    return True, (f"no_connect already present at "
                                   f"({x:.2f},{y:.2f}) — left as-is")
    nc = [
        sexpdata.Symbol("no_connect"),
        [sexpdata.Symbol("at"), x, y],
        [sexpdata.Symbol("uuid"), _new_uuid()],
    ]
    root.append(nc)
    return True, f"no_connect at ({x:.2f},{y:.2f})"


def _op_snap_endpoint(root: list, x: float, y: float,
                       tol: float = 1.0) -> Tuple[bool, str]:
    """Snap an off-grid WIRE endpoint near (x, y) onto the 1.27 mm grid —
    the fix for an `endpoint_off_grid` ERC error caused by a wire end that
    misses a legal connection point. Coords in mm.

    Scope = WIRE ENDS ONLY (the safe case). A symbol PIN off grid would
    need the whole symbol moved (which can break other connections), so
    when the off-grid point belongs to a pin and not a wire end this
    returns False and the caller leaves it for human review — and the
    regression guard would reject a wrong move anyway."""
    def _snap(v: float) -> float:
        return round(v / 1.27) * 1.27

    best = None   # (dist, pts_node, idx, ex, ey)
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "wire"):
            continue
        pts = None
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "pts":
                pts = sub
                break
        if pts is None or len(pts) < 3:
            continue
        for idx in (1, 2):
            try:
                ex, ey = float(pts[idx][1]), float(pts[idx][2])
            except (IndexError, TypeError, ValueError):
                continue
            off_grid = (abs(ex - _snap(ex)) > 1e-6
                        or abs(ey - _snap(ey)) > 1e-6)
            if not off_grid:
                continue
            d = math.hypot(ex - x, ey - y)
            if d <= tol and (best is None or d < best[0]):
                best = (d, pts, idx, ex, ey)

    if best is None:
        return False, (f"no off-grid wire endpoint within {tol}mm of "
                        f"({x:.2f},{y:.2f}) — likely a symbol pin off grid; "
                        f"needs manual placement")
    _d, pts, idx, ex, ey = best
    other = pts[2] if idx == 1 else pts[1]
    try:
        ox, oy = float(other[1]), float(other[2])
    except (IndexError, TypeError, ValueError):
        ox, oy = None, None
    if (ox is not None and abs(_snap(ex) - ox) < 1e-6
            and abs(_snap(ey) - oy) < 1e-6):
        return False, ("snapping would collapse the wire to zero length — "
                        "left for manual review")
    pts[idx][1] = _snap(ex)
    pts[idx][2] = _snap(ey)
    return True, (f"snapped wire end ({ex:.3f},{ey:.3f}) -> "
                   f"({_snap(ex):.2f},{_snap(ey):.2f})")


# ---------------------------------------------------------------------------
# Phase 8 — add_component (in-place injection)
# ---------------------------------------------------------------------------

def _find_or_create_lib_symbols(root: list) -> list:
    """Return the schematic's (lib_symbols ...) node, creating an empty one
    in the correct position (right after the header / title_block, before
    any instances) when the file doesn't have one yet."""
    for child in root[1:]:
        if isinstance(child, list) and _head(child) == "lib_symbols":
            return child
    node = [sexpdata.Symbol("lib_symbols")]
    header_heads = {"version", "generator", "generator_version", "uuid",
                    "paper", "title_block"}
    insert_at = len(root)
    for i, child in enumerate(root[1:], start=1):
        if isinstance(child, list) and _head(child) not in header_heads:
            insert_at = i
            break
    root.insert(insert_at, node)
    return node


def _lib_symbols_contains(libsym: list, lib_id: str) -> bool:
    for sym_def in libsym[1:]:
        if (isinstance(sym_def, list) and _head(sym_def) == "symbol"
                and len(sym_def) >= 2 and str(sym_def[1]) == lib_id):
            return True
    return False


def _ensure_lib_symbol(root: list, lib_id: str) -> Tuple[Optional[str], str]:
    """Search the REAL symbol libraries for `lib_id`, inline its definition
    into the schematic's (lib_symbols ...) block, and return the RESOLVED
    lib_id (a fuzzy match may rename it, e.g. Connector:Conn_01x02 ->
    Connector:Conn_01x02_Pin).

    This is the fix for the '?' placeholder: eeschema does NOT resolve a
    lib_id from the (here, empty) KiCad install at open time, so the symbol
    definition has to travel inside the .kicad_sch. build_circuit already
    inlines via the engine; in-place adds must do the same. Nothing about
    which libraries exist is hardcoded — load_symbol scans the configured
    roots ($KICAD_SYMBOL_DIR + the user's kicad-sym-lib) dynamically.

    Returns (resolved_lib_id, note); resolved_lib_id is None when the part
    is in NO available library (caller should refuse the add rather than
    inject a symbol that would render as '?')."""
    try:
        from ..kicad.symbol_geom import load_symbol
        geom = load_symbol(lib_id)
    except Exception as exc:
        return None, f"lib_id {lib_id!r} not found in any library ({exc})"
    resolved = getattr(geom, "lib_id", lib_id) or lib_id
    raw = getattr(geom, "raw_symbol_sexpr", None)
    if not raw:
        return None, f"lib_id {lib_id!r} resolved but has no symbol body"
    libsym = _find_or_create_lib_symbols(root)
    if not _lib_symbols_contains(libsym, resolved):
        sym = list(raw)
        sym[1] = resolved
        libsym.append(sym)
    return resolved, (f"resolved {lib_id!r}->{resolved!r}"
                       if resolved != lib_id else "ok")


def _op_add_component(root: list, ref: str, lib_id: str, value: str,
                       x: float, y: float, rotation: float = 0.0,
                       footprint: str = "") -> Tuple[bool, str]:
    """Inject a new (symbol ...) instance into the schematic at position
    (x, y). Coordinates in mm.

    The symbol's library definition is SEARCHED in the real libraries and
    INLINED into the schematic's (lib_symbols ...) block — eeschema does not
    resolve a lib_id from the install at open time, so without this the part
    renders as a '?' placeholder. The lib_id is also validated against the
    available libraries (with fuzzy fallback); if it exists in NO library the
    add is REFUSED instead of dropping a '?'. Which libraries exist is not
    hardcoded — the resolver scans the configured symbol roots dynamically.

    Designer-friendly: when the user says 'add a 10k pullup', the agent
    picks ref/lib_id/value and calls this verb. Auto-wiring to the target
    pin happens via a SEPARATE add_wire call after the component lands."""
    # Snap to grid
    def _snap(v: float) -> float:
        return round(v / 1.27) * 1.27
    x, y = _snap(x), _snap(y)

    # Refuse if ref already exists (avoid silent duplicates)
    if _find_symbol(root, ref) is not None:
        return False, f"a symbol with Reference={ref!r} already exists"

    # Search the libraries + inline the definition so KiCad never shows '?'.
    resolved_lib, lib_note = _ensure_lib_symbol(root, lib_id)
    if resolved_lib is None:
        return False, f"add {ref}: {lib_note}"
    lib_id = resolved_lib

    symbol = [
        sexpdata.Symbol("symbol"),
        [sexpdata.Symbol("lib_id"), lib_id],
        [sexpdata.Symbol("at"), x, y, rotation],
        [sexpdata.Symbol("unit"), 1],
        [sexpdata.Symbol("exclude_from_sim"), sexpdata.Symbol("no")],
        [sexpdata.Symbol("in_bom"), sexpdata.Symbol("yes")],
        [sexpdata.Symbol("on_board"), sexpdata.Symbol("yes")],
        [sexpdata.Symbol("dnp"), sexpdata.Symbol("no")],
        [sexpdata.Symbol("uuid"), _new_uuid()],
        [sexpdata.Symbol("property"), "Reference", ref,
         [sexpdata.Symbol("at"), x + 2.54, y - 1.27, 0.0]],
        [sexpdata.Symbol("property"), "Value", value,
         [sexpdata.Symbol("at"), x + 2.54, y + 1.27, 0.0]],
        [sexpdata.Symbol("property"), "Footprint", footprint,
         [sexpdata.Symbol("at"), x, y, 0.0]],
        [sexpdata.Symbol("property"), "Datasheet", "",
         [sexpdata.Symbol("at"), x, y, 0.0]],
        [sexpdata.Symbol("property"), "Description", "",
         [sexpdata.Symbol("at"), x, y, 0.0]],
    ]
    root.append(symbol)
    extra = "" if lib_note in ("ok",) else f" [{lib_note}]"
    return True, (f"added {ref} ({lib_id}, value={value!r}) at "
                   f"({x:.2f}, {y:.2f}); symbol inlined{extra}")


# ---------------------------------------------------------------------------
# Phase 11 — pull-up / pull-down auto-add
# ---------------------------------------------------------------------------

def _find_label_position(root: list, net_name: str) -> Optional[Tuple[float, float]]:
    """Find any (label "<net_name>") or (global_label ...) on the sheet.
    Returns its (x, y) — useful when we need to drop a pullup adjacent
    to a labelled net even without a known IC pin."""
    if not isinstance(root, list):
        return None
    target = net_name.strip()
    if not target:
        return None
    candidates = []
    for child in root[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)
        if h not in ("label", "global_label", "hierarchical_label"):
            continue
        # (label "name" (at x y rot) ...)
        if len(child) < 2:
            continue
        if str(child[1]).strip() != target:
            continue
        # Find (at x y ...)
        for sub in child[2:]:
            if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
                try:
                    return (float(sub[1]), float(sub[2]))
                except (TypeError, ValueError):
                    pass
    return None


def _next_refdes(root: list, prefix: str) -> str:
    """Pick the next unused refdes for a given prefix (R, C, U, ...).
    Scans all symbol nodes for existing References starting with prefix +
    digit, returns prefix + (max_n + 1)."""
    max_n = 0
    for child in root[1:]:
        if not isinstance(child, list) or _head(child) != "symbol":
            continue
        ref = _ref_of_symbol(child) or ""
        if ref.startswith(prefix):
            tail = ref[len(prefix):]
            if tail.isdigit():
                max_n = max(max_n, int(tail))
    return f"{prefix}{max_n + 1}"


# ---------------------------------------------------------------------------
# Phase 13 — rename_net (find/replace all labels matching old name)
# ---------------------------------------------------------------------------

def _load_rename_net_cfg() -> dict:
    """Read rename_net section from layout_config.json. Returns defaults
    when the section is missing so the verb keeps working on installs
    that haven't migrated their config yet."""
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("rename_net", {}) or {}
    except Exception:
        return {}


def _op_rename_net(root: list, old_name: str, new_name: str) -> Tuple[bool, str]:
    """Rename a net by rewriting:
      - every label / global_label / hierarchical_label carrying the
        old name (signal nets) — node-head list from config
        `rename_net.label_node_heads`
      - every (symbol (lib_id "<PREFIX><OLD>") ...) power-port instance
        + its `Value` property (power rails like GND, +3V3, +5V) — the
        lib_id prefix comes from config `rename_net.power_lib_prefix`,
        default `power:`. Projects using a custom power library like
        `Project_Power:` just edit that string.

    Per `feedback_no_hardcode_json_config`: no library prefix or label
    head name is hardcoded in this function. Edit the JSON config to
    extend coverage; never edit this file."""
    if not old_name.strip() or not new_name.strip():
        return False, "old and new net names both required"
    if old_name == new_name:
        return False, "old and new names are identical"
    cfg = _load_rename_net_cfg()
    power_prefix = str(cfg.get("power_lib_prefix", "power:"))
    label_heads = list(cfg.get("label_node_heads",
                                 ["label", "global_label", "hierarchical_label"]))
    label_head_set = set(label_heads)

    old_s = old_name.strip()
    old_lib = f"{power_prefix}{old_s}"
    new_lib = f"{power_prefix}{new_name}"

    count = 0
    pwr_count = 0
    for child in root[1:] if isinstance(root, list) else []:
        if not isinstance(child, list):
            continue
        h = _head(child)
        if h in label_head_set:
            if len(child) >= 2 and str(child[1]).strip() == old_s:
                child[1] = new_name
                count += 1
            continue
        if h == "symbol":
            saw_match = False
            for sub in child[1:]:
                if isinstance(sub, list) and _head(sub) == "lib_id":
                    if len(sub) >= 2 and str(sub[1]) == old_lib:
                        sub[1] = new_lib
                        saw_match = True
            if not saw_match:
                continue
            for sub in child[1:]:
                if (isinstance(sub, list) and _head(sub) == "property"
                        and len(sub) >= 3 and str(sub[1]) == "Value"
                        and str(sub[2]).strip() == old_s):
                    sub[2] = new_name
            pwr_count += 1
    for child in root[1:] if isinstance(root, list) else []:
        if isinstance(child, list) and _head(child) == "lib_symbols":
            for sub in child[1:]:
                if (isinstance(sub, list) and _head(sub) == "symbol"
                        and len(sub) >= 2 and str(sub[1]) == old_lib):
                    sub[1] = new_lib
    total = count + pwr_count
    if total == 0:
        return False, f"no labels or power ports found with name {old_name!r}"
    return True, (f"renamed net {old_name!r} -> {new_name!r} "
                   f"({count} labels + {pwr_count} power ports)")


# ---------------------------------------------------------------------------
# Phase 7-ext — add_wire_by_pin (resolve "R1.2" -> pin abs coords)
# ---------------------------------------------------------------------------

def _resolve_pin_position(root: list, pin_ref: str) -> Optional[Tuple[float, float]]:
    """Given a pin ref like 'R1.2' or 'U1.VDD', return its absolute (x, y)
    on the sheet. Uses the symbol's lib_id + (at ...) to compute via the
    engine's symbol_geom helpers."""
    if "." not in pin_ref:
        return None
    comp_ref, pin_key = pin_ref.split(".", 1)
    sym = _find_symbol(root, comp_ref)
    if sym is None:
        return None
    # Get lib_id + position
    lib_id = ""
    for child in sym[1:]:
        if isinstance(child, list) and _head(child) == "lib_id" and len(child) >= 2:
            lib_id = str(child[1])
            break
    if not lib_id:
        return None
    at = _at_of_symbol(sym)
    if at is None or len(at) < 3:
        return None
    try:
        cx = float(at[1]); cy = float(at[2])
        rot = float(at[3]) if len(at) >= 4 else 0.0
    except (TypeError, ValueError):
        return None
    try:
        from ..kicad.symbol_geom import load_symbol, place_pin
        geom = load_symbol(lib_id)
        pin = geom.resolve_pin(pin_key)
        if pin is None:
            return None
        abs_pos = place_pin(pin, cx, cy, rot)
        return (abs_pos[0], abs_pos[1])
    except Exception:
        return None


def _analyze_wire_route(ax: float, ay: float, bx: float, by: float,
                          root: list) -> Dict[str, Any]:
    """Pre-commit analysis: for a wire from (ax,ay)→(bx,by), report which
    route type works (straight / L1 / L2 / Z-detour) given the current
    schematic's component bodies. Output is structured so the agent /
    user can see the choice BEFORE the wire is added.

    Returns a dict with keys:
      route_type:  one of 'straight', 'L1', 'L2', 'Z', 'impossible'
      path:        list of (x, y) tuples (empty when impossible)
      bend_points: subset of path (intermediate corners)
      blocking:    list of refdes that block the rejected variants
                    (empty when a clean variant exists)
      reason:      short English explanation of the choice
    """
    bboxes_with_ref = _symbol_bboxes_from_root(
        root, exclude_endpoints=[(ax, ay), (bx, by)])
    obstacles = [(b[0], b[1], b[2], b[3]) for b in bboxes_with_ref]
    blockers_by_ref = {b[4]: (b[0], b[1], b[2], b[3])
                       for b in bboxes_with_ref}
    existing_wires = _existing_wire_segments(root)

    def _seg_pierces(p1, p2):
        hit = []
        for ref, bb in blockers_by_ref.items():
            if _segment_pierces_bbox(p1[0], p1[1], p2[0], p2[1], bb):
                hit.append(ref)
        return hit

    def _seg_overlaps_wire(p1, p2) -> bool:
        return _segment_overlaps_existing(p1[0], p1[1], p2[0], p2[1],
                                            existing_wires)

    def _path_overlaps_wire(path) -> bool:
        for i in range(len(path) - 1):
            if _seg_overlaps_wire(path[i], path[i + 1]):
                return True
        return False

    # 1. Straight (shared axis)
    if abs(ax - bx) < 0.01 or abs(ay - by) < 0.01:
        hit = _seg_pierces((ax, ay), (bx, by))
        if not hit:
            return {"route_type": "straight",
                    "path": [(ax, ay), (bx, by)],
                    "bend_points": [],
                    "blocking": [],
                    "reason": "endpoints share an axis, no obstacle"}
    # 2. L1 (H then V) — bend at (bx, ay)
    l1_bend = (bx, ay)
    l1_hit = _seg_pierces((ax, ay), l1_bend) + _seg_pierces(l1_bend, (bx, by))
    # 3. L2 (V then H) — bend at (ax, by)
    l2_bend = (ax, by)
    l2_hit = _seg_pierces((ax, ay), l2_bend) + _seg_pierces(l2_bend, (bx, by))

    # Pick a clean variant — must clear BOTH component bbox AND existing
    # wires. If both Ls are bbox-clean, prefer the one without wire
    # overlap. If only one is bbox-clean but it overlaps an existing
    # wire, fall through to Z-detour rather than emit a parallel wire.
    l1_path = [(ax, ay), l1_bend, (bx, by)]
    l2_path = [(ax, ay), l2_bend, (bx, by)]
    l1_wire_clash = _path_overlaps_wire(l1_path)
    l2_wire_clash = _path_overlaps_wire(l2_path)
    if not l1_hit and not l2_hit:
        # Both bbox-clean. Pick the one without wire overlap.
        if not l1_wire_clash:
            return {"route_type": "L1",
                    "path": l1_path,
                    "bend_points": [l1_bend],
                    "blocking": [],
                    "reason": "L1 (H→V) clean of bodies and existing wires"}
        if not l2_wire_clash:
            return {"route_type": "L2",
                    "path": l2_path,
                    "bend_points": [l2_bend],
                    "blocking": [],
                    "reason": ("L1 would overlap an existing wire; "
                                "L2 (V→H) clean")}
        # Both Ls overlap a wire — drop through to Z-detour
    elif not l1_hit and not l1_wire_clash:
        return {"route_type": "L1",
                "path": l1_path,
                "bend_points": [l1_bend],
                "blocking": list(set(l2_hit)),
                "reason": (f"L1 clean; L2 would pierce "
                            f"{', '.join(sorted(set(l2_hit)))}")}
    elif not l2_hit and not l2_wire_clash:
        return {"route_type": "L2",
                "path": l2_path,
                "bend_points": [l2_bend],
                "blocking": list(set(l1_hit)),
                "reason": (f"L2 clean; L1 would pierce "
                            f"{', '.join(sorted(set(l1_hit)))}")}
    # 4. Z-detour — try going AROUND the first blocker. For each direction,
    # snap the detour to grid + add 2.54 mm clearance.
    GRID = 1.27
    def _snap(v): return round(v / GRID) * GRID
    clearance = 2.54
    # Use the union bbox of all blockers as the detour zone
    blk_x1 = min(b[0] for b in blockers_by_ref.values())
    blk_y1 = min(b[1] for b in blockers_by_ref.values())
    blk_x2 = max(b[2] for b in blockers_by_ref.values())
    blk_y2 = max(b[3] for b in blockers_by_ref.values())
    candidates = []
    # Horizontal detour: go ABOVE or BELOW the blocker
    for dy in (_snap(blk_y1 - clearance), _snap(blk_y2 + clearance)):
        path = [(ax, ay), (ax, dy), (bx, dy), (bx, by)]
        hit = (_seg_pierces(path[0], path[1])
               + _seg_pierces(path[1], path[2])
               + _seg_pierces(path[2], path[3]))
        if not hit:
            candidates.append((abs(dy - ay) + abs(dy - by), "Z (top/bottom)",
                                 path))
    # Vertical detour: go LEFT or RIGHT of the blocker
    for dx in (_snap(blk_x1 - clearance), _snap(blk_x2 + clearance)):
        path = [(ax, ay), (dx, ay), (dx, by), (bx, by)]
        hit = (_seg_pierces(path[0], path[1])
               + _seg_pierces(path[1], path[2])
               + _seg_pierces(path[2], path[3]))
        if not hit:
            candidates.append((abs(dx - ax) + abs(dx - bx), "Z (left/right)",
                                 path))
    if candidates:
        candidates.sort()  # shortest detour wins
        _, kind, path = candidates[0]
        return {"route_type": "Z",
                "path": path,
                "bend_points": path[1:-1],
                "blocking": list(set(l1_hit) | set(l2_hit)),
                "reason": (f"both L variants pierce "
                            f"{', '.join(sorted(set(l1_hit) | set(l2_hit)))}; "
                            f"detour via {kind}")}
    # 5. Impossible — wire would always pierce something
    return {"route_type": "impossible",
            "path": [],
            "bend_points": [],
            "blocking": list(set(l1_hit) | set(l2_hit)),
            "reason": (f"no clean path — every L and Z variant pierces "
                        f"{', '.join(sorted(set(l1_hit) | set(l2_hit)))}. "
                        f"Move a component or accept a crossing manually.")}


def _op_add_wire_by_pin(root: list, from_pin: str, to_pin: str) -> Tuple[bool, str]:
    """High-level wire add: take pin references like 'R1.2' and 'C3.1',
    look up their absolute coordinates, analyze the layout, pick the
    best route (straight / L1 / L2 / Z-detour), and emit it as one
    or more `add_wire` segments.

    Pre-commit analysis runs `_analyze_wire_route` so the chat reply
    can describe WHICH route type was chosen and WHY (e.g. 'L2 chosen
    because L1 would pierce U1'). User's 2026-05-27 ask: "ERC run
    panni visualy analysis panna idea kedaikum wire connection eppadi
    kudukanum L or Z or cross type — first analysis"."""
    a = _resolve_pin_position(root, from_pin)
    if a is None:
        return False, f"could not resolve pin {from_pin!r}"
    b = _resolve_pin_position(root, to_pin)
    if b is None:
        return False, f"could not resolve pin {to_pin!r}"
    ax, ay = a
    bx, by = b
    analysis = _analyze_wire_route(ax, ay, bx, by, root)
    rtype = analysis["route_type"]
    if rtype == "impossible":
        return False, (f"add_wire {from_pin}->{to_pin}: {analysis['reason']}")
    # Drop consecutive duplicate points — the route analyzer can emit an
    # L/Z path whose corner coincides with an endpoint, which would make one
    # leg zero-length. Dedup on the SAME 1.27 mm grid _op_add_wire snaps to
    # (points in one grid cell are electrically identical), so this turns a
    # would-be zero-length leg into a clean straight wire instead of aborting.
    def _gsnap(v: float) -> float:
        return round(v / 1.27) * 1.27
    path = []
    for p in analysis["path"]:
        gp = (_gsnap(p[0]), _gsnap(p[1]))
        if not path or gp != path[-1]:
            path.append(gp)
    if len(path) < 2:
        return False, (f"add_wire {from_pin}->{to_pin}: source and target "
                        f"resolve to the same point")
    for i in range(len(path) - 1):
        ok, msg = _op_add_wire(root, path[i][0], path[i][1],
                                  path[i + 1][0], path[i + 1][1])
        if not ok:
            return False, f"segment {i+1} failed: {msg}"
    bend_str = ""
    if analysis["bend_points"]:
        bend_str = " via " + ", ".join(
            f"({bp[0]:.2f},{bp[1]:.2f})" for bp in analysis["bend_points"])
    return True, (f"{rtype}-routed wire {from_pin}->{to_pin}{bend_str} "
                   f"({analysis['reason']})")


# ---------------------------------------------------------------------------
# Phase 12 — add_decoupling (caps on every VDD-like pin of an IC)
# ---------------------------------------------------------------------------

def _load_decoupling_defaults() -> dict:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("decoupling_auto_add", {})
    except Exception:
        return {}


def _op_add_decoupling(root: list, ic_ref: str, hf_value: str,
                        bulk_value: str, include_bulk: bool) -> Tuple[bool, str]:
    """For target IC `ic_ref`, find every VDD/VCC-like pin and place a
    HF decoupling cap (default 100n) plus optional bulk cap (default
    10u) adjacent to each. Pin name patterns + values are config-driven
    via layout_config.json:decoupling_auto_add — never hardcoded."""
    cfg = _load_decoupling_defaults()
    if not hf_value:
        hf_value = str(cfg.get("hf_cap_value", "100n"))
    if not bulk_value:
        bulk_value = str(cfg.get("bulk_cap_value", "10u"))
    hf_lib = str(cfg.get("hf_cap_lib_id", "Device:C"))
    bulk_lib = str(cfg.get("bulk_cap_lib_id", "Device:C_Polarized"))
    patterns = [p.upper() for p in cfg.get("vdd_pin_name_patterns", [
        "VCC", "VDD", "AVDD", "DVDD", "VBAT", "VDDA", "VDDIO", "VSYS",
    ])]

    sym = _find_symbol(root, ic_ref)
    if sym is None:
        return False, f"no symbol with Reference={ic_ref!r}"
    lib_id = ""
    for child in sym[1:]:
        if isinstance(child, list) and _head(child) == "lib_id" and len(child) >= 2:
            lib_id = str(child[1])
            break
    if not lib_id:
        return False, f"{ic_ref} has no lib_id"
    at = _at_of_symbol(sym)
    if at is None or len(at) < 3:
        return False, f"{ic_ref} has no (at ...) clause"
    try:
        cx = float(at[1]); cy = float(at[2])
        rot = float(at[3]) if len(at) >= 4 else 0.0
    except (TypeError, ValueError):
        return False, f"{ic_ref} (at ...) coords not numeric"

    try:
        from ..kicad.symbol_geom import load_symbol, place_pin
        geom = load_symbol(lib_id)
    except Exception as exc:
        return False, f"could not load symbol {lib_id!r}: {exc}"

    vdd_pins: List[Tuple[str, Tuple[float, float, float]]] = []
    for pin in geom.pins:
        pname = (pin.name or "").upper()
        if any(p in pname for p in patterns):
            abs_pos = place_pin(pin, cx, cy, rot)
            vdd_pins.append((pin.name, abs_pos))

    if not vdd_pins:
        return False, f"no VDD-like pins found on {ic_ref} (patterns: {patterns})"

    added: List[str] = []
    for pin_name, pin_abs in vdd_pins:
        hf_ref = _next_refdes(root, "C")
        # Place HF cap 6mm to the right of the VDD pin tip
        x = round((pin_abs[0] + 6.35) / 1.27) * 1.27
        y = round(pin_abs[1] / 1.27) * 1.27
        ok, _ = _op_add_component(root, hf_ref, hf_lib, hf_value, x, y, rotation=0.0)
        if ok:
            added.append(f"{hf_ref}({hf_value}) on {ic_ref}.{pin_name}")
        if include_bulk:
            bulk_ref = _next_refdes(root, "C")
            x2 = round((pin_abs[0] + 12.7) / 1.27) * 1.27
            ok2, _ = _op_add_component(root, bulk_ref, bulk_lib, bulk_value,
                                          x2, y, rotation=0.0)
            if ok2:
                added.append(f"{bulk_ref}({bulk_value})")
    return True, f"added {len(added)} decoupling cap(s) for {ic_ref}: {', '.join(added)}"


def _load_pullup_defaults() -> dict:
    """All defaults for pullup / pulldown verbs are CONFIG-DRIVEN — never
    hardcoded in code. Per user rule `feedback_dynamic_universal_quality`:
    no per-circuit hardcoding, no part-number specialisation. Edit
    layout_config.json:pullup_pulldown_defaults to tune for any project
    (different supply rail, different default value, different lib_id)
    without touching code."""
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("pullup_pulldown_defaults", {})
    except Exception:
        return {}


def _op_add_pullup(root: list, net: str, value: str,
                    to_rail: str) -> Tuple[bool, str]:
    """Add a resistor pulling `net` up to `to_rail`. Defaults pulled from
    layout_config.json:pullup_pulldown_defaults — change them there to
    re-tune for any project; never hardcode in this file."""
    cfg = _load_pullup_defaults()
    if not net.strip():
        return False, "net name required"
    if not value.strip():
        value = str(cfg.get("default_pullup_value", "10k"))
    if not to_rail.strip():
        to_rail = str(cfg.get("default_pullup_rail", "+3V3"))
    lib_id = str(cfg.get("default_resistor_lib_id", "Device:R"))
    off_x = float(cfg.get("placement_offset_x_mm", 12.7))

    ref = _next_refdes(root, "R")
    pos = _find_label_position(root, net)
    if pos is None:
        # No label match — drop near sheet centre. Sheet centre is the
        # engine's anchor convention, not hardcoded per-circuit.
        from ..intent.engine import SHEET_CENTRE as _CENTRE
        x, y = _CENTRE[0] + off_x * 2, _CENTRE[1]
    else:
        x, y = pos[0] + off_x, pos[1]
    ok, note = _op_add_component(root, ref, lib_id, value,
                                   x, y, rotation=90.0)
    if not ok:
        return ok, note
    return True, (f"added pullup {ref} ({value}) on net {net!r} "
                   f"toward {to_rail} at ({x:.2f}, {y:.2f}) — "
                   f"wire to existing {net} label manually for now")


def _op_add_pulldown(root: list, net: str, value: str,
                      to_rail: str) -> Tuple[bool, str]:
    """Mirror of add_pullup but for ground-side pull. All defaults
    config-driven, no hardcoding."""
    cfg = _load_pullup_defaults()
    if not net.strip():
        return False, "net name required"
    if not value.strip():
        value = str(cfg.get("default_pulldown_value", "10k"))
    if not to_rail.strip():
        to_rail = str(cfg.get("default_pulldown_rail", "GND"))
    lib_id = str(cfg.get("default_resistor_lib_id", "Device:R"))
    off_x = float(cfg.get("placement_offset_x_mm", 12.7))
    off_y = float(cfg.get("placement_offset_y_mm", 12.7))

    ref = _next_refdes(root, "R")
    pos = _find_label_position(root, net)
    if pos is None:
        from ..intent.engine import SHEET_CENTRE as _CENTRE
        x, y = _CENTRE[0] + off_x * 2, _CENTRE[1]
    else:
        x, y = pos[0] + off_x, pos[1] + off_y
    ok, note = _op_add_component(root, ref, lib_id, value,
                                   x, y, rotation=90.0)
    if not ok:
        return ok, note
    return True, (f"added pulldown {ref} ({value}) on net {net!r} "
                   f"toward {to_rail} at ({x:.2f}, {y:.2f}) — "
                   f"wire to existing {net} label manually for now")


# ---------------------------------------------------------------------------
# Phase 14 — add_led (indicator LED: series resistor + LED + GND, fully wired)
# ---------------------------------------------------------------------------

def _load_led_defaults() -> dict:
    """Defaults for the add_led verb. Config-driven via
    layout_config.json:led_indicator_defaults — never hardcode per project."""
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("led_indicator_defaults", {}) or {}
    except Exception:
        return {}


def _apply_ops_defaults() -> dict:
    """Fallback values for the dispatch when an op omits a field. Read from
    layout_config.json:apply_ops_defaults so nothing is hardcoded in this
    file — user-supplied op fields always take precedence over these."""
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("apply_ops_defaults", {}) or {}
    except Exception:
        return {}


def _hierarchy_edit_enabled() -> bool:
    """Gate for cross-sheet edit routing. layout_config.json:
    hierarchy_edit.enabled (default True). Defaults ON because the
    routing is a pure FALLBACK — it only fires when a ref isn't found on
    the primary file, which never happens for a flat / single-sheet
    design, so a non-hierarchical project is byte-for-byte unaffected."""
    try:
        from ..intent.engine import _load_layout_config
        cfg = _load_layout_config().get("hierarchy_edit", {}) or {}
        return bool(cfg.get("enabled", True))
    except Exception:
        return True


def _resolve_source_pin(root: list, source: str
                         ) -> Tuple[Optional[Tuple[float, float]], Optional[str]]:
    """Resolve an add_led `source` spec to (abs_pos, pin_ref).

    `source` may be:
      - a qualified pin ref 'U1.PA1' -> resolved directly
      - a bare pin name 'PA1'        -> scan every symbol for a pin whose
                                         name matches; return its comp.pin ref
      - a net label 'LED_STAT'       -> fall back to the label position
                                         (pin_ref is None — caller wires the
                                         GPIO end from coords / manually)
    Returns (None, None) when nothing matches."""
    src = (source or "").strip()
    if not src:
        return None, None
    # Qualified ref — let _resolve_pin_position do the geometry.
    if "." in src:
        pos = _resolve_pin_position(root, src)
        return pos, (src if pos else None)
    # Bare pin name — search every symbol's pin list.
    try:
        from ..kicad.symbol_geom import load_symbol, place_pin
    except Exception:
        load_symbol = None
    if load_symbol is not None:
        for child in root[1:]:
            if not isinstance(child, list) or _head(child) != "symbol":
                continue
            comp_ref = _ref_of_symbol(child) or ""
            if not comp_ref:
                continue
            lib_id = ""
            for sub in child[1:]:
                if (isinstance(sub, list) and _head(sub) == "lib_id"
                        and len(sub) >= 2):
                    lib_id = str(sub[1]); break
            if not lib_id:
                continue
            at = _at_of_symbol(child)
            if at is None or len(at) < 3:
                continue
            try:
                cx = float(at[1]); cy = float(at[2])
                rot = float(at[3]) if len(at) >= 4 else 0.0
            except (TypeError, ValueError):
                continue
            try:
                pin = load_symbol(lib_id).pin_by_name(src)
            except Exception:
                pin = None
            if pin is not None:
                abs_pos = place_pin(pin, cx, cy, rot)
                return (abs_pos[0], abs_pos[1]), f"{comp_ref}.{src}"
    # Net-label fallback (no pin ref to route from).
    return _find_label_position(root, src), None


def _op_add_led(root: list, source: str, value: str, color: str,
                 to_rail: str) -> Tuple[bool, str]:
    """Add an indicator LED driven from `source` (a GPIO pin name like
    'PA1', a qualified ref 'U1.PA1', or a net label): places a series
    resistor + LED + ground port and wires
        source -> R.1 , R.2 -> LED.A(2) , LED.K(1) -> rail.

    This is what 'add an LED from PA1' should produce — a single bare
    floating symbol (plain add_component) is NOT an indicator LED. All
    defaults config-driven via layout_config.json:led_indicator_defaults."""
    cfg = _load_led_defaults()
    if not value.strip():
        value = str(cfg.get("default_resistor_value", "330"))
    if not color.strip():
        color = str(cfg.get("default_color", "green"))
    if not to_rail.strip():
        to_rail = str(cfg.get("default_rail", "GND"))
    r_lib   = str(cfg.get("resistor_lib_id", "Device:R"))
    led_lib = str(cfg.get("led_lib_id", "Device:LED"))
    gnd_lib = str(cfg.get("gnd_lib_id", "power:GND"))
    off     = float(cfg.get("spacing_mm", 10.16))

    src_pos, src_ref = _resolve_source_pin(root, source)
    if src_pos is None:
        return False, (f"add_led: could not locate source {source!r} — give "
                        f"a pin name (PA1), a qualified ref (U1.PA1), or an "
                        f"existing net label")
    sx, sy = src_pos

    def _snap(v: float) -> float:
        return round(v / 1.27) * 1.27

    # Outward direction — lay the chain along the axis pointing AWAY from the
    # source component's body so nothing lands on top of the MCU. Derived
    # from the pin tip vs. the owning symbol's centre; defaults to +x when
    # the source is a bare net label (no owning symbol to measure against).
    ux, uy = 1.0, 0.0
    if src_ref:
        owner = _find_symbol(root, src_ref.split(".", 1)[0])
        oat = _at_of_symbol(owner) if owner is not None else None
        if oat is not None and len(oat) >= 3:
            try:
                cx, cy = float(oat[1]), float(oat[2])
                dxv, dyv = sx - cx, sy - cy
                if abs(dxv) >= abs(dyv):
                    ux, uy = (1.0 if dxv >= 0 else -1.0), 0.0
                else:
                    ux, uy = 0.0, (1.0 if dyv >= 0 else -1.0)
            except (TypeError, ValueError):
                pass

    horizontal = (abs(uy) < abs(ux)) or uy == 0.0

    # Symbol rotations are derived from the chain axis so every pin lands ON
    # the chain line — that keeps all three wires as short, disjoint, straight
    # colinear hops (no L-routes, no overlaps). A resistor's pins are vertical
    # at 0°, an LED's are horizontal at 0°, so they take opposite rotations
    # for the same axis. The LED's exact rotation (which way the anode faces)
    # is chosen below from the geometry, not hardcoded.
    r_rot = 90.0 if horizontal else 0.0

    r_ref = _next_refdes(root, "R")
    d_ref = _next_refdes(root, "D")
    g_ref = _next_refdes(root, "#PWR")           # power ports use #PWR refs
    rx, ry = _snap(sx + ux * off),     _snap(sy + uy * off)
    dx, dy = _snap(sx + ux * off * 2), _snap(sy + uy * off * 2)
    gx, gy = _snap(sx + ux * off * 3), _snap(sy + uy * off * 3)

    # Pick the LED rotation that (a) keeps both pins on the chain axis and
    # (b) points the ANODE inward (toward the resistor) and the CATHODE
    # outward (toward the rail) — correct current direction without assuming
    # any particular library default. Falls back to a fixed angle if the LED
    # symbol can't be introspected.
    led_rot = 0.0 if horizontal else 90.0
    try:
        from ..kicad.symbol_geom import load_symbol, place_pin
        lg = load_symbol(led_lib)
        pin_a = lg.pin_by_name("A") or lg.pin_by_number("2")
        pin_k = lg.pin_by_name("K") or lg.pin_by_number("1")
        best = None
        for cand in (0.0, 90.0, 180.0, 270.0):
            a = place_pin(pin_a, dx, dy, cand)
            k = place_pin(pin_k, dx, dy, cand)
            # offsets relative to the LED centre, decomposed onto the chain
            # (along = outward) and perpendicular axes.
            perp_a = (a[0] - dx) * (-uy) + (a[1] - dy) * ux
            perp_k = (k[0] - dx) * (-uy) + (k[1] - dy) * ux
            along_a = (a[0] - dx) * ux + (a[1] - dy) * uy
            along_k = (k[0] - dx) * ux + (k[1] - dy) * uy
            on_axis = abs(perp_a) < 0.5 and abs(perp_k) < 0.5
            anode_inward = along_a < along_k          # anode nearer the source
            score = (on_axis, anode_inward, -abs(perp_a) - abs(perp_k))
            if best is None or score > best[0]:
                best = (score, cand)
        if best is not None:
            led_rot = best[1]
    except Exception:
        pass

    okr, nr = _op_add_component(root, r_ref, r_lib, value, rx, ry, rotation=r_rot)
    if not okr:
        return False, f"add_led: resistor placement failed ({nr})"
    okd, nd = _op_add_component(root, d_ref, led_lib, color, dx, dy, rotation=led_rot)
    if not okd:
        return False, f"add_led: LED placement failed ({nd})"
    # Ground port — Value carries the rail name so rename_net keeps working.
    _op_add_component(root, g_ref, gnd_lib, to_rail, gx, gy, rotation=0.0)

    notes = [f"placed {r_ref}({value}) + {d_ref}(LED {color}) + {to_rail} port"]

    def _d2(a, b) -> float:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    def _link(a_ref: str, b_ref: str, a_pos=None) -> None:
        """Connect two pins. Try a single straight colinear wire first
        (the common case here); fall back to the auto-router when the two
        pins aren't axis-aligned."""
        pa = a_pos if a_pos is not None else _resolve_pin_position(root, a_ref)
        pb = _resolve_pin_position(root, b_ref)
        if pa and pb and _d2(pa, pb) <= 1e-4:
            return                                   # already coincident
        if pa and pb:
            ok, _msg = _op_add_wire(root, pa[0], pa[1], pb[0], pb[1])
            if ok:
                notes.append(f" wired {a_ref}->{b_ref} [straight]")
                return
        ok, msg = _op_add_wire_by_pin(root, a_ref, b_ref)
        notes.append((" wired " if ok else " WIRE-FAILED ") + f"{a_ref}->{b_ref} [{msg}]")

    # Resistor is symmetric — feed it from whichever pin is nearer the source.
    r1 = _resolve_pin_position(root, f"{r_ref}.1")
    r2 = _resolve_pin_position(root, f"{r_ref}.2")
    if r1 and r2:
        r_in, r_out = ("1", "2") if _d2(r1, src_pos) <= _d2(r2, src_pos) else ("2", "1")
    else:
        r_in, r_out = "1", "2"

    if src_ref:
        _link(src_ref, f"{r_ref}.{r_in}")
    else:
        # Net-label source: no pin ref to route from — wire straight from the
        # label coords to the resistor input pin.
        _link(source, f"{r_ref}.{r_in}", a_pos=src_pos)
    _link(f"{r_ref}.{r_out}", f"{d_ref}.2")          # R -> LED anode (pin 2 / A)
    _link(f"{d_ref}.1", f"{g_ref}.1")                # LED cathode (pin 1 / K) -> rail
    return True, "add_led: " + ";".join(notes)


def _op_set_dnp(root: list, ref: str, dnp: bool) -> Tuple[bool, str]:
    """KiCad 7+ DNP flag — `(dnp yes)` child of the symbol. Used to mark
    components as Do Not Populate so they're skipped in BOM / placement
    but stay in the schematic for documentation."""
    target = _find_symbol(root, ref)
    if target is None:
        return False, f"no symbol with Reference={ref!r} found"
    # Remove any existing (dnp ...) child
    target_after = [target[0]]
    for child in target[1:]:
        if isinstance(child, list) and _head(child) == "dnp":
            continue
        target_after.append(child)
    target[:] = target_after
    # Re-insert with desired value
    target.append([sexpdata.Symbol("dnp"),
                   sexpdata.Symbol("yes" if dnp else "no")])
    return True, f"{ref} DNP -> {'yes' if dnp else 'no'}"


# ---------------------------------------------------------------------------
# delete_wire_by_pin — pin-pair endpoints instead of raw coordinates
# ---------------------------------------------------------------------------

def _op_delete_wire_by_pin(root: list, from_pin: str,
                            to_pin: str) -> Tuple[bool, str]:
    """High-level wire delete: take pin refs like 'R5.2' and 'U1.3',
    look up their absolute coords, and remove the matching (wire ...)
    node. Mirror of add_wire_by_pin. Tolerance widened to 2.54 mm so
    L-routed wires (whose segment endpoints may not exactly land on the
    pin tip after a bend) still match."""
    a = _resolve_pin_position(root, from_pin)
    if a is None:
        return False, f"could not resolve pin {from_pin!r}"
    b = _resolve_pin_position(root, to_pin)
    if b is None:
        return False, f"could not resolve pin {to_pin!r}"
    return _op_delete_wire(root, a[0], a[1], b[0], b[1], tol=2.54)


# ---------------------------------------------------------------------------
# reroute_crossing_wires — audit + fix wires that pass through component bodies
# ---------------------------------------------------------------------------

def _seg_intersects_rect(x1: float, y1: float, x2: float, y2: float,
                          rx1: float, ry1: float,
                          rx2: float, ry2: float,
                          margin: float = 0.0) -> bool:
    """True if the line segment (x1,y1)→(x2,y2) intersects the interior
    of the rectangle (rx1,ry1)-(rx2,ry2), expanded by `margin`. Endpoint-
    only contact (segment terminates AT the rectangle edge) is NOT
    counted as a crossing — pin tips legitimately touch component bodies."""
    rx1 -= margin; ry1 -= margin; rx2 += margin; ry2 += margin
    # Quick reject: both endpoints outside same half-plane
    if max(x1, x2) <= rx1 or min(x1, x2) >= rx2:
        return False
    if max(y1, y2) <= ry1 or min(y1, y2) >= ry2:
        return False
    # Endpoint-on-edge is the legal pin-tip case
    def _on_edge(x, y):
        on_x = (abs(x - rx1) < 0.5 or abs(x - rx2) < 0.5) and ry1 - 0.5 <= y <= ry2 + 0.5
        on_y = (abs(y - ry1) < 0.5 or abs(y - ry2) < 0.5) and rx1 - 0.5 <= x <= rx2 + 0.5
        return on_x or on_y
    if _on_edge(x1, y1) and _on_edge(x2, y2):
        return False
    # If either endpoint is strictly INSIDE the rectangle interior, it's
    # a crossing (axis-aligned wire piercing the body).
    eps = 1.0
    inside1 = (rx1 + eps < x1 < rx2 - eps) and (ry1 + eps < y1 < ry2 - eps)
    inside2 = (rx1 + eps < x2 < rx2 - eps) and (ry1 + eps < y2 < ry2 - eps)
    if inside1 or inside2:
        return True
    # Axis-aligned segments only (KiCad wires are orthogonal). Check
    # whether the segment line passes through the rect interior.
    if abs(x1 - x2) < 0.01:  # vertical
        x = x1
        if rx1 + eps < x < rx2 - eps:
            ymin, ymax = sorted((y1, y2))
            if ymin < ry2 - eps and ymax > ry1 + eps:
                return True
    elif abs(y1 - y2) < 0.01:  # horizontal
        y = y1
        if ry1 + eps < y < ry2 - eps:
            xmin, xmax = sorted((x1, x2))
            if xmin < rx2 - eps and xmax > rx1 + eps:
                return True
    return False


def _collect_component_bboxes(root: list) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    """Walk all (symbol ...) instances and return [(ref, (x1,y1,x2,y2)), ...]
    in absolute schematic coords using the engine's symbol_geom loader.
    Power-port symbols (#PWR refs) are skipped — their bbox is a tiny
    triangle that wires must touch anyway."""
    try:
        from ..kicad.symbol_geom import load_symbol
    except Exception:
        return []
    import math
    out: List[Tuple[str, Tuple[float, float, float, float]]] = []
    for child in root[1:]:
        if not isinstance(child, list) or _head(child) != "symbol":
            continue
        ref = _ref_of_symbol(child) or ""
        if ref.startswith("#") or not ref:
            continue
        lib_id = ""
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "lib_id" and len(sub) >= 2:
                lib_id = str(sub[1])
                break
        at = _at_of_symbol(child)
        if not lib_id or at is None or len(at) < 3:
            continue
        try:
            cx = float(at[1]); cy = float(at[2])
            rot = float(at[3]) if len(at) >= 4 else 0.0
            geom = load_symbol(lib_id)
        except Exception:
            continue
        x1, y1, x2, y2 = geom.outer_bbox  # local Y-up
        corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
        corners = [(x, -y) for x, y in corners]
        rad = math.radians(rot)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r) for x, y in corners]
        corners = [(cx + x, cy + y) for x, y in corners]
        xs = [p[0] for p in corners]; ys = [p[1] for p in corners]
        out.append((ref, (min(xs), min(ys), max(xs), max(ys))))
    return out


def _op_reroute_crossing_wires(root: list,
                                target_ref: str = "") -> Tuple[bool, str]:
    """Audit pass. Find every (wire ...) segment that pierces a component
    body and replace it with an L-route that detours around all body
    bboxes. If `target_ref` is set, only inspect wires intersecting that
    one component's bbox (per-component triage); empty string means
    sweep the whole sheet.

    Returns (ok, summary) where summary lists checked / fixed counts.
    Wires whose new L-route would still cross some body are dropped
    silently — a missing wire is recoverable; a wire through a body is
    visually broken."""
    bboxes = _collect_component_bboxes(root)
    if target_ref:
        bboxes = [(r, b) for r, b in bboxes if r == target_ref]
        if not bboxes:
            return False, f"no component bbox for {target_ref!r}"

    # Gather all (wire ...) nodes with their endpoints up-front so we can
    # mutate root[] safely during iteration.
    wires_meta: List[Tuple[list, float, float, float, float]] = []
    for child in root[1:]:
        if not isinstance(child, list) or _head(child) != "wire":
            continue
        pts = None
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "pts":
                pts = sub
                break
        if pts is None or len(pts) < 3:
            continue
        try:
            a = pts[1]; b = pts[2]
            ax, ay = float(a[1]), float(a[2])
            bx, by = float(b[1]), float(b[2])
        except (IndexError, TypeError, ValueError):
            continue
        wires_meta.append((child, ax, ay, bx, by))

    checked = len(wires_meta)
    fixed = 0
    dropped = 0
    all_boxes = [b for _, b in bboxes]
    # Also need OTHER bboxes when checking rerouted candidates (not just
    # the target — a reroute around U1 mustn't pierce R5).
    full_bboxes = [b for _, b in _collect_component_bboxes(root)]

    for wire_node, ax, ay, bx, by in wires_meta:
        crosses = any(_seg_intersects_rect(ax, ay, bx, by, *bb, margin=0.0)
                       for bb in all_boxes)
        if not crosses:
            continue
        # Try L-routes — pick the first one that clears EVERY component
        # bbox (not just the target).
        candidates = [
            ((ax, ay), (bx, ay), (bx, by)),  # horizontal-first
            ((ax, ay), (ax, by), (bx, by)),  # vertical-first
        ]
        chosen = None
        for path in candidates:
            ok = True
            for i in range(len(path) - 1):
                p1, p2 = path[i], path[i + 1]
                for bb in full_bboxes:
                    if _seg_intersects_rect(p1[0], p1[1], p2[0], p2[1],
                                              *bb, margin=0.0):
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                chosen = path
                break
        # Remove the original wire either way (it's broken)
        try:
            root.remove(wire_node)
        except ValueError:
            continue
        if chosen is None:
            dropped += 1
            continue
        # Emit the L-route as 2 new wire segments
        for i in range(len(chosen) - 1):
            p1, p2 = chosen[i], chosen[i + 1]
            if abs(p1[0] - p2[0]) < 0.01 and abs(p1[1] - p2[1]) < 0.01:
                continue
            _op_add_wire(root, p1[0], p1[1], p2[0], p2[1])
        fixed += 1

    note = f"checked={checked}, fixed={fixed}, dropped={dropped}"
    if target_ref:
        note += f" (target={target_ref})"
    return True, note


# ---------------------------------------------------------------------------
# Snapshot / undo system
# ---------------------------------------------------------------------------

def _load_snapshot_cfg() -> dict:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("apply_ops_snapshots", {}) or {}
    except Exception:
        return {}


def _snapshot_dir(sch: Path) -> Path:
    """Resolve the per-project snapshot directory. By default
    `<sch_dir>/.envil-snapshots/`; override via JSON config."""
    cfg = _load_snapshot_cfg()
    name = str(cfg.get("dirname", ".envil-snapshots"))
    return sch.parent / name


def _take_snapshot(sch: Path, ops_summary: str) -> Optional[Path]:
    """Write a timestamped copy of `sch` into the per-project snapshot
    folder. Returns the snapshot path (or None when disabled/on error).
    Idempotent across rapid edits — uses millisecond timestamps."""
    cfg = _load_snapshot_cfg()
    if not cfg.get("enabled", True):
        return None
    import time
    snap_dir = _snapshot_dir(sch)
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    ms = int((time.time() % 1) * 1000)
    snap_name = f"{sch.stem}.{ts}-{ms:03d}.envil-bak{sch.suffix}"
    snap_path = snap_dir / snap_name
    try:
        snap_path.write_text(sch.read_text(encoding="utf-8"),
                              encoding="utf-8")
    except OSError:
        return None
    # Write a sidecar describing what's about to happen
    meta_path = snap_path.with_suffix(snap_path.suffix + ".meta.txt")
    try:
        meta_path.write_text(
            f"original: {sch}\n"
            f"taken: {ts}\n"
            f"ops_summary: {ops_summary}\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    # Enforce retention — drop oldest snapshots beyond the cap
    keep = int(cfg.get("max_snapshots", 20))
    try:
        snaps = sorted(snap_dir.glob(f"{sch.stem}.*.envil-bak{sch.suffix}"))
        while len(snaps) > keep:
            old = snaps.pop(0)
            try:
                old.unlink(missing_ok=True)
                meta = old.with_suffix(old.suffix + ".meta.txt")
                if meta.exists():
                    meta.unlink(missing_ok=True)
            except OSError:
                break
    except Exception:
        pass
    return snap_path


def _list_snapshots(sch: Path) -> List[Dict[str, Any]]:
    snap_dir = _snapshot_dir(sch)
    if not snap_dir.exists():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(snap_dir.glob(f"{sch.stem}.*.envil-bak{sch.suffix}"),
                     reverse=True):
        meta = p.with_suffix(p.suffix + ".meta.txt")
        summary = ""
        taken = ""
        if meta.exists():
            try:
                for line in meta.read_text(encoding="utf-8").splitlines():
                    if line.startswith("ops_summary:"):
                        summary = line.split(":", 1)[1].strip()
                    elif line.startswith("taken:"):
                        taken = line.split(":", 1)[1].strip()
            except OSError:
                pass
        out.append({
            "path": str(p),
            "name": p.name,
            "size_bytes": p.stat().st_size if p.exists() else 0,
            "taken": taken,
            "ops_summary": summary,
        })
    return out


def _restore_snapshot(sch: Path,
                       which: Optional[str] = None) -> Tuple[bool, str]:
    """Restore `sch` from a snapshot. `which` is the snapshot filename
    (basename) — defaults to the MOST RECENT snapshot. Takes a fresh
    snapshot of the current state before overwriting (so 'undo' is itself
    undoable)."""
    snaps = _list_snapshots(sch)
    if not snaps:
        return False, "no snapshots available"
    target = None
    if which:
        for s in snaps:
            if s["name"] == which or s["path"] == which:
                target = s; break
        if target is None:
            return False, f"snapshot not found: {which}"
    else:
        target = snaps[0]
    snap_path = Path(target["path"])
    if not snap_path.exists():
        return False, f"snapshot file missing on disk: {snap_path}"
    try:
        sch.write_text(snap_path.read_text(encoding="utf-8"),
                        encoding="utf-8")
    except OSError as exc:
        return False, f"restore failed: {exc}"
    name = snap_path.name
    # Consume the snapshot so a chain of undo calls walks BACK through
    # history one step at a time. Without this, every undo would restore
    # the same most-recent snapshot indefinitely. Side-car .meta.txt is
    # cleaned up too.
    try:
        snap_path.unlink(missing_ok=True)
        meta = snap_path.with_suffix(snap_path.suffix + ".meta.txt")
        if meta.exists():
            meta.unlink(missing_ok=True)
    except OSError:
        pass
    return True, f"restored from {name}"


# ---------------------------------------------------------------------------
# Cross-sheet undo: a transaction manifest groups the snapshots taken for
# a SINGLE apply_ops call that touched more than the primary file, so
# undo_last_edit can roll back every sheet it changed (not just the root).
# Only written when a child sheet was actually edited — pure single-file
# edits keep the original single-snapshot undo path untouched.
# ---------------------------------------------------------------------------

def _write_txn(primary: Path, entries: List[Dict[str, str]],
                ops_summary: str) -> None:
    """Record {file, snapshot} pairs for a multi-sheet edit. Lives in the
    primary file's snapshot dir; pruned alongside the bak files."""
    import time
    snap_dir = _snapshot_dir(primary)
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    ts = time.strftime("%Y%m%d-%H%M%S") + f"-{int((time.time() % 1) * 1000):03d}"
    p = snap_dir / f"{primary.stem}.{ts}.envil-txn.json"
    try:
        p.write_text(json.dumps({"ops_summary": ops_summary,
                                  "entries": entries}, indent=2),
                      encoding="utf-8")
    except OSError:
        return
    # Light retention — keep the same count as bak snapshots.
    cfg = _load_snapshot_cfg()
    keep = int(cfg.get("max_snapshots", 20))
    try:
        txns = sorted(snap_dir.glob(f"{primary.stem}.*.envil-txn.json"))
        while len(txns) > keep:
            old = txns.pop(0)
            old.unlink(missing_ok=True)
    except OSError:
        pass


def _restore_latest_txn(primary: Path) -> Optional[Tuple[bool, str]]:
    """If the most recent edit was a multi-sheet transaction, restore every
    file it changed from its snapshot and consume the manifest (so chained
    undo walks back one step at a time). Returns None when there's no
    manifest — caller then falls back to the single-file restore."""
    snap_dir = _snapshot_dir(primary)
    if not snap_dir.exists():
        return None
    txns = sorted(snap_dir.glob(f"{primary.stem}.*.envil-txn.json"),
                   reverse=True)
    if not txns:
        return None
    latest = txns[0]
    try:
        data = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return None
    restored: List[str] = []
    for entry in data.get("entries", []) or []:
        fp = Path(entry.get("file", ""))
        snap_name = entry.get("snapshot", "")
        if not str(fp) or not snap_name:
            continue
        snap = _snapshot_dir(fp) / snap_name
        if not snap.exists():
            continue
        try:
            fp.write_text(snap.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            continue
        restored.append(fp.name)
        # Consume the snapshot + sidecar so the next undo steps further back.
        try:
            snap.unlink(missing_ok=True)
            meta = snap.with_suffix(snap.suffix + ".meta.txt")
            if meta.exists():
                meta.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        latest.unlink(missing_ok=True)
    except OSError:
        pass
    if not restored:
        return None
    return True, (f"undid last edit across {len(restored)} sheet(s): "
                   f"{', '.join(restored)}")


# ---------------------------------------------------------------------------
# @tool entrypoint
# ---------------------------------------------------------------------------

@tool(
    name="apply_ops",
    description=(
        "Apply in-place edits to the open .kicad_sch. Covers the daily "
        "designer workflow: add / delete / move / rotate components, "
        "edit values, footprints, properties, DNP flag, AND add / "
        "delete wires. HIERARCHY-AWARE: pass the open (root) path and the "
        "tool resolves which child sheet owns the target ref/net/pin "
        "automatically — you do NOT need to know which sheet a part is "
        "on. For coordinate-only verbs (add_wire / add_component) you may "
        "add an optional \"sheet\":\"<name>\" to target a specific child. "
        "Op shapes:\n"
        '  delete:          {"verb": "delete_component", "ref": "C3"}\n'
        '  move (mm):       {"verb": "move_component",   "ref": "U1", "dx": 10.16, "dy": 0}\n'
        '  rotate (deg):    {"verb": "rotate_component", "ref": "D2", "angle": 90}\n'
        '  change value:    {"verb": "change_value",     "ref": "R1", "value": "4k7"}\n'
        '  change FP:       {"verb": "change_footprint", "ref": "C5", "footprint": "Capacitor_SMD:C_0805_2012Metric"}\n'
        '  set property:    {"verb": "set_property",     "ref": "U1", "key": "Datasheet", "value": "https://..."}\n'
        '                   (use for Tolerance, MPN, Manufacturer, etc.)\n'
        '  DNP marker:      {"verb": "set_dnp",          "ref": "R7", "dnp": true}\n'
        '  add wire:        {"verb": "add_wire",   "x1": 148.59, "y1": 100.0, "x2": 148.59, "y2": 110.0}\n'
        '  delete wire:     {"verb": "delete_wire","x1": 148.59, "y1": 100.0, "x2": 148.59, "y2": 110.0}\n'
        '  no-connect (X):  {"verb": "add_no_connect", "x": 148.59, "y": 100.0}\n'
        '                   (ERC pin_not_connected fix — flags a genuinely-unused pin)\n'
        '  snap to grid:    {"verb": "snap_endpoint", "x": 148.6, "y": 100.1, "tol": 1.0}\n'
        '                   (ERC endpoint_off_grid fix — snaps a wire end onto the 1.27mm grid)\n'
        '  add component:   {"verb": "add_component", "ref": "R10", "lib_id": "Device:R",\n'
        '                    "value": "10k", "x": 170.0, "y": 100.0, "rotation": 90}\n'
        '  add pullup:      {"verb": "add_pullup",   "net": "NRST", "value": "10k", "to_rail": "+3V3"}\n'
        '  add pulldown:    {"verb": "add_pulldown", "net": "SDA",  "value": "4k7", "to_rail": "GND"}\n'
        '  add LED (indic): {"verb": "add_led", "source": "PA1", "value": "330", "color": "red", "to_rail": "GND"}\n'
        '                   (places series R + LED + GND port and WIRES source->R->LED->rail;\n'
        '                    source = pin name PA1, qualified ref U1.PA1, or a net label)\n'
        '  rename net:      {"verb": "rename_net", "old": "VBUS", "new": "USB_5V"}\n'
        '  wire by pin:     {"verb": "add_wire_by_pin", "from": "R1.2", "to": "C3.1"}\n'
        '  delete by pin:   {"verb": "delete_wire_by_pin", "from": "R5.2", "to": "U1.3"}\n'
        '  reroute crosses: {"verb": "reroute_crossing_wires", "target_ref": "U1"}\n'
        '                   (target_ref optional; empty = sweep whole sheet)\n'
        '  decoupling:      {"verb": "add_decoupling", "ic_ref": "U1", "hf_value": "100n", "bulk_value": "10u", "include_bulk": true}\n'
        '  undo:            {"verb": "undo_last_edit"}\n'
        '                   {"verb": "undo_last_edit", "snapshot": "<name>"}\n'
        '  history:         {"verb": "list_snapshots"}\n'
        "All coords in mm. Returns per-op {verb, ref, ok, note}. Batch "
        "multiple ops in one call — they apply in order. For a brand-new "
        "complete circuit, use build_circuit instead."
    ),
    input_schema={"path": str, "ops": list},
)
async def apply_ops(args: dict[str, Any]) -> dict[str, Any]:
    raw_path = args.get("path", "")
    ops = args.get("ops") or []
    path = Path(raw_path).expanduser()
    if not path.exists():
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: file not found: {path}"}],
            "is_error": True,
        }
    if not isinstance(ops, list) or not ops:
        return {
            "content": [{"type": "text",
                          "text": "ERROR: 'ops' must be a non-empty list"}],
            "is_error": True,
        }

    try:
        text = path.read_text(encoding="utf-8")
        root = sexpdata.loads(text)
    except Exception as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: failed to parse {path}: {exc}"}],
            "is_error": True,
        }
    if not isinstance(root, list) or _head(root) != "kicad_sch":
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: {path} is not a kicad_sch file"}],
            "is_error": True,
        }

    # Take a pre-mutation snapshot so the user can undo any apply_ops
    # call. Best-effort — never fails the op chain. Skipped when ALL
    # ops are read-only / undo verbs (no point backing up before an
    # undo — that'd shadow the snapshot we're about to restore from).
    META_VERBS = {"undo_last_edit", "list_snapshots"}
    ops_summary = ", ".join((op.get("verb", "?") + (
        f"({op.get('ref', '')})" if op.get("ref") else ""))
        for op in ops if isinstance(op, dict))[:200]
    if any(isinstance(op, dict) and op.get("verb") not in META_VERBS
            for op in ops):
        snapshot_path = _take_snapshot(path, ops_summary)
    else:
        snapshot_path = None

    # Verb-alias map. The agent's SYSTEM_PROMPT already maps user
    # phrasings to canonical verbs (delete_component, add_component, ...)
    # but the model occasionally emits a shorter natural-language verb
    # ('delete', 'remove', 'add'). Normalising here makes the tool
    # robust to those — users can type 'delete R1' / 'remove R1' /
    # 'drop R1' and the right thing happens regardless of which form
    # the model serializes.
    _VERB_ALIASES = {
        # delete_component family
        "delete": "delete_component", "remove": "delete_component",
        "drop": "delete_component", "kill": "delete_component",
        "rm": "delete_component", "erase": "delete_component",
        "del": "delete_component", "destroy": "delete_component",
        # add_component family
        "add": "add_component", "insert": "add_component",
        "place": "add_component", "put": "add_component",
        "create": "add_component", "new": "add_component",
        # move_component family
        "move": "move_component", "shift": "move_component",
        "nudge": "move_component", "drag": "move_component",
        "translate": "move_component", "offset": "move_component",
        # rotate_component family
        "rotate": "rotate_component", "turn": "rotate_component",
        "spin": "rotate_component", "rot": "rotate_component",
        # change_value family
        "set_value": "change_value", "value": "change_value",
        "val": "change_value", "update_value": "change_value",
        # change_footprint family
        "set_footprint": "change_footprint", "footprint": "change_footprint",
        "fp": "change_footprint", "update_footprint": "change_footprint",
        # set_property family
        "set_prop": "set_property", "prop": "set_property",
        "property": "set_property",
        # set_dnp family
        "dnp": "set_dnp", "mark_dnp": "set_dnp",
        # wires
        "wire": "add_wire", "connect": "add_wire",
        "remove_wire": "delete_wire", "disconnect": "delete_wire",
        # ERC safe-auto verbs
        "no_connect": "add_no_connect", "nc": "add_no_connect",
        "mark_no_connect": "add_no_connect",
        "snap": "snap_endpoint", "snap_to_grid": "snap_endpoint",
        "wire_by_pin": "add_wire_by_pin", "connect_pins": "add_wire_by_pin",
        "disconnect_pins": "delete_wire_by_pin",
        # nets
        "rename": "rename_net", "net_rename": "rename_net",
        # decoupling / pullup / pulldown
        "decouple": "add_decoupling", "decoupling": "add_decoupling",
        "pullup": "add_pullup", "pull_up": "add_pullup",
        "pulldown": "add_pulldown", "pull_down": "add_pulldown",
        # indicator LED (series R + LED + GND, fully wired)
        "led": "add_led", "add_indicator": "add_led", "indicator": "add_led",
        "status_led": "add_led", "blinky": "add_led",
        # reroute
        "reroute": "reroute_crossing_wires",
        "fix_wires": "reroute_crossing_wires",
        # undo / history
        "undo": "undo_last_edit", "revert": "undo_last_edit",
        "rollback": "undo_last_edit",
        "history": "list_snapshots", "snapshots": "list_snapshots",
    }

    def _normalize_verb(v: str) -> str:
        k = (v or "").strip().lower()
        return _VERB_ALIASES.get(k, k)

    # Dispatch fallbacks — config-driven, never hardcoded here. Op fields win.
    _aod = _apply_ops_defaults()

    # --- Hierarchy-aware edit routing ---------------------------------
    # A KiCad hierarchical project keeps its parts on CHILD sheets; the
    # root .kicad_sch is just a wrapper of (sheet ...) stubs. A ref-based
    # edit ("delete R2") therefore can't be found on the root and used to
    # fail with "no symbol with Reference=...". We route each ref-targeted
    # op to the sheet file that actually owns the ref, edit that file, and
    # write it back. This is a strict FALLBACK: when the ref IS on the
    # primary file (every flat / single-sheet design), we use the primary
    # root exactly as before — zero behaviour change off the hierarchy
    # path. Child files are parsed lazily and only when a ref misses.
    _hier_enabled = _hierarchy_edit_enabled()
    primary_root = root
    primary_rp = path.resolve()
    _roots = {primary_rp: primary_root}      # resolved path -> parsed root
    _dirty = set()                            # resolved paths to write back
    _hier_paths_cache = None                  # lazy: sibling/child sheet files

    # --- which sheet does an op target? -------------------------------
    # Each verb declares HOW its target sheet is identified:
    #   _ROUTE_BY_REF    refdes of an existing part        (op["ref"])
    #   _ROUTE_BY_ICREF  the IC a cap-row attaches to       (op["ic_ref"])
    #   _ROUTE_BY_NET    a net's label/pin anchor           (op["net"])
    #   _ROUTE_BY_SOURCE an LED driver: pin / ref / net      (op["source"])
    #   _ROUTE_BY_PINS   both endpoints of a pin-to-pin wire (op["from/to"])
    #   _BROADCAST       spans the whole design (every sheet)
    # An explicit op["sheet"] selector (file stem / name / Sheetname)
    # overrides routing for the coordinate verbs (add_wire / add_component)
    # that carry no ref to locate. Everything is a strict FALLBACK to the
    # primary file, so flat / single-sheet designs are untouched.
    _ROUTE_BY_REF = {"delete_component", "move_component", "rotate_component",
                     "change_value", "change_footprint", "set_property",
                     "set_dnp"}
    _ROUTE_BY_ICREF = {"add_decoupling"}
    _ROUTE_BY_NET = {"add_pullup", "add_pulldown"}
    _ROUTE_BY_SOURCE = {"add_led"}
    _ROUTE_BY_PINS = {"add_wire_by_pin", "delete_wire_by_pin"}

    def _hier_paths():
        nonlocal _hier_paths_cache
        if _hier_paths_cache is None:
            try:
                from ..kicad.project_summary import collect_sheet_paths
                allp = collect_sheet_paths(path)
            except Exception:
                allp = [primary_rp]
            _hier_paths_cache = [p.resolve() for p in allp
                                  if p.resolve() != primary_rp]
        return _hier_paths_cache

    def _root_for(p):
        rp = p.resolve()
        if rp in _roots:
            return _roots[rp]
        try:
            r = sexpdata.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(r, list) or _head(r) != "kicad_sch":
            return None
        _roots[rp] = r
        return r

    def _all_roots():
        """[(path, root), ...] for the primary file + every hierarchy
        sheet that parses. Primary first."""
        out = [(primary_rp, primary_root)]
        for fp in _hier_paths():
            r = _root_for(fp)
            if r is not None:
                out.append((fp, r))
        return out

    _sheet_sel_cache = None

    def _resolve_sheet_selector(sel):
        """Map an explicit op['sheet'] value (file stem, file name, or the
        Sheetname shown in the hierarchy) to a sheet path, or None."""
        nonlocal _sheet_sel_cache
        if _sheet_sel_cache is None:
            m = {}
            for fp in [primary_rp] + _hier_paths():
                m[fp.stem.lower()] = fp
                m[fp.name.lower()] = fp
            try:
                from ..kicad.project_summary import _list_child_sheets
                for fp in [primary_rp] + _hier_paths():
                    for sn, cp in _list_child_sheets(fp):
                        m.setdefault(sn.lower(), cp.resolve())
            except Exception:
                pass
            _sheet_sel_cache = m
        return _sheet_sel_cache.get(str(sel).strip().lower())

    def _sheet_owning_ref(ref_key):
        """First (path, root) whose file contains a symbol with this
        refdes — primary preferred. None when nothing owns it."""
        if _find_symbol(primary_root, ref_key) is not None:
            return primary_rp, primary_root
        for fp in _hier_paths():
            r = _root_for(fp)
            if r is not None and _find_symbol(r, ref_key) is not None:
                return fp, r
        return None

    def _route(verb_name, op_dict):
        """Return (resolved_path, root) the op should operate on. Primary
        file unless the op's target lives on another sheet."""
        if not _hier_enabled:
            return primary_rp, primary_root

        # Explicit sheet override wins for any verb.
        sel = op_dict.get("sheet")
        if sel:
            tp = _resolve_sheet_selector(sel)
            if tp is not None:
                r = _root_for(tp)
                if r is not None:
                    return tp, r

        # Ref / IC-ref / single-ref reroute target.
        rkey = ""
        if verb_name in _ROUTE_BY_REF:
            rkey = str(op_dict.get("ref", "") or "")
        elif verb_name in _ROUTE_BY_ICREF:
            rkey = str(op_dict.get("ic_ref", "") or "")
        elif verb_name == "reroute_crossing_wires":
            rkey = str(op_dict.get("target_ref", "") or "")
        if rkey:
            owner = _sheet_owning_ref(rkey)
            return owner if owner is not None else (primary_rp, primary_root)

        # Net anchor (pullup / pulldown): the sheet holding that net's
        # label or pin. Falls back to primary (centre placement) on miss.
        if verb_name in _ROUTE_BY_NET:
            net = str(op_dict.get("net", "") or "")
            if net:
                for tp, r in _all_roots():
                    if _find_label_position(r, net) is not None:
                        return tp, r
            return primary_rp, primary_root

        # LED source: pin name / qualified ref / net label.
        if verb_name in _ROUTE_BY_SOURCE:
            src = str(op_dict.get("source")
                      or op_dict.get("net") or op_dict.get("pin") or "")
            if src:
                for tp, r in _all_roots():
                    pos, _pref = _resolve_source_pin(r, src)
                    if pos is not None:
                        return tp, r
            return primary_rp, primary_root

        # Pin-to-pin wire: a wire can't cross sheets, so route to the ONE
        # sheet that owns BOTH endpoints. None found -> primary (the op
        # then fails with a clear "could not resolve pin" message).
        if verb_name in _ROUTE_BY_PINS:
            a = str(op_dict.get("from", "") or "")
            b = str(op_dict.get("to", "") or "")
            ca = a.split(".", 1)[0] if a else ""
            cb = b.split(".", 1)[0] if b else ""
            for tp, r in _all_roots():
                a_ok = (not ca) or _find_symbol(r, ca) is not None
                b_ok = (not cb) or _find_symbol(r, cb) is not None
                if a_ok and b_ok and (ca or cb):
                    return tp, r
            return primary_rp, primary_root

        return primary_rp, primary_root

    def _maybe_broadcast(verb_name, op_dict):
        """Verbs that span the WHOLE design (a net rename, or a global
        crossing-wire sweep) apply to every sheet. Returns a result dict
        when handled, else None so the normal single-sheet dispatch runs.
        Only engages on a real hierarchy (>1 sheet)."""
        if not _hier_enabled:
            return None
        is_rename = verb_name == "rename_net"
        is_global_reroute = (verb_name == "reroute_crossing_wires"
                             and not str(op_dict.get("target_ref", "") or ""))
        if not (is_rename or is_global_reroute):
            return None
        roots = _all_roots()
        if len(roots) < 2:
            return None                      # flat: let normal dispatch run
        any_ok = False
        hits = []
        for tp, r in roots:
            try:
                if is_rename:
                    ok, note = _op_rename_net(
                        r, str(op_dict.get("old", "")), str(op_dict.get("new", "")))
                else:
                    ok, note = _op_reroute_crossing_wires(r, "")
            except Exception as exc:        # noqa: BLE001
                ok, note = False, f"{type(exc).__name__}: {exc}"
            if ok:
                any_ok = True
                _dirty.add(tp)
                hits.append(f"{tp.name}: {note}")
        return {
            "verb": verb_name,
            "ref": "",
            "ok": any_ok,
            "note": ("; ".join(hits) if hits
                     else "no sheet contained that net/crossing"),
            "sheets": sorted(p.name for p, _ in roots if p in _dirty),
        }

    results: List[dict] = []
    for op in ops:
        if not isinstance(op, dict):
            results.append({"verb": "?", "ok": False,
                              "note": "op entry not a dict"})
            continue
        verb = _normalize_verb(op.get("verb", ""))
        ref = op.get("ref", "")
        # Design-spanning verbs (rename a net, sweep all crossings) apply
        # to every sheet — handle them before the single-sheet dispatch.
        bc = _maybe_broadcast(verb, op)
        if bc is not None:
            results.append(bc)
            continue
        # Pick the sheet file this op edits, then bind `root` to it so the
        # whole dispatch below operates on the right file unchanged.
        op_rp, root = _route(verb, op)
        try:
            if verb == "delete_component":
                ok, note = _op_delete(root, ref)
            elif verb == "move_component":
                ok, note = _op_move(root, ref,
                                      float(op.get("dx", 0.0)),
                                      float(op.get("dy", 0.0)))
            elif verb == "rotate_component":
                ok, note = _op_rotate(root, ref,
                                        float(op.get("angle",
                                            _aod.get("rotate_angle_deg", 90.0))))
            elif verb == "change_value":
                ok, note = _op_change_value(root, ref, str(op.get("value", "")))
            elif verb == "change_footprint":
                ok, note = _op_change_footprint(
                    root, ref, str(op.get("footprint", "")))
            elif verb == "set_property":
                ok, note = _op_set_property(
                    root, ref, str(op.get("key", "")),
                    str(op.get("value", "")))
            elif verb == "set_dnp":
                ok, note = _op_set_dnp(root, ref,
                    bool(op.get("dnp", _aod.get("set_dnp_default", True))))
            elif verb == "add_wire":
                ok, note = _op_add_wire(root,
                                          float(op.get("x1", 0.0)),
                                          float(op.get("y1", 0.0)),
                                          float(op.get("x2", 0.0)),
                                          float(op.get("y2", 0.0)))
            elif verb == "delete_wire":
                ok, note = _op_delete_wire(root,
                                              float(op.get("x1", 0.0)),
                                              float(op.get("y1", 0.0)),
                                              float(op.get("x2", 0.0)),
                                              float(op.get("y2", 0.0)),
                                              float(op.get("tol",
                                                  _aod.get("delete_wire_tol_mm", 0.5))),
                                              str(op.get("match", "both")))
            elif verb == "add_no_connect":
                ok, note = _op_add_no_connect(root,
                                                float(op.get("x", 0.0)),
                                                float(op.get("y", 0.0)))
            elif verb == "snap_endpoint":
                ok, note = _op_snap_endpoint(root,
                                               float(op.get("x", 0.0)),
                                               float(op.get("y", 0.0)),
                                               float(op.get("tol", 1.0)))
            elif verb == "add_component":
                ok, note = _op_add_component(root, ref,
                                                str(op.get("lib_id", "")),
                                                str(op.get("value", "")),
                                                float(op.get("x",
                                                    _aod.get("add_component_fallback_x_mm", 148.59))),
                                                float(op.get("y",
                                                    _aod.get("add_component_fallback_y_mm", 105.41))),
                                                float(op.get("rotation", 0.0)),
                                                str(op.get("footprint", "")))
            elif verb == "add_pullup":
                # Pass blanks through — _op_add_pullup resolves value/rail from
                # layout_config.json:pullup_pulldown_defaults. Hardcoding them
                # here would shadow that config and make it dead.
                ok, note = _op_add_pullup(root,
                                             str(op.get("net", "")),
                                             str(op.get("value", "")),
                                             str(op.get("to_rail", "")))
            elif verb == "add_pulldown":
                ok, note = _op_add_pulldown(root,
                                               str(op.get("net", "")),
                                               str(op.get("value", "")),
                                               str(op.get("to_rail", "")))
            elif verb == "add_led":
                ok, note = _op_add_led(root,
                                          str(op.get("source", op.get("net", op.get("pin", "")))),
                                          str(op.get("value", "")),
                                          str(op.get("color", op.get("led_color", ""))),
                                          str(op.get("to_rail", op.get("rail", ""))))
            elif verb == "rename_net":
                ok, note = _op_rename_net(root,
                                             str(op.get("old", "")),
                                             str(op.get("new", "")))
            elif verb == "add_wire_by_pin":
                ok, note = _op_add_wire_by_pin(root,
                                                  str(op.get("from", "")),
                                                  str(op.get("to", "")))
            elif verb == "add_decoupling":
                ok, note = _op_add_decoupling(root,
                                                 str(op.get("ic_ref", "")),
                                                 str(op.get("hf_value", "")),
                                                 str(op.get("bulk_value", "")),
                                                 bool(op.get("include_bulk",
                                                     _aod.get("add_decoupling_include_bulk", True))))
            elif verb == "delete_wire_by_pin":
                ok, note = _op_delete_wire_by_pin(root,
                                                     str(op.get("from", "")),
                                                     str(op.get("to", "")))
            elif verb == "reroute_crossing_wires":
                ok, note = _op_reroute_crossing_wires(root,
                                                         str(op.get("target_ref", "")))
            elif verb == "undo_last_edit":
                # Special verb: restores from snapshot. Skips the in-mem
                # mutation pipeline entirely — we restore the file on disk
                # and reload root from the snapshot text. When the last
                # edit spanned multiple sheets (a transaction manifest
                # exists) restore them ALL; otherwise fall back to the
                # single-file snapshot restore. An explicit `snapshot`
                # name always uses the single-file path.
                which = str(op.get("snapshot", "")) or None
                txn_res = None
                if which is None and _hier_enabled:
                    txn_res = _restore_latest_txn(path)
                ok, note = txn_res if txn_res is not None \
                    else _restore_snapshot(path, which)
                if ok:
                    try:
                        text = path.read_text(encoding="utf-8")
                        root[:] = sexpdata.loads(text)
                    except Exception as exc:
                        ok, note = False, f"reloaded ok but parse failed: {exc}"
            elif verb == "list_snapshots":
                # Read-only — populate `note` with a JSON-encoded list
                # so the agent can relay it. Doesn't mutate.
                snaps = _list_snapshots(path)
                ok = True
                note = json.dumps([
                    {"name": s["name"], "taken": s["taken"],
                      "ops_summary": s["ops_summary"]}
                    for s in snaps
                ])
            elif verb == "combine_sheets":
                # Multi-file op — delegates to the standalone
                # combine_sheets tool. The `path` arg of apply_ops is
                # used as the parent hierarchy sheet (its (sheet ...)
                # entries get rewritten if a 'parent' is implied).
                # Args expected on the op: sources (list), output
                # (str), parent (optional, defaults to current path).
                #
                # CRITICAL: combine_sheets writes the parent .kicad_sch
                # directly to disk. After it returns, we must reload
                # `root` from disk — otherwise apply_ops's end-of-loop
                # write-back uses the stale in-memory copy and
                # overwrites the just-completed parent update. Same
                # pattern as undo_last_edit above.
                import importlib as _imp
                _cs = _imp.import_module("envil_agent.tools.combine_sheets")
                _r = await _cs.combine_sheets.handler({
                    "sources": op.get("sources", []),
                    "output":  op.get("output", ""),
                    "parent":  op.get("parent") or str(path),
                })
                if _r.get("is_error"):
                    ok, note = False, _r["content"][0]["text"]
                else:
                    ok, note = True, _r["content"][0]["text"]
                    try:
                        text = path.read_text(encoding="utf-8")
                        root[:] = sexpdata.loads(text)
                    except Exception as exc:
                        ok, note = False, (
                            f"combine_sheets wrote files but "
                            f"reloading parent root failed: {exc}")
            else:
                ok, note = False, f"unsupported verb {verb!r}"
        except Exception as exc:
            ok, note = False, f"{type(exc).__name__}: {exc}"
        # Annotate the owning sheet file when the edit landed on a child
        # (not the primary) so the chat reply can say where it happened.
        res = {"verb": verb, "ref": ref, "ok": ok, "note": note}
        if ok and op_rp != primary_rp:
            res["sheet"] = op_rp.name
        results.append(res)
        if ok:
            _dirty.add(op_rp)

    # Write back EVERY sheet file we mutated. The primary file was already
    # snapshotted up-front; snapshot each extra child before writing so
    # undo_last_edit covers cross-sheet edits too. Collect {file, snapshot}
    # pairs into a transaction manifest when >1 sheet changed.
    txn_entries: List[Dict[str, str]] = []
    for rp in sorted(_dirty):
        robj = _roots.get(rp)
        if robj is None:
            continue
        snap = snapshot_path if rp == primary_rp else _take_snapshot(rp, ops_summary)
        try:
            rp.write_text(_emit_multiline(robj), encoding="utf-8")
        except Exception as exc:
            return {
                "content": [{"type": "text",
                              "text": (f"ERROR: ops applied in memory but "
                                        f"writing back {rp} failed: "
                                        f"{type(exc).__name__}: {exc}")}],
                "is_error": True,
            }
        if snap is not None:
            txn_entries.append({"file": str(rp), "snapshot": snap.name})

    # Only record a transaction when a CHILD sheet was edited — pure
    # primary-file edits keep the original single-snapshot undo path.
    if txn_entries and any(rp != primary_rp for rp in _dirty):
        _write_txn(path, txn_entries, ops_summary)

    summary = {
        "path": str(path),
        "sheets_written": sorted(p.name for p in _dirty),
        "ops_applied": sum(1 for r in results if r["ok"]),
        "ops_failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }
    return {
        "content": [{"type": "text",
                      "text": json.dumps(summary, indent=2)}],
    }
