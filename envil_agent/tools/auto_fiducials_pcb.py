"""Tool: drop fiducial markers near the corners of a .kicad_pcb.

Pick-and-place machines use fiducials to register the board's origin
+ rotation before populating SMD parts. A 3-fiducial asymmetric layout
(bottom-left, top-left, top-right) lets the P&P detect which side of
the board is up.

Universal — works on any .kicad_pcb. All geometry from
`layout_config.json:auto_fiducials_pcb` (pad / mask opening sizes,
inset distance, count). Run AFTER auto_outline_pcb.
"""
from __future__ import annotations

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


def _layer_of(node: list) -> Optional[str]:
    for child in node[1:]:
        if (isinstance(child, list) and _head(child) == "layer"
                and len(child) >= 2):
            return str(child[1])
    return None


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("auto_fiducials_pcb", {}) or {}
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
# Geometry
# ---------------------------------------------------------------------------

def _edge_bbox(root: list) -> Optional[Tuple[float, float, float, float]]:
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

def _make_fiducial(x: float, y: float, lib_id: str,
                    pad_size_mm: float, mask_size_mm: float,
                    exclude_from_pos: bool) -> list:
    """Build one fiducial footprint node — SMD pad on F.Cu + F.Mask
    opening per IPC-7351 standard. solder_mask_margin = (mask - pad)/2."""
    mask_margin = max(0.0, (mask_size_mm - pad_size_mm) / 2.0)
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
    pad = [
        sexpdata.Symbol("pad"),
        "1",
        sexpdata.Symbol("smd"),
        sexpdata.Symbol("circle"),
        [sexpdata.Symbol("at"), 0, 0],
        [sexpdata.Symbol("size"), pad_size_mm, pad_size_mm],
        [sexpdata.Symbol("layers"), "F.Cu", "F.Mask"],
        [sexpdata.Symbol("uuid"), str(_uuid.uuid4())],
    ]
    if mask_margin > 0:
        pad.append([sexpdata.Symbol("solder_mask_margin"), mask_margin])
    nodes.append(pad)
    return nodes


def _is_fiducial(node: list, lib_id_match: str) -> bool:
    if not (isinstance(node, list) and _head(node) == "footprint"):
        return False
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
    name="auto_fiducials_pcb",
    description=(
        "Place pick-and-place fiducial markers near the corners of a "
        ".kicad_pcb. Default: 3 fiducials (bottom-left, top-left, "
        "top-right) — asymmetric so the P&P machine can determine "
        "board orientation. 1 mm copper pad / 2 mm mask opening per "
        "IPC-7351. Universal — works on any board.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}     # required\n'
        '  {"pcb_path": "...", "count": 2}            # 2-fiducial diagonal\n'
        '  {"pcb_path": "...", "inset_mm": 3.0}       # closer to edge\n'
        '  {"pcb_path": "...", "pad_diameter_mm": 1.5}\n'
        "All defaults in layout_config.json:auto_fiducials_pcb. Run "
        "AFTER auto_outline_pcb. Idempotent — replaces previously-placed "
        "fiducials with the same lib_id unless replace=false."
    ),
    input_schema={"pcb_path": str},
)
async def auto_fiducials_pcb(args: dict[str, Any]) -> dict[str, Any]:
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
                              "text": "auto_fiducials_pcb disabled"}],
                 "is_error": True}

    count   = int(args.get("count", cfg.get("count", 3)))
    inset   = float(args.get("inset_mm", cfg.get("inset_mm", 5.0)))
    pad_d   = float(args.get("pad_diameter_mm",
                              cfg.get("pad_diameter_mm", 1.0)))
    mask_d  = float(args.get("mask_opening_mm",
                              cfg.get("mask_opening_mm", 2.0)))
    lib_id  = str(args.get("library_id",
                            cfg.get("library_id",
                                    "Fiducial:Fiducial_1mm_Mask2mm")))
    exclude = bool(args.get("exclude_from_pos_files",
                              cfg.get("exclude_from_pos_files", True)))
    replace = bool(args.get("replace",
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

    removed = 0
    if replace:
        kept: List[Any] = [root[0]]
        for child in root[1:]:
            if _is_fiducial(child, lib_id):
                removed += 1
                continue
            kept.append(child)
        root = kept

    bl = (x1 + inset, y2 - inset)
    tl = (x1 + inset, y1 + inset)
    tr = (x2 - inset, y1 + inset)
    if count == 2:
        positions = [bl, tr]
    elif count >= 3:
        positions = [bl, tl, tr]
    else:
        positions = [bl]

    for (x, y) in positions:
        x = round(x / 0.01) * 0.01
        y = round(y / 0.01) * 0.01
        root.append(_make_fiducial(x, y, lib_id, pad_d, mask_d, exclude))

    try:
        pcb_path.write_text(_emit(root), encoding="utf-8")
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: write failed: {exc}"}],
                 "is_error": True}

    return {
        "content": [{"type": "text",
                      "text": (f"placed {len(positions)} fiducial(s) on "
                                f"{pcb_path.name}\n"
                                f"  pad: {pad_d} mm  mask opening: {mask_d} mm\n"
                                f"  inset: {inset} mm  lib: {lib_id}\n"
                                f"  removed {removed} pre-existing fiducials")}],
        "ok": True,
        "path": str(pcb_path),
        "placed": len(positions),
        "removed": removed,
        "pad_diameter_mm": pad_d,
        "mask_opening_mm": mask_d,
    }
