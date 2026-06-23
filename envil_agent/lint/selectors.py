"""Schematic-lint selector helpers --- pure functions that detect
violations in a parsed schematic.

These functions are SELECTORS: they take a parsed schematic (or its
constituent parts) and return a list of violation dicts. They do NOT
mutate the schematic --- mutators (such as `split_four_way_junctions`
for rule R4) live in the engine because they need to re-emit
s-expressions.

The lint engine (`lint.engine.LintEngine`) dispatches to these by name
from `config/lint_rules.json`, so adding a new rule is one selector
function + one JSON entry, no boilerplate.

All distances are in millimetres. All coordinates use KiCad's
convention (positive Y down).

Non-breaking: `tools/audit_wires.py` already implements R2/R4/R9
detection inline; this module REPLICATES that logic so the legacy tool
keeps working while new callers can use the typed helpers. The two
implementations are intentionally separate so a future cleanup can
delete the inline version without coordination.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

# Shared types --------------------------------------------------------------
#
# Wire:      ((x1, y1), (x2, y2))
# Bbox:      (ref, xmin, ymin, xmax, ymax)
# PinPos:    (x, y, ref)
# Label:     (name, x, y)
# Issue:     {"id": str, "severity": str, "message": str, "where": dict, ...}

Point   = Tuple[float, float]
Wire    = Tuple[Point, Point]
Bbox    = Tuple[str, float, float, float, float]
PinPos  = Tuple[float, float, str]
Label   = Tuple[str, float, float]
Issue   = Dict[str, Any]


_GRID_MM = 1.27


def _issue(rule_id: str, severity: str, message: str, **where) -> Issue:
    return {
        "id": rule_id,
        "severity": severity,
        "message": message,
        "where": where,
    }


# ----- R2: wires piercing component bodies --------------------------------

def find_wires_piercing_bodies(
    wires: List[Wire],
    bboxes: List[Bbox],
    pin_positions: List[PinPos],
    margin_mm: float = 0.5,
) -> List[Issue]:
    """Return one issue per wire segment that crosses through a
    component's outer bbox without terminating at one of that
    component's pins."""
    out: List[Issue] = []
    for (ax, ay), (bx, by) in wires:
        for ref, x1, y1, x2, y2 in bboxes:
            pin_on_this = any(
                ref == pref and (
                    (abs(px - ax) < margin_mm and abs(py - ay) < margin_mm) or
                    (abs(px - bx) < margin_mm and abs(py - by) < margin_mm)
                )
                for px, py, pref in pin_positions
            )
            if pin_on_this:
                continue
            if max(ax, bx) <= x1 or min(ax, bx) >= x2:
                continue
            if max(ay, by) <= y1 or min(ay, by) >= y2:
                continue
            inside = (
                lambda x, y: x1 + margin_mm < x < x2 - margin_mm
                and y1 + margin_mm < y < y2 - margin_mm
            )
            midx, midy = (ax + bx) / 2, (ay + by) / 2
            if inside(ax, ay) or inside(bx, by) or inside(midx, midy):
                out.append(_issue(
                    "R2", "error",
                    f"wire ({ax:.2f},{ay:.2f})->({bx:.2f},{by:.2f}) "
                    f"pierces component {ref}",
                    wire_start=[round(ax, 2), round(ay, 2)],
                    wire_end=[round(bx, 2), round(by, 2)],
                    pierces=ref,
                ))
                break
    return out


# ----- R4: 4-way wire junctions -------------------------------------------

def find_four_way_junctions(wires: List[Wire]) -> List[Issue]:
    """Return one issue per grid point where 4 or more wire endpoints
    meet. KiCad's junction dot then becomes ambiguous between a 4-way
    join and two crossing-but-not-connecting wires (rule R4 recommends
    offsetting one wire by 1 grid to make two T-junctions)."""
    counts: Dict[Tuple[float, float], int] = defaultdict(int)
    for (ax, ay), (bx, by) in wires:
        counts[(round(ax, 3), round(ay, 3))] += 1
        counts[(round(bx, 3), round(by, 3))] += 1
    out: List[Issue] = []
    for (x, y), n in counts.items():
        if n >= 4:
            out.append(_issue(
                "R4", "warning",
                f"{n} wire endpoints meet at ({x},{y}) -- ambiguous "
                "junction; offset one wire by 1 grid (1.27 mm)",
                x=x, y=y, endpoints=n,
            ))
    return out


# ----- R16: drawn wire crossing a block boundary --------------------------

Block = Tuple[str, float, float, float, float]


def _block_of_point(
    x: float, y: float, blocks: List[Block]
) -> Optional[Block]:
    """Return the smallest block rectangle that contains (x, y), or None
    when the point is outside every block box. Smallest-wins keeps the
    attribution unambiguous if boxes ever overlap."""
    best: Optional[Block] = None
    best_area: Optional[float] = None
    for b in blocks:
        _name, x1, y1, x2, y2 = b
        if x1 <= x <= x2 and y1 <= y <= y2:
            area = (x2 - x1) * (y2 - y1)
            if best_area is None or area < best_area:
                best, best_area = b, area
    return best


def find_crossblock_wires(
    wires: List[Wire],
    blocks: List[Block],
) -> List[Issue]:
    """R16: report any drawn wire whose two endpoints fall inside DIFFERENT
    functional-block rectangles.

    The reference convention is 'WIRES inside a block, LABELS between
    blocks' — a wire spanning a block boundary should instead be a
    same-named net label on the pin in each block (KiCad bonds them by
    name). The engine enforces this at generation time
    (`multi_block.cross_block_signal_wires=false`); this selector VERIFIES
    it held after `apply_ops` edits, hand edits, or imported sheets.

    Endpoints that fall outside every block box are ignored (cannot be
    attributed to a block), so power-port stubs and free wires never
    false-positive. Purely geometric: block membership comes from the
    drawn rectangles, with no IR / refdes / net-name knowledge."""
    if not blocks:
        return []
    out: List[Issue] = []
    for (ax, ay), (bx, by) in wires:
        ba = _block_of_point(ax, ay, blocks)
        bb = _block_of_point(bx, by, blocks)
        if ba is None or bb is None:
            continue
        if ba[1:] == bb[1:]:           # same rectangle -> intra-block, OK
            continue
        out.append(_issue(
            "R16", "warning",
            f"wire ({ax:.2f},{ay:.2f})->({bx:.2f},{by:.2f}) crosses from "
            f"block '{ba[0] or '?'}' to '{bb[0] or '?'}' -- use a net label "
            "on the pin in each block instead of a cross-block wire",
            wire_start=[round(ax, 2), round(ay, 2)],
            wire_end=[round(bx, 2), round(by, 2)],
            from_block=ba[0],
            to_block=bb[0],
        ))
    return out


# ----- R17: net label used for an INTRA-block connection ------------------

def find_labels_inside_block(
    labels: List[Label],
    blocks: List[Block],
) -> List[Issue]:
    """R17 (inverse of R16): report a net label whose ALL same-named instances
    sit inside ONE functional block.

    Convention: WIRES inside a block, LABELS only BETWEEN blocks. A net whose
    label instances are all in a single block is an INTRA-block connection that
    must be drawn with wires instead --- the label hides the connection and
    breaks the block's visual flow (durable user rule
    'block ulla wire tha use pannanum'). A net that legitimately spans blocks
    has labels in 2+ blocks and is NOT flagged.

    Power-port symbols (+3V3/GND, '#'-ref) are NOT regular labels and never
    reach this selector. Purely geometric: block membership from the drawn
    rectangles, grouping by label name."""
    if not blocks:
        return []
    by_name: Dict[str, List[Tuple[float, float, Optional[Block]]]] = defaultdict(list)
    for (name, lx, ly) in labels:
        by_name[name].append((lx, ly, _block_of_point(lx, ly, blocks)))
    out: List[Issue] = []
    for name, insts in by_name.items():
        owning = {b[0] for (_x, _y, b) in insts if b is not None}
        # All instances land in exactly one block (and none fall outside any
        # block) -> the whole net is intra-block -> should be wires.
        if len(owning) == 1 and all(b is not None for (_x, _y, b) in insts):
            blk = next(iter(owning))
            for (lx, ly, _b) in insts:
                out.append(_issue(
                    "R17", "warning",
                    f"label {name!r} at ({lx:.2f},{ly:.2f}) bonds an INTRA-block "
                    f"net (all instances inside block '{blk}') -- connect these "
                    "pins with drawn WIRES, not a label",
                    name=name, x=round(lx, 2), y=round(ly, 2), block=blk,
                ))
    return out


# ----- R9: bus-notation candidates ----------------------------------------

_BUS_PATTERN_RE = re.compile(r"^([A-Za-z_]+)(\d+)$")


def detect_bus_candidates(
    label_names: List[str], min_group_size: int = 4,
) -> List[Issue]:
    """Return one issue per `<prefix><digit>` group large enough to be a
    bus. `DATA0..DATA7` -> one issue suggesting `DATA[0..7]`."""
    groups: Dict[str, List[int]] = {}
    for name in set(label_names):
        m = _BUS_PATTERN_RE.match(name)
        if not m:
            continue
        try:
            groups.setdefault(m.group(1), []).append(int(m.group(2)))
        except ValueError:
            continue
    out: List[Issue] = []
    for prefix, idxs in groups.items():
        if len(idxs) < min_group_size:
            continue
        idxs.sort()
        out.append(_issue(
            "R9", "info",
            f"{len(idxs)} labels with prefix {prefix!r} could become a "
            f"bus: {prefix}[{idxs[0]}..{idxs[-1]}]",
            prefix=prefix,
            count=len(idxs),
            range=f"{prefix}[{idxs[0]}..{idxs[-1]}]",
        ))
    return out


# ----- R11: label-on-wire overlap -----------------------------------------

def label_overlaps_wire(
    label_pt: Point,
    label_text: str,
    wires: List[Wire],
    font_height_mm: float = _GRID_MM,
    char_width_ratio: float = 0.7,
) -> bool:
    """True when the label's rendered text bbox intersects any wire
    segment. Used by the engine to push a label perpendicular when the
    natural anchor would obscure the wire (rule R11).

    The text bbox is approximated as: width = chars * font_height *
    char_width_ratio, height = font_height. KiCad's actual glyph
    metrics differ slightly but the approximation is conservative."""
    if not label_text:
        return False
    half_w = (len(label_text) * font_height_mm * char_width_ratio) / 2
    half_h = font_height_mm / 2
    lx, ly = label_pt
    text_x1, text_x2 = lx - half_w, lx + half_w
    text_y1, text_y2 = ly - half_h, ly + half_h
    for (ax, ay), (bx, by) in wires:
        # AABB vs line-segment intersection (cheap approximation:
        # treat each wire endpoint as the candidate intersection
        # check, plus the segment's AABB overlap with the text AABB).
        wire_x1, wire_x2 = min(ax, bx), max(ax, bx)
        wire_y1, wire_y2 = min(ay, by), max(ay, by)
        if wire_x2 < text_x1 or wire_x1 > text_x2:
            continue
        if wire_y2 < text_y1 or wire_y1 > text_y2:
            continue
        return True
    return False


def find_labels_overlapping_wires(
    labels: List[Label],
    wires: List[Wire],
    font_height_mm: float = _GRID_MM,
) -> List[Issue]:
    """Iterate the schematic's labels and report each one whose text
    bbox sits on top of a wire. Selector form of `label_overlaps_wire`
    suitable for the lint engine's declarative dispatcher."""
    out: List[Issue] = []
    for name, lx, ly in labels:
        if label_overlaps_wire((lx, ly), name, wires, font_height_mm):
            out.append(_issue(
                "R11", "warning",
                f"label {name!r} at ({lx:.2f},{ly:.2f}) overlaps a wire "
                "-- reposition perpendicular by 1 grid (1.27 mm)",
                name=name, x=round(lx, 2), y=round(ly, 2),
            ))
    return out


# ----- IR-level: floating nets --------------------------------------------

def detect_floating_nets(
    ir_nets: List[Any], min_pins: int = 2,
) -> List[Issue]:
    """Return one issue per net with fewer pins than `min_pins`. A net
    with one pin is electrically meaningless: there is nothing for the
    pin to connect TO. Power nets are exempt from the lower bound when
    the engine synthesises a PWR_FLAG (R5), so callers may filter
    `is_power=True` nets out before calling this. Pure IR check ---
    runs before render, so the architect's retry sees the error."""
    out: List[Issue] = []
    for net in ir_nets:
        # Duck-type: works on TopologyIR.IRNet (dataclass) or a dict.
        name = getattr(net, "name", None) or (
            net.get("name") if isinstance(net, dict) else None
        )
        pins = getattr(net, "pins", None) or (
            net.get("pins", []) if isinstance(net, dict) else []
        )
        if not name:
            continue
        if len(pins) < min_pins:
            out.append(_issue(
                "NET_FLOATING", "error",
                f"net {name!r} has only {len(pins)} pin(s); "
                f"every net needs at least {min_pins} "
                "(one driver + one receiver)",
                net=name, pin_count=len(pins), pins=list(pins),
            ))
    return out


# ===========================================================================
# Geometric connectivity selectors --- image-checklist gap closure (2026-06-02)
#
# The selectors above (R2/R4/R9/R11) + detect_floating_nets cover ~3 of the 10
# items on the "AI circuit generation" wiring checklist (the user's reference
# image). The block below closes the remaining GEOMETRIC-connectivity gaps so
# the SAME declarative engine can audit an edited / imported / apply_ops-touched
# .kicad_sch --- not only engine-generated output, which is already correct by
# construction. Mapping to the image checklist:
#
#   #1 pin-to-wire connected ........ find_unconnected_pins  (near_miss flag)
#   #2 floating wires ............... find_dangling_wire_endpoints
#   #3 wire through symbol .......... find_wires_piercing_bodies   (existing R2)
#   #4 missing junction dots ........ find_missing_junction_dots  (3-way T) + R4
#   #5 crossing mistaken-connected .. find_wrong_crossing_junctions
#   #6 net labels touching wire ..... find_labels_not_on_wire
#   #7 unused pins marked NC ........ find_unconnected_pins
#   #8 wire overlapping text ........ find_labels_overlapping_wires (existing) +
#                                     find_wires_over_component_text
#   #9 acute-angle routing .......... find_diagonal_wires
#   #10 open wire ends .............. find_dangling_wire_endpoints
#
# Every threshold is a parameter so config/lint_rules.json drives it (no
# hardcoded literals). Coordinate model: KiCad schematic space, Y positive
# DOWN, mm. Pins arrive as dicts {x, y, ref, name, number, etype}; wires as
# ((ax, ay), (bx, by)).
# ===========================================================================


def _pt_eq(ax: float, ay: float, bx: float, by: float, tol: float) -> bool:
    return abs(ax - bx) <= tol and abs(ay - by) <= tol


def _on_seg(px: float, py: float, ax: float, ay: float,
            bx: float, by: float, tol: float) -> bool:
    """True if (px,py) lies on segment A-B (endpoints included), within tol."""
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= tol * tol:
        return _pt_eq(px, py, ax, ay, tol)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    slack = tol / math.sqrt(seg2)
    if t < -slack or t > 1 + slack:
        return False
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy) <= tol


def _on_seg_interior(px: float, py: float, ax: float, ay: float,
                     bx: float, by: float, tol: float) -> bool:
    """On the segment but NOT at either endpoint --- i.e. a mid-span tap
    (the T of a T-junction)."""
    if _pt_eq(px, py, ax, ay, tol) or _pt_eq(px, py, bx, by, tol):
        return False
    return _on_seg(px, py, ax, ay, bx, by, tol)


# ----- R1: diagonal / acute-angle wires (checklist #9) --------------------

def find_diagonal_wires(wires: List[Wire], tol_mm: float = 0.01) -> List[Issue]:
    """Every KiCad net wire must be orthogonal (pure H or pure V). A segment
    with both dx and dy non-zero is a diagonal/acute run --- visually
    ambiguous and against R1. The engine only emits H+V, but apply_ops edits
    or imported sheets can introduce diagonals; this catches them."""
    out: List[Issue] = []
    for (ax, ay), (bx, by) in wires:
        if abs(ax - bx) > tol_mm and abs(ay - by) > tol_mm:
            out.append(_issue(
                "R1", "error",
                f"diagonal wire ({ax:.2f},{ay:.2f})->({bx:.2f},{by:.2f}); "
                "use orthogonal H/V segments only",
                wire_start=[round(ax, 2), round(ay, 2)],
                wire_end=[round(bx, 2), round(by, 2)],
            ))
    return out


# ----- Missing junction dot at 3+ meets / T-taps (checklist #4) -----------

def find_missing_junction_dots(
    wires: List[Wire],
    junctions: Optional[List[Point]] = None,
    tol_mm: float = 0.05,
) -> List[Issue]:
    """A junction dot is required where 3+ wire ends meet (T/X) OR where a
    wire end taps the mid-span of another wire (a T-tap). A pure crossing
    (two wires passing straight through, neither ending) needs NO dot and is
    intentionally NOT flagged here --- see find_wrong_crossing_junctions for
    the inverse case. Reports any qualifying meet that lacks a (junction)."""
    junctions = list(junctions or [])
    # Candidate points = the distinct wire endpoints (a T-tap point is always
    # the endpoint of the tapping wire, so it is in this set).
    cand: Dict[Tuple[float, float], Point] = {}
    for (ax, ay), (bx, by) in wires:
        cand.setdefault((round(ax, 2), round(ay, 2)), (ax, ay))
        cand.setdefault((round(bx, 2), round(by, 2)), (bx, by))
    out: List[Issue] = []
    for (x, y) in cand.values():
        ends = taps = 0
        for (ax, ay), (bx, by) in wires:
            ea = _pt_eq(x, y, ax, ay, tol_mm)
            eb = _pt_eq(x, y, bx, by, tol_mm)
            if ea:
                ends += 1
            if eb:
                ends += 1
            if not ea and not eb and _on_seg_interior(x, y, ax, ay, bx, by, tol_mm):
                taps += 1
        needs_dot = ends >= 3 or (ends >= 1 and taps >= 1)
        if not needs_dot:
            continue
        if any(_pt_eq(x, y, jx, jy, tol_mm) for jx, jy in junctions):
            continue
        out.append(_issue(
            "JUNCTION_MISSING", "warning",
            f"{ends + taps} wires connect at ({x:.2f},{y:.2f}) with no "
            "junction dot; 3+ joined wires need an explicit (junction)",
            x=round(x, 2), y=round(y, 2), wire_ends=ends, taps=taps,
        ))
    return out


# ----- Junction sitting on a pure crossing (checklist #5) -----------------

def find_wrong_crossing_junctions(
    wires: List[Wire],
    junctions: Optional[List[Point]] = None,
    tol_mm: float = 0.05,
) -> List[Issue]:
    """A (junction) placed where two wires merely CROSS (both pass straight
    through, no wire ends there) wrongly bonds two nets that should stay
    separate (image rule 3 'dot at crossing makes them connected --- wrong').
    Info severity: it may be an intentional 4-way join, so flag for review."""
    junctions = list(junctions or [])
    out: List[Issue] = []
    for (jx, jy) in junctions:
        ends = crossings = 0
        for (ax, ay), (bx, by) in wires:
            ea = _pt_eq(jx, jy, ax, ay, tol_mm)
            eb = _pt_eq(jx, jy, bx, by, tol_mm)
            if ea or eb:
                ends += 1
            elif _on_seg_interior(jx, jy, ax, ay, bx, by, tol_mm):
                crossings += 1
        if ends == 0 and crossings >= 2:
            out.append(_issue(
                "JUNCTION_AT_CROSSING", "info",
                f"junction at ({jx:.2f},{jy:.2f}) sits on a pure wire "
                "crossing (no wire ends here) -- verify these nets are "
                "meant to connect",
                x=round(jx, 2), y=round(jy, 2), crossing_wires=crossings,
            ))
    return out


# ----- Dangling / open wire endpoints (checklist #2, #10) -----------------

def find_dangling_wire_endpoints(
    wires: List[Wire],
    pins: Optional[List[Dict[str, Any]]] = None,
    labels: Optional[List[Label]] = None,
    junctions: Optional[List[Point]] = None,
    no_connects: Optional[List[Point]] = None,
    tol_mm: float = 0.05,
) -> List[Issue]:
    """A wire endpoint must terminate on SOMETHING: a pin tip, another wire,
    a label anchor, a junction, or a no-connect marker. An endpoint touching
    none of those is a dangling stub (R15 / ERC unconnected_wire_endpoint)."""
    pins = pins or []
    labels = labels or []
    junctions = junctions or []
    no_connects = no_connects or []
    pin_pts = [(p["x"], p["y"]) for p in pins]
    label_pts = [(lx, ly) for (_n, lx, ly) in labels]
    out: List[Issue] = []
    for i, ((ax, ay), (bx, by)) in enumerate(wires):
        for (ex, ey) in ((ax, ay), (bx, by)):
            if any(_pt_eq(ex, ey, px, py, tol_mm) for px, py in pin_pts):
                continue
            if any(_pt_eq(ex, ey, lx, ly, tol_mm) for lx, ly in label_pts):
                continue
            if any(_pt_eq(ex, ey, jx, jy, tol_mm) for jx, jy in junctions):
                continue
            if any(_pt_eq(ex, ey, nx, ny, tol_mm) for nx, ny in no_connects):
                continue
            touches = False
            for j, ((cx, cy), (dx, dy)) in enumerate(wires):
                if j == i:
                    continue
                if (_pt_eq(ex, ey, cx, cy, tol_mm)
                        or _pt_eq(ex, ey, dx, dy, tol_mm)
                        or _on_seg_interior(ex, ey, cx, cy, dx, dy, tol_mm)):
                    touches = True
                    break
            if touches:
                continue
            out.append(_issue(
                "WIRE_DANGLING", "error",
                f"wire endpoint ({ex:.2f},{ey:.2f}) connects to nothing "
                "(no pin, wire, label, junction or no-connect)",
                x=round(ex, 2), y=round(ey, 2),
            ))
    return out


# ----- Unconnected pins / near-miss / NC coverage (checklist #1, #7) ------

def find_unconnected_pins(
    pins: Optional[List[Dict[str, Any]]] = None,
    wires: Optional[List[Wire]] = None,
    labels: Optional[List[Label]] = None,
    no_connects: Optional[List[Point]] = None,
    tol_mm: float = 0.05,
    near_band_mm: float = 1.27,
    require_etypes: Optional[List[str]] = None,
) -> List[Issue]:
    """Classify every pin tip: connected (wire/label on it), NC-marked, or
    UNCONNECTED. Reports unconnected pins whose electrical type needs a
    connection. If a wire endpoint sits within (tol, near_band] of the tip
    but not ON it, the issue is a 'near miss' (image rule 1 / case 11.3-11.5
    'wire looks near the pin but is not actually connected') --- error
    severity. A genuinely open pin is a warning ('wire it or add NC')."""
    pins = pins or []
    wires = wires or []
    labels = labels or []
    no_connects = no_connects or []
    if require_etypes is None:
        require_etypes = [
            "input", "output", "bidirectional", "power_in", "power_out",
            "passive", "tri_state", "open_collector", "open_emitter",
            "unspecified",
        ]
    require = set(require_etypes)
    label_pts = [(lx, ly) for (_n, lx, ly) in labels]
    out: List[Issue] = []
    for p in pins:
        et = (p.get("etype") or "").strip()
        if et == "no_connect" or et not in require:
            continue
        px, py = p["x"], p["y"]
        on_wire = any(
            _pt_eq(px, py, ax, ay, tol_mm) or _pt_eq(px, py, bx, by, tol_mm)
            or _on_seg_interior(px, py, ax, ay, bx, by, tol_mm)
            for (ax, ay), (bx, by) in wires
        )
        on_label = any(_pt_eq(px, py, lx, ly, tol_mm) for lx, ly in label_pts)
        nc = any(_pt_eq(px, py, nx, ny, tol_mm) for nx, ny in no_connects)
        if on_wire or on_label or nc:
            continue
        ref = p.get("ref", "?")
        name = p.get("name") or p.get("number", "?")
        nearest: Optional[float] = None
        for (ax, ay), (bx, by) in wires:
            for (wx, wy) in ((ax, ay), (bx, by)):
                d = math.hypot(px - wx, py - wy)
                if tol_mm < d <= near_band_mm and (nearest is None or d < nearest):
                    nearest = d
        if nearest is not None:
            out.append(_issue(
                "PIN_UNCONNECTED", "error",
                f"pin {ref}.{name} at ({px:.2f},{py:.2f}) has a wire end "
                f"{nearest:.2f} mm away but NOT on the tip -- near miss, "
                "snap the wire to the pin centre",
                ref=ref, pin=name, x=round(px, 2), y=round(py, 2),
                gap_mm=round(nearest, 2), near_miss=True,
            ))
        else:
            out.append(_issue(
                "PIN_UNCONNECTED", "warning",
                f"pin {ref}.{name} ({et}) at ({px:.2f},{py:.2f}) is "
                "unconnected -- wire it or add a (no_connect) marker",
                ref=ref, pin=name, x=round(px, 2), y=round(py, 2),
                near_miss=False,
            ))
    return out


# ----- Net labels not landing on a wire/pin (checklist #6) ----------------

def find_labels_not_on_wire(
    labels: Optional[List[Label]] = None,
    wires: Optional[List[Wire]] = None,
    pins: Optional[List[Dict[str, Any]]] = None,
    tol_mm: float = 0.05,
) -> List[Issue]:
    """A net label must anchor on a wire (endpoint or mid-span) or a pin tip.
    A label floating near --- but not on --- a wire bonds nothing and silently
    forms its own one-pin net (image rule 7 'label looks close but not
    connected to wire')."""
    labels = labels or []
    wires = wires or []
    pins = pins or []
    pin_pts = [(p["x"], p["y"]) for p in pins]
    out: List[Issue] = []
    for (name, lx, ly) in labels:
        on_wire = any(
            _pt_eq(lx, ly, ax, ay, tol_mm) or _pt_eq(lx, ly, bx, by, tol_mm)
            or _on_seg_interior(lx, ly, ax, ay, bx, by, tol_mm)
            for (ax, ay), (bx, by) in wires
        )
        if on_wire or any(_pt_eq(lx, ly, px, py, tol_mm) for px, py in pin_pts):
            continue
        out.append(_issue(
            "LABEL_FLOATING", "error",
            f"label {name!r} at ({lx:.2f},{ly:.2f}) is not on any wire or "
            "pin tip -- it bonds nothing and forms its own net",
            name=name, x=round(lx, 2), y=round(ly, 2),
        ))
    return out


# ----- Wire-on-wire overlap (collinear, stacked/duplicate segments) -------

def find_overlapping_wires(
    wires: List[Wire], tol_mm: float = 0.05, min_overlap_mm: float = 0.05,
) -> List[Issue]:
    """Two COLLINEAR wire segments that overlap along their length (share more
    than a single point) are a 'wire on wire' defect: redundant/stacked
    duplicate copy, or --- worse --- two different nets routed onto the same
    line (a short hiding in plain sight). KiCad treats overlapping collinear
    segments as one connection, so this also feeds short detection.

    Only pure horizontal or pure vertical pairs are compared (diagonals are
    caught by find_diagonal_wires). Reports one issue per overlapping pair with
    the shared span length."""
    out: List[Issue] = []
    n = len(wires)
    for i in range(n):
        (ax, ay), (bx, by) = wires[i]
        for j in range(i + 1, n):
            (cx, cy), (dx, dy) = wires[j]
            # Horizontal pair on the same row.
            if (abs(ay - by) <= tol_mm and abs(cy - dy) <= tol_mm
                    and abs(ay - cy) <= tol_mm):
                lo = max(min(ax, bx), min(cx, dx))
                hi = min(max(ax, bx), max(cx, dx))
                axis, coord = "horizontal", ay
            # Vertical pair on the same column.
            elif (abs(ax - bx) <= tol_mm and abs(cx - dx) <= tol_mm
                    and abs(ax - cx) <= tol_mm):
                lo = max(min(ay, by), min(cy, dy))
                hi = min(max(ay, by), max(cy, dy))
                axis, coord = "vertical", ax
            else:
                continue
            if hi - lo <= min_overlap_mm:
                continue
            out.append(_issue(
                "WIRE_OVERLAP", "warning",
                f"two {axis} wires overlap along {hi - lo:.2f} mm at "
                f"{coord:.2f} ({lo:.2f}..{hi:.2f}) -- stacked/duplicate "
                "segments; delete one or split so each net has its own path",
                axis=axis, coord=round(coord, 2),
                span=[round(lo, 2), round(hi, 2)],
                overlap_mm=round(hi - lo, 2),
            ))
    return out


# ----- Power-rail short: two different rails bonded onto one net ----------

def find_power_rail_shorts(
    wires: Optional[List[Wire]] = None,
    junctions: Optional[List[Point]] = None,
    labels: Optional[List[Label]] = None,
    power_ports: Optional[List[Label]] = None,
    tol_mm: float = 0.05,
    ignore_rails: Optional[List[str]] = None,
) -> List[Issue]:
    """Build the net-connectivity graph and report any net carrying TWO OR MORE
    DIFFERENT power rails --- a dead short (e.g. +3V3 wired to GND). This is the
    'wire shortage' (short-circuit) case: KiCad's ERC raises it as two power
    nets connected / power output short.

    Connectivity mirrors KiCad: wires bond their own two endpoints; coincident
    endpoints of different wires bond; an explicit junction bonds every wire
    endpoint AND mid-span tap at its point; collinear overlapping wires bond;
    same-named labels bond. Mid-span taps WITHOUT a junction do NOT bond (that
    is a missing-dot warning, not a connection). `ignore_rails` (default
    ['PWR_FLAG']) are net-driver markers allowed on any rail."""
    wires = wires or []
    junctions = junctions or []
    labels = labels or []
    power_ports = power_ports or []
    ignore = {r.upper() for r in (ignore_rails or ["PWR_FLAG"])}

    parent: Dict[Tuple[float, float], Tuple[float, float]] = {}

    def _key(x: float, y: float) -> Tuple[float, float]:
        return (round(x, 2), round(y, 2))

    def _find(k: Tuple[float, float]) -> Tuple[float, float]:
        parent.setdefault(k, k)
        root = k
        while parent[root] != root:
            root = parent[root]
        while parent[k] != root:
            parent[k], k = root, parent[k]
        return root

    def _union(a: Tuple[float, float], b: Tuple[float, float]) -> None:
        parent[_find(a)] = _find(b)

    # 1) each wire bonds its own endpoints; coincident endpoints share a key.
    for (ax, ay), (bx, by) in wires:
        _union(_key(ax, ay), _key(bx, by))
    # 2) collinear overlapping wires bond (KiCad merges them even without a
    #    shared endpoint --- one segment lies on top of the other).
    n = len(wires)
    for i in range(n):
        (ax, ay), (bx, by) = wires[i]
        for j in range(i + 1, n):
            (cx, cy), (dx, dy) = wires[j]
            horiz = (abs(ay - by) <= tol_mm and abs(cy - dy) <= tol_mm
                     and abs(ay - cy) <= tol_mm)
            vert = (abs(ax - bx) <= tol_mm and abs(cx - dx) <= tol_mm
                    and abs(ax - cx) <= tol_mm)
            if horiz:
                lo = max(min(ax, bx), min(cx, dx))
                hi = min(max(ax, bx), max(cx, dx))
            elif vert:
                lo = max(min(ay, by), min(cy, dy))
                hi = min(max(ay, by), max(cy, dy))
            else:
                continue
            if hi - lo > tol_mm:
                _union(_key(ax, ay), _key(cx, cy))
    # 3) junctions bond every wire endpoint + mid-span tap at the dot.
    for (jx, jy) in junctions:
        for (ax, ay), (bx, by) in wires:
            if (_pt_eq(jx, jy, ax, ay, tol_mm) or _pt_eq(jx, jy, bx, by, tol_mm)
                    or _on_seg_interior(jx, jy, ax, ay, bx, by, tol_mm)):
                _union(_key(jx, jy), _key(ax, ay))
                _union(_key(jx, jy), _key(bx, by))
    # 4) same-named labels bond.
    by_name: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for (name, lx, ly) in labels:
        by_name[name].append(_key(lx, ly))
    for pts in by_name.values():
        for p in pts[1:]:
            _union(pts[0], p)

    # Group power ports by net root; flag nets holding 2+ distinct real rails.
    net_rails: Dict[Tuple[float, float], Dict[str, Tuple[float, float]]] = \
        defaultdict(dict)
    for (rail, px, py) in power_ports:
        if rail.upper() in ignore:
            continue
        net_rails[_find(_key(px, py))].setdefault(rail, (round(px, 2),
                                                          round(py, 2)))
    out: List[Issue] = []
    for rails in net_rails.values():
        if len(rails) < 2:
            continue
        names = sorted(rails)
        locs = "; ".join(f"{r}@({x:.2f},{y:.2f})" for r, (x, y) in
                         sorted(rails.items()))
        out.append(_issue(
            "POWER_SHORT", "error",
            f"power rails {names} are on the SAME net -- short circuit "
            f"({locs}); break the wire/label bonding them",
            rails=names, ports=locs,
        ))
    return out


# ----- Net-name conflict: two different labels on one physical net --------

def find_conflicting_net_names(
    wires: Optional[List[Wire]] = None,
    junctions: Optional[List[Point]] = None,
    labels: Optional[List[Label]] = None,
    tol_mm: float = 0.05,
) -> List[Issue]:
    """Report any single physical net that carries TWO OR MORE DIFFERENT net
    labels. KiCad's rule is explicit: 'A net can only have one name. If two
    different labels are placed on the same net, an ERC violation will be
    generated.' (verified deep-research 2026-06-22 against the KiCad docs,
    3-0 adversarial vote). This is the inverse of R8: same-named labels are
    MEANT to bond, but two DISTINCT names on one geometrically-connected wire
    net is a naming conflict, not a connection.

    Connectivity is built from GEOMETRY only --- wires bond their own
    endpoints, coincident endpoints bond, collinear overlapping wires bond, and
    an explicit junction bonds every wire endpoint + mid-span tap at its point
    --- exactly as `find_power_rail_shorts` does. Labels are deliberately NOT
    pre-bonded by name here (that name-merge is the very thing under test);
    instead each label is attached to whatever wire net its anchor point lands
    on, then nets holding 2+ distinct names are flagged. A label that sits on
    no wire is left to LABEL_FLOATING.

    Scale-free: every threshold is a param and the check is per-net, so a
    5-pin or 500-pin board is handled identically --- no component/pin/net
    count appears anywhere."""
    wires = wires or []
    junctions = junctions or []
    labels = labels or []

    parent: Dict[Tuple[float, float], Tuple[float, float]] = {}

    def _key(x: float, y: float) -> Tuple[float, float]:
        return (round(x, 2), round(y, 2))

    def _find(k: Tuple[float, float]) -> Tuple[float, float]:
        parent.setdefault(k, k)
        root = k
        while parent[root] != root:
            root = parent[root]
        while parent[k] != root:
            parent[k], k = root, parent[k]
        return root

    def _union(a: Tuple[float, float], b: Tuple[float, float]) -> None:
        parent[_find(a)] = _find(b)

    # 1) each wire bonds its own endpoints; coincident endpoints share a key.
    for (ax, ay), (bx, by) in wires:
        _union(_key(ax, ay), _key(bx, by))
    # 2) collinear overlapping wires bond (KiCad merges them even without a
    #    shared endpoint --- one segment lies on top of the other).
    n = len(wires)
    for i in range(n):
        (ax, ay), (bx, by) = wires[i]
        for j in range(i + 1, n):
            (cx, cy), (dx, dy) = wires[j]
            horiz = (abs(ay - by) <= tol_mm and abs(cy - dy) <= tol_mm
                     and abs(ay - cy) <= tol_mm)
            vert = (abs(ax - bx) <= tol_mm and abs(cx - dx) <= tol_mm
                    and abs(ax - cx) <= tol_mm)
            if horiz:
                lo = max(min(ax, bx), min(cx, dx))
                hi = min(max(ax, bx), max(cx, dx))
            elif vert:
                lo = max(min(ay, by), min(cy, dy))
                hi = min(max(ay, by), max(cy, dy))
            else:
                continue
            if hi - lo > tol_mm:
                _union(_key(ax, ay), _key(cx, cy))
    # 3) junctions bond every wire endpoint + mid-span tap at the dot.
    for (jx, jy) in junctions:
        for (ax, ay), (bx, by) in wires:
            if (_pt_eq(jx, jy, ax, ay, tol_mm) or _pt_eq(jx, jy, bx, by, tol_mm)
                    or _on_seg_interior(jx, jy, ax, ay, bx, by, tol_mm)):
                _union(_key(jx, jy), _key(ax, ay))
                _union(_key(jx, jy), _key(bx, by))

    # Attach each label to the wire net its anchor lands on (endpoint OR
    # mid-span tap), then collect the distinct names seen per net root.
    net_names: Dict[Tuple[float, float], Dict[str, Tuple[float, float]]] = \
        defaultdict(dict)
    for (name, lx, ly) in labels:
        root: Optional[Tuple[float, float]] = None
        for (ax, ay), (bx, by) in wires:
            if _pt_eq(lx, ly, ax, ay, tol_mm):
                root = _find(_key(ax, ay)); break
            if _pt_eq(lx, ly, bx, by, tol_mm):
                root = _find(_key(bx, by)); break
            if _on_seg_interior(lx, ly, ax, ay, bx, by, tol_mm):
                root = _find(_key(ax, ay)); break
        if root is None:
            continue  # floating label -> LABEL_FLOATING owns that case
        net_names[root].setdefault(name, (round(lx, 2), round(ly, 2)))

    out: List[Issue] = []
    for names in net_names.values():
        if len(names) < 2:
            continue
        sorted_names = sorted(names)
        locs = "; ".join(f"{nm}@({x:.2f},{y:.2f})" for nm, (x, y) in
                         sorted(names.items()))
        out.append(_issue(
            "NET_NAME_CONFLICT", "error",
            f"net carries {len(sorted_names)} different labels "
            f"{sorted_names} -- a net may have only one name ({locs}); rename "
            "all but one, or split the wire if these are meant to be separate "
            "nets",
            names=sorted_names, labels=locs,
        ))
    return out


# ----- Wire crossing a component's RefDes/Value text (checklist #8) -------

def find_wires_over_component_text(
    wires: Optional[List[Wire]] = None,
    text_bboxes: Optional[List[Tuple[str, str, float, float, float, float]]] = None,
    margin_mm: float = 0.0,
) -> List[Issue]:
    """A wire passing over a component's Reference or Value text obscures it
    (image rule 8 'wire overlaps symbol text. Avoid this.'). text_bboxes are
    (ref, field, x1, y1, x2, y2). find_labels_overlapping_wires already covers
    NET-LABEL text; this covers SYMBOL field text."""
    wires = wires or []
    text_bboxes = text_bboxes or []
    out: List[Issue] = []
    for (ax, ay), (bx, by) in wires:
        for (ref, field, x1, y1, x2, y2) in text_bboxes:
            if max(ax, bx) < x1 - margin_mm or min(ax, bx) > x2 + margin_mm:
                continue
            if max(ay, by) < y1 - margin_mm or min(ay, by) > y2 + margin_mm:
                continue
            # The two AABB rejects above already prove the wire's bounding
            # box overlaps the text box. The engine emits ONLY Manhattan
            # (H/V) wires, and a zero-thickness H/V segment whose AABB
            # overlaps a rectangle genuinely passes THROUGH that rectangle --
            # so AABB overlap IS an exact crossing test here. The old gate
            # sampled only the two endpoints + the midpoint, which MISSED a
            # long wire crossing a small text box anywhere but its midpoint
            # (the reported 'still same issue': a rail running from an IC pin
            # across R1's '10k' / C8's '22p'). Keep the 3-point sample only
            # as a fallback for a diagonal wire the engine never actually
            # produces.
            horiz = abs(ay - by) <= 1e-6
            vert = abs(ax - bx) <= 1e-6
            if horiz or vert:
                hit = True
            else:
                def _inside(x, y):
                    return (x1 - margin_mm <= x <= x2 + margin_mm
                            and y1 - margin_mm <= y <= y2 + margin_mm)
                midx, midy = (ax + bx) / 2, (ay + by) / 2
                hit = _inside(ax, ay) or _inside(bx, by) or _inside(midx, midy)
            if hit:
                out.append(_issue(
                    "R11_TEXT", "warning",
                    f"wire ({ax:.2f},{ay:.2f})->({bx:.2f},{by:.2f}) overlaps "
                    f"{ref} {field} text -- route clear of the text",
                    wire_start=[round(ax, 2), round(ay, 2)],
                    wire_end=[round(bx, 2), round(by, 2)],
                    over=f"{ref}.{field}",
                ))
                # NO break: one wire can cross SEVERAL field boxes (a rail
                # running across both a part's RefDes AND its Value). Reporting
                # only the first left the second obscured until a later pass --
                # the repair then needed multiple runs to converge. Emit one
                # issue per (wire, field) crossing; clear_wires_over_text dedups
                # by (ref, field) so duplicate wires over one field still move
                # it once.
    return out
