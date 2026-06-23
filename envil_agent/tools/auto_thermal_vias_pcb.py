"""Tool: stitch thermal vias under exposed-pad (paddle) pads.

QFN / DFN / DPAK / SOT-223 / TO-263 footprints expose a metal pad on
the bottom of the package for heat conduction. To dump that heat into
the inner / back-side copper plane you tile a grid of small vias on
the paddle pad — typical pattern is 0.3 mm drill / 0.6 mm pad, 1 mm
pitch.

This tool scans every footprint on a .kicad_pcb, finds pads whose
number matches the `paddle_pad_numbers` allowlist, and drops a via
grid centered on each matching pad. The via net is taken from the
pad itself (usually GND) so the vias join the pour automatically.

Universal — works on any board. All geometry from
`layout_config.json:auto_thermal_vias_pcb`. Idempotent: pre-existing
vias already inside a paddle bbox are removed before the new grid is
emitted.
"""
from __future__ import annotations

import re
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool


def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _at(node: list) -> Tuple[float, float, float]:
    for child in node[1:]:
        if isinstance(child, list) and _head(child) == "at":
            try:
                x = float(child[1]); y = float(child[2])
                r = float(child[3]) if len(child) > 3 else 0.0
                return (x, y, r)
            except (IndexError, TypeError, ValueError):
                return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def _prop(node: list, name: str) -> Optional[str]:
    for child in node[1:]:
        if (isinstance(child, list) and _head(child) == "property"
                and len(child) >= 3 and str(child[1]) == name):
            return str(child[2])
    return None


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("auto_thermal_vias_pcb", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Serializer
# ---------------------------------------------------------------------------

def _is_short(node: Any, threshold: int = 60) -> bool:
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


def _emit(node: Any, indent: int = 0) -> str:
    if _is_short(node):
        return "\t" * indent + _compact(node)
    if not isinstance(node, list):
        return "\t" * indent + _compact(node)
    head = _compact(node[0]) if node else ""
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
        lines.append(_emit(child, indent + 1))
    lines.append("\t" * indent + ")")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pad inspection
# ---------------------------------------------------------------------------

def _pad_number(pad: list) -> str:
    """The pad number is the first STRING child after the `pad` head."""
    for child in pad[1:]:
        if isinstance(child, str):
            return child
    return ""


def _pad_size(pad: list) -> Tuple[float, float]:
    for child in pad[1:]:
        if (isinstance(child, list) and _head(child) == "size"
                and len(child) >= 3):
            try:
                return (float(child[1]), float(child[2]))
            except (TypeError, ValueError):
                return (0.0, 0.0)
    return (0.0, 0.0)


def _pad_net_id(pad: list) -> int:
    """Some pads carry a (net N "name") clause when the netlist has
    been pushed. Returns the int net id or 0 (unconnected)."""
    for child in pad[1:]:
        if (isinstance(child, list) and _head(child) == "net"
                and len(child) >= 2):
            try:
                return int(child[1])
            except (TypeError, ValueError):
                return 0
    return 0


def _rotate_offset(dx: float, dy: float, rot_deg: float) -> Tuple[float, float]:
    """Rotate a (dx,dy) offset by `rot_deg`. KiCad applies the footprint's
    rotation to every pad's relative position — vias are placed in
    board-absolute coords so we have to do the same math."""
    if rot_deg == 0.0:
        return dx, dy
    import math as _m
    a = _m.radians(rot_deg)
    c, s = _m.cos(a), _m.sin(a)
    return c * dx - s * dy, s * dx + c * dy


# ---------------------------------------------------------------------------
# Via builder
# ---------------------------------------------------------------------------

def _make_via(x: float, y: float, drill_mm: float, size_mm: float,
               net_id: int) -> list:
    nodes: List[Any] = [
        sexpdata.Symbol("via"),
        [sexpdata.Symbol("at"), x, y],
        [sexpdata.Symbol("size"), size_mm],
        [sexpdata.Symbol("drill"), drill_mm],
        [sexpdata.Symbol("layers"), "F.Cu", "B.Cu"],
    ]
    if net_id > 0:
        nodes.append([sexpdata.Symbol("net"), net_id])
    nodes.append([sexpdata.Symbol("uuid"), str(_uuid.uuid4())])
    return nodes


# ---------------------------------------------------------------------------
# Tool entry
# ---------------------------------------------------------------------------

@tool(
    name="auto_thermal_vias_pcb",
    description=(
        "Stitch a via array under exposed-pad (paddle) pads on a "
        ".kicad_pcb to conduct heat into the inner / back-side copper. "
        "Scans every footprint, finds pads whose number matches the "
        "configured paddle list (default EP / 99 / 11 / PAD / ePAD / "
        "Thermal), drops a grid of small vias on each. Vias join the "
        "pad's net automatically (usually GND).\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}        # required\n'
        '  {"pcb_path": "...", "ref_filter": "^U[12]$"} # only U1, U2\n'
        '  {"pcb_path": "...", "grid_pitch_mm": 1.2}    # looser grid\n'
        '  {"pcb_path": "...", "via_drill_mm": 0.25}    # smaller drills\n'
        "All defaults in layout_config.json:auto_thermal_vias_pcb. "
        "Run AFTER auto_place_pcb so paddle positions are stable. "
        "Idempotent — re-running removes prior thermal vias on the "
        "same paddles before emitting the new grid."
    ),
    input_schema={"pcb_path": str},
)
async def auto_thermal_vias_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text",
                              "text": f"ERROR: .kicad_pcb not found: {pcb_path}"}],
                 "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text",
                              "text": "ERROR: expected .kicad_pcb"}],
                 "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                              "text": "auto_thermal_vias_pcb disabled"}],
                 "is_error": True}

    paddle_pads = set(str(n) for n in (
        args.get("paddle_pad_numbers")
        or cfg.get("paddle_pad_numbers", ["EP", "99", "11", "PAD",
                                          "ePAD", "Thermal"])))
    via_drill = float(args.get("via_drill_mm", cfg.get("via_drill_mm", 0.3)))
    via_size  = float(args.get("via_diameter_mm",
                                 cfg.get("via_diameter_mm", 0.6)))
    pitch     = float(args.get("grid_pitch_mm",
                                 cfg.get("grid_pitch_mm", 1.0)))
    min_pad   = float(args.get("min_pad_size_mm",
                                 cfg.get("min_pad_size_mm", 2.0)))
    ref_filter_raw = str(args.get("ref_filter",
                                    cfg.get("ref_filter", "")) or "")
    replace = bool(args.get("replace",
                              cfg.get("replace_existing", True)))
    try:
        ref_re = re.compile(ref_filter_raw) if ref_filter_raw else None
    except re.error as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: bad ref_filter regex: {exc}"}],
                 "is_error": True}

    try:
        text = pcb_path.read_text(encoding="utf-8")
        root = sexpdata.loads(text)
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: parse failed: {exc}"}],
                 "is_error": True}
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        return {"content": [{"type": "text",
                              "text": "ERROR: not a kicad_pcb"}],
                 "is_error": True}

    # Pass 1 — find every paddle pad. Record:
    #   (ref, board_x, board_y, pad_w, pad_h, rot, net_id)
    paddles: List[Tuple[str, float, float, float, float, float, int]] = []
    for fp in root[1:]:
        if not (isinstance(fp, list) and _head(fp) == "footprint"):
            continue
        ref = _prop(fp, "Reference") or ""
        if ref_re and not ref_re.search(ref):
            continue
        fp_x, fp_y, fp_rot = _at(fp)
        for child in fp[1:]:
            if not (isinstance(child, list) and _head(child) == "pad"):
                continue
            num = _pad_number(child)
            if num not in paddle_pads:
                continue
            pad_x, pad_y, _ = _at(child)
            pw, ph = _pad_size(child)
            if pw < min_pad or ph < min_pad:
                continue
            dx, dy = _rotate_offset(pad_x, pad_y, fp_rot)
            net_id = _pad_net_id(child)
            paddles.append((ref, fp_x + dx, fp_y + dy, pw, ph, fp_rot, net_id))

    if not paddles:
        return {"content": [{"type": "text",
                              "text": ("no paddle pads found "
                                        f"(searched for pad numbers "
                                        f"{sorted(paddle_pads)}; "
                                        f"no footprint matched ref_filter "
                                        f"={ref_filter_raw or 'ANY'})")}],
                 "ok": True, "placed": 0, "paddles": 0}

    # Pass 2 — if replacing, drop existing vias that lie within ANY
    # paddle bbox. Bbox is the unrotated pad rectangle; cheap inclusive
    # check skips inner-area vias the user might've added by hand.
    removed = 0
    if replace:
        kept: List[Any] = [root[0]]
        for child in root[1:]:
            if isinstance(child, list) and _head(child) == "via":
                vx, vy, _ = _at(child)
                hit = False
                for (_, px, py, pw, ph, _rot, _nid) in paddles:
                    # Treat the paddle as an axis-aligned rect at (px,py)
                    # of size (pw,ph). Rotation isn't applied here because
                    # paddles are usually rect at multiples of 90°; the
                    # check is intentionally a bit forgiving.
                    if (px - pw / 2 <= vx <= px + pw / 2
                            and py - ph / 2 <= vy <= py + ph / 2):
                        hit = True
                        break
                if hit:
                    removed += 1
                    continue
            kept.append(child)
        root = kept

    # Pass 3 — emit the via grid for each paddle.
    placed = 0
    per_paddle: Dict[str, int] = {}
    for (ref, px, py, pw, ph, _rot, net_id) in paddles:
        # Tile a grid that fits inside the paddle minus a one-pitch
        # margin so vias don't kiss the pad edge.
        usable_w = max(0.0, pw - pitch)
        usable_h = max(0.0, ph - pitch)
        cols = max(1, int(usable_w / pitch) + 1)
        rows = max(1, int(usable_h / pitch) + 1)
        start_x = px - (cols - 1) * pitch / 2.0
        start_y = py - (rows - 1) * pitch / 2.0
        for r in range(rows):
            for c in range(cols):
                vx = round((start_x + c * pitch) / 0.01) * 0.01
                vy = round((start_y + r * pitch) / 0.01) * 0.01
                root.append(_make_via(vx, vy, via_drill, via_size, net_id))
                placed += 1
        per_paddle[ref] = per_paddle.get(ref, 0) + cols * rows

    try:
        pcb_path.write_text(_emit(root), encoding="utf-8")
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: write failed: {exc}"}],
                 "is_error": True}

    detail = ", ".join(f"{r}={n}" for r, n in sorted(per_paddle.items()))
    return {
        "content": [{"type": "text",
                      "text": (f"placed {placed} thermal vias under "
                                f"{len(paddles)} paddle pad(s)\n"
                                f"  per refdes: {detail}\n"
                                f"  drill: {via_drill} mm  pad: {via_size} mm  "
                                f"pitch: {pitch} mm\n"
                                f"  removed {removed} pre-existing vias in "
                                f"paddle areas")}],
        "ok": True,
        "path": str(pcb_path),
        "placed": placed,
        "removed": removed,
        "paddles": len(paddles),
        "per_paddle": per_paddle,
    }
