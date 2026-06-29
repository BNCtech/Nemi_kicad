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
                    exclude_from_pos: bool, pour_clearance_mm: float = 0.0) -> list:
    """Build one fiducial footprint node — SMD pad on F.Cu + F.Mask
    opening per IPC-7351 standard. solder_mask_margin = (mask - pad)/2.

    ``pour_clearance_mm`` is written as the pad's own ``clearance`` so the GND
    copper pour keeps that distance from the fiducial — the STANDARD fix for the
    "solder mask aperture bridges different nets" DRC (the pour must stay outside
    the fiducial's mask opening). Fiducials are meant to be isolated copper."""
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
    if pour_clearance_mm > 0:
        pad.append([sexpdata.Symbol("clearance"), round(pour_clearance_mm, 3)])
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

def _rot(dx: float, dy: float, deg: float) -> Tuple[float, float]:
    if not deg:
        return dx, dy
    import math
    a = math.radians(-deg)          # KiCad places CW (-deg), board Y down
    c, s = math.cos(a), math.sin(a)
    return c * dx - s * dy, s * dx + c * dy


def _pad_keepouts(root: list) -> List[Tuple[float, float, float]]:
    """(x, y, radius) keep-out circle for every existing pad on the board, in
    board coords — radius = half the pad's larger dimension + a mask margin, so a
    fiducial kept this far from the centre cannot bridge its solder mask."""
    import math
    outs: List[Tuple[float, float, float]] = []
    for fp in root[1:]:
        if not (isinstance(fp, list) and _head(fp) == "footprint"):
            continue
        fat = next((c for c in fp[1:] if isinstance(c, list) and _head(c) == "at"), None)
        try:
            fx, fy = float(fat[1]), float(fat[2])
            fr = float(fat[3]) if len(fat) > 3 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        for ch in fp[1:]:
            if not (isinstance(ch, list) and _head(ch) == "pad"):
                continue
            pat = next((c for c in ch[1:] if isinstance(c, list) and _head(c) == "at"), None)
            psz = next((c for c in ch[1:] if isinstance(c, list) and _head(c) == "size"), None)
            try:
                px, py = float(pat[1]), float(pat[2])
                w, h = float(psz[1]), float(psz[2])
            except (TypeError, ValueError, IndexError):
                continue
            dx, dy = _rot(px, py, fr)
            outs.append((fx + dx, fy + dy, max(w, h) / 2.0))
    return outs


def _place_clear(x: float, y: float, keepouts: List[Tuple[float, float, float]],
                 fid_r: float, clearance: float, cx: float, cy: float,
                 step: float, max_steps: int) -> Tuple[float, float]:
    """Nudge a fiducial corner candidate TOWARD the board centre until it clears
    every existing pad's keep-out by `clearance`. Inward is safe — it moves away
    from the corner mounting holes. Returns the first clear spot (or the best try)."""
    import math
    def _clear(tx: float, ty: float) -> bool:
        for (kx, ky, kr) in keepouts:
            if math.hypot(tx - kx, ty - ky) < (fid_r + kr + clearance):
                return False
        return True
    if _clear(x, y):
        return x, y
    vx, vy = cx - x, cy - y
    d = math.hypot(vx, vy) or 1.0
    ux, uy = vx / d, vy / d
    for i in range(1, max_steps + 1):
        nx, ny = x + ux * step * i, y + uy * step * i
        if _clear(nx, ny):
            return nx, ny
    return x + ux * step * max_steps, y + uy * step * max_steps


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

    # Keep each fiducial clear of every existing pad (mounting holes especially)
    # so its solder-mask opening can't bridge them — the root cause of the
    # solder_mask_bridge DRC errors. Dynamic: a clearance search, no hardcoded
    # positions. clearance from config (min_clearance_mm).
    keepouts = _pad_keepouts(root)
    clr = float(args.get("min_clearance_mm", cfg.get("min_clearance_mm", 0.5)))
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    fid_r = mask_d / 2.0
    # Pad clearance that keeps the GND pour OUTSIDE the fiducial's mask opening:
    # mask extends (mask-pad)/2 past the pad edge; add a mask gap so the pour's
    # copper is clear of the opening (kills the solder_mask_bridge DRC). Dynamic
    # from the fiducial's own geometry — works for any pad/mask size.
    pour_clr = (mask_d - pad_d) / 2.0 + float(cfg.get("pour_clearance_extra_mm", 0.3))
    for (x, y) in positions:
        x, y = _place_clear(x, y, keepouts, fid_r, clr, cx, cy,
                            step=0.5, max_steps=40)
        x = round(x / 0.01) * 0.01
        y = round(y / 0.01) * 0.01
        root.append(_make_fiducial(x, y, lib_id, pad_d, mask_d, exclude, pour_clr))

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
