"""Tool: silkscreen_cleanup_pcb — move silkscreen text (RefDes / Value) off the
footprint's own pads so it stays legible and survives the solder-mask opening.

DFM problem this solves (research stage 10 / the user's "Text on component",
"Reference hidden"): a reference designator or value printed ON a pad gets clipped
by the mask, is unreadable after assembly, and trips KiCad's "silk over pad" /
silk_overlap DRC. The fix is to relocate the text to the nearest clear position
just outside the part's pad cluster.

Approach (local footprint frame — the same frame KiCad's own silk-over-pad check
uses, so fixing it clears that violation):
  1. For each footprint, build the bbox of every pad (local coords, inflated by
     ``pad_inflate_mm``).
  2. For each relocatable silk text (the property names in ``text_fields`` that
     sit on a ``silk_layers`` layer), estimate its bbox from its string length
     and font size.
  3. If that bbox overlaps any pad, search a ring of candidate positions just
     outside the pad cluster (top / bottom / left / right at growing gaps) and
     move the text to the first candidate whose bbox clears every pad.
  4. If nothing clears within ``max_rings`` and ``hide_if_no_room`` is set, hide
     the text; otherwise leave it and report it.

EVERYTHING is config-driven via ``layout_config.json:silkscreen_cleanup_pcb`` —
layers, clearances, which fields to move, ring geometry, the glyph width factor.
No circuit-specific or magic constants in the code. Universal: works on any
.kicad_pcb. Never raises on a bad node; per-footprint failures are counted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool

# Reuse the hardened serializer so kicad-cli accepts the output.
from .auto_place_pcb import _emit, _head


BBox = Tuple[float, float, float, float]


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("silkscreen_cleanup_pcb", {}) or {}
    except Exception:
        return {}


def _child(node: list, name: str) -> Optional[list]:
    for c in node[1:] if isinstance(node, list) else []:
        if isinstance(c, list) and _head(c) == name:
            return c
    return None


def _layer_of(node: list) -> str:
    c = _child(node, "layer")
    return str(c[1]).strip('"') if c and len(c) >= 2 else ""


def _at(node: list) -> Tuple[float, float, float]:
    c = _child(node, "at")
    if c and len(c) >= 3:
        try:
            return (float(c[1]), float(c[2]),
                    float(c[3]) if len(c) >= 4 else 0.0)
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def _pad_size(pad: list) -> Tuple[float, float]:
    c = _child(pad, "size")
    if c and len(c) >= 3:
        try:
            return (float(c[1]), float(c[2]))
        except (TypeError, ValueError):
            return (0.0, 0.0)
    return (0.0, 0.0)


def _font_size(node: list, default_mm: float) -> Tuple[float, float]:
    eff = _child(node, "effects")
    if eff:
        font = _child(eff, "font")
        if font:
            sz = _child(font, "size")
            if sz and len(sz) >= 3:
                try:
                    return (float(sz[1]), float(sz[2]))
                except (TypeError, ValueError):
                    pass
    return (default_mm, default_mm)


def _text_string(node: list) -> str:
    """The displayed string of a (property "Name" "Value" ...) or
    (fp_text reference "R1" ...) node."""
    if _head(node) == "property" and len(node) >= 3:
        return str(node[2]).strip('"')
    if _head(node) == "fp_text" and len(node) >= 3:
        return str(node[2]).strip('"')
    return ""


def _bbox_overlap(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _text_bbox(cx: float, cy: float, s: str,
               fsx: float, fsy: float,
               char_w_factor: float, pad: float) -> BBox:
    """Approximate axis-aligned bbox of a centre-justified silk string."""
    w = max(len(s), 1) * fsx * char_w_factor
    h = fsy
    return (cx - w / 2 - pad, cy - h / 2 - pad,
            cx + w / 2 + pad, cy + h / 2 + pad)


def _is_hidden(node: list) -> bool:
    # property: (property ... hide) or (property ... (hide yes))
    for c in node[1:]:
        if isinstance(c, sexpdata.Symbol) and c.value() == "hide":
            return True
        if isinstance(c, list) and _head(c) == "hide" and len(c) >= 2:
            return str(c[1]) in ("yes", "true")
    return False


def _set_at(node: list, x: float, y: float, rot: float) -> None:
    c = _child(node, "at")
    if c is None:
        node.append([sexpdata.Symbol("at"), x, y, rot])
        return
    c[1] = round(x / 0.001) * 0.001
    c[2] = round(y / 0.001) * 0.001
    if rot:
        if len(c) >= 4:
            c[3] = rot
        else:
            c.append(rot)


def _candidate_positions(pad_bbox: BBox, texth: float, textw: float,
                         gap: float, step: float, rings: int
                         ) -> List[Tuple[float, float]]:
    """Ring of candidate text centres just outside the pad cluster bbox:
    above, below, left, right, growing outward by ``step`` for ``rings``."""
    x0, y0, x1, y1 = pad_bbox
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    out: List[Tuple[float, float]] = []
    for r in range(rings):
        d = gap + r * step
        out.append((cx, y0 - d - texth / 2.0))   # above
        out.append((cx, y1 + d + texth / 2.0))   # below
        out.append((x1 + d + textw / 2.0, cy))   # right
        out.append((x0 - d - textw / 2.0, cy))   # left
    return out


@tool(
    name="silkscreen_cleanup_pcb",
    description=(
        "DFM silkscreen fix: relocate each footprint's reference-designator / "
        "value text so it no longer sits on a pad (clipped by the mask / "
        "unreadable / silk-over-pad DRC). Searches just outside the part's pad "
        "cluster for the nearest clear spot. Use for 'fix silkscreen', 'move "
        "reference text off pads', 'make silk legible', or as the auto-fix for "
        "silk_overlap. All thresholds in layout_config.json:silkscreen_cleanup_pcb.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}            # apply (default)\n'
        '  {"pcb_path": "...", "preview_only": true}         # dry-run, no write\n'
        '  {"pcb_path": "...", "fields": ["Reference"]}      # only move RefDes'
    ),
    input_schema={"pcb_path": str},
)
async def silkscreen_cleanup_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
                "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "silkscreen_cleanup_pcb disabled"}],
                "is_error": True}

    # ---- All knobs from config (cfg.get fallbacks only) ----
    silk_layers   = set(cfg.get("silk_layers", ["F.SilkS", "B.SilkS"]))
    pad_inflate   = float(cfg.get("pad_inflate_mm", 0.15))
    text_pad      = float(cfg.get("text_clearance_mm", 0.1))
    text_fields   = list(args.get("fields") or cfg.get("text_fields", ["Reference"]))
    char_w_factor = float(cfg.get("char_width_factor", 0.75))
    default_tsize = float(cfg.get("default_text_size_mm", 1.0))
    gap           = float(cfg.get("gap_mm", 0.3))
    step          = float(cfg.get("ring_step_mm", 0.5))
    rings         = int(cfg.get("max_rings", 12))
    hide_no_room  = bool(cfg.get("hide_if_no_room", False))
    preview_only  = bool(args.get("preview_only", cfg.get("preview_only", False)))

    try:
        root = sexpdata.loads(pcb_path.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: parse failed: {exc}"}],
                "is_error": True}
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: not a kicad_pcb"}],
                "is_error": True}

    moved: List[str] = []
    hidden: List[str] = []
    stuck: List[str] = []
    examined = 0

    for fp in root[1:]:
        if not (isinstance(fp, list) and _head(fp) == "footprint"):
            continue
        # Reference designator (for messages)
        ref = ""
        for c in fp[1:]:
            if (isinstance(c, list) and _head(c) == "property"
                    and len(c) >= 3 and str(c[1]).strip('"') == "Reference"):
                ref = str(c[2]).strip('"'); break

        # Pad bboxes (local frame, inflated)
        pad_bboxes: List[BBox] = []
        for c in fp[1:]:
            if isinstance(c, list) and _head(c) == "pad":
                px, py, _r = _at(c)
                pw, ph = _pad_size(c)
                if pw <= 0 and ph <= 0:
                    continue
                pad_bboxes.append((px - pw / 2 - pad_inflate, py - ph / 2 - pad_inflate,
                                   px + pw / 2 + pad_inflate, py + ph / 2 + pad_inflate))
        if not pad_bboxes:
            continue
        # Union pad-cluster bbox for candidate ring geometry
        cluster = (min(b[0] for b in pad_bboxes), min(b[1] for b in pad_bboxes),
                   max(b[2] for b in pad_bboxes), max(b[3] for b in pad_bboxes))

        # Relocatable silk texts on this footprint
        for c in fp[1:]:
            if not (isinstance(c, list) and _head(c) in ("property", "fp_text")):
                continue
            if _layer_of(c) not in silk_layers:
                continue
            if _is_hidden(c):
                continue
            # Only the configured fields (property name, or fp_text "reference"/"value")
            if _head(c) == "property":
                fname = str(c[1]).strip('"') if len(c) >= 2 else ""
            else:  # fp_text — the type token is c[1] (reference/value/user)
                fname = (c[1].value().capitalize()
                         if isinstance(c[1], sexpdata.Symbol) else str(c[1]).capitalize())
            if fname not in text_fields:
                continue
            s = _text_string(c)
            if not s:
                continue
            examined += 1
            tx, ty, trot = _at(c)
            fsx, fsy = _font_size(c, default_tsize)
            tb = _text_bbox(tx, ty, s, fsx, fsy, char_w_factor, text_pad)
            if not any(_bbox_overlap(tb, pb) for pb in pad_bboxes):
                continue                                  # already clear — leave it

            textw = max(len(s), 1) * fsx * char_w_factor
            relocated = False
            for (nx, ny) in _candidate_positions(cluster, fsy, textw, gap, step, rings):
                cb = _text_bbox(nx, ny, s, fsx, fsy, char_w_factor, text_pad)
                if not any(_bbox_overlap(cb, pb) for pb in pad_bboxes):
                    if not preview_only:
                        _set_at(c, nx, ny, trot)
                    moved.append(f"{ref}.{fname}")
                    relocated = True
                    break
            if not relocated:
                if hide_no_room and not preview_only:
                    c.append([sexpdata.Symbol("hide"), sexpdata.Symbol("yes")])
                    hidden.append(f"{ref}.{fname}")
                else:
                    stuck.append(f"{ref}.{fname}")

    if not preview_only and (moved or hidden):
        try:
            pcb_path.write_text(_emit(root), encoding="utf-8")
        except Exception as exc:                          # noqa: BLE001
            return {"content": [{"type": "text", "text": f"ERROR: write failed: {exc}"}],
                    "is_error": True}

    lines = [f"silkscreen_cleanup_pcb -> {pcb_path.name}",
             f"  examined silk texts: {examined}",
             f"  moved off pads:      {len(moved)}"
             + ("  (preview — not written)" if preview_only else "")]
    if hidden:
        lines.append(f"  hidden (no room):    {len(hidden)}")
    if stuck:
        lines.append(f"  could not place:     {len(stuck)} ({', '.join(stuck[:6])})")
    if moved:
        lines.append("  moved: " + ", ".join(moved[:12])
                     + (f" (+{len(moved) - 12} more)" if len(moved) > 12 else ""))

    return {"content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": True, "path": str(pcb_path),
            "moved": moved, "hidden": hidden, "stuck": stuck,
            "examined": examined, "preview_only": preview_only}
