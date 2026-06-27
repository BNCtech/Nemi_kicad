"""Tool: pcb_quality — the scored "perfect PCB" analyzer.

The one artifact that ties the user's two design specs together: instead of
the user manually running "check placement, check routing, run DRC", this
walks a ``.kicad_pcb`` and emits ONE scored card —

    PCB QUALITY — proj.kicad_pcb
    Placement        82%
    Routing          64%
    Power Integrity  90%
    Signal / Layers  100%
    Manufacturing    95%
    ─────────────────────
    Overall: 84% — GOOD

    ⚠ C12 is 18.0 mm from U1
       Reason: decoupling cap should sit at the IC pin
       Fix → auto_place_pcb

— exactly the "Understand → Detect → Explain → Suggest → Apply" loop.

Design rules (all of them):
  * READ-ONLY. This tool never mutates the board. Every finding instead
    carries a ``fix`` pointing at an EXISTING mutating tool
    (auto_place_pcb / auto_outline_pcb / set_track_widths_pcb /
    auto_zones_pcb / route_pcb_simple / drc_autofix) so the chat can render
    a real "Apply Fix" button that calls something that already works.
  * Additive + config-driven. Every threshold / weight lives in
    ``layout_config.json:pcb_quality``; nothing hardcoded, nothing in the
    existing pipeline changes.
  * Degrades gracefully. A check that can't run (no Edge.Cuts, DRC
    unavailable, parse hiccup) is reported as "n/a" and dropped from the
    weighted average rather than scoring the board to zero.

Dimensions map 1:1 onto the user's "perfect PCB" + "layer rules" lists:
  Placement     — decoupling-cap distance, board-area efficiency, courtyard overlap
  Routing       — fraction of must-route nets that actually have copper
  Power         — power-net track widths vs IPC-2221 ampacity, GND copper pour
  Signal/Layers — copper-layer count, continuous GND plane, via presence
  Manufacturing — real kicad-cli DRC + every footprint inside the board outline
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool

# Reuse the proven ampacity math rather than re-deriving IPC-2221 here.
from .set_track_widths_pcb import ipc2221_width_mm, _classify
# Shared s-expr + banding helpers — one definition, not a 21st private copy.
from ._pcb_sexpr import (head as _head, child as _child, children as _children,
                         atom as _atom, at as _at, prop as _prop, rot as _rot,
                         band_for)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("pcb_quality", {}) or {}
    except Exception:
        return {}


def _ampacity_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("ampacity", {}) or {}
    except Exception:
        return {}


def _power_patterns() -> List[str]:
    try:
        from ..intent.engine import _load_layout_config
        c = _load_layout_config()
        # Both keys carry power-net prefixes; merge so either convention works.
        a = list((c.get("power_net_patterns", {}) or {}).get("patterns", []))
        return a
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Board model
# --------------------------------------------------------------------------- #

class _FP:
    __slots__ = ("ref", "value", "fpid", "x", "y", "rot", "layer",
                 "pads", "bbox")

    def __init__(self) -> None:
        self.ref = ""
        self.value = ""
        self.fpid = ""
        self.x = self.y = self.rot = 0.0
        self.layer = "F.Cu"
        self.pads: List[Tuple[int, float, float]] = []   # (net_idx, abs_x, abs_y)
        self.bbox: Optional[Tuple[float, float, float, float]] = None


def _parse_board(root: list) -> Tuple[List[_FP], Dict[int, str],
                                      Dict[int, List[float]],
                                      List[Tuple[float, float]],
                                      Optional[Tuple[float, float, float, float]],
                                      int, List[Tuple[int, str]]]:
    """Return (footprints, net_names, net_widths, via_nets, edge_bbox,
    copper_layers, zone_nets)."""
    fps: List[_FP] = []
    net_names: Dict[int, str] = {}
    net_widths: Dict[int, List[float]] = {}     # net idx -> [seg widths]
    via_pts: List[Tuple[float, float]] = []
    edge_pts: List[Tuple[float, float]] = []
    zone_nets: List[Tuple[int, str]] = []
    copper_layers = 2

    for node in root[1:] if isinstance(root, list) else []:
        h = _head(node)
        if h == "net" and len(node) >= 3:
            try:
                net_names[int(node[1])] = str(node[2]).strip('"')
            except (TypeError, ValueError):
                pass
        elif h == "layers":
            # Top-level (layers ...) table: each entry is
            # (ordinal "Name" type [user_name]). Copper layers are the
            # ones whose canonical name ends in ".Cu" (F.Cu, In1.Cu, B.Cu).
            cu = 0
            for ln in node[1:]:
                if isinstance(ln, list) and len(ln) >= 2 \
                        and str(ln[1]).strip('"').endswith(".Cu"):
                    cu += 1
            if cu:
                copper_layers = cu
        elif h in ("segment", "arc"):
            n = _child(node, "net")
            w = _child(node, "width")
            if n and len(n) >= 2:
                try:
                    idx = int(n[1])
                    wid = float(w[1]) if w and len(w) >= 2 else 0.0
                    net_widths.setdefault(idx, []).append(wid)
                except (TypeError, ValueError):
                    pass
        elif h == "via":
            x, y, _ = _at(node)
            via_pts.append((x, y))
        elif h == "zone":
            n = _child(node, "net")
            nn = _child(node, "net_name")
            if n and len(n) >= 2:
                try:
                    zone_nets.append((int(n[1]), _atom(nn, 1) or ""))
                except (TypeError, ValueError):
                    pass
        elif h in ("gr_line", "gr_rect", "gr_poly", "gr_arc"):
            lay = _child(node, "layer")
            if lay and _atom(lay, 1) == "Edge.Cuts":
                for key in ("start", "end", "center", "mid"):
                    p = _child(node, key)
                    if p and len(p) >= 3:
                        try:
                            edge_pts.append((float(p[1]), float(p[2])))
                        except (TypeError, ValueError):
                            pass
                ptsn = _child(node, "pts")
                if ptsn:
                    for xy in _children(ptsn, "xy"):
                        if len(xy) >= 3:
                            try:
                                edge_pts.append((float(xy[1]), float(xy[2])))
                            except (TypeError, ValueError):
                                pass
        elif h == "footprint":
            fp = _FP()
            fp.fpid = _atom(node, 1) or ""
            fp.x, fp.y, fp.rot = _at(node)
            lay = _child(node, "layer")
            fp.layer = _atom(lay, 1) or "F.Cu"
            fp.ref = _prop(node, "Reference")
            fp.value = _prop(node, "Value")
            xs: List[float] = []
            ys: List[float] = []
            for pad in _children(node, "pad"):
                px, py, _pr = _at(pad)
                dx, dy = _rot(px, py, fp.rot)
                ax, ay = fp.x + dx, fp.y + dy
                netn = _child(pad, "net")
                nidx = 0
                if netn and len(netn) >= 2:
                    try:
                        nidx = int(netn[1])
                    except (TypeError, ValueError):
                        nidx = 0
                fp.pads.append((nidx, ax, ay))
                sz = _child(pad, "size")
                pw = float(sz[1]) if sz and len(sz) >= 2 else 0.0
                ph = float(sz[2]) if sz and len(sz) >= 3 else 0.0
                xs += [ax - pw / 2, ax + pw / 2]
                ys += [ay - ph / 2, ay + ph / 2]
            if xs:
                fp.bbox = (min(xs), min(ys), max(xs), max(ys))
            fps.append(fp)

    edge_bbox: Optional[Tuple[float, float, float, float]] = None
    if edge_pts:
        xs = [p[0] for p in edge_pts]
        ys = [p[1] for p in edge_pts]
        edge_bbox = (min(xs), min(ys), max(xs), max(ys))
    return fps, net_names, net_widths, via_pts, edge_bbox, copper_layers, zone_nets


# --------------------------------------------------------------------------- #
# Small geometry / value helpers
# --------------------------------------------------------------------------- #

def _area(b: Optional[Tuple[float, float, float, float]]) -> float:
    if not b:
        return 0.0
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a: Tuple[float, float, float, float],
             b: Tuple[float, float, float, float], clr: float) -> bool:
    return not (a[2] + clr < b[0] or a[0] - clr > b[2]
                or a[3] + clr < b[1] or a[1] - clr > b[3])


# Default decoupling-cap value patterns — overridable via
# layout_config.json:pcb_quality.decap_value_patterns so "what is a decap"
# is data, never a code assumption about a particular circuit.
_DECAP_DEFAULT_PATTERNS = [r"^\s*100\s*n", r"^\s*0\.1\s*[uµ]", r"^\s*0u1", r"^\s*n100"]


def _decap_matcher(cfg: Dict[str, Any]):
    pats = list(cfg.get("decap_value_patterns", _DECAP_DEFAULT_PATTERNS))
    compiled = []
    for p in pats:
        try:
            compiled.append(re.compile(p, re.I))
        except re.error:
            continue

    def _is_decap(value: str) -> bool:
        v = value or ""
        return any(rx.match(v) for rx in compiled)

    return _is_decap


def _is_ic(fp: _FP, ic_prefixes: List[str], ic_min_pads: int) -> bool:
    up = (fp.ref or "").upper()
    if any(up.startswith(p.upper()) for p in ic_prefixes):
        return True
    return len(fp.pads) >= ic_min_pads


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def analyze(root: list, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Pure analysis: take a parsed board, return the scored report dict.

    No I/O, no DRC (DRC is folded in by the async tool wrapper which can
    await it). Unit-testable in isolation.
    """
    (fps, net_names, net_widths, vias, edge_bbox,
     copper_layers, zone_nets) = _parse_board(root)

    ic_prefixes = list(cfg.get("ic_ref_prefixes", ["U"]))
    ic_min_pads = int(cfg.get("ic_min_pads", 6))
    decap_max_mm = float(cfg.get("decap_max_mm", 5.0))
    min_efficiency = float(cfg.get("min_board_efficiency", 0.35))
    stack_min_mm = float(cfg.get("stack_min_mm", 0.5))
    is_decap = _decap_matcher(cfg)

    findings: List[Dict[str, Any]] = []
    dims: Dict[str, Optional[float]] = {}

    def add(sev: str, msg: str, reason: str, tool_name: str,
            args: Dict[str, Any]) -> None:
        findings.append({"severity": sev, "message": msg, "reason": reason,
                         "fix_tool": tool_name, "fix_args": args})

    ics = [f for f in fps if _is_ic(f, ic_prefixes, ic_min_pads)]

    # ---------------- Placement ---------------- #
    place_scores: List[float] = []
    # (a) decoupling-cap proximity
    decaps = [f for f in fps if is_decap(f.value)]
    if decaps and ics:
        ok = 0
        for c in decaps:
            nearest = min(ics, key=lambda u: math.hypot(u.x - c.x, u.y - c.y))
            d = math.hypot(nearest.x - c.x, nearest.y - c.y)
            if d <= decap_max_mm:
                ok += 1
            else:
                add("warn",
                    f"{c.ref} is {d:.1f} mm from {nearest.ref}",
                    "decoupling cap should sit at the IC power pin (< "
                    f"{decap_max_mm:.0f} mm)",
                    "auto_place_pcb", {})
        place_scores.append(100.0 * ok / len(decaps))
    # (b) stacked-parts detector — the real auto-place failure mode is
    # footprints landing on top of each other (centres coincident at 0,0),
    # NOT a tight-but-valid decap. We flag only near-coincident CENTRES so a
    # cap placed 1 mm off an IC (ideal!) never trips it. True courtyard
    # overlap at a distance is left to kicad-cli DRC (folded into Mfg).
    boxed = [f for f in fps if f.bbox]
    stacked = 0
    for i in range(len(boxed)):
        for j in range(i + 1, len(boxed)):
            if math.hypot(boxed[i].x - boxed[j].x,
                          boxed[i].y - boxed[j].y) <= stack_min_mm:
                stacked += 1
    if boxed:
        place_scores.append(100.0 if stacked == 0
                            else max(0.0, 100.0 - 25.0 * stacked))
        if stacked:
            add("error",
                f"{stacked} stacked footprint pair(s)",
                "components sit on top of each other — placement failed / unbuildable",
                "auto_place_pcb", {})
    dims["Placement"] = (sum(place_scores) / len(place_scores)
                         if place_scores else None)

    # (c) board-area efficiency — ADVISORY ONLY, never scored. Pad-area ÷
    # board-area is biased low (ignores routing channels, connectors,
    # keepout, mounting holes) so it's unfit to grade on; we only surface a
    # shrink suggestion when the board is egregiously empty.
    used = sum(_area(f.bbox) for f in fps)
    board_area = _area(edge_bbox)
    if board_area > 0 and used > 0:
        eff = min(1.0, used / board_area)
        if eff < min_efficiency:
            add("info",
                f"board ~{(1 - eff) * 100:.0f}% empty "
                f"({board_area:.0f} mm² outline, {used:.0f} mm² of parts)",
                "outline may be oversized — consider shrink-to-fit",
                "auto_outline_pcb", {})

    # ---------------- Routing ---------------- #
    # nets that must be routed = appear on >= 2 pads
    pad_net_count: Dict[int, int] = {}
    for f in fps:
        for nidx, _x, _y in f.pads:
            if nidx > 0:
                pad_net_count[nidx] = pad_net_count.get(nidx, 0) + 1
    must_route = {n for n, c in pad_net_count.items() if c >= 2}
    # A net is satisfied by copper segments OR a filled zone (pour) on it —
    # GND/power served by a plane has no segments yet is correctly connected.
    pour_nets = {zn[0] for zn in zone_nets}
    if must_route:
        routed = {n for n in must_route if net_widths.get(n) or n in pour_nets}
        frac = len(routed) / len(must_route)
        dims["Routing"] = 100.0 * frac
        if frac < 1.0:
            miss = sorted(net_names.get(n, str(n)) for n in (must_route - routed))
            add("warn",
                f"{len(must_route) - len(routed)} of {len(must_route)} nets unrouted",
                "no copper between pads — only ratsnest: "
                + ", ".join(miss[:6]) + (" …" if len(miss) > 6 else ""),
                "route_pcb_simple", {})
    else:
        dims["Routing"] = None

    # ---------------- Power integrity ---------------- #
    amp = _ampacity_cfg()
    power_pats = _power_patterns()
    class_curr = dict(amp.get("class_design_current_a",
                              {"POWER": 2.0, "Default": 0.5}))
    net_current = {str(k): float(v) for k, v in (amp.get("net_current_a", {}) or {}).items()}
    temp_rise = float(amp.get("temp_rise_c", 10.0))
    copper_oz = float(amp.get("copper_weight_oz", 1.0))
    oz_to_mm = float(amp.get("copper_thickness_mm_per_oz", 0.0348))
    k = float(amp.get("k_external", 0.048))
    factor = float(amp.get("ipc2152_factor", 1.0))

    power_nets = [n for n in must_route
                  if _classify(net_names.get(n, ""), power_pats, net_current) in ("POWER", "__explicit__")]
    pwr_scores: List[float] = []
    if power_nets:
        ok = 0
        checked = 0
        for n in power_nets:
            if n in pour_nets:
                # Served by a copper pour — ampacity is the plane's job,
                # not a trace width. Skip from the width tally.
                continue
            checked += 1
            nm = net_names.get(n, "")
            cur = net_current.get(nm) or net_current.get(nm.upper()) or class_curr.get("POWER", 2.0)
            req = ipc2221_width_mm(float(cur), temp_rise, copper_oz, k, oz_to_mm, factor)
            widths = net_widths.get(n) or []
            have = min(widths) if widths else 0.0
            if have >= req * 0.95 and have > 0:
                ok += 1
            elif req > 0:
                add("warn",
                    f"{nm}: track {have:.2f} mm, needs {req:.2f} mm for {cur:g} A",
                    "under-sized power trace — IPC-2221 ampacity",
                    "set_track_widths_pcb", {})
        if checked:
            pwr_scores.append(100.0 * ok / checked)
    # GND copper pour present? Match the ground token (GND / AGND / DGND /
    # PGND / GNDA …) plus VSS / EARTH — NOT bare "D…" which would catch DATA.
    gnd_pat = re.compile(cfg.get("gnd_net_regex", r"GND|^VSS|^EARTH"), re.I)
    gnd_net_idxs = {n for n in net_names if gnd_pat.search(net_names.get(n, ""))}
    has_gnd_pour = any(zn[0] in gnd_net_idxs or gnd_pat.search(zn[1] or "")
                       for zn in zone_nets)
    if gnd_net_idxs:
        pwr_scores.append(100.0 if has_gnd_pour else 40.0)
        if not has_gnd_pour:
            add("warn", "no GND copper pour",
                "ground plane missing — high impedance return path / EMI",
                "auto_zones_pcb", {})
    dims["Power Integrity"] = (sum(pwr_scores) / len(pwr_scores)
                               if pwr_scores else None)

    # ---------------- Signal / Layers ---------------- #
    sig_scores: List[float] = []
    # continuous ground plane = GND pour exists (re-uses the check above)
    if gnd_net_idxs:
        sig_scores.append(100.0 if has_gnd_pour else 50.0)
    # 4-layer boards should actually use inner vias; 2-layer is fine as-is
    if copper_layers >= 4:
        sig_scores.append(100.0 if vias else 70.0)
        if not vias:
            add("info", f"{copper_layers}-layer board with no vias",
                "inner GND/power planes unreachable without vias",
                "route_pcb_simple", {})
    else:
        sig_scores.append(100.0)
    dims["Signal / Layers"] = sum(sig_scores) / len(sig_scores) if sig_scores else None

    # ---------------- Manufacturing (DRC folded in by wrapper) ---------------- #
    # components inside the board outline
    mfg_scores: List[float] = []
    if edge_bbox:
        outside = [f for f in boxed
                   if not _overlap(f.bbox, edge_bbox, 0.0)]
        mfg_scores.append(100.0 if not outside
                          else max(0.0, 100.0 - 25.0 * len(outside)))
        for f in outside:
            add("error", f"{f.ref} sits outside the board outline",
                "footprint is off the board — will not manufacture",
                "auto_outline_pcb", {})
    dims["Manufacturing"] = sum(mfg_scores) / len(mfg_scores) if mfg_scores else None

    return {
        "dims": dims,
        "findings": findings,
        "stats": {
            "footprints": len(fps),
            "ics": len(ics),
            "decaps": len(decaps),
            "nets": len([n for n in net_names if n > 0]),
            "must_route_nets": len(must_route),
            "power_nets": len(power_nets),
            "copper_layers": copper_layers,
            "vias": len(vias),
            "has_gnd_pour": has_gnd_pour,
            "board_mm2": round(board_area, 1),
            "stacked_pairs": stacked,
        },
    }


def _compose_card(name: str, report: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[str, float]:
    weights = dict(cfg.get("weights", {
        "Placement": 0.20, "Routing": 0.20, "Power Integrity": 0.25,
        "Signal / Layers": 0.15, "Manufacturing": 0.20,
    }))
    bands = [(float(t), str(l)) for t, l in cfg.get("bands", [
        [90, "EXCELLENT"], [75, "GOOD"], [60, "REVIEW"], [0, "NEEDS WORK"],
    ])]

    dims = report["dims"]
    # weighted overall over the dimensions that actually ran (renormalise)
    num = den = 0.0
    for d, sc in dims.items():
        if sc is None:
            continue
        w = float(weights.get(d, 0.0))
        num += w * sc
        den += w
    overall = (num / den) if den else 0.0
    # The user prefers plain words over a percentage (it reads as a grade, not
    # actionable). When show_percentage is false the card reports each dimension
    # and the overall as a verdict WORD (the band label), not a number.
    show_pct = bool(cfg.get("show_percentage", True))

    lines = [f"# PCB QUALITY — {name}"]
    for d in ["Placement", "Routing", "Power Integrity", "Signal / Layers", "Manufacturing"]:
        sc = dims.get(d)
        if sc is None:
            bar = "n/a"
        elif show_pct:
            bar = f"{sc:5.0f}%"
        else:
            bar = band_for(sc, bands)
        lines.append(f"  {d:<16}{bar}")
    lines.append("  " + "─" * 21)
    if show_pct:
        lines.append(f"  **Overall: {overall:.0f}% — {band_for(overall, bands)}**")
    else:
        lines.append(f"  **Overall: {band_for(overall, bands)}**")

    fixes = report["findings"]
    if fixes:
        lines.append("")
        order = {"error": 0, "warn": 1, "info": 2}
        for f in sorted(fixes, key=lambda x: order.get(x["severity"], 3)):
            mark = {"error": "✗", "warn": "⚠", "info": "•"}.get(f["severity"], "•")
            lines.append(f"  {mark} {f['message']}")
            lines.append(f"      reason: {f['reason']}")
            lines.append(f"      Fix → {f['fix_tool']}")
    else:
        lines.append("")
        lines.append("  ✓ no issues found")
    return "\n".join(lines), overall


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #

@tool(
    name="pcb_quality",
    description=(
        "Score a .kicad_pcb like a senior PCB reviewer and return ONE card: "
        "Placement / Routing / Power / Signal-Layers / Manufacturing percentages, "
        "an overall 'perfect PCB' score + verdict, and an ordered list of "
        "Apply-Fix suggestions. READ-ONLY — never mutates the board; each fix "
        "names an existing tool (auto_place_pcb, auto_outline_pcb, "
        "set_track_widths_pcb, auto_zones_pcb, route_pcb_simple, drc_autofix) "
        "the user/agent can then run. Use for 'check my PCB', 'is this board "
        "good', 'PCB quality / score / review', or before ship_design.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}            # full report (incl. DRC)\n'
        '  {"pcb_path": "...", "skip_drc": true}            # fast, static-only\n'
        "All thresholds/weights in layout_config.json:pcb_quality."
    ),
    input_schema={"pcb_path": str},
)
async def pcb_quality(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
                "is_error": True}

    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "pcb_quality disabled in layout_config.json"}],
                "is_error": True}

    try:
        root = sexpdata.loads(pcb_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: could not parse board: {exc}"}],
                "is_error": True}

    report = analyze(root, cfg)

    # Fold in REAL DRC as the Manufacturing signal (kicad-cli), unless skipped.
    if not args.get("skip_drc") and (cfg.get("steps", {}) or {}).get("drc", True):
        try:
            import importlib
            DRC = importlib.import_module("envil_agent.tools.drc_check")
            r = await DRC.drc_check.handler({"pcb_path": str(pcb_path)})
            if not r.get("is_error"):
                err = int(r.get("error_count", 0) or 0)
                warn = int(r.get("warning_count", 0) or 0)
                if err >= 0:
                    drc_score = 100.0 if err == 0 else max(0.0, 100.0 - 10.0 * err)
                    prev = report["dims"].get("Manufacturing")
                    report["dims"]["Manufacturing"] = (
                        drc_score if prev is None else (prev + drc_score) / 2.0)
                    report["stats"]["drc_errors"] = err
                    report["stats"]["drc_warnings"] = warn
                    if err > 0:
                        report["findings"].append({
                            "severity": "error",
                            "message": f"DRC: {err} error(s), {warn} warning(s)",
                            "reason": "real kicad-cli design-rule violations",
                            "fix_tool": "drc_autofix", "fix_args": {"pcb_path": str(pcb_path)},
                        })
        except Exception:
            report["stats"]["drc"] = "unavailable"

    card, overall = _compose_card(pcb_path.name, report, cfg)

    return {
        "content": [{"type": "text", "text": card}],
        "ok": True,
        "pcb_path": str(pcb_path),
        "overall": round(overall, 1),
        "dimensions": report["dims"],
        "fixes": report["findings"],
        "stats": report["stats"],
    }
