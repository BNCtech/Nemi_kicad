"""Post-render schematic mutators.

The lint engine in `lint/engine.py` is read-only --- it produces issue
lists. This module pairs with it: it APPLIES fixes by mutating a
.kicad_sch in place. Each mutator is idempotent (safe to call after
every render) and gated by a `wiring_rules.*` flag in
`layout_config.json`.

Current mutators:
  - `split_four_way_junctions(sch_path)` --- R4 from WIRING_RULES.md.
    Finds grid points where 4+ wire endpoints meet, shifts one wire
    perpendicular by one grid cell, inserts a short connector wire.
    Result: two T-junctions where there was one ambiguous 4-way.

Non-breaking: every entry point checks the config flag first and is a
no-op when disabled. The caller (graphs/nodes/render.py) calls
`repair_after_render(sch_path, ir=None)` which dispatches to whichever
rules are turned on. Adding a new mutator = one function here + one
config key in `wiring_rules` + one dispatch line in
`repair_after_render`.
"""
from __future__ import annotations

import json
import math
import re
import uuid as _uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata


_GRID_MM = 1.27
_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "layout_config.json"
)


def _load_wiring_rules() -> dict:
    try:
        return (json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                .get("wiring_rules") or {})
    except (OSError, ValueError):
        return {}


def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


# ----- R4: split 4-way wire junctions into two T-junctions ----------------

_WIRE_PTS_RE = re.compile(
    r"\(pts\s*\(xy\s+([-0-9.]+)\s+([-0-9.]+)\)\s*"
    r"\(xy\s+([-0-9.]+)\s+([-0-9.]+)\)\s*\)",
    re.DOTALL,
)
_UUID_RE = re.compile(r"\(uuid\s+\"?([0-9a-fA-F-]+)\"?\)")


def _find_balanced_spans(text: str, opener: str) -> List[Tuple[int, int]]:
    """Find every `(<opener>...)` block via balanced-paren walking.
    Returns [(start, end_exclusive), ...]. Robust against nested
    `(stroke (width 0) (type default))` etc. that regex can't handle.

    `opener` is the s-expr head, e.g. "wire", "label", "bus", "bus_entry".
    Matches the literal `(<opener>` with optional whitespace after the
    opening paren --- anchored to a `(` boundary so we don't false-match
    inside a string literal.
    """
    spans: List[Tuple[int, int]] = []
    needle = "(" + opener
    pos = 0
    n = len(text)
    while True:
        idx = text.find(needle, pos)
        if idx < 0:
            break
        # The next char must be whitespace or `(` so we don't match
        # "wires" or "wireguide" by accident.
        nxt = text[idx + len(needle)] if idx + len(needle) < n else ""
        if nxt not in (" ", "\t", "\n", "\r", "("):
            pos = idx + len(needle)
            continue
        # Walk balanced parens forward from idx.
        depth = 0
        i = idx
        end = -1
        in_string = False
        while i < n:
            c = text[i]
            if c == '"':
                in_string = not in_string
            elif not in_string:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            i += 1
        if end < 0:
            break
        spans.append((idx, end))
        pos = end
    return spans


def _find_four_way_points(
    wires: List[Tuple[float, float, float, float]],
) -> Dict[Tuple[float, float], List[int]]:
    """Return a map of {grid_point: [wire_index, ...]} for points with
    4 or more wire endpoints meeting."""
    by_point: Dict[Tuple[float, float], List[int]] = defaultdict(list)
    for idx, (ax, ay, bx, by) in enumerate(wires):
        by_point[(round(ax, 3), round(ay, 3))].append(idx)
        by_point[(round(bx, 3), round(by, 3))].append(idx)
    return {pt: idxs for pt, idxs in by_point.items() if len(idxs) >= 4}


def _extract_wires(text: str) -> Tuple[List[Tuple[float, float, float, float]],
                                         List[Tuple[int, int, Optional[str]]]]:
    """Walk `text`, find every (wire ...) span via balanced-paren parsing,
    extract its two coords + uuid. Returns parallel lists:
      coords:   [(ax, ay, bx, by), ...]
      spans:    [(start_offset, end_offset, uuid_or_None), ...]
    """
    coords: List[Tuple[float, float, float, float]] = []
    spans: List[Tuple[int, int, Optional[str]]] = []
    for start, end in _find_balanced_spans(text, "wire"):
        chunk = text[start:end]
        m_pts = _WIRE_PTS_RE.search(chunk)
        if not m_pts:
            continue
        try:
            coords.append((
                float(m_pts.group(1)), float(m_pts.group(2)),
                float(m_pts.group(3)), float(m_pts.group(4)),
            ))
        except (TypeError, ValueError):
            continue
        m_uuid = _UUID_RE.search(chunk)
        uuid_val = m_uuid.group(1) if m_uuid else None
        spans.append((start, end, uuid_val))
    return coords, spans


_JUNCTION_RE = re.compile(
    r"\(junction\s*\(at\s+([-0-9.]+)\s+([-0-9.]+)\)"
)


def _existing_junction_points(text: str) -> set:
    out = set()
    for m in _JUNCTION_RE.finditer(text):
        try:
            out.add((round(float(m.group(1)), 3),
                     round(float(m.group(2)), 3)))
        except (TypeError, ValueError):
            continue
    return out


def split_four_way_junctions(sch_path: Path) -> Dict[str, Any]:
    """R4 enforcement (conservative variant).

    Identifies every grid point where 4+ wire endpoints meet AND no
    junction dot is present, then ADDS a junction dot so the
    connection is electrically unambiguous. The full "split into two
    T-junctions by shifting one wire" mutation is NOT safe to apply
    without electrical-net awareness (would either reintroduce the
    4-way via the connector wire, or break connectivity by shifting
    a wire whose far end is the only path to the rest of the net),
    so v1 stops at the dot-adding pass.

    Returns {"junctions_added": N, "four_ways_total": M,
             "four_ways_already_dotted": K, "remaining_undotted": L,
             "ok": bool}.

    Idempotent --- a second call sees the dot we just added and is a
    no-op. Safe to call after every render."""
    text = sch_path.read_text(encoding="utf-8")
    wires, _spans = _extract_wires(text)
    four_ways = _find_four_way_points(wires)
    if not four_ways:
        return {
            "junctions_added": 0,
            "four_ways_total": 0,
            "four_ways_already_dotted": 0,
            "remaining_undotted": 0,
            "ok": True,
        }

    existing_junctions = _existing_junction_points(text)
    undotted = [
        pt for pt in four_ways.keys() if pt not in existing_junctions
    ]
    already_dotted = len(four_ways) - len(undotted)
    if not undotted:
        return {
            "junctions_added": 0,
            "four_ways_total": len(four_ways),
            "four_ways_already_dotted": already_dotted,
            "remaining_undotted": 0,
            "ok": True,
        }

    def _num(v):
        return f"{v:g}"
    additions = "".join(
        f"\t(junction (at {_num(x)} {_num(y)}) (diameter 0) (color 0 0 0 0)\n"
        f"\t\t(uuid \"{_uuid.uuid4()}\")\n"
        "\t)\n"
        for (x, y) in undotted
    )
    last_paren = text.rfind(")")
    if last_paren < 0:
        return {
            "junctions_added": 0,
            "four_ways_total": len(four_ways),
            "four_ways_already_dotted": already_dotted,
            "remaining_undotted": len(undotted),
            "ok": False,
        }
    new_text = text[:last_paren] + "\n" + additions + text[last_paren:]
    sch_path.write_text(new_text, encoding="utf-8")
    return {
        "junctions_added": len(undotted),
        "four_ways_total": len(four_ways),
        "four_ways_already_dotted": already_dotted,
        "remaining_undotted": 0,
        "ok": True,
    }


# ----- R9: basic bus emission ---------------------------------------------

def emit_buses(sch_path: Path, ir_buses: List[Any]) -> Dict[str, Any]:
    """Post-render bus rendering for a known set of BusDefs.

    For each BusDef whose members are all present as collinear labels
    on the schematic, replace them with:
      - one bus segment (a `(bus ...)` line) running parallel to the
        column of labels, offset by one grid cell;
      - one bus_entry per member at the wire-end pointing to the bus;
      - one local label at the end of the bus with the canonical name
        (`DATA[0..7]`).

    Falls back to a no-op for any BusDef whose members are not all
    present or are not collinear --- the architect's IR may declare a
    bus that doesn't make sense post-render and we don't want to leave
    the file half-mutated.

    Returns {"buses_emitted": N, "skipped": K, "ok": bool}.

    NOTE: v1 supports only the simplest layout (members are a vertical
    column on the right side of an IC, all labels share the same outward
    axis). More exotic layouts fall back gracefully.
    """
    if not ir_buses:
        return {"buses_emitted": 0, "skipped": 0, "ok": True}

    text = sch_path.read_text(encoding="utf-8")

    # Find each (label ...) via balanced-paren walking, then parse the
    # name + (at ...) coords from inside the chunk. Regex alone can't
    # handle the nested `(effects (font (size ...)) (justify ...))`
    # block, so we rely on the paren walker for span detection.
    label_name_re = re.compile(r"\(label\s+\"([^\"]+)\"")
    label_at_re = re.compile(
        r"\(at\s+([-0-9.]+)\s+([-0-9.]+)(?:\s+([-0-9.]+))?\)"
    )
    labels: List[Tuple[str, float, float, int, int]] = []
    for start, end in _find_balanced_spans(text, "label"):
        chunk = text[start:end]
        m_name = label_name_re.search(chunk)
        m_at = label_at_re.search(chunk)
        if not m_name or not m_at:
            continue
        try:
            labels.append((
                m_name.group(1),
                float(m_at.group(1)),
                float(m_at.group(2)),
                start,
                end,
            ))
        except (TypeError, ValueError):
            continue
    if not labels:
        return {"buses_emitted": 0, "skipped": len(ir_buses), "ok": True}

    label_by_name: Dict[str, Tuple[float, float, int, int]] = {
        name: (x, y, s, e) for name, x, y, s, e in labels
    }

    insertions: List[str] = []
    removals: List[Tuple[int, int]] = []
    emitted = 0
    skipped = 0

    for bus in ir_buses:
        members = list(getattr(bus, "members", []) or [])
        if len(members) < 4:
            skipped += 1
            continue
        positions: List[Tuple[float, float, int, int]] = []
        missing = False
        for name in members:
            entry = label_by_name.get(name)
            if entry is None:
                missing = True
                break
            positions.append(entry)
        if missing:
            skipped += 1
            continue

        # Collinearity check: vertical column (all x equal) OR horizontal
        # row (all y equal). Use a 0.5 mm tolerance to allow for rounding.
        xs = [p[0] for p in positions]
        ys = [p[1] for p in positions]
        vertical_column = max(xs) - min(xs) < 0.5
        horizontal_row = max(ys) - min(ys) < 0.5
        if not (vertical_column or horizontal_row):
            skipped += 1
            continue

        # Place the bus line one grid cell further from the labels
        # (along the same outward axis). For a vertical column we offset
        # in x; for a horizontal row we offset in y. Direction chosen to
        # keep the bus on the OUTSIDE of the label column (the side away
        # from the pins). Heuristic: pick the direction with the larger
        # absolute coordinate (labels at high x -> bus farther right).
        if vertical_column:
            label_x = xs[0]
            bus_x = label_x + _GRID_MM
            y_lo = min(ys) - _GRID_MM
            y_hi = max(ys) + _GRID_MM
            bus_p1 = (bus_x, y_lo)
            bus_p2 = (bus_x, y_hi)
            entry_dx = _GRID_MM
            entry_dy = 0.0
            bus_label_pos = (bus_x, y_lo)
        else:
            label_y = ys[0]
            bus_y = label_y + _GRID_MM
            x_lo = min(xs) - _GRID_MM
            x_hi = max(xs) + _GRID_MM
            bus_p1 = (x_lo, bus_y)
            bus_p2 = (x_hi, bus_y)
            entry_dx = 0.0
            entry_dy = _GRID_MM
            bus_label_pos = (x_lo, bus_y)

        # Emit the bus + bus_entries + bus label.
        def _num(v):
            return f"{v:g}"
        bus_segment = (
            f"\t(bus (pts (xy {_num(bus_p1[0])} {_num(bus_p1[1])}) "
            f"(xy {_num(bus_p2[0])} {_num(bus_p2[1])}))\n"
            "\t\t(stroke (width 0) (type default))\n"
            f"\t\t(uuid \"{_uuid.uuid4()}\")\n"
            "\t)\n"
        )
        insertions.append(bus_segment)
        for (x, y, s, e) in positions:
            be = (
                f"\t(bus_entry (at {_num(x)} {_num(y)}) "
                f"(size {_num(entry_dx)} {_num(entry_dy)})\n"
                "\t\t(stroke (width 0) (type default))\n"
                f"\t\t(uuid \"{_uuid.uuid4()}\")\n"
                "\t)\n"
            )
            insertions.append(be)
            # Remove the per-net label that the engine emitted.
            removals.append((s, e))

        # One canonical bus label at the start of the bus segment.
        canonical = bus.canonical_name() if hasattr(bus, "canonical_name") else str(getattr(bus, "name", ""))
        bus_label = (
            f"\t(label \"{canonical}\" (at {_num(bus_label_pos[0])} "
            f"{_num(bus_label_pos[1])} 0)\n"
            "\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n"
            f"\t\t(uuid \"{_uuid.uuid4()}\")\n"
            "\t)\n"
        )
        insertions.append(bus_label)
        emitted += 1

    if not insertions:
        return {"buses_emitted": 0, "skipped": skipped, "ok": True}

    # Apply removals right-to-left.
    removals.sort(key=lambda r: r[0], reverse=True)
    new_text = text
    for s, e in removals:
        # Trim trailing newline if present so we don't leave blank lines.
        end = e
        if end < len(new_text) and new_text[end] == "\n":
            end += 1
        new_text = new_text[:s] + new_text[end:]

    # Insert before the closing `)` of (kicad_sch ...).
    last_paren = new_text.rfind(")")
    if last_paren < 0:
        return {"buses_emitted": 0, "skipped": skipped + emitted, "ok": False}
    insertion_text = "\n" + "".join(insertions)
    new_text = new_text[:last_paren] + insertion_text + new_text[last_paren:]
    sch_path.write_text(new_text, encoding="utf-8")
    return {"buses_emitted": emitted, "skipped": skipped, "ok": True}


# ----- R4 (general): junction dot at every multi-wire meet ----------------

def add_missing_junction_dots(sch_path: Path) -> Dict[str, Any]:
    """R4 general enforcement --- the SUPERSET of split_four_way_junctions.

    Adds an explicit `(junction)` dot at EVERY wire meet that needs one and
    lacks it:
      - 3+ wire endpoints meeting (T or X),
      - 4+ way meets (the old split_four_way_junctions subset),
      - mid-span T-taps (a wire endpoint landing on another wire's interior).

    The T-tap case is a real CONNECTIVITY repair, not cosmetics: in KiCad a
    wire ending on another wire's mid-span WITHOUT a junction dot is
    electrically OPEN, so those pins never join the net. Pure 2-wire crossings
    are never dotted --- the detection (lint.selectors.find_missing_junction_dots)
    excludes them --- so unrelated nets are never merged.

    Dynamic: keys off wire geometry alone, so it works on any schematic
    (engine-generated, apply_ops-edited, or hand-drawn). Idempotent: a second
    call sees the dots it added and is a no-op.

    Returns {"junctions_added": N, "points_needing_dots": M, "ok": bool}.
    """
    from .context import build_context
    from .selectors import find_missing_junction_dots

    try:
        ctx = build_context(sch_path)
    except Exception as exc:
        return {"junctions_added": 0, "points_needing_dots": 0,
                "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    needed = find_missing_junction_dots(ctx["wires"], ctx["junctions"])
    pts = [(iss["where"]["x"], iss["where"]["y"]) for iss in needed]
    if not pts:
        return {"junctions_added": 0, "points_needing_dots": 0, "ok": True}

    text = sch_path.read_text(encoding="utf-8")

    def _num(v):
        return f"{v:g}"
    additions = "".join(
        f"\t(junction (at {_num(x)} {_num(y)}) (diameter 0) (color 0 0 0 0)\n"
        f"\t\t(uuid \"{_uuid.uuid4()}\")\n"
        "\t)\n"
        for (x, y) in pts
    )
    last_paren = text.rfind(")")
    if last_paren < 0:
        return {"junctions_added": 0, "points_needing_dots": len(pts),
                "ok": False}
    new_text = text[:last_paren] + "\n" + additions + text[last_paren:]
    sch_path.write_text(new_text, encoding="utf-8")
    return {"junctions_added": len(pts), "points_needing_dots": len(pts),
            "ok": True}


# ----- R11_TEXT: move symbol field text out from under wires --------------

def _num(v: float) -> str:
    return f"{v:g}"


_FIELD_AT_RE = re.compile(
    r"\(at\s+(-?[0-9.]+)\s+(-?[0-9.]+)(\s+-?[0-9.]+)?\)"
)
_PROP_NAME_RE = re.compile(r"\(property\s+\"([^\"]+)\"")
_SYM_REF_RE = re.compile(r"\(property\s+\"Reference\"\s+\"([^\"]+)\"")


def _set_field_at(text: str, ref: str, field: str,
                  nx: float, ny: float) -> Tuple[str, bool]:
    """Rewrite the `(at x y rot)` of `ref`'s `field` (Reference/Value)
    property to (nx, ny), preserving the rotation token. Returns
    (new_text, moved). Walks (symbol ...) spans, matches the instance by
    its Reference property, then the target property by name --- balanced
    parens so the nested (effects (font ...)) never confuses the match.
    Touches ONLY the text anchor: no pin, wire, junction or net moves, so
    connectivity is byte-for-byte unchanged."""
    for s, e in _find_balanced_spans(text, "symbol"):
        chunk = text[s:e]
        m_ref = _SYM_REF_RE.search(chunk)
        if not m_ref or m_ref.group(1) != ref:
            continue
        for ps, pe in _find_balanced_spans(chunk, "property"):
            pchunk = chunk[ps:pe]
            m_name = _PROP_NAME_RE.match(pchunk)
            if not m_name or m_name.group(1) != field:
                continue
            m_at = _FIELD_AT_RE.search(pchunk)
            if not m_at:
                return text, False
            rot = m_at.group(3) or ""
            new_at = f"(at {_num(nx)} {_num(ny)}{rot})"
            new_pchunk = pchunk[:m_at.start()] + new_at + pchunk[m_at.end():]
            new_chunk = chunk[:ps] + new_pchunk + chunk[pe:]
            return text[:s] + new_chunk + text[e:], True
        return text, False
    return text, False


def clear_wires_over_text(sch_path: Path) -> Dict[str, Any]:
    """R11_TEXT enforcement (WIRING_RULES.md post-render checklist #8).

    A wire that runs across a component's Reference or Value text obscures
    it ('wire overlaps symbol text. Avoid this.'). The router's obstacle
    set is body-only (`_abs_outer_bbox`), so it never avoided the field
    text in the first place, and `find_wires_over_component_text` only
    WARNS. This repair RESOLVES the warning the KLC-S5 way: it repositions
    the obscured text, never the wire.

    For each (ref, field) the detector flags, the field anchor is nudged
    outward in 1-grid steps (4 cardinal directions, nearest-clear wins)
    until its text bbox clears EVERY wire and every OTHER component body.
    No clear spot inside the search radius -> the field is left untouched
    (an overlap is better than text flung across the sheet).

    Connectivity-safe (moves only a `(property ... (at))` anchor),
    idempotent (a relocated field no longer overlaps -> second call is a
    no-op), and byte-stable on a schematic with no wire-over-text.

    Returns {"fields_moved": N, "overlaps_found": M, "unresolved": K,
             "ok": bool}.
    """
    from .context import build_context, text_aabb, DEFAULT_TEXT_SIZE_MM
    from .selectors import find_wires_over_component_text

    rules = _load_wiring_rules()
    step = float(rules.get("r11_text_clear_step_mm", _GRID_MM))
    max_tries = int(rules.get("r11_text_clear_max_tries", 6))
    margin = float(rules.get("r11_text_clear_margin_mm", 0.3))

    try:
        ctx = build_context(sch_path)
    except Exception as exc:
        return {"fields_moved": 0, "overlaps_found": 0, "unresolved": 0,
                "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    wires = ctx["wires"]
    text_bboxes = ctx["text_bboxes"]      # (ref, field, x1, y1, x2, y2)
    bodies = ctx["bboxes"]                # (ref, x1, y1, x2, y2)
    # Text obstacles the relocated anchor must ALSO clear, so a moved
    # RefDes/Value never lands on a net label, a power-port name, or a
    # neighbour/sibling field (the text-on-text collision the unified-ink
    # invariant exists to prevent). Net labels and power ports are bare
    # anchor points in the context -> synthesize a box with the shared
    # rotation-aware text model.
    label_boxes = [text_aabb(nm, x, y, DEFAULT_TEXT_SIZE_MM)
                   for (nm, x, y) in ctx.get("labels", [])]
    port_boxes = [text_aabb(rail, x, y, DEFAULT_TEXT_SIZE_MM)
                  for (rail, x, y) in ctx.get("power_ports", [])]

    overlaps = find_wires_over_component_text(wires, text_bboxes)
    if not overlaps:
        return {"fields_moved": 0, "overlaps_found": 0, "unresolved": 0,
                "ok": True}

    bbox_by_key: Dict[Tuple[str, str],
                      Tuple[float, float, float, float]] = {
        (r, f): (x1, y1, x2, y2)
        for (r, f, x1, y1, x2, y2) in text_bboxes
    }

    # Distinct (ref, field) anchors to move (the detector may report the
    # same field once per crossing wire).
    keys: List[Tuple[str, str]] = []
    seen: set = set()
    for iss in overlaps:
        over = (iss.get("where") or {}).get("over", "")
        if "." not in over:
            continue
        r, f = over.split(".", 1)
        if (r, f) in seen:
            continue
        seen.add((r, f))
        keys.append((r, f))

    def _hits_wire(x1, y1, x2, y2) -> bool:
        for (ax, ay), (bx, by) in wires:
            wx1, wx2 = (ax, bx) if ax <= bx else (bx, ax)
            wy1, wy2 = (ay, by) if ay <= by else (by, ay)
            if (x1 - margin <= wx2 and x2 + margin >= wx1
                    and y1 - margin <= wy2 and y2 + margin >= wy1):
                return True
        return False

    def _hits_other_body(x1, y1, x2, y2, own_ref) -> bool:
        for (br, rx1, ry1, rx2, ry2) in bodies:
            if br == own_ref:
                continue
            # Same configured clearance as _hits_wire so the one knob
            # (r11_text_clear_margin_mm) governs every obstacle uniformly.
            if (x1 - margin <= rx2 and x2 + margin >= rx1
                    and y1 - margin <= ry2 and y2 + margin >= ry1):
                return True
        return False

    def _aabb_hit(b, c) -> bool:
        return (b[0] - margin <= c[2] and b[2] + margin >= c[0]
                and b[1] - margin <= c[3] and b[3] + margin >= c[1])

    # Fields already relocated in THIS pass — a later sibling must not be
    # dropped onto one of them (the two-fields-same-direction pile-up).
    claimed: List[Tuple[float, float, float, float]] = []

    def _hits_text(nb, own_key) -> bool:
        for k, bx in bbox_by_key.items():
            if k == own_key:                 # the field being moved
                continue
            if _aabb_hit(nb, bx):
                return True
        for bx in label_boxes:
            if _aabb_hit(nb, bx):
                return True
        for bx in port_boxes:
            if _aabb_hit(nb, bx):
                return True
        for bx in claimed:
            if _aabb_hit(nb, bx):
                return True
        return False

    moves: Dict[Tuple[str, str], Tuple[float, float]] = {}
    unresolved = 0
    for (r, f) in keys:
        bb = bbox_by_key.get((r, f))
        if bb is None:
            unresolved += 1
            continue
        x1, y1, x2, y2 = bb
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        hw, hh = (x2 - x1) / 2.0, (y2 - y1) / 2.0
        found: Optional[Tuple[float, float]] = None
        for k in range(1, max_tries + 1):
            for (dx, dy) in ((0, -1), (0, 1), (1, 0), (-1, 0)):
                nx, ny = cx + dx * step * k, cy + dy * step * k
                nb = (nx - hw, ny - hh, nx + hw, ny + hh)
                if (not _hits_wire(*nb)
                        and not _hits_other_body(*nb, own_ref=r)
                        and not _hits_text(nb, (r, f))):
                    found = (nx, ny)
                    claimed.append(nb)
                    break
            if found:
                break
        if found:
            moves[(r, f)] = found
        else:
            unresolved += 1

    if not moves:
        return {"fields_moved": 0, "overlaps_found": len(keys),
                "unresolved": unresolved, "ok": True}

    text = sch_path.read_text(encoding="utf-8")
    moved = 0
    for (r, f), (nx, ny) in moves.items():
        new_text, ok = _set_field_at(text, r, f, nx, ny)
        if ok:
            text = new_text
            moved += 1
        else:
            unresolved += 1
    if moved:
        sch_path.write_text(text, encoding="utf-8")
    return {"fields_moved": moved, "overlaps_found": len(keys),
            "unresolved": unresolved, "ok": True}


# ----- top-level dispatcher used by graphs/nodes/render.py ----------------

def repair_after_render(
    sch_path: Path, ir: Optional[Any] = None
) -> Dict[str, Any]:
    """Single entry point the render node calls. Reads the wiring_rules
    config and dispatches to whichever mutators are enabled. Each
    mutator is idempotent and safe to skip --- this function returns a
    summary dict combining each mutator's result."""
    rules = _load_wiring_rules()
    summary: Dict[str, Any] = {"ran": [], "skipped": []}
    if not sch_path.exists():
        return summary

    # R4 general: add a junction dot at every 3+ meet AND mid-span T-tap.
    # Superset of r4_split_four_way (4-way only) --- repairs electrically-open
    # T-taps too. Default on once implemented (idempotent + electrically safe).
    if bool(rules.get("r4_add_junction_dots", True)):
        try:
            r4d_res = add_missing_junction_dots(sch_path)
            summary["ran"].append({"rule": "R4_DOTS", "result": r4d_res})
        except Exception as exc:
            summary["skipped"].append({
                "rule": "R4_DOTS",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    # R11_TEXT: relocate any Reference/Value text a wire runs across.
    # Text-only move -> connectivity unchanged. Idempotent; no-op when no
    # wire crosses a field. Default on (safe, matches r4_add_junction_dots).
    if bool(rules.get("r11_move_text_off_wire", True)):
        try:
            r11t_res = clear_wires_over_text(sch_path)
            summary["ran"].append({"rule": "R11_TEXT", "result": r11t_res})
        except Exception as exc:
            summary["skipped"].append({
                "rule": "R11_TEXT",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    if bool(rules.get("r4_split_four_way", False)):
        try:
            r4_res = split_four_way_junctions(sch_path)
            summary["ran"].append({"rule": "R4", "result": r4_res})
        except Exception as exc:
            summary["skipped"].append({
                "rule": "R4",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    if bool(rules.get("r9_buses_enabled", False)):
        ir_buses = list(getattr(ir, "buses", []) or []) if ir is not None else []
        if ir_buses:
            try:
                r9_res = emit_buses(sch_path, ir_buses)
                summary["ran"].append({"rule": "R9", "result": r9_res})
            except Exception as exc:
                summary["skipped"].append({
                    "rule": "R9",
                    "reason": f"{type(exc).__name__}: {exc}",
                })
        else:
            summary["skipped"].append({
                "rule": "R9",
                "reason": "IR has no buses[] declarations",
            })

    return summary
