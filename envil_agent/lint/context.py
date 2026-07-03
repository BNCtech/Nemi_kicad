"""Parse a `.kicad_sch` into the canonical lint context dict.

Additive: this is the adapter that lets `lint.engine.run_lint` audit a real
schematic FILE (not just engine-internal IR). It reuses `kicad.symbol_geom`
for pin geometry + bboxes --- the SAME source the engine emits from --- so
detection coordinates match generation coordinates exactly (no drift between
"how a wire was drawn" and "where the linter looks for it").

Returns every key the rule set in `config/lint_rules.json` can consume:

  wires:          [((ax, ay), (bx, by))]
  bboxes:         [(ref, x1, y1, x2, y2)]            (outer body bbox, Y-down)
  pin_positions:  [(x, y, ref)]                       legacy tuple form (R2)
  pins:           [{x, y, ref, name, number, etype}]  typed form (new rules);
                  includes hierarchical SHEET pins (ref = sheet name) so a wire
                  landing on a sheet-border pin is a valid termination
  labels:         [(name, x, y)]
  label_names:    [str]
  junctions:      [(x, y)]
  no_connects:    [(x, y)]
  text_bboxes:    [(ref, field, x1, y1, x2, y2)]      RefDes/Value text boxes
  power_ports:    [(rail_name, x, y)]                 +5V/+3V3/GND/... tips
  blocks:         [(name, x1, y1, x2, y2)]            drawn functional-block boxes
  ref_to_block:   {ref: block_name}                   component -> owning block

Universal --- no per-part / per-circuit hardcoding. Symbols that fail to
resolve in the library are skipped (same tolerance as tools/audit_wires.py),
so a partial library never aborts the whole lint pass.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata


def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _sval(node: Any) -> str:
    """Atom -> real string (sexpdata wraps unquoted tokens in Symbol whose
    str() mangles specials like '/'; .value() gives the true text)."""
    if isinstance(node, sexpdata.Symbol):
        return node.value()
    return str(node)


def _first_child(node: Any, name: str) -> Optional[list]:
    if not isinstance(node, list):
        return None
    for c in node[1:]:
        if isinstance(c, list) and _head(c) == name:
            return c
    return None


def _property(sym_node: list, want: str) -> Optional[list]:
    for c in sym_node[1:]:
        if (isinstance(c, list) and _head(c) == "property"
                and len(c) >= 3 and _sval(c[1]) == want):
            return c
    return None


def _font_size(prop_node: list) -> float:
    eff = _first_child(prop_node, "effects")
    font = _first_child(eff, "font") if eff else None
    size = _first_child(font, "size") if font else None
    if size and len(size) >= 2:
        try:
            return float(size[1])
        except (TypeError, ValueError):
            return 1.27
    return 1.27


def _is_hidden(prop_node: list) -> bool:
    """A hidden field is not drawn, so it cannot be overlapped --- skip it.
    KiCad 7/8 mark hidden as a bare `hide` token inside (effects ...);
    KiCad 9 uses `(hide yes)`. Handle both."""
    eff = _first_child(prop_node, "effects")
    if eff:
        for c in eff[1:]:
            if isinstance(c, sexpdata.Symbol) and c.value() == "hide":
                return True
            if (isinstance(c, list) and _head(c) == "hide" and len(c) >= 2
                    and str(_sval(c[1])).lower() in ("yes", "true")):
                return True
    return False


# Text-geometry model — SINGLE source of truth shared by the symbol-field
# bbox (_text_bbox) and any caller that needs a box for a bare text anchor
# (net labels, power-port names) so detection and de-collision agree. The
# glyph-aspect / floor constants mirror the engine's render-time text model.
DEFAULT_TEXT_SIZE_MM = 1.27    # KiCad default field/label font size
_GLYPH_ASPECT = 0.7            # char advance as a fraction of font size
_MIN_HALF_W_FACTOR = 0.5       # min half-width floor (fraction of font size)


def text_aabb(text: str, cx: float, cy: float,
              size: float = DEFAULT_TEXT_SIZE_MM,
              rot: float = 0.0) -> Tuple[float, float, float, float]:
    """Axis-aligned bbox of one line of text centred at (cx, cy).

    Width ~= len(text) * size * _GLYPH_ASPECT (floored at a min half-width);
    height = size. A 90/270 rotation TRANSPOSES the axes — KiCad rotates the
    glyphs, so a vertical RefDes/Value occupies a tall-narrow box, not a
    wide-short one. Ignoring that mis-modelled ~15% of real-world fields
    (vertical 2-pin passives), causing both missed and phantom moves."""
    half_w = max(len(text) * size * _GLYPH_ASPECT / 2.0,
                 size * _MIN_HALF_W_FACTOR)
    half_h = size / 2.0
    if int(round(rot)) % 180 == 90:
        half_w, half_h = half_h, half_w
    return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def _text_bbox(prop_node: list, ref: str, field: str
               ) -> Optional[Tuple[str, str, float, float, float, float]]:
    """Approximate the rendered AABB of a symbol field's text. The field
    `(at x y rot)` is absolute schematic coords on a placed instance; the
    box is built by the shared `text_aabb` model (rotation-aware)."""
    if _is_hidden(prop_node):
        return None
    text = _sval(prop_node[2]) if len(prop_node) >= 3 else ""
    if not text:
        return None
    at = _first_child(prop_node, "at")
    if not at or len(at) < 3:
        return None
    try:
        px, py = float(at[1]), float(at[2])
        rot = float(at[3]) if len(at) >= 4 else 0.0
    except (TypeError, ValueError):
        return None
    h = _font_size(prop_node)
    x1, y1, x2, y2 = text_aabb(text, px, py, h, rot)
    return (ref, field, x1, y1, x2, y2)


def build_context(sch_path: Path) -> Dict[str, Any]:
    """Parse `sch_path` and return the lint context dict. Raises on a file
    that is not a kicad_sch; per-symbol library failures are swallowed."""
    from ..kicad.symbol_geom import load_symbol, place_pin

    text = Path(sch_path).read_text(encoding="utf-8")
    root = sexpdata.loads(text)
    if not isinstance(root, list) or _head(root) != "kicad_sch":
        raise ValueError("not a kicad_sch")

    bboxes: List[Tuple[str, float, float, float, float]] = []
    pin_positions: List[Tuple[float, float, str]] = []
    pins: List[Dict[str, Any]] = []
    values: Dict[str, str] = {}        # ref -> Value field (for SHORTED_COMPONENT 0R/DNP exempt)
    text_bboxes: List[Tuple[str, str, float, float, float, float]] = []
    wires: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    labels: List[Tuple[str, float, float]] = []
    label_names: List[str] = []
    junctions: List[Tuple[float, float]] = []
    no_connects: List[Tuple[float, float]] = []
    power_ports: List[Tuple[str, float, float]] = []
    # Block boxes are drawn as graphic (rectangle ...) + a "N. NAME" (text ...)
    # title; collected raw here, then matched into `blocks` after the parse.
    rect_boxes: List[Tuple[float, float, float, float]] = []
    texts: List[Tuple[str, float, float]] = []

    for child in root[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)

        if h == "symbol":
            ref = lib_id = value = ""
            atx = aty = atrot = 0.0
            mirror: Optional[str] = None
            unit = 1
            for c in child[1:]:
                if not isinstance(c, list):
                    continue
                ch = _head(c)
                if ch == "lib_id" and len(c) >= 2:
                    lib_id = _sval(c[1])
                elif ch == "at":
                    try:
                        atx = float(c[1]); aty = float(c[2])
                        if len(c) >= 4:
                            atrot = float(c[3])
                    except (TypeError, ValueError):
                        pass
                elif ch == "mirror" and len(c) >= 2:
                    m = _sval(c[1])
                    mirror = m if m in ("x", "y") else None
                elif ch == "unit" and len(c) >= 2:
                    try:
                        unit = int(c[1])
                    except (TypeError, ValueError):
                        unit = 1
                elif (ch == "property" and len(c) >= 3
                        and _sval(c[1]) == "Reference"):
                    ref = _sval(c[2])
                elif (ch == "property" and len(c) >= 3
                        and _sval(c[1]) == "Value"):
                    value = _sval(c[2])
            if not ref or not lib_id:
                continue
            values[ref] = value
            try:
                g = load_symbol(lib_id)
            except Exception:
                continue

            # Power-port / PWR_FLAG symbols use KiCad's '#'-prefixed refs
            # (#PWR*, #FLG*) by convention. They carry a real connection PIN
            # but no meaningful drawn body. Register their pin --- a wire
            # endpoint landing on a +5V/+3V3/GND/PWR_FLAG tip is CONNECTED, not
            # dangling --- while skipping their bbox/text (no body to pierce,
            # no RefDes/Value text to overlap). Dropping these symbols outright
            # is what made find_dangling_wire_endpoints raise false positives
            # on every power net. Dynamic: keys off the '#' ref convention, not
            # a hardcoded list of power-net names.
            is_power_port = ref.startswith("#")

            if not is_power_port:
                # Outer bbox (Y-down, rotated) --- identical maths to
                # tools/audit_wires.py so R2 behaves the same here.
                x1, y1, x2, y2 = g.outer_bbox
                corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
                corners = [(x, -y) for x, y in corners]
                rad = math.radians(atrot)
                cos_r, sin_r = math.cos(rad), math.sin(rad)
                corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r)
                           for x, y in corners]
                corners = [(atx + x, aty + y) for x, y in corners]
                xs = [p[0] for p in corners]; ys = [p[1] for p in corners]
                bboxes.append((ref, min(xs), min(ys), max(xs), max(ys)))

            for pin in g.pins:
                if pin.unit not in (0, unit):
                    continue
                px, py, _r = place_pin(pin, atx, aty, atrot, mirror=mirror)
                pin_positions.append((px, py, ref))
                pins.append({
                    "x": px, "y": py, "ref": ref,
                    "name": pin.name, "number": pin.number,
                    "etype": pin.etype,
                })
                # Power ports carry the rail NAME in their Value (+3V3, GND,
                # VIN, ...). Record (rail, tip) so find_power_rail_shorts can
                # detect two DIFFERENT rails landing on the same net (a short).
                # PWR_FLAG is a net-driver marker, not a rail --- it is allowed
                # to share any net, so the short selector ignores it.
                if is_power_port and value:
                    power_ports.append((value, px, py))

            if not is_power_port:
                for field in ("Reference", "Value"):
                    pn = _property(child, field)
                    if pn is not None:
                        tb = _text_bbox(pn, ref, field)
                        if tb is not None:
                            text_bboxes.append(tb)

        elif h == "wire":
            pts = _first_child(child, "pts")
            if not pts:
                continue
            try:
                ax, ay = float(pts[1][1]), float(pts[1][2])
                bx, by = float(pts[2][1]), float(pts[2][2])
            except (IndexError, TypeError, ValueError):
                continue
            wires.append(((ax, ay), (bx, by)))

        elif h in ("label", "global_label", "hierarchical_label"):
            name = _sval(child[1]) if len(child) >= 2 else ""
            at = _first_child(child, "at")
            if name and at and len(at) >= 3:
                try:
                    labels.append((name, float(at[1]), float(at[2])))
                    label_names.append(name)
                except (TypeError, ValueError):
                    pass

        elif h == "junction":
            at = _first_child(child, "at")
            if at and len(at) >= 3:
                try:
                    junctions.append((float(at[1]), float(at[2])))
                except (TypeError, ValueError):
                    pass

        elif h == "no_connect":
            at = _first_child(child, "at")
            if at and len(at) >= 3:
                try:
                    no_connects.append((float(at[1]), float(at[2])))
                except (TypeError, ValueError):
                    pass

        elif h == "sheet":
            # Hierarchical sheet INSTANCE on the parent sheet. Its `(pin ...)`
            # children are the connection terminals on the sheet border --- a
            # wire landing on one is CONNECTED, not dangling. Without this the
            # linter never sees them, so every wire terminating on a sheet pin
            # false-flagged as WIRE_DANGLING and every sheet pin was invisible
            # to PIN_UNCONNECTED (the hierarchy-top-sheet false-positive class).
            # Sheet pin coords in `(at x y rot)` are ABSOLUTE schematic coords
            # (already on the border), so no symbol transform is needed. The
            # sheet body itself is a border box, not a component --- no bbox /
            # RefDes text is registered (mirrors the power-port handling above).
            sheet_name = ""
            for c in child[1:]:
                if (isinstance(c, list) and _head(c) == "property"
                        and len(c) >= 3 and _sval(c[1]) == "Sheetname"):
                    sheet_name = _sval(c[2])
                    break
            for c in child[1:]:
                if not (isinstance(c, list) and _head(c) == "pin" and len(c) >= 2):
                    continue
                pname = _sval(c[1])
                # KiCad stores the sheet-pin direction as the 2nd atom
                # (input / output / bidirectional / tri_state / passive).
                petype = "passive"
                if len(c) >= 3 and isinstance(c[2], sexpdata.Symbol):
                    petype = c[2].value()
                at = _first_child(c, "at")
                if not (at and len(at) >= 3):
                    continue
                try:
                    px, py = float(at[1]), float(at[2])
                except (TypeError, ValueError):
                    continue
                sref = sheet_name or "SHEET"
                pin_positions.append((px, py, sref))
                pins.append({
                    "x": px, "y": py, "ref": sref,
                    "name": pname, "number": pname, "etype": petype,
                })

        elif h == "rectangle":
            # Functional-block box (intent/engine.py:_emit_block_rectangle).
            start = _first_child(child, "start")
            end = _first_child(child, "end")
            if start and end and len(start) >= 3 and len(end) >= 3:
                try:
                    sx, sy = float(start[1]), float(start[2])
                    ex, ey = float(end[1]), float(end[2])
                    rect_boxes.append((min(sx, ex), min(sy, ey),
                                       max(sx, ex), max(sy, ey)))
                except (TypeError, ValueError):
                    pass

        elif h == "text":
            # Free graphic text — block titles ("N. NAME") live here.
            txt = _sval(child[1]) if len(child) >= 2 else ""
            at = _first_child(child, "at")
            if txt and at and len(at) >= 3:
                try:
                    texts.append((txt, float(at[1]), float(at[2])))
                except (TypeError, ValueError):
                    pass

    # ---- Functional blocks: recover the drawn rectangles + their titles ----
    # Each block is a (rectangle ...) plus a "N. NAME" title anchored just
    # outside its top-left corner (offset ~5 mm right, ~4 mm above the top
    # edge). Matching them back lets the wire-vs-label rule (R16) be VERIFIED
    # on any file: a drawn wire crossing a block boundary should be a label.
    # Purely geometric — no IR, refdes, or net-name knowledge required.
    blocks: List[Tuple[str, float, float, float, float]] = []
    for (x1, y1, x2, y2) in rect_boxes:
        name = ""
        best_d: Optional[float] = None
        for txt, tx, ty in texts:
            if tx < x1 - 2.0 or tx > x1 + 20.0:
                continue
            if ty < y1 - 12.0 or ty > y1 + 6.0:
                continue
            d = abs(tx - x1) + abs(ty - y1)
            if best_d is None or d < best_d:
                best_d = d
                name = re.sub(r"^\s*\d+[.)]\s*", "", txt).strip()
        blocks.append((name, x1, y1, x2, y2))

    # ref -> owning block: component-centroid inside a block rectangle; the
    # smallest containing rect wins so the attribution stays unambiguous.
    ref_to_block: Dict[str, str] = {}
    for ref, bx1, by1, bx2, by2 in bboxes:
        cx, cy = (bx1 + bx2) / 2.0, (by1 + by2) / 2.0
        best_area: Optional[float] = None
        for bname, rx1, ry1, rx2, ry2 in blocks:
            if rx1 <= cx <= rx2 and ry1 <= cy <= ry2:
                area = (rx2 - rx1) * (ry2 - ry1)
                if best_area is None or area < best_area:
                    best_area = area
                    ref_to_block[ref] = bname

    return {
        "wires": wires,
        "bboxes": bboxes,
        "pin_positions": pin_positions,
        "pins": pins,
        "values": values,
        "labels": labels,
        "label_names": label_names,
        "junctions": junctions,
        "no_connects": no_connects,
        "text_bboxes": text_bboxes,
        "power_ports": power_ports,
        "blocks": blocks,
        "ref_to_block": ref_to_block,
    }
