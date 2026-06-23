"""Tool: add ground-pour zones on a .kicad_pcb so the user doesn't
have to manually route every GND track.

Zones are KiCad's filled-copper regions tied to a net (typically GND).
Pads on that net get thermal-relief connections; the rest of the
copper acts as a return path / EMI shield / heatsink.

What the tool does:
  1. Parse the .kicad_pcb
  2. Find the GND-like net (configurable name list)
  3. Use the existing Edge.Cuts polygon (or fall back to footprint
     bounding box) as the zone outline — with the configured inset
  4. Emit one (zone ...) per requested layer (default F.Cu + B.Cu)
  5. Write back

Universal — every flag in `layout_config.json:auto_zones_pcb`."""
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


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("auto_zones_pcb", {}) or {}
    except Exception:
        return {}


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
    """Multi-line emit — leading atom children stay on the head line."""
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


def _find_net_id(root: list, target_names: List[str]) -> Optional[Tuple[int, str]]:
    """Look up the first net whose name matches one of `target_names`
    (case-insensitive). Returns (net_id, actual_name) or None."""
    wanted = {n.upper() for n in target_names}
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "net"):
            continue
        if len(child) < 3:
            continue
        try:
            nid = int(child[1])
        except (ValueError, TypeError):
            continue
        name = str(child[2])
        if name.upper() in wanted:
            return nid, name
    return None


def _edge_bbox(root: list) -> Optional[Tuple[float, float, float, float]]:
    """Find the bbox of all Edge.Cuts gr_line segments. Returns
    (xmin, ymin, xmax, ymax) or None if no edges present."""
    xs, ys = [], []
    for child in root[1:]:
        if not isinstance(child, list):
            continue
        if _head(child) not in ("gr_line", "gr_rect", "gr_arc", "gr_poly"):
            continue
        layer = next((c for c in child[1:]
                       if isinstance(c, list) and _head(c) == "layer"), None)
        if not (layer and len(layer) >= 2 and str(layer[1]) == "Edge.Cuts"):
            continue
        for c in child[1:]:
            if isinstance(c, list) and _head(c) in ("start", "end"):
                try:
                    xs.append(float(c[1])); ys.append(float(c[2]))
                except (IndexError, TypeError, ValueError):
                    pass
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _footprint_bbox(root: list) -> Optional[Tuple[float, float, float, float]]:
    xs, ys = [], []
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "footprint"):
            continue
        at_node = next((c for c in child[1:]
                         if isinstance(c, list) and _head(c) == "at"), None)
        if at_node is None or len(at_node) < 3:
            continue
        try:
            xs.append(float(at_node[1])); ys.append(float(at_node[2]))
        except (IndexError, TypeError, ValueError):
            pass
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _make_zone(net_id: int, net_name: str, layer: str,
                bbox: Tuple[float, float, float, float],
                cfg: Dict[str, Any]) -> list:
    x1, y1, x2, y2 = bbox
    inset = float(cfg.get("inset_mm", 0.5))
    x1 += inset; y1 += inset; x2 -= inset; y2 -= inset

    pad_clearance  = float(cfg.get("pad_clearance_mm", 0.2))
    min_thickness  = float(cfg.get("min_thickness_mm", 0.25))
    thermal_gap    = float(cfg.get("thermal_gap_mm", 0.5))
    thermal_bridge = float(cfg.get("thermal_bridge_mm", 0.5))
    hatch_pitch    = float(cfg.get("hatch_pitch_mm", 0.5))

    return [
        sexpdata.Symbol("zone"),
        [sexpdata.Symbol("net"), net_id],
        [sexpdata.Symbol("net_name"), net_name],
        [sexpdata.Symbol("layer"), layer],
        [sexpdata.Symbol("uuid"), str(_uuid.uuid4())],
        [sexpdata.Symbol("hatch"),
         sexpdata.Symbol("edge"), hatch_pitch],
        [sexpdata.Symbol("connect_pads"),
         [sexpdata.Symbol("clearance"), pad_clearance]],
        [sexpdata.Symbol("min_thickness"), min_thickness],
        [sexpdata.Symbol("filled_areas_thickness"),
         sexpdata.Symbol("no")],
        [sexpdata.Symbol("fill"),
         sexpdata.Symbol("yes"),
         [sexpdata.Symbol("thermal_gap"), thermal_gap],
         [sexpdata.Symbol("thermal_bridge_width"), thermal_bridge]],
        [sexpdata.Symbol("polygon"),
         [sexpdata.Symbol("pts"),
          [sexpdata.Symbol("xy"), x1, y1],
          [sexpdata.Symbol("xy"), x2, y1],
          [sexpdata.Symbol("xy"), x2, y2],
          [sexpdata.Symbol("xy"), x1, y2]]],
    ]


@tool(
    name="auto_zones_pcb",
    description=(
        "Add GND copper pour zones on F.Cu + B.Cu of a .kicad_pcb. "
        "Eliminates ~90% of manual GND track routing. Use after F8 + "
        "auto_place_pcb + auto_outline_pcb.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}              # required\n'
        '  {"pcb_path": "...", "net": "GND"}                  # which net to pour\n'
        '  {"pcb_path": "...", "layers": ["F.Cu","B.Cu"]}    # which layers\n'
        '  {"pcb_path": "...", "replace": true}               # drop existing zones first\n'
        "All zone parameters (inset, thermal gap, min thickness, hatch) "
        "come from layout_config.json:auto_zones_pcb."
    ),
    input_schema={"pcb_path": str},
)
async def auto_zones_pcb(args: dict[str, Any]) -> dict[str, Any]:
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
                              "text": "auto_zones_pcb disabled"}],
                 "is_error": True}

    net_target = args.get("net") or cfg.get("default_net", "GND")
    # Ground net allowlist — `auto_zones_pcb.ground_net_names` in JSON
    # is the authoritative source. Python fallback below is kept tiny
    # (just GND) so a missing key still works; production tuning
    # belongs in the JSON.
    net_candidates = (
        [str(net_target)]
        + [n for n in cfg.get("ground_net_names", ["GND"])
            if n.upper() != str(net_target).upper()]
    )
    layers = list(args.get("layers", cfg.get("layers", ["F.Cu", "B.Cu"])))
    replace = bool(args.get("replace", cfg.get("replace_existing", True)))

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

    net_info = _find_net_id(root, net_candidates)
    if net_info is None:
        return {"content": [{"type": "text",
                              "text": (f"ERROR: no matching net found "
                                        f"(tried {net_candidates}). The PCB "
                                        f"may have no nets yet — run F8 "
                                        f"(Update PCB from Schematic) first.")}],
                 "is_error": True}
    net_id, net_name = net_info

    bbox = _edge_bbox(root)
    if bbox is None:
        bbox = _footprint_bbox(root)
        # Inflate footprint bbox a bit so zones cover the board area
        if bbox is not None:
            x1, y1, x2, y2 = bbox
            inflate = float(cfg.get("footprint_bbox_inflate_mm", 5.0))
            bbox = (x1 - inflate, y1 - inflate, x2 + inflate, y2 + inflate)
    if bbox is None:
        return {"content": [{"type": "text",
                              "text": ("PCB has no Edge.Cuts AND no "
                                        "footprints. Run auto_outline_pcb "
                                        "or F8 first.")}],
                 "is_error": True}

    removed = 0
    if replace:
        kept = [root[0]]
        for child in root[1:]:
            if isinstance(child, list) and _head(child) == "zone":
                nn = next((c for c in child[1:]
                            if isinstance(c, list) and _head(c) == "net_name"),
                           None)
                if nn and len(nn) >= 2 and str(nn[1]) == net_name:
                    removed += 1
                    continue
            kept.append(child)
        root = kept

    added = 0
    for layer in layers:
        zone = _make_zone(net_id, net_name, layer, bbox, cfg)
        root.append(zone)
        added += 1

    try:
        pcb_path.write_text(_emit(root), encoding="utf-8")
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: write failed: {exc}"}],
                 "is_error": True}

    return {
        "content": [{"type": "text",
                      "text": (f"added {added} GND zone(s) on {layers}\n"
                                f"  net: {net_name} (id {net_id})\n"
                                f"  removed {removed} existing {net_name} zone(s)\n"
                                f"  bbox: ({bbox[0]:.1f}, {bbox[1]:.1f}) -> "
                                f"({bbox[2]:.1f}, {bbox[3]:.1f})")}],
        "ok": True,
        "path": str(pcb_path),
        "zones_added": added,
        "net": net_name,
    }
