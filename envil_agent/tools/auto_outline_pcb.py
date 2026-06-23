"""Tool: auto-draw a board outline (Edge.Cuts) around the placed
footprints on a .kicad_pcb.

Why: a fresh PCB after Update-PCB-from-Schematic (F8) has no edges
on the Edge.Cuts layer, so KiCad's DRC reports `invalid_outline` and
fab houses reject the Gerbers. This tool computes the bounding box
of all placed footprints, adds a configurable margin, and emits 4
(gr_line ...) segments forming a rectangle — enough to clear DRC and
give the fab a real board outline.

Universal — works on any .kicad_pcb. Config-driven (no hardcoded
margin per board) per `feedback_no_hardcode_json_config`."""
from __future__ import annotations

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


def _at_xy(at_node: list) -> Optional[Tuple[float, float]]:
    try:
        return float(at_node[1]), float(at_node[2])
    except (IndexError, TypeError, ValueError):
        return None


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("auto_outline_pcb", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Serializer (shared style with the other apply tools)
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
        # 6 decimals = nanometer precision; matches kicad-cli's own
        # output. 4 decimals corrupts arc midpoints.
        return f"{node:.10f}".rstrip("0").rstrip(".")
    return str(node)


def _emit(node: Any, indent: int = 0) -> str:
    """Multi-line emit — leading atom children stay on the head line.
    See apply_ops _emit_multiline for the same rationale."""
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


def _make_gr_line(x1: float, y1: float, x2: float, y2: float,
                   stroke_mm: float = 0.1) -> list:
    """Build a (gr_line (start X Y) (end X Y) (stroke (width W) (type
    default)) (layer "Edge.Cuts") (uuid "...")) s-expression node."""
    import uuid as _uuid
    return [
        sexpdata.Symbol("gr_line"),
        [sexpdata.Symbol("start"), x1, y1],
        [sexpdata.Symbol("end"),   x2, y2],
        [sexpdata.Symbol("stroke"),
         [sexpdata.Symbol("width"), stroke_mm],
         [sexpdata.Symbol("type"), sexpdata.Symbol("default")]],
        [sexpdata.Symbol("layer"), "Edge.Cuts"],
        [sexpdata.Symbol("uuid"), str(_uuid.uuid4())],
    ]


@tool(
    name="auto_outline_pcb",
    description=(
        "Draw a rectangular board outline on the Edge.Cuts layer "
        "around all placed footprints. Use after F8 + auto_place_pcb "
        "to clear KiCad's 'invalid_outline' DRC error and produce "
        "Gerbers a fab house will accept.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}        # required\n'
        '  {"pcb_path": "...", "margin_mm": 3.0}        # board edge to nearest part\n'
        '  {"pcb_path": "...", "replace": true}         # delete existing Edge.Cuts first\n'
        "Default margin + stroke come from "
        "layout_config.json:auto_outline_pcb."
    ),
    input_schema={"pcb_path": str},
)
async def auto_outline_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text",
                              "text": f"ERROR: .kicad_pcb not found: {pcb_path}"}],
                 "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text",
                              "text": f"ERROR: expected .kicad_pcb"}],
                 "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                              "text": "auto_outline_pcb disabled"}],
                 "is_error": True}

    margin = float(args.get("margin_mm", cfg.get("margin_mm", 3.0)))
    stroke = float(args.get("stroke_mm", cfg.get("stroke_mm", 0.1)))
    replace = bool(args.get("replace", cfg.get("replace_existing", True)))
    # Half-extent assumed per footprint so the outline clears bodies.
    # Per-footprint courtyard parsing would be more precise but is
    # heavy; this constant matches typical SMD courtyard radius.
    fp_radius = float(cfg.get("footprint_radius_mm", 4.0))
    # R2.2 (2026-05-27). Enforce IPC-2221 minimum copper-to-edge of
    # 0.25 mm — most fab houses (JLCPCB, PCBWay, etc.) reject boards
    # below this. The actual margin used is max(margin_mm,
    # copper_to_edge_min_mm + fp_radius) so a project that explicitly
    # set margin=0 still gets the fab minimum. JSON-gated; project can
    # raise to 0.5 mm for tight V-cut panels.
    copper_to_edge_min = float(cfg.get("copper_to_edge_min_mm", 0.25))
    if margin < fp_radius + copper_to_edge_min:
        margin = fp_radius + copper_to_edge_min

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

    # Collect footprint positions
    xs: List[float] = []
    ys: List[float] = []
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "footprint"):
            continue
        at_node = next((c for c in child[1:]
                         if isinstance(c, list) and _head(c) == "at"), None)
        if at_node is None:
            continue
        xy = _at_xy(at_node)
        if xy is None:
            continue
        xs.append(xy[0])
        ys.append(xy[1])

    if not xs:
        return {"content": [{"type": "text",
                              "text": ("PCB has no footprints. Run "
                                        "F8 (Update PCB from Schematic) "
                                        "first, then re-run this tool.")}],
                 "is_error": True}

    # Bounding box + margin + per-footprint half-extent
    x1 = round(min(xs) - fp_radius - margin, 2)
    y1 = round(min(ys) - fp_radius - margin, 2)
    x2 = round(max(xs) + fp_radius + margin, 2)
    y2 = round(max(ys) + fp_radius + margin, 2)

    # Optional: remove any existing graphics on Edge.Cuts so the new
    # outline is clean. Caller can opt out via replace=False to keep
    # custom cutouts / mounting holes.
    removed_existing = 0
    if replace:
        kept = [root[0]]
        for child in root[1:]:
            keep = True
            if isinstance(child, list) and _head(child) in (
                    "gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly"):
                # Check layer
                lay = next((c for c in child[1:]
                              if isinstance(c, list) and _head(c) == "layer"),
                             None)
                if lay and len(lay) >= 2 and str(lay[1]) == "Edge.Cuts":
                    keep = False
                    removed_existing += 1
            if keep:
                kept.append(child)
        root = kept

    # Emit 4 edge lines (rectangle)
    edges = [
        _make_gr_line(x1, y1, x2, y1, stroke),  # top
        _make_gr_line(x2, y1, x2, y2, stroke),  # right
        _make_gr_line(x2, y2, x1, y2, stroke),  # bottom
        _make_gr_line(x1, y2, x1, y1, stroke),  # left
    ]
    for e in edges:
        root.append(e)

    try:
        pcb_path.write_text(_emit(root), encoding="utf-8")
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: write failed: {exc}"}],
                 "is_error": True}

    w = round(x2 - x1, 2)
    h = round(y2 - y1, 2)
    return {
        "content": [{"type": "text",
                      "text": (f"board outline added: {w} x {h} mm\n"
                                f"  corners: ({x1}, {y1}) -> ({x2}, {y2})\n"
                                f"  margin: {margin} mm, stroke: {stroke} mm\n"
                                f"  removed {removed_existing} existing "
                                f"Edge.Cuts items")}],
        "ok": True,
        "path": str(pcb_path),
        "width_mm": w,
        "height_mm": h,
        "corners": [x1, y1, x2, y2],
    }
