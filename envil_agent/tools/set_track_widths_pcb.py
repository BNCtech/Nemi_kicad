"""Tool: set_track_widths_pcb — widen under-sized copper tracks to their current
capacity (IPC-2221 / IPC-2152 ampacity).

Research stage 6 ("Track width insufficient -> increase"): a power trace drawn at
the default signal width overheats. The width a trace needs is a deterministic
calculation once you know its current — the AI's job is to KNOW the current. We
infer it dynamically:

  1. classify each net: matches config ``power_net_patterns`` -> "POWER"; an
     explicit name in ``ampacity.net_current_a`` -> that exact current; else
     -> "Default".
  2. each class has a ``class_design_current_a`` (config).
  3. required width = IPC-2221 area equation for that current, copper weight,
     temperature rise and layer, with an optional IPC-2152 correction factor:
        A[mil^2] = ( I / (k * dT^0.44) ) ^ (1/0.725)        (k: ext/int from cfg)
        width    = A / (copper_thickness_in_mils)           (/ ipc2152_factor)
  4. every track segment on that net is widened to max(current_width, required)
     (``only_widen`` — never shrinks a track the user made wide on purpose),
     clamped to ``min_width_mm`` and rounded to ``round_to_mm``.

The equation reproduces the published IPC-2221 chart (1 A, 10 C rise, 1 oz outer
-> ~0.30 mm). EVERYTHING is config-driven via ``layout_config.json:ampacity`` —
no magic numbers, no per-circuit logic. Works on any .kicad_pcb. Never raises;
preview by default? No — like the other PCB mutators it applies and reports,
with ``preview_only`` for a dry run.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool

from .auto_place_pcb import _emit, _head

_MIL_PER_MM = 1.0 / 0.0254


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("ampacity", {}) or {}
    except Exception:
        return {}


def _power_patterns() -> List[str]:
    try:
        from ..intent.engine import _load_layout_config
        return list((_load_layout_config().get("power_net_patterns", {}) or {})
                    .get("patterns", []))
    except Exception:
        return []


def _child(node: list, name: str) -> Optional[list]:
    for c in node[1:] if isinstance(node, list) else []:
        if isinstance(c, list) and _head(c) == name:
            return c
    return None


def _seg_net(seg: list) -> int:
    c = _child(seg, "net")
    if c and len(c) >= 2:
        try:
            return int(c[1])
        except (TypeError, ValueError):
            return 0
    return 0


def _seg_width(seg: list) -> Optional[list]:
    return _child(seg, "width")


def _net_table(root: list) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for c in root[1:]:
        if isinstance(c, list) and _head(c) == "net" and len(c) >= 3:
            try:
                out[int(c[1])] = str(c[2]).strip('"')
            except (TypeError, ValueError):
                pass
    return out


def ipc2221_width_mm(current_a: float, temp_rise_c: float, copper_oz: float,
                     k: float, oz_to_mm: float, factor: float) -> float:
    """IPC-2221 minimum track width for a target current. Returns mm."""
    if current_a <= 0 or temp_rise_c <= 0:
        return 0.0
    area_mil2 = (current_a / (k * (temp_rise_c ** 0.44))) ** (1.0 / 0.725)
    if factor and factor > 0:
        area_mil2 /= factor            # IPC-2152 correction: >1 => narrower OK
    thickness_mil = (copper_oz * oz_to_mm) * _MIL_PER_MM
    if thickness_mil <= 0:
        return 0.0
    width_mil = area_mil2 / thickness_mil
    return width_mil / _MIL_PER_MM


def _classify(net_name: str, power_pats: List[str],
              net_current: Dict[str, float]) -> str:
    up = net_name.upper()
    if net_name in net_current or up in {k.upper() for k in net_current}:
        return "__explicit__"
    for p in power_pats:
        if up.startswith(p.upper()):
            return "POWER"
    return "Default"


@tool(
    name="set_track_widths_pcb",
    description=(
        "AMPACITY: widen copper tracks to their current capacity (IPC-2221 / "
        "IPC-2152). Classifies each net (power rails via config power_net_patterns "
        "-> a design current), computes the required width, and widens every "
        "under-sized track segment on that net (never shrinks). Use for 'fix track "
        "widths', 'power trace too thin', 'make power tracks wider', or as a layout "
        "step before fab. All parameters in layout_config.json:ampacity.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}            # apply\n'
        '  {"pcb_path": "...", "preview_only": true}         # dry-run table only\n'
        '  {"pcb_path": "...", "temp_rise_c": 20}            # override rise'
    ),
    input_schema={"pcb_path": str},
)
async def set_track_widths_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
                "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "ampacity (set_track_widths_pcb) disabled"}],
                "is_error": True}

    # ---- Config knobs (cfg.get fallbacks only) ----
    temp_rise   = float(args.get("temp_rise_c", cfg.get("temp_rise_c", 10.0)))
    copper_oz   = float(cfg.get("copper_weight_oz", 1.0))
    oz_to_mm    = float(cfg.get("copper_thickness_mm_per_oz", 0.0348))
    layer_kind  = str(cfg.get("layer_kind", "external")).lower()
    k_ext       = float(cfg.get("k_external", 0.048))
    k_int       = float(cfg.get("k_internal", 0.024))
    k = k_int if layer_kind.startswith("int") else k_ext
    factor      = float(cfg.get("ipc2152_factor", 1.0))
    min_w       = float(cfg.get("min_width_mm", 0.15))
    round_to    = float(cfg.get("round_to_mm", 0.05))
    only_widen  = bool(cfg.get("only_widen", True))
    class_curr  = dict(cfg.get("class_design_current_a",
                               {"POWER": 2.0, "Default": 0.5}))
    net_current = {str(k2): float(v) for k2, v in
                   (cfg.get("net_current_a", {}) or {}).items()}
    preview_only = bool(args.get("preview_only", cfg.get("preview_only", False)))
    power_pats  = _power_patterns()

    try:
        root = sexpdata.loads(pcb_path.read_text(encoding="utf-8"))
    except Exception as exc:                              # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: parse failed: {exc}"}],
                "is_error": True}
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: not a kicad_pcb"}],
                "is_error": True}

    nt = _net_table(root)

    def _round(w: float) -> float:
        if round_to <= 0:
            return w
        return round(w / round_to) * round_to

    # Required width per net (cache by net id)
    req_by_net: Dict[int, float] = {}
    cls_by_net: Dict[int, str] = {}
    for nid, name in nt.items():
        cls = _classify(name, power_pats, net_current)
        if cls == "__explicit__":
            cur = net_current.get(name) or net_current.get(name.upper(), 0.0)
        else:
            cur = float(class_curr.get(cls, class_curr.get("Default", 0.5)))
        w = ipc2221_width_mm(cur, temp_rise, copper_oz, k, oz_to_mm, factor)
        w = max(w, min_w)
        req_by_net[nid] = _round(w)
        cls_by_net[nid] = "explicit" if cls == "__explicit__" else cls

    # Walk track segments, widen under-sized ones
    widened = 0
    touched_nets: Dict[str, Tuple[float, float]] = {}   # name -> (from, to)
    for seg in root[1:]:
        if not (isinstance(seg, list) and _head(seg) == "segment"):
            continue
        nid = _seg_net(seg)
        req = req_by_net.get(nid, min_w)
        wnode = _seg_width(seg)
        if wnode is None or len(wnode) < 2:
            continue
        try:
            cur_w = float(wnode[1])
        except (TypeError, ValueError):
            continue
        if only_widen and cur_w >= req - 1e-6:
            continue
        new_w = req if only_widen else req
        if abs(new_w - cur_w) < 1e-6:
            continue
        if not preview_only:
            wnode[1] = round(new_w / 0.001) * 0.001
        widened += 1
        nm = nt.get(nid, f"(net {nid})")
        prev = touched_nets.get(nm)
        touched_nets[nm] = (prev[0] if prev else cur_w, new_w)

    if not preview_only and widened:
        try:
            pcb_path.write_text(_emit(root), encoding="utf-8")
        except Exception as exc:                          # noqa: BLE001
            return {"content": [{"type": "text", "text": f"ERROR: write failed: {exc}"}],
                    "is_error": True}

    # Report: per-class required width + segments widened
    by_cls: Dict[str, float] = {}
    for nid, w in req_by_net.items():
        by_cls.setdefault(cls_by_net[nid], w)
    lines = [f"set_track_widths_pcb -> {pcb_path.name}  "
             f"(IPC-2221, {copper_oz:g}oz {layer_kind}, dT={temp_rise:g}C"
             + (f", IPC-2152 x{factor:g}" if factor and factor != 1.0 else "") + ")",
             "  required width by class:"]
    for cls, w in sorted(by_cls.items()):
        cur = (net_current and cls == "explicit")
        lines.append(f"    {cls}: {w:.2f} mm")
    lines.append(f"  segments widened: {widened}"
                 + ("  (preview — not written)" if preview_only else ""))
    for nm, (a, b) in list(touched_nets.items())[:10]:
        lines.append(f"    {nm}: {a:.2f} -> {b:.2f} mm")
    if len(touched_nets) > 10:
        lines.append(f"    ... (+{len(touched_nets) - 10} more nets)")

    return {"content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": True, "path": str(pcb_path),
            "widened": widened,
            "required_width_by_class": by_cls,
            "preview_only": preview_only}
