"""Tool: drop NPTH mounting holes at the corners of a .kicad_pcb.

Boards need mechanical mounting holes; users routinely forget them.
This tool reads the Edge.Cuts bounding box, insets by `inset_mm`, and
emits one mounting-hole footprint per corner.

Universal — works on any .kicad_pcb. All geometry comes from
`layout_config.json:auto_mounting_holes_pcb` (diameter, inset, library
id, corner count) so a project can change hole size by editing JSON.

Run AFTER auto_outline_pcb so the Edge.Cuts bbox exists.
"""
from __future__ import annotations

import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool


# ---------------------------------------------------------------------------
# Shared s-expression helpers (mirror the other PCB tools)
# ---------------------------------------------------------------------------

def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _layer_of(node: list) -> Optional[str]:
    for child in node[1:]:
        if (isinstance(child, list) and _head(child) == "layer"
                and len(child) >= 2):
            return str(child[1])
    return None


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("auto_mounting_holes_pcb", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Serializer (same style as apply_ops / auto_outline_pcb)
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
# Geometry — find Edge.Cuts bbox
# ---------------------------------------------------------------------------

def _edge_bbox(root: list) -> Optional[Tuple[float, float, float, float]]:
    """Walk every gr_* graphic on Edge.Cuts and return (xmin, ymin,
    xmax, ymax). Returns None when no Edge.Cuts shapes exist — the
    caller surfaces a clean error instead of placing holes at (0,0)."""
    xs: List[float] = []
    ys: List[float] = []
    for child in root[1:]:
        if not isinstance(child, list):
            continue
        if _head(child) not in ("gr_line", "gr_arc", "gr_rect",
                                  "gr_poly", "gr_circle"):
            continue
        if _layer_of(child) != "Edge.Cuts":
            continue
        for c in child[1:]:
            if isinstance(c, list) and _head(c) in ("start", "end", "center"):
                try:
                    xs.append(float(c[1]))
                    ys.append(float(c[2]))
                except (IndexError, TypeError, ValueError):
                    pass
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# Footprint builder
# ---------------------------------------------------------------------------

def _make_mounting_hole(x: float, y: float, lib_id: str,
                         diameter_mm: float,
                         exclude_from_pos: bool) -> list:
    """Build one NPTH mounting-hole footprint node centered at (x, y)."""
    nodes: List[Any] = [
        sexpdata.Symbol("footprint"),
        lib_id,
        [sexpdata.Symbol("layer"), "F.Cu"],
        [sexpdata.Symbol("uuid"), str(_uuid.uuid4())],
        [sexpdata.Symbol("at"), x, y],
    ]
    if exclude_from_pos:
        nodes.append([sexpdata.Symbol("attr"),
                      sexpdata.Symbol("exclude_from_pos_files")])
    # The single NPTH pad — drill == size for a true clearance hole.
    nodes.append([
        sexpdata.Symbol("pad"),
        "",                                # pad number (blank for NPTH)
        sexpdata.Symbol("np_thru_hole"),
        sexpdata.Symbol("circle"),
        [sexpdata.Symbol("at"), 0, 0],
        [sexpdata.Symbol("size"), diameter_mm, diameter_mm],
        [sexpdata.Symbol("drill"), diameter_mm],
        [sexpdata.Symbol("layers"), "*.Cu", "*.Mask"],
        [sexpdata.Symbol("uuid"), str(_uuid.uuid4())],
    ])
    return nodes


def _is_mounting_hole(node: list, lib_id_match: str) -> bool:
    if not (isinstance(node, list) and _head(node) == "footprint"):
        return False
    # First atom child is the library id string
    for child in node[1:]:
        if isinstance(child, str):
            return child == lib_id_match
        if not isinstance(child, list):
            continue
    return False


# ---------------------------------------------------------------------------
# Tool entry
# ---------------------------------------------------------------------------

@tool(
    name="auto_mounting_holes_pcb",
    description=(
        "Drop NPTH mounting holes at the corners of a .kicad_pcb so the "
        "board can be screwed to a chassis. Reads the Edge.Cuts bbox, "
        "insets by `inset_mm`, places one hole per corner. Universal "
        "— works on any board. Default M3 (3.2 mm clearance), 4 holes; "
        "all geometry in `layout_config.json:auto_mounting_holes_pcb`.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}        # required\n'
        '  {"pcb_path": "...", "diameter_mm": 2.5}       # M2.5 holes\n'
        '  {"pcb_path": "...", "inset_mm": 3.5}          # closer to edge\n'
        '  {"pcb_path": "...", "corner_strategy": 2}     # only 2 holes\n'
        '  {"pcb_path": "...", "replace": false}         # keep existing\n'
        "Run AFTER auto_outline_pcb so Edge.Cuts exists. Idempotent "
        "by default — re-running replaces previously-placed holes."
    ),
    input_schema={"pcb_path": str},
)
async def auto_mounting_holes_pcb(args: dict[str, Any]) -> dict[str, Any]:
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
                              "text": "auto_mounting_holes_pcb disabled"}],
                 "is_error": True}

    diameter = float(args.get("diameter_mm", cfg.get("diameter_mm", 3.2)))
    inset    = float(args.get("inset_mm", cfg.get("inset_mm", 5.0)))
    strategy = int(args.get("corner_strategy",
                              cfg.get("corner_strategy", 4)))
    lib_id   = str(args.get("library_id",
                              cfg.get("library_id",
                                      "MountingHole:MountingHole_3.2mm")))
    exclude  = bool(args.get("exclude_from_pos_files",
                              cfg.get("exclude_from_pos_files", True)))
    replace  = bool(args.get("replace",
                              cfg.get("replace_existing", True)))

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

    bbox = _edge_bbox(root)
    if bbox is None:
        return {"content": [{"type": "text",
                              "text": ("PCB has no Edge.Cuts outline. "
                                        "Run auto_outline_pcb first.")}],
                 "is_error": True}

    x1, y1, x2, y2 = bbox
    if (x2 - x1) < 2 * inset + diameter or (y2 - y1) < 2 * inset + diameter:
        return {"content": [{"type": "text",
                              "text": (f"Board {x2-x1:.1f} x {y2-y1:.1f} mm "
                                        f"too small for inset={inset} + "
                                        f"diameter={diameter} mm")}],
                 "is_error": True}

    # Optionally remove pre-existing mounting holes with the same lib id
    removed = 0
    if replace:
        kept: List[Any] = [root[0]]
        for child in root[1:]:
            if _is_mounting_hole(child, lib_id):
                removed += 1
                continue
            kept.append(child)
        root = kept

    # Corner positions: standard 4-corner layout. corner_strategy=2
    # places diagonally opposite holes only (bottom-left + top-right).
    bl = (x1 + inset, y2 - inset)        # bottom-left  (KiCad Y is down)
    tl = (x1 + inset, y1 + inset)        # top-left
    tr = (x2 - inset, y1 + inset)        # top-right
    br = (x2 - inset, y2 - inset)        # bottom-right
    if strategy == 2:
        positions = [bl, tr]
    else:
        positions = [bl, tl, tr, br]

    for (x, y) in positions:
        x = round(x / 0.01) * 0.01
        y = round(y / 0.01) * 0.01
        root.append(_make_mounting_hole(x, y, lib_id, diameter, exclude))

    try:
        pcb_path.write_text(_emit(root), encoding="utf-8")
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: write failed: {exc}"}],
                 "is_error": True}

    return {
        "content": [{"type": "text",
                      "text": (f"placed {len(positions)} mounting hole(s) "
                                f"({diameter} mm) on {pcb_path.name}\n"
                                f"  inset: {inset} mm  lib: {lib_id}\n"
                                f"  removed {removed} pre-existing holes")}],
        "ok": True,
        "path": str(pcb_path),
        "placed": len(positions),
        "removed": removed,
        "diameter_mm": diameter,
        "inset_mm": inset,
    }
