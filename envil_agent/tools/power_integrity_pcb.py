"""Tool: power_integrity_pcb — voltage-drop / current-density REPORT for a routed
.kicad_pcb (Phase 2 of the Universal PCB AI Engine).

This is a READ-ONLY check, the power-side twin of drc_check. For every POWER net
it measures the routed copper straight from the board, infers the net's design
current, and computes the physics:

    R      = rho * L / A            (trace resistance)
    Vdrop  = I * R                  (+ optional per-via resistance)
    J      = I / A                  (current density)

then flags a WARNING when the drop or density exceeds the thresholds in
``config/power_integrity.json`` OR the net's narrowest track is thinner than the
IPC-2152 recommended width for its current. Nothing is mutated — the tool hands
back a table + a word verdict so the user (or an autofixer that calls
``set_track_widths_pcb``) can act.

Current inference reuses the SAME sources as the ampacity tool (no new guesswork):
  1. ``net_currents`` arg override           {"+12V": 8.0}
  2. ``layout_config.json:ampacity.net_current_a`` explicit table
  3. a current parsed from the net NAME       ("MOTOR_5A" -> 5 A)
  4. a power-net pattern match                -> ampacity class_design_current_a["POWER"]
  5. otherwise the net is treated as signal and skipped.

Rail voltage (for the Vdrop %) comes from ``net_voltages`` arg or the net name
("+12V" -> 12 V); unknown -> absolute drop only. Universal, config-driven, never
raises.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool

from .auto_place_pcb import _head
from .set_track_widths_pcb import _net_table, _seg_net, _child, _power_patterns


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _load_json(name: str) -> Dict[str, Any]:
    try:
        return json.loads((_config_dir() / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _load_cfg() -> Dict[str, Any]:
    return _load_json("power_integrity.json")


def _ampacity_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("ampacity", {}) or {}
    except Exception:
        return {}


def _ipc_width_bands() -> List[Tuple[float, float]]:
    """IPC-2152 (max_a, width_mm) bands, sorted by current. Same table the
    IPC-2152 DRU rules use — one source of truth for 'recommended width'."""
    bands = ((_load_json("ipc_constraints.json").get("width_by_current", {}) or {})
             .get("bands") or [])
    out: List[Tuple[float, float]] = []
    for b in bands:
        try:
            out.append((float(b.get("max_a")), float(b.get("width_mm"))))
        except (TypeError, ValueError):
            continue
    return sorted(out)


def _ipc_recommended_width(current_a: float,
                           bands: List[Tuple[float, float]]) -> Optional[float]:
    """Smallest IPC band whose current covers `current_a` -> its width. Above the
    top band, scale the top width linearly with current (area ~ current)."""
    if not bands:
        return None
    for max_a, width in bands:
        if current_a <= max_a:
            return width
    top_a, top_w = bands[-1]
    return top_w * (current_a / top_a) if top_a > 0 else top_w


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def _xy(node: list, tag: str) -> Optional[Tuple[float, float]]:
    c = _child(node, tag)
    if c and len(c) >= 3:
        try:
            return (float(c[1]), float(c[2]))
        except (TypeError, ValueError):
            return None
    return None


def _seg_len_mm(seg: list) -> float:
    s = _xy(seg, "start")
    e = _xy(seg, "end")
    if not s or not e:
        return 0.0
    return math.hypot(e[0] - s[0], e[1] - s[1])


def _arc_len_mm(arc: list) -> float:
    """Length of an (arc (start)(mid)(end)...) track. Fits the circle through the
    three points; degenerate/collinear -> straight chord fallback."""
    s = _xy(arc, "start")
    m = _xy(arc, "mid")
    e = _xy(arc, "end")
    if not s or not e:
        return 0.0
    if not m:
        return math.hypot(e[0] - s[0], e[1] - s[1])
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return math.hypot(e[0] - s[0], e[1] - s[1])  # collinear
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    if r < 1e-9:
        return math.hypot(e[0] - s[0], e[1] - s[1])
    a1 = math.atan2(ay - uy, ax - ux)
    a2 = math.atan2(cy - uy, cx - ux)
    dtheta = abs(a2 - a1)
    if dtheta > math.pi:
        dtheta = 2 * math.pi - dtheta
    return r * dtheta


def _seg_width_val(seg: list) -> Optional[float]:
    c = _child(seg, "width")
    if c and len(c) >= 2:
        try:
            return float(c[1])
        except (TypeError, ValueError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Current / voltage inference
# --------------------------------------------------------------------------- #

def _infer_current(name: str, amp_cfg: Dict[str, Any], power_pats: List[str],
                   net_currents_arg: Dict[str, float]
                   ) -> Tuple[Optional[float], Optional[str]]:
    """Return (amps, source) for a net. ``source`` records HOW the current was
    determined so a caller can tell a DERIVED value from a blanket ASSUMPTION:
      "explicit"      caller-supplied net_currents arg
      "table"         config ampacity.net_current_a entry
      "name"          parsed from the net name (e.g. MOTOR_5A)
      "class_default" the POWER-class design current — an ASSUMPTION, not the
                      real net current; never hardcoded in code, read from
                      config (ampacity.class_design_current_a). None if unset.
      None            not a power net / no current."""
    up = name.upper()
    # 1. explicit arg override
    for k, v in net_currents_arg.items():
        if k.upper() == up:
            return float(v), "explicit"
    # 2. ampacity explicit table
    explicit = {str(k).upper(): float(v)
                for k, v in (amp_cfg.get("net_current_a", {}) or {}).items()}
    if up in explicit:
        return explicit[up], "table"
    # 3. current parsed from the net name
    try:
        from ..layout.net_roles import parse_current_from_name
        amps = parse_current_from_name(name)
        if amps:
            return float(amps), "name"
    except Exception:
        pass
    # 4. power-net pattern -> class design current (an assumption, config-driven)
    for p in power_pats:
        if up.startswith(str(p).upper()):
            cc = amp_cfg.get("class_design_current_a", {}) or {}
            v = cc.get("POWER")
            return (float(v), "class_default") if v is not None else (None, None)
    return None, None


def _infer_voltage(name: str, net_voltages_arg: Dict[str, float]) -> Optional[float]:
    up = name.upper()
    for k, v in net_voltages_arg.items():
        if k.upper() == up:
            return float(v)
    try:
        from ..layout.net_roles import parse_voltage_from_name
        return parse_voltage_from_name(name)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #

@tool(
    name="power_integrity_pcb",
    description=(
        "POWER INTEGRITY report (read-only): for every power net on a routed "
        ".kicad_pcb, measures the copper (length + narrowest width), infers the "
        "design current, and computes trace resistance, VOLTAGE DROP (I*R) and "
        "CURRENT DENSITY (I/A). Flags nets whose drop exceeds the budget or whose "
        "trace is thinner than the IPC-2152 width for its current. Does NOT modify "
        "the board — pair with set_track_widths_pcb to fix. Use for 'check voltage "
        "drop', 'is my power trace thick enough', 'power integrity', 'IR drop'.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}                     # required\n'
        '  {"pcb_path": "...", "net_currents": {"+12V": 8.0}}        # set rail current (A)\n'
        '  {"pcb_path": "...", "net_voltages": {"+12V": 12.0}}       # set rail voltage (V)\n'
        '  {"pcb_path": "...", "temp_c": 70}                         # model a hot board\n'
        "Thresholds + copper weight in config/power_integrity.json."
    ),
    input_schema={"pcb_path": str},
)
async def power_integrity_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
                "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "power_integrity_pcb disabled"}],
                "is_error": True}

    # ---- Physical constants (config) ----
    rho0     = float(cfg.get("resistivity_ohm_m", 1.724e-8))
    tempco   = float(cfg.get("temp_coefficient_per_c", 0.00393))
    ref_t    = float(cfg.get("reference_temp_c", 20.0))
    temp_c   = args.get("temp_c", None)
    if temp_c is not None:
        try:
            rho = rho0 * (1.0 + tempco * (float(temp_c) - ref_t))
        except (TypeError, ValueError):
            rho = rho0
    else:
        rho = rho0
    copper_oz = float(args.get("copper_weight_oz", cfg.get("default_copper_weight_oz", 1.0)))
    mm_per_oz = float(cfg.get("copper_thickness_mm_per_oz", 0.0348))
    thick_mm  = copper_oz * mm_per_oz
    min_cur   = float(cfg.get("min_power_current_a", 0.5))
    via_r_ohm = float(cfg.get("via_resistance_mohm", 1.0)) / 1000.0
    warn_pct  = float(cfg.get("vdrop_warn_pct", 3.0))
    warn_abs  = float(cfg.get("vdrop_warn_abs_v", 0.10))
    j_warn    = float(cfg.get("current_density_warn_a_per_mm2", 55.0))
    w_margin  = float(cfg.get("width_margin_pct", 10.0)) / 100.0

    net_currents_arg = {str(k): float(v)
                        for k, v in (args.get("net_currents", {}) or {}).items()}
    net_voltages_arg = {str(k): float(v)
                        for k, v in (args.get("net_voltages", {}) or {}).items()}

    try:
        root = sexpdata.loads(pcb_path.read_text(encoding="utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: parse failed: {exc}"}],
                "is_error": True}
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: not a kicad_pcb"}],
                "is_error": True}

    nt = _net_table(root)

    # Accumulate routed copper per net id: total length, min width, via count.
    length_mm: Dict[int, float] = {}
    min_w: Dict[int, float] = {}
    via_n: Dict[int, int] = {}
    for node in root[1:]:
        if not isinstance(node, list):
            continue
        h = _head(node)
        if h == "segment" or h == "arc":
            nid = _seg_net(node)
            ln = _seg_len_mm(node) if h == "segment" else _arc_len_mm(node)
            length_mm[nid] = length_mm.get(nid, 0.0) + ln
            w = _seg_width_val(node)
            if w is not None and w > 0:
                min_w[nid] = min(min_w.get(nid, w), w)
        elif h == "via":
            nid = _seg_net(node)
            via_n[nid] = via_n.get(nid, 0) + 1

    bands = _ipc_width_bands()
    amp_cfg = _ampacity_cfg()
    power_pats = _power_patterns()

    rows: List[Dict[str, Any]] = []
    for nid, name in nt.items():
        if not name or nid == 0:
            continue
        cur, cur_src = _infer_current(name, amp_cfg, power_pats, net_currents_arg)
        if cur is None or cur < min_cur:
            continue  # signal net — power integrity not relevant
        L = length_mm.get(nid, 0.0)
        w = min_w.get(nid)
        if not L or not w:
            # net carries current but isn't routed yet (or only via/zone) — flag it
            rows.append({"net": name, "current_a": round(cur, 2), "routed": False,
                         "current_source": cur_src,
                         "length_mm": round(L, 1), "width_mm": w,
                         "verdict": "UNROUTED"})
            continue
        area_mm2 = w * thick_mm
        # R = rho * L / A  (SI: L in m, A in m^2)
        r_ohm = rho * (L / 1000.0) / (area_mm2 * 1e-6) if area_mm2 > 0 else 0.0
        r_ohm += via_n.get(nid, 0) * via_r_ohm
        vdrop = cur * r_ohm
        jdens = cur / area_mm2 if area_mm2 > 0 else 0.0
        volt = _infer_voltage(name, net_voltages_arg)
        vpct = (vdrop / volt * 100.0) if volt and volt > 0 else None
        rec_w = _ipc_recommended_width(cur, bands)

        warnings: List[str] = []
        if vpct is not None and vpct > warn_pct:
            warnings.append(f"drop {vpct:.1f}% > {warn_pct:g}%")
        if vdrop > warn_abs:
            warnings.append(f"drop {vdrop*1000:.0f}mV > {warn_abs*1000:.0f}mV")
        if rec_w and w < rec_w * (1.0 - w_margin):
            warnings.append(f"width {w:.2f} < IPC {rec_w:.2f}mm")
        if jdens > j_warn:
            warnings.append(f"J {jdens:.0f} > {j_warn:g} A/mm2")

        rows.append({
            "net": name, "current_a": round(cur, 2), "routed": True,
            "current_source": cur_src,
            "length_mm": round(L, 1), "width_mm": round(w, 3),
            "resistance_mohm": round(r_ohm * 1000.0, 2),
            "vdrop_mv": round(vdrop * 1000.0, 1),
            "vdrop_pct": round(vpct, 2) if vpct is not None else None,
            "current_density_a_mm2": round(jdens, 1),
            "ipc_width_mm": round(rec_w, 3) if rec_w else None,
            "vias": via_n.get(nid, 0),
            "rail_v": volt,
            "verdict": "WARN" if warnings else "OK",
            "warnings": warnings,
        })

    if not rows:
        return {"content": [{"type": "text",
                             "text": ("No power nets found to analyse (no net had "
                                      f"an inferred current >= {min_cur:g} A). If a "
                                      "rail isn't named conventionally, pass "
                                      "net_currents={\"NAME\": amps}.")}],
                "ok": True, "nets": []}

    # Sort worst-first: WARN before OK before UNROUTED, then by voltage drop.
    order = {"WARN": 0, "UNROUTED": 1, "OK": 2}
    rows.sort(key=lambda r: (order.get(r["verdict"], 3), -(r.get("vdrop_mv") or 0)))

    n_warn = sum(1 for r in rows if r["verdict"] == "WARN")
    n_unrouted = sum(1 for r in rows if r["verdict"] == "UNROUTED")

    hot = "" if temp_c is None else f", @{float(temp_c):g}C"
    lines = [f"Power integrity: {pcb_path.name}  ({copper_oz:g}oz copper{hot})"]
    for r in rows:
        if r["verdict"] == "UNROUTED":
            lines.append(f"  [UNROUTED] {r['net']} ({r['current_a']}A) — no copper routed yet")
            continue
        vp = f"{r['vdrop_pct']:.1f}%" if r["vdrop_pct"] is not None else "—"
        tag = "WARN" if r["verdict"] == "WARN" else " OK "
        lines.append(
            f"  [{tag}] {r['net']}: {r['current_a']}A over {r['length_mm']:.0f}mm @ "
            f"{r['width_mm']:.2f}mm -> {r['resistance_mohm']:.1f}mOhm, "
            f"Vdrop {r['vdrop_mv']:.0f}mV ({vp}), J {r['current_density_a_mm2']:.0f} A/mm2")
        if r["warnings"]:
            lines.append(f"           ! {'; '.join(r['warnings'])}")

    # Word verdict — no percentage quality score (per house style).
    if n_warn == 0 and n_unrouted == 0:
        verdict = "PASS — every power net is within its voltage-drop and width budget."
    elif n_warn == 0 and n_unrouted:
        verdict = (f"INCOMPLETE — {n_unrouted} power net(s) not yet routed; the routed "
                   "ones pass. Route them, then re-check.")
    else:
        verdict = (f"NEEDS ATTENTION — {n_warn} power net(s) over budget. Widen them "
                   "(set_track_widths_pcb) or shorten/parallel the run, then re-check.")
    lines.append("")
    lines.append(verdict)

    return {"content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": True, "path": str(pcb_path),
            "warn_count": n_warn, "unrouted_count": n_unrouted,
            "clean": n_warn == 0 and n_unrouted == 0,
            "nets": rows}
