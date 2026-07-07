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


def _load_electrical_integrity() -> dict:
    """`electrical_integrity.*` gates (C1 net-aware junctions, C2 pin
    completeness). All default OFF so behaviour is byte-identical until a
    project opts in."""
    try:
        return (json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                .get("electrical_integrity") or {})
    except (OSError, ValueError):
        return {}


def _load_layout_section(name: str) -> dict:
    try:
        return (json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                .get(name) or {})
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

def add_missing_junction_dots(sch_path: Path,
                              net_aware: bool = False) -> Dict[str, Any]:
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

    `net_aware` (C1): when True, any candidate point whose wires resolve to two
    DIFFERENT named nets is NOT dotted --- dotting it would short two circuits
    that merely landed on top of each other. Those points are returned under
    `cross_net_skipped` so the build can flag / re-place them. Default False
    keeps the pure-geometry behaviour byte-identical.

    Returns {"junctions_added": N, "points_needing_dots": M, "ok": bool}
    (plus "cross_net_skipped" when net_aware).
    """
    from .context import build_context
    from .selectors import find_missing_junction_dots, find_cross_net_touches
    from .selectors import _pt_eq as _pteq

    try:
        ctx = build_context(sch_path)
    except Exception as exc:
        return {"junctions_added": 0, "points_needing_dots": 0,
                "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    needed = find_missing_junction_dots(ctx["wires"], ctx["junctions"])
    pts = [(iss["where"]["x"], iss["where"]["y"]) for iss in needed]

    cross_net: List[Dict[str, Any]] = []
    if net_aware:
        touches = find_cross_net_touches(
            ctx["wires"], ctx.get("labels"), ctx.get("power_ports"),
            ctx.get("pins"), ctx["junctions"])
        # Only "meet" touches carry a point to withhold a dot from; "overlap"
        # touches have no dot to suppress (they need physical separation) but
        # are still surfaced.
        block = [(t["where"]["x"], t["where"]["y"]) for t in touches
                 if t.get("where", {}).get("x") is not None
                 and t.get("where", {}).get("kind") == "meet"]
        cross_net = touches
        if block:
            pts = [p for p in pts
                   if not any(_pteq(p[0], p[1], bx, by, 0.05) for bx, by in block)]

    if not pts:
        res = {"junctions_added": 0, "points_needing_dots": 0, "ok": True}
        if net_aware:
            res["cross_net_skipped"] = cross_net
        return res

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
    res = {"junctions_added": len(pts), "points_needing_dots": len(pts),
           "ok": True}
    if net_aware:
        res["cross_net_skipped"] = cross_net
    return res


# ----- C2: exhaustive pin completeness ------------------------------------

def complete_pins(sch_path: Path,
                  emit_no_connect: bool = False) -> Dict[str, Any]:
    """Report every pin of every placed symbol that terminates on nothing
    (see selectors.find_incomplete_pins). When `emit_no_connect`, append an
    explicit `(no_connect)` at each such pin so a genuinely-unused pin becomes
    INTENTIONAL rather than silently floating (and stops tripping ERC).

    Auto-WIRING a floating pin by role is done upstream in the IR
    (intent/pin_complete.py, gated by pin_completion rules) where the net
    context exists; this post-render pass is detection + no-connect only, and
    returns the incomplete-pin list so self-heal / the build can surface it.
    Idempotent: pins already carrying a no-connect are exempt."""
    from .context import build_context
    from .selectors import find_incomplete_pins

    try:
        ctx = build_context(sch_path)
    except Exception as exc:
        return {"incomplete": 0, "no_connects_added": 0, "ok": False,
                "error": f"{type(exc).__name__}: {exc}"}

    issues = find_incomplete_pins(
        ctx.get("pins"), ctx.get("wires"), ctx.get("labels"),
        ctx.get("no_connects"))
    res: Dict[str, Any] = {
        "incomplete": len(issues),
        "no_connects_added": 0,
        "ok": True,
        "pins": issues,
    }
    if not issues or not emit_no_connect:
        return res

    pts = [(iss["where"]["x"], iss["where"]["y"]) for iss in issues]
    text = sch_path.read_text(encoding="utf-8")

    def _n(v):
        return f"{v:g}"
    additions = "".join(
        f"\t(no_connect (at {_n(x)} {_n(y)}) (uuid \"{_uuid.uuid4()}\"))\n"
        for (x, y) in pts
    )
    last_paren = text.rfind(")")
    if last_paren < 0:
        res["ok"] = False
        return res
    sch_path.write_text(text[:last_paren] + "\n" + additions + text[last_paren:],
                        encoding="utf-8")
    res["no_connects_added"] = len(pts)
    return res


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


# ----- R2 auto-fix: reroute a wire that pierces a component body ----------

def _wire_block(p: Tuple[float, float], q: Tuple[float, float],
                uuid_val: str) -> str:
    """One `(wire ...)` s-expr in the file's canonical form, WITHOUT the
    leading tab (the caller reuses the tab that preceded the replaced
    wire) and WITHOUT a trailing newline."""
    return (
        f"(wire (pts (xy {_num(p[0])} {_num(p[1])}) "
        f"(xy {_num(q[0])} {_num(q[1])}))\n"
        "\t\t(stroke (width 0) (type default))\n"
        f"\t\t(uuid \"{uuid_val}\")\n"
        "\t)"
    )


def _seg_pierces_box(a: Tuple[float, float], b: Tuple[float, float],
                     box: Tuple[float, float, float, float],
                     margin: float) -> bool:
    """True if the Manhattan segment a-b passes through the INTERIOR of
    `box` (shrunk by `margin`, so an endpoint sitting on the edge / a pin
    tip is a touch, not a pierce). Manhattan (H or V) only; a diagonal
    (should never occur here) falls back to a midpoint test."""
    x1, y1, x2, y2 = box
    sx1, sy1, sx2, sy2 = x1 + margin, y1 + margin, x2 - margin, y2 - margin
    if sx1 >= sx2 or sy1 >= sy2:
        return False
    (ax, ay), (bx, by) = a, b
    if abs(ay - by) < 1e-6:                       # horizontal
        if not (sy1 < ay < sy2):
            return False
        lo, hi = (ax, bx) if ax <= bx else (bx, ax)
        return lo < sx2 and hi > sx1
    if abs(ax - bx) < 1e-6:                        # vertical
        if not (sx1 < ax < sx2):
            return False
        lo, hi = (ay, by) if ay <= by else (by, ay)
        return lo < sy2 and hi > sy1
    mx, my = (ax + bx) / 2, (ay + by) / 2
    return sx1 < mx < sx2 and sy1 < my < sy2


def _seg_overlaps_wire(a: Tuple[float, float], b: Tuple[float, float],
                       wire: Tuple[Tuple[float, float], Tuple[float, float]],
                       tol: float) -> bool:
    """True if segment a-b lies COLLINEAR and overlapping with `wire` --- a
    stacked overlap is a short in KiCad, so a detour must never create one.
    Perpendicular crossings are allowed (crossing wires do not connect
    without a junction dot), so this only tests the collinear case."""
    (ax, ay), (bx, by) = a, b
    (cx, cy), (dx, dy) = wire
    horiz = (abs(ay - by) <= tol and abs(cy - dy) <= tol
             and abs(ay - cy) <= tol)
    vert = (abs(ax - bx) <= tol and abs(cx - dx) <= tol
            and abs(ax - cx) <= tol)
    if horiz:
        lo = max(min(ax, bx), min(cx, dx))
        hi = min(max(ax, bx), max(cx, dx))
    elif vert:
        lo = max(min(ay, by), min(cy, dy))
        hi = min(max(ay, by), max(cy, dy))
    else:
        return False
    return hi - lo > tol


def _seg_pierces_foreign(
    a: Tuple[float, float], b: Tuple[float, float],
    boxes_ref: List[Tuple[str, float, float, float, float]],
    pins: List[Tuple[float, float, str]],
    margin: float,
) -> bool:
    """True if segment a-b pierces a body it does NOT connect to --- the
    exact rule the R2 detector (`find_wires_piercing_bodies`) uses: a body is
    EXEMPT for this segment when one of the segment's endpoints is a pin of
    that same body (a wire may leave/enter its own part's body at the pin).
    Keeping this identical to the detector guarantees the repair never
    produces a segment the detector will re-flag."""
    for (ref, x1, y1, x2, y2) in boxes_ref:
        touches_own = any(
            pref == ref and (_pt_close(a, (px, py), margin)
                             or _pt_close(b, (px, py), margin))
            for (px, py, pref) in pins)
        if touches_own:
            continue
        if _seg_pierces_box(a, b, (x1, y1, x2, y2), margin):
            return True
    return False


def _path_ok(path: List[Tuple[float, float]],
             boxes_ref: List[Tuple[str, float, float, float, float]],
             pins: List[Tuple[float, float, str]],
             other_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]],
             margin: float, tol: float) -> bool:
    """A candidate detour is acceptable iff no segment pierces a FOREIGN body
    (pin-aware, matching the R2 detector) AND no segment stacks collinearly
    on another net's wire (which would be a new short)."""
    for i in range(len(path) - 1):
        seg_a, seg_b = path[i], path[i + 1]
        if _seg_pierces_foreign(seg_a, seg_b, boxes_ref, pins, margin):
            return False
        for w in other_wires:
            if _seg_overlaps_wire(seg_a, seg_b, w, tol):
                return False
    return True


def _grid_beyond(edge: float, direction: int, grid: float = _GRID_MM) -> float:
    """Snap `edge` to the nearest grid line strictly on the `direction`
    side (-1 = smaller coord, +1 = larger), so a detour always clears the
    body edge and lands on-grid (KiCad needs grid-aligned endpoints)."""
    if direction < 0:
        return math.floor(edge / grid) * grid
    return math.ceil(edge / grid) * grid


def _route_around_box(
    a: Tuple[float, float], b: Tuple[float, float],
    blocker: Tuple[float, float, float, float],
    boxes_ref: List[Tuple[str, float, float, float, float]],
    pins: List[Tuple[float, float, str]],
    other_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    clear: float, margin: float, tol: float,
) -> Optional[List[Tuple[float, float]]]:
    """Return a Manhattan detour [a, ..., b] that clears every FOREIGN body
    and every other-net wire, or None if no simple L / Z does. The endpoints
    a and b are preserved EXACTLY --- only the middle bends away from the
    body, so connectivity is byte-identical."""
    ax, ay = a
    bx, by = b
    # 1. Two L variants (only distinct when a and b differ on both axes).
    if abs(ax - bx) > 1e-6 and abs(ay - by) > 1e-6:
        for corner in ((bx, ay), (ax, by)):
            path = [a, corner, b]
            if _path_ok(path, boxes_ref, pins, other_wires, margin, tol):
                return path
    # 2. Z-detour around the blocker's nearer long edge.
    x1, y1, x2, y2 = blocker
    if abs(ay - by) < 1e-6:                       # horizontal wire -> over/under
        for edge, d in ((y1 - clear, -1), (y2 + clear, +1)):
            dy = _grid_beyond(edge, d)
            if y1 - margin <= dy <= y2 + margin:  # snapped back onto the body
                continue
            path = [a, (ax, dy), (bx, dy), b]
            if _path_ok(path, boxes_ref, pins, other_wires, margin, tol):
                return path
    elif abs(ax - bx) < 1e-6:                      # vertical wire -> left/right
        for edge, d in ((x1 - clear, -1), (x2 + clear, +1)):
            dx = _grid_beyond(edge, d)
            if x1 - margin <= dx <= x2 + margin:
                continue
            path = [a, (dx, ay), (dx, by), b]
            if _path_ok(path, boxes_ref, pins, other_wires, margin, tol):
                return path
    return None


def _pt_close(p: Tuple[float, float], q: Tuple[float, float],
              tol: float) -> bool:
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def _endpoint_degree(pt: Tuple[float, float],
                     wires: List[Tuple[Tuple[float, float], Tuple[float, float]]],
                     tol: float) -> int:
    """How many wire ENDPOINTS coincide with `pt`. Degree 1 == the wire
    dead-ends here (nothing else is wired to this point)."""
    n = 0
    for (a, b) in wires:
        if _pt_close(pt, a, tol):
            n += 1
        if _pt_close(pt, b, tol):
            n += 1
    return n


def _on_segment(px: float, py: float, ax: float, ay: float,
                bx: float, by: float, tol: float) -> bool:
    """(px,py) on Manhattan segment a-b (endpoints included), within tol."""
    if abs(ay - by) <= tol:                       # horizontal
        lo, hi = (ax, bx) if ax <= bx else (bx, ax)
        return abs(py - ay) <= tol and lo - tol <= px <= hi + tol
    if abs(ax - bx) <= tol:                        # vertical
        lo, hi = (ay, by) if ay <= by else (by, ay)
        return abs(px - ax) <= tol and lo - tol <= py <= hi + tol
    return False


def _body_entry_point(keep: Tuple[float, float], moved: Tuple[float, float],
                      box: Tuple[float, float, float, float]
                      ) -> Optional[Tuple[float, float]]:
    """Where the segment keep->moved crosses the boundary of `box`, coming
    from the outside endpoint `keep`. Manhattan only."""
    x1, y1, x2, y2 = box
    if abs(keep[1] - moved[1]) < 1e-6:            # horizontal
        entry_x = x1 if keep[0] < moved[0] else x2
        return (entry_x, keep[1])
    if abs(keep[0] - moved[0]) < 1e-6:             # vertical
        entry_y = y1 if keep[1] < moved[1] else y2
        return (keep[0], entry_y)
    return None


def _clamp_overshoot(
    a: Tuple[float, float], b: Tuple[float, float],
    blocker: Tuple[float, float, float, float], pierced_ref: str,
    ctx: Dict[str, Any],
    wires: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    other_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    margin: float, tol: float,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """When a wire OVERSHOOTS its target pin and dead-ends INSIDE the pierced
    body (the common 'wire over component' cause), pull the buried dead-end
    back onto the first pin of that body the wire crosses (or the body-entry
    edge). Returns (keep_endpoint, new_target) or None when it is not safe.

    Safe only when the buried end is a genuine dead-end: degree 1, no
    junction / label / foreign pin on it, and the removed overshoot does not
    lie on another wire (which would be a real bond). Those guards keep the
    net list identical --- the wire connects the same live points, just
    without the dead stub that poked into the body."""
    x1, y1, x2, y2 = blocker
    sx1, sy1, sx2, sy2 = x1 + margin, y1 + margin, x2 - margin, y2 - margin

    def _inside(p):
        return sx1 < p[0] < sx2 and sy1 < p[1] < sy2

    if _inside(a) and not _inside(b):
        moved, keep = a, b
    elif _inside(b) and not _inside(a):
        moved, keep = b, a
    else:
        return None                               # both / neither inside

    # The buried end must bond nothing but this wire.
    if _endpoint_degree(moved, wires, tol) != 1:
        return None
    if any(_pt_close(moved, (jx, jy), tol)
           for (jx, jy) in ctx.get("junctions", [])):
        return None
    if any(_pt_close(moved, (lx, ly), tol)
           for (_n, lx, ly) in ctx.get("labels", [])):
        return None
    if any(_pt_close(moved, (px, py), tol)
           for (px, py, _r) in ctx.get("pin_positions", [])):
        return None                               # a pin sits on it -> leave it

    # Prefer landing on the first pin of the pierced body along keep->moved.
    pins_on = [(px, py) for (px, py, pref) in ctx.get("pin_positions", [])
               if pref == pierced_ref
               and _on_segment(px, py, keep[0], keep[1], moved[0], moved[1], tol)]
    if pins_on:
        target = min(pins_on,
                     key=lambda p: (p[0] - keep[0]) ** 2 + (p[1] - keep[1]) ** 2)
    else:
        target = _body_entry_point(keep, moved, blocker)
        if target is None:
            return None

    if _pt_close(target, keep, tol) or _pt_close(target, moved, tol):
        return None
    if _seg_pierces_foreign(keep, target, ctx.get("bboxes", []),
                            ctx.get("pin_positions", []), margin):
        return None
    # The removed overshoot (target->moved) must not bond to another wire.
    for w in other_wires:
        if _seg_overlaps_wire(target, moved, w, tol):
            return None
    return keep, target


_LABEL_NAME_RE = re.compile(r"\(label\s+\"([^\"]+)\"")
_LABEL_AT_RE = re.compile(r"\(at\s+(-?[0-9.]+)\s+(-?[0-9.]+)(\s+-?[0-9.]+)?\)")


def _move_label(text: str, name: str, old_xy: Tuple[float, float],
                new_xy: Tuple[float, float], tol: float) -> Tuple[str, bool]:
    """Rewrite the `(at x y rot)` of the local label `name` sitting at
    `old_xy` to `new_xy`, preserving the rotation token. Matches by name +
    position so the right instance moves when a net has several labels."""
    for s, e in _find_balanced_spans(text, "label"):
        chunk = text[s:e]
        m_name = _LABEL_NAME_RE.search(chunk)
        if not m_name or m_name.group(1) != name:
            continue
        m_at = _LABEL_AT_RE.search(chunk)
        if not m_at:
            continue
        try:
            lx, ly = float(m_at.group(1)), float(m_at.group(2))
        except (TypeError, ValueError):
            continue
        if abs(lx - old_xy[0]) > tol or abs(ly - old_xy[1]) > tol:
            continue
        rot = m_at.group(3) or ""
        new_at = f"(at {_num(new_xy[0])} {_num(new_xy[1])}{rot})"
        new_chunk = chunk[:m_at.start()] + new_at + chunk[m_at.end():]
        return text[:s] + new_chunk + text[e:], True
    return text, False


def _relocate_buried_label(
    a: Tuple[float, float], b: Tuple[float, float],
    ctx: Dict[str, Any],
    wires: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    other_wires: List[Tuple[Tuple[float, float], Tuple[float, float]]],
    boxes_ref: List[Tuple[str, float, float, float, float]],
    margin: float, tol: float, clear: float, max_tries: int,
) -> Optional[Tuple[Tuple[float, float], Tuple[float, float], str,
                    Tuple[float, float]]]:
    """When a body-piercing wire dead-ends at a NET LABEL that was placed
    inside a body (the ne555 'DIS label inside U1' case), relocate the label
    + its stub to a clear spot reachable by a clean straight stub from the
    pin. A local net label bonds purely by NAME, so moving it keeps the net
    intact --- proven here by comparing KiCad's netlist before/after.

    Returns (keep_pin, new_anchor, label_name, old_anchor) or None. Safe only
    when the buried end is a degree-1 dead-end carrying exactly one label and
    a clear straight stub exists that pierces no foreign body, overlaps no
    wire, and lands on no pin / wire-end / junction / other label."""
    bboxes = ctx.get("bboxes", [])
    pins = ctx.get("pin_positions", [])
    labels = ctx.get("labels", [])
    junctions = ctx.get("junctions", [])

    def _inside_any(p):
        for (_r, x1, y1, x2, y2) in bboxes:
            if (x1 + margin < p[0] < x2 - margin
                    and y1 + margin < p[1] < y2 - margin):
                return True
        return False

    if _inside_any(a) and not _inside_any(b):
        moved, keep = a, b
    elif _inside_any(b) and not _inside_any(a):
        moved, keep = b, a
    else:
        return None
    if _endpoint_degree(moved, wires, tol) != 1:
        return None
    named = [nm for (nm, lx, ly) in labels if _pt_close(moved, (lx, ly), tol)]
    if len(named) != 1:
        return None                               # no label, or ambiguous
    name = named[0]

    def _clear_anchor(p):
        if _inside_any(p):
            return False
        if any(_pt_close(p, (px, py), tol) for (px, py, _r) in pins):
            return False
        if any(_pt_close(p, wp, tol) for w in wires for wp in w):
            return False
        if any(_pt_close(p, (jx, jy), tol) for (jx, jy) in junctions):
            return False
        if any(_pt_close(p, (lx, ly), tol) for (_n, lx, ly) in labels):
            return False
        return True

    # Straight stub from the pin in each cardinal direction; nearest clear
    # anchor with a clean route wins.
    for r in range(1, max_tries + 1):
        for (dx, dy) in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            p = (round(keep[0] + dx * clear * r, 2),
                 round(keep[1] + dy * clear * r, 2))
            if not _clear_anchor(p):
                continue
            if _seg_pierces_foreign(keep, p, boxes_ref, pins, margin):
                continue
            if any(_seg_overlaps_wire(keep, p, w, tol) for w in other_wires):
                continue
            return keep, p, name, moved
    return None


def reroute_wires_off_bodies(
    sch_path: Path, ctx: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """R2 auto-fix (WIRING_RULES.md R2): reroute any wire that runs THROUGH
    a component's body around it, keeping both endpoints fixed.

    This is the missing repair for the user's 'wire over component' report.
    `find_wires_piercing_bodies` already detects a wire crossing a body it
    does NOT connect to (it skips a wire terminating at that part's own
    pins); this pass RESOLVES it by bending the wire's middle clear of the
    body. Because only the interior bends and the two endpoints are
    untouched, the net list is byte-identical --- the wire connects exactly
    the same two points, just via an L or Z instead of straight through.

    Deliberately does NOT touch the pin-to-pin SHORT case (a single wire
    whose two ENDS land on both pins of one part). Per the KiCad
    connectivity rule confirmed from the official docs --- 'only wire ends
    create connections; a wire merely crossing a pin's middle does not
    connect' --- that case genuinely bonds both pins, and an L from pin to
    pin would still bond them. It cannot be fixed geometrically without
    knowing the intended net, so it is left for SHORTED_COMPONENT /
    COLINEAR_WIRE_BRIDGE to surface (see [[project_silent_short_lint]]).

    Never creates a new short: a candidate detour is rejected if any of its
    segments would stack collinearly on another wire. Idempotent (a
    rerouted wire no longer pierces -> second call is a no-op) and
    byte-stable when no wire pierces a body.

    Returns {"wires_rerouted": N, "piercings_found": M, "unresolved": K,
             "ok": bool}.
    """
    from .context import build_context
    from .selectors import find_wires_piercing_bodies

    rules = _load_wiring_rules()
    margin = float(rules.get("r2_reroute_body_margin_mm", 0.5))
    clear = float(rules.get("r2_reroute_clearance_mm", _GRID_MM))
    tol = float(rules.get("r2_reroute_overlap_tol_mm", 0.05))

    if ctx is None:
        try:
            ctx = build_context(sch_path)
        except Exception as exc:
            return {"wires_rerouted": 0, "piercings_found": 0, "unresolved": 0,
                    "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    wires = ctx["wires"]
    bboxes = ctx["bboxes"]
    pin_positions = ctx["pin_positions"]

    issues = find_wires_piercing_bodies(wires, bboxes, pin_positions,
                                        margin_mm=margin)
    if not issues:
        return {"wires_rerouted": 0, "piercings_found": 0, "unresolved": 0,
                "ok": True}

    bbox_by_ref = {ref: (x1, y1, x2, y2) for (ref, x1, y1, x2, y2) in bboxes}

    text = sch_path.read_text(encoding="utf-8")
    coords, spans = _extract_wires(text)

    def _close(u: float, v: float) -> bool:
        return abs(u - v) <= 0.02

    def _match_span(ws, we, used) -> Optional[int]:
        for i, (cx1, cy1, cx2, cy2) in enumerate(coords):
            if i in used:
                continue
            fwd = (_close(cx1, ws[0]) and _close(cy1, ws[1])
                   and _close(cx2, we[0]) and _close(cy2, we[1]))
            rev = (_close(cx1, we[0]) and _close(cy1, we[1])
                   and _close(cx2, ws[0]) and _close(cy2, ws[1]))
            if fwd or rev:
                return i
        return None

    reloc_max = int(rules.get("r2_reroute_relocate_max_tries", 10))
    replacements: List[Tuple[int, int, str]] = []
    label_moves: List[Tuple[str, Tuple[float, float], Tuple[float, float]]] = []
    used: set = set()
    unresolved = 0
    for iss in issues:
        where = iss.get("where", {})
        ws = where.get("wire_start")
        we = where.get("wire_end")
        pierced = where.get("pierces")
        if not ws or not we or pierced not in bbox_by_ref:
            unresolved += 1
            continue
        si = _match_span(ws, we, used)
        if si is None:
            unresolved += 1
            continue
        cx1, cy1, cx2, cy2 = coords[si]
        a, b = (cx1, cy1), (cx2, cy2)
        blocker = bbox_by_ref[pierced]
        # Route around EVERY body via the pin-aware foreign-pierce test: a
        # body is exempt only for a segment that touches its own pin (exactly
        # the R2 detector's rule), so the detour may leave/enter the endpoint
        # parts at their pins but never cuts through any other body. Using the
        # detector's own rule guarantees the result never re-flags as R2.
        other_wires = [w for j, w in enumerate(wires) if j != si]
        path = _route_around_box(a, b, blocker, bboxes, pin_positions,
                                 other_wires, clear, margin, tol)
        if path is None or len(path) < 2:
            # Reroute-around can't help when an endpoint is buried inside the
            # body (the wire overshot its target pin). Try clamping the dead
            # overshoot back onto the pin it crossed.
            clamp = _clamp_overshoot(a, b, blocker, pierced, ctx, wires,
                                     other_wires, margin, tol)
            if clamp is not None:
                keep, target = clamp
                s, e, orig_uuid = spans[si]
                uid = orig_uuid or str(_uuid.uuid4())
                replacements.append((s, e, _wire_block(keep, target, uid)))
                used.add(si)
                continue
            # Last resort: the wire dead-ends at a NET LABEL placed inside the
            # body. Relocate the label + stub to a clear spot (bonds by name,
            # so the net is unchanged).
            reloc = _relocate_buried_label(a, b, ctx, wires, other_wires,
                                           bboxes, margin, tol, clear, reloc_max)
            if reloc is None:
                unresolved += 1
                continue
            keep, new_anchor, lname, old_anchor = reloc
            s, e, orig_uuid = spans[si]
            uid = orig_uuid or str(_uuid.uuid4())
            replacements.append((s, e, _wire_block(keep, new_anchor, uid)))
            label_moves.append((lname, old_anchor, new_anchor))
            used.add(si)
            continue
        s, e, orig_uuid = spans[si]
        blocks = []
        for k in range(len(path) - 1):
            uid = (orig_uuid or str(_uuid.uuid4())) if k == 0 else str(_uuid.uuid4())
            blocks.append(_wire_block(path[k], path[k + 1], uid))
        replacements.append((s, e, "\n\t".join(blocks)))
        used.add(si)

    if not replacements:
        return {"wires_rerouted": 0, "piercings_found": len(issues),
                "unresolved": unresolved, "ok": True}

    # Apply wire replacements right-to-left so earlier offsets stay valid.
    replacements.sort(key=lambda r: r[0], reverse=True)
    for s, e, new_text in replacements:
        text = text[:s] + new_text + text[e:]
    # Then move any relocated labels (content-matched, offset-independent).
    labels_moved = 0
    for (lname, old_xy, new_xy) in label_moves:
        text, ok = _move_label(text, lname, old_xy, new_xy, tol)
        if ok:
            labels_moved += 1
    sch_path.write_text(text, encoding="utf-8")
    return {"wires_rerouted": len(replacements), "piercings_found": len(issues),
            "labels_moved": labels_moved, "unresolved": unresolved, "ok": True}


# ----- fit-to-sheet: keep the whole design inside the page border ---------

_PAPER_RE = re.compile(r'\(paper\s+"([^"]+)"\)')
_AT_TOK_RE = re.compile(r"\(at\s+(-?[0-9.]+)\s+(-?[0-9.]+)((?:\s+-?[0-9.]+)?)\)")
# Absolute 2-coordinate tokens that carry a drawn position: wire/polyline
# points (xy) and graphic rectangle/arc corners (start/end/mid/center).
# `at` is handled separately (it keeps an optional rotation field).
_COORD2_RE = re.compile(
    r"\((xy|start|end|mid|center)\s+(-?[0-9.]+)\s+(-?[0-9.]+)\)")


def _content_bbox(ctx: Dict[str, Any]):
    """Union bbox of everything drawn: component bodies, wires, label anchors,
    field/RefDes text boxes, power-port names, junctions and block rectangles.
    Returns (minx, miny, maxx, maxy) or None when the sheet is empty."""
    xs: List[float] = []
    ys: List[float] = []
    for (a, b) in ctx.get("wires", []):
        xs += [a[0], b[0]]
        ys += [a[1], b[1]]
    for (_r, x1, y1, x2, y2) in ctx.get("bboxes", []):
        xs += [x1, x2]
        ys += [y1, y2]
    for (_r, _f, x1, y1, x2, y2) in ctx.get("text_bboxes", []):
        xs += [x1, x2]
        ys += [y1, y2]
    for tup in ctx.get("blocks", []):
        _n, x1, y1, x2, y2 = tup
        xs += [x1, x2]
        ys += [y1, y2]
    for (_n, x, y) in ctx.get("labels", []):
        xs.append(x)
        ys.append(y)
    for (_n, x, y) in ctx.get("power_ports", []):
        xs.append(x)
        ys.append(y)
    for (x, y) in ctx.get("junctions", []):
        xs.append(x)
        ys.append(y)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _pick_paper(cw: float, ch: float, current: str, pages: Dict[str, Any],
                ml: float, mr: float, mt: float, mb: float,
                tbw: float, tbh: float) -> Optional[str]:
    """Smallest page (>= current where possible) whose drawable area fits a
    cw x ch design AND lets it dodge the bottom-right title block by top-left
    placement. Returns a paper name, or None if even the largest can't fit
    (caller then best-effort-places on the largest)."""
    cands = []
    for name, d in pages.items():
        try:
            cands.append((name, float(d["w_mm"]), float(d["h_mm"])))
        except (TypeError, ValueError, KeyError):
            continue
    if not cands:
        return None
    cands.sort(key=lambda p: p[1] * p[2])

    def _fits(w, h):
        dw, dh = w - ml - mr, h - mt - mb
        if cw > dw or ch > dh:
            return False
        # top-left placement dodges the title block if the content clears it
        # on at least one axis.
        return (cw <= dw - tbw) or (ch <= dh - tbh)

    for name, w, h in cands:
        if _fits(w, h):
            return name
    return None


def fit_design_to_sheet(sch_path: Path,
                        ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Enforce the HARD 'design never crosses the sheet border / title block'
    rule POST-RENDER (see [[feedback_layout_within_sheet]]).

    If the drawn content sits outside the drawable area (page minus margins)
    or over the bottom-right title block, this (1) UNIFORMLY translates the
    whole design so its top-left corner lands at the margin -- every
    coordinate shifts by the SAME (dx, dy) so the net list is byte-identical
    -- and (2) if the content genuinely does not fit the current paper,
    upsizes `(paper "..")` to the smallest configured page that fits.

    Coordinates inside `(lib_symbols ...)` are symbol-INTERNAL (relative to
    each symbol origin) and are left untouched; only absolute instance /
    wire / label / junction / text coordinates move. Idempotent (a fitted
    design needs dx=dy=0) and byte-stable when the design already fits.

    Returns {"translated": bool, "dx", "dy", "paper_from", "paper_to",
             "was_outside": bool, "ok": bool}.
    """
    hl = _load_layout_section("hierarchy_layout")
    pages = hl.get("page_sizes") or {}
    m = hl.get("margin_mm") or {}
    ml = float(m.get("left", 15.0))
    mr = float(m.get("right", 15.0))
    mt = float(m.get("top", 15.0))
    mb = float(m.get("bottom", 12.0))
    tb = hl.get("title_block_reserve_mm") or {}
    tbw = float(tb.get("width_mm", 110.0))
    tbh = float(tb.get("height_mm", 30.0))

    noop = {"translated": False, "dx": 0.0, "dy": 0.0,
            "was_outside": False, "ok": True}
    try:
        if ctx is None:
            from .context import build_context
            ctx = build_context(sch_path)
    except Exception as exc:
        return {**noop, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    bbox = _content_bbox(ctx)
    if bbox is None:
        return noop
    minx, miny, maxx, maxy = bbox
    cw, ch = maxx - minx, maxy - miny

    text = sch_path.read_text(encoding="utf-8")
    pm = _PAPER_RE.search(text)
    cur_paper = pm.group(1) if pm else "A4"
    cur = pages.get(cur_paper)
    cur_w = float(cur["w_mm"]) if cur else 297.0
    cur_h = float(cur["h_mm"]) if cur else 210.0

    # Does the design already sit fully inside the drawable area AND clear the
    # title block on the current paper? If so, nothing to do (byte-stable).
    draw_x1, draw_y1 = ml, mt
    draw_x2, draw_y2 = cur_w - mr, cur_h - mb
    tb_x1, tb_y1 = draw_x2 - tbw, draw_y2 - tbh
    inside = (minx >= draw_x1 - 0.01 and miny >= draw_y1 - 0.01
              and maxx <= draw_x2 + 0.01 and maxy <= draw_y2 + 0.01)
    clears_tb = (maxx <= tb_x1 + 0.01) or (maxy <= tb_y1 + 0.01)
    if inside and clears_tb:
        return noop

    # Need to move (and maybe grow the page). Pick the target paper.
    target = _pick_paper(cw, ch, cur_paper, pages, ml, mr, mt, mb, tbw, tbh)
    if target is None:                       # nothing fits -> use the largest
        big = sorted(((n, float(d["w_mm"]), float(d["h_mm"]))
                      for n, d in pages.items()), key=lambda p: p[1] * p[2])
        target = big[-1][0] if big else cur_paper
    tw = float(pages[target]["w_mm"]) if target in pages else cur_w
    th = float(pages[target]["h_mm"]) if target in pages else cur_h

    # Translate so the content's top-left lands at the margin.
    dx = ml - minx
    dy = mt - miny
    if abs(dx) < 0.01 and abs(dy) < 0.01 and target == cur_paper:
        return noop

    # Rewrite paper token first (offsets before lib_symbols are unaffected).
    if target != cur_paper and pm:
        text = text[:pm.start()] + f'(paper "{target}")' + text[pm.end():]

    # Exclude the (lib_symbols ...) span: those coordinates are symbol-relative.
    lib_spans = _find_balanced_spans(text, "lib_symbols")
    lib_lo, lib_hi = (lib_spans[0] if lib_spans else (-1, -1))

    def _in_lib(pos: int) -> bool:
        return lib_lo <= pos < lib_hi

    # Shift every absolute coordinate token OUTSIDE lib_symbols: instance /
    # field / label / text / junction `(at)`, wire & polyline `(xy)`, and
    # graphic rectangle/arc `(start|end|mid|center)` (the block boxes are
    # top-level rectangles). lib_symbols coords are symbol-relative -> skipped.
    edits: List[Tuple[int, int, str]] = []
    for mo in _AT_TOK_RE.finditer(text):
        if _in_lib(mo.start()):
            continue
        x = float(mo.group(1)) + dx
        y = float(mo.group(2)) + dy
        edits.append((mo.start(), mo.end(),
                      f"(at {_num(x)} {_num(y)}{mo.group(3)})"))
    for mo in _COORD2_RE.finditer(text):
        if _in_lib(mo.start()):
            continue
        tok = mo.group(1)
        x = float(mo.group(2)) + dx
        y = float(mo.group(3)) + dy
        edits.append((mo.start(), mo.end(), f"({tok} {_num(x)} {_num(y)})"))

    edits.sort(key=lambda e: e[0], reverse=True)
    for s, e, rep in edits:
        text = text[:s] + rep + text[e:]
    sch_path.write_text(text, encoding="utf-8")
    return {"translated": True, "dx": round(dx, 3), "dy": round(dy, 3),
            "paper_from": cur_paper, "paper_to": target,
            "was_outside": True, "tokens_shifted": len(edits), "ok": True}


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

    # Fit-to-sheet: keep the whole design inside the page border + off the
    # title block (HARD layout rule). Runs FIRST -- it uniformly translates
    # every coordinate (and may upsize the paper), so all later passes see the
    # final positions. Connectivity byte-identical; byte-stable when the
    # design already fits.
    if bool(rules.get("fit_design_to_sheet", True)):
        try:
            fit_res = fit_design_to_sheet(sch_path)
            summary["ran"].append({"rule": "FIT_TO_SHEET", "result": fit_res})
        except Exception as exc:
            summary["skipped"].append({
                "rule": "FIT_TO_SHEET",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    # R2: reroute any wire that runs THROUGH a component body around it.
    # Runs after fit-to-sheet so it sees final positions. Endpoints are
    # preserved -> connectivity is byte-identical; only acts when a wire
    # genuinely pierces a body, and never creates a new collinear overlap.
    # Idempotent + byte-stable when nothing pierces. Off -> legacy warn-only.
    if bool(rules.get("r2_reroute_wires_off_bodies", True)):
        try:
            r2_res = reroute_wires_off_bodies(sch_path)
            summary["ran"].append({"rule": "R2_REROUTE", "result": r2_res})
        except Exception as exc:
            summary["skipped"].append({
                "rule": "R2_REROUTE",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    # R4 general: add a junction dot at every 3+ meet AND mid-span T-tap.
    # Superset of r4_split_four_way (4-way only) --- repairs electrically-open
    # T-taps too. Default on once implemented (idempotent + electrically safe).
    if bool(rules.get("r4_add_junction_dots", True)):
        try:
            _ei = _load_electrical_integrity()
            _net_aware = bool(_ei.get("net_aware_junctions", False))
            r4d_res = add_missing_junction_dots(sch_path, net_aware=_net_aware)
            summary["ran"].append({"rule": "R4_DOTS", "result": r4d_res})
        except Exception as exc:
            summary["skipped"].append({
                "rule": "R4_DOTS",
                "reason": f"{type(exc).__name__}: {exc}",
            })

    # R11_TEXT: relocate any Reference/Value text a wire runs across.
    # Text-only move -> connectivity unchanged. Idempotent; no-op when no
    # wire crosses a field. Default on (safe, matches r4_add_junction_dots).
    # C2: exhaustive pin-completeness gate. Detection always when enabled;
    # no-connect emission only when pin_completeness_emit_no_connect. Default
    # off -> no-op, output byte-identical.
    _ei2 = _load_electrical_integrity()
    if bool(_ei2.get("pin_completeness", False)):
        try:
            pc_res = complete_pins(
                sch_path,
                emit_no_connect=bool(_ei2.get("pin_completeness_emit_no_connect",
                                              False)))
            summary["ran"].append({"rule": "C2_PINS", "result": pc_res})
        except Exception as exc:
            summary["skipped"].append({
                "rule": "C2_PINS",
                "reason": f"{type(exc).__name__}: {exc}",
            })

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
