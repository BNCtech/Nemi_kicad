"""Tool: routing_audit — the deterministic gate for step 7 (PCB Routing).

After routing, this proves the copper is legal before DRC/fab: every net's
tracks meet their net-class width, routing is complete (no unrouted nets),
tracks stay off the board edge, and corners are 45deg not 90. Read-only — it
reuses route_pcb_simple's net-class resolution (so it agrees with the router)
and placement_audit's outline/edge helpers; it never lays or moves copper.

Checks (gated in ``config/routing_audit.json``):
  unrouted_nets        — every multi-pad net has copper (ratsnest = 0). [rule 7]
  track_width_vs_class — each segment >= its net-class width + board min. [rules 1/3]
  track_edge_clearance — no track within copper-to-edge of Edge.Cuts.  [rule 6]
  corner_90            — no 90deg corners (use 45deg).                 [rule 5]

Net -> class is derived DYNAMICALLY from the net name via the project's own
netclass patterns, so it works on any circuit (feedback_dynamic_derive_not_list).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "routing_audit.json"
        _CFG_CACHE = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CFG_CACHE = {}
    return _CFG_CACHE


def _rule(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    r = (cfg.get("rules", {}) or {}).get(name, {})
    return r if isinstance(r, dict) else {}


def _rule_on(cfg: Dict[str, Any], name: str) -> bool:
    return bool(_rule(cfg, name).get("enabled", True))


def _rule_sev(cfg: Dict[str, Any], name: str, default: str = "warning") -> str:
    return str(_rule(cfg, name).get("severity", default))


def _seg_ends(node: list):
    """(start, end, width) of a (segment ...) node, or None."""
    from ..layout.place_refine import _child
    s, e, w = _child(node, "start"), _child(node, "end"), _child(node, "width")
    if not (s and e and len(s) >= 3 and len(e) >= 3):
        return None
    try:
        p1 = (float(s[1]), float(s[2]))
        p2 = (float(e[1]), float(e[2]))
        wd = float(w[1]) if w and len(w) >= 2 else 0.0
        return p1, p2, wd
    except (TypeError, ValueError):
        return None


@tool(
    name="routing_audit",
    description=(
        "Validate PCB routing (step 7) on a .kicad_pcb before DRC. Read-only. "
        "Checks: every multi-pad net is routed (ratsnest=0), each track >= its "
        "net-class width + board minimum, no track within copper-to-edge of the "
        "outline, and no 90-degree corners (use 45). Net class is derived from "
        "the net name via the project's own patterns, so it works on any "
        "circuit. Run it after routing, before drc_check.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_pcb"}   # or .kicad_sch / .kicad_pro / dir\n'
        "Reuses route_pcb_simple's net-class resolution so it agrees with the "
        "router. Verdict in words. Policy in config/routing_audit.json. It "
        "reports — route_pcb_* fix."
    ),
    input_schema={"path": str},
)
async def routing_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "routing_audit disabled in config"}],
                "is_error": True}

    from .placement_audit import _resolve_pcb, _board_outline_bbox, _edge_clearance_mm
    pcb = _resolve_pcb(str(args.get("path", "")).strip())
    if pcb is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: no .kicad_pcb found for "
                                     f"{args.get('path', '')!r}"}],
                "is_error": True}

    import sexpdata
    from ..layout.place_refine import _head, _child, _children, _pad_net_id
    from .route_pcb_simple import (
        _load_netclass_widths, _load_netclass_patterns, _track_width_for,
    )
    try:
        root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"content": [{"type": "text",
                             "text": f"ERROR: cannot parse {pcb.name}: {exc}"}],
                "is_error": True}

    vt = cfg.get("verdict", {}) or {}
    eps = 1e-6
    cap = int(cfg.get("max_examples_per_rule", 15))
    copper = set(cfg.get("copper_layers", ["F.Cu", "B.Cu"]))

    # --- net table + per-net pad counts ---
    net_name: Dict[int, str] = {}
    net_pads: Dict[int, int] = {}
    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list):
            continue
        h = _head(node)
        if h == "net" and len(node) >= 3:
            try:
                net_name[int(node[1])] = str(node[2]).strip('"')
            except (TypeError, ValueError):
                pass
        elif h == "footprint":
            for pad in _children(node, "pad"):
                nid = _pad_net_id(pad)
                if nid > 0:
                    net_pads[nid] = net_pads.get(nid, 0) + 1

    # --- tracks (segments) + vias ---
    segments: List[Dict[str, Any]] = []
    routed_nets: set = set()
    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list):
            continue
        h = _head(node)
        if h == "segment":
            ends = _seg_ends(node)
            if ends is None:
                continue
            layer = _child(node, "layer")
            lname = str(layer[1]).strip('"') if layer and len(layer) >= 2 else ""
            netc = _child(node, "net")
            nid = int(netc[1]) if netc and len(netc) >= 2 else 0
            segments.append({"net": nid, "layer": lname,
                             "p1": ends[0], "p2": ends[1], "w": ends[2]})
            if nid > 0:
                routed_nets.add(nid)
        elif h in ("via", "arc"):
            netc = _child(node, "net")
            if netc and len(netc) >= 2:
                try:
                    routed_nets.add(int(netc[1]))
                except (TypeError, ValueError):
                    pass

    # A net with an ASSIGNED copper zone (ground/power plane) is routed BY that
    # plane — the pour is how you route GND/power. KiCad fills zones on load and
    # before DRC/export, so an outline assigned to the net = routing intent for
    # it; we don't require the file's cached fill. Dynamic: read the actual
    # zones. Division of labour: power_audit warns if a zone is unfilled (must
    # fill before fab), and drc_audit (kicad-cli fills, then checks) is the
    # authority for any pad the pour geometrically misses. So routing_audit
    # answers "is this net routed?" (track/via/plane) without false-flagging a
    # plane net, while the real connectivity + fill state stay gated elsewhere.
    zone_routed: set = set()
    try:
        from .power_audit import _parse_zones
        zone_routed = {z["net"] for z in _parse_zones(root) if z.get("net")}
    except Exception:                                       # noqa: BLE001
        zone_routed = set()
    routed_nets |= zone_routed

    total_nets = sum(1 for nid, cnt in net_pads.items() if cnt >= 2 and nid != 0)

    # --- board with NO tracks at all: report honestly, don't spam ---
    if not segments and not routed_nets:
        verdict = vt.get("no_tracks",
                         "NOT ROUTED — no tracks ({t} nets)").format(t=total_nets)
        return {"content": [{"type": "text",
                             "text": f"# Routing audit — {pcb.name}\n"
                                     f"  {total_nets} nets, 0 tracks\n\n"
                                     f"**{verdict}**"}],
                "ok": False, "verdict": verdict, "routed": False,
                "pcb": str(pcb).replace("\\", "/"), "total_nets": total_nets}

    findings: List[Dict[str, str]] = []

    def add(scope: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"scope": scope, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    # --- unrouted_nets ---
    for nid, cnt in net_pads.items():
        if nid != 0 and cnt >= 2 and nid not in routed_nets:
            add(net_name.get(nid, f"net{nid}"), "unrouted_nets",
                f"net '{net_name.get(nid, nid)}' has {cnt} pads but no copper")

    # --- track_width_vs_class ---
    widths = _load_netclass_widths(pcb)
    patterns = _load_netclass_patterns(pcb)
    default_w = float(cfg.get("default_track_width_mm", 0.25))
    for seg in segments:
        if seg["layer"] not in copper:
            continue
        nm = net_name.get(seg["net"], "")
        need = _track_width_for(nm, widths, patterns, default_w)   # net's class width
        if seg["w"] + eps < need:
            add(nm or f"net{seg['net']}", "track_width_vs_class",
                f"track {seg['w']:g} mm < class width {need:g} mm on {seg['layer']}")

    # --- track_edge_clearance ---
    outline = _board_outline_bbox(root)
    if outline is not None:
        ecl = _edge_clearance_mm(pcb)
        ox0, oy0, ox1, oy1 = outline
        for seg in segments:
            for (px, py) in (seg["p1"], seg["p2"]):
                if (px < ox0 + ecl - eps or px > ox1 - ecl + eps
                        or py < oy0 + ecl - eps or py > oy1 - ecl + eps):
                    add(net_name.get(seg["net"], f"net{seg['net']}"),
                        "track_edge_clearance",
                        f"track within {ecl:g} mm of board edge at "
                        f"({px:.1f}, {py:.1f})")
                    break

    # --- corner_90 (dynamic geometry) ---
    tol = float(cfg.get("corner_tol_deg", 10.0))
    if _rule_on(cfg, "corner_90"):
        # group away-vectors by (net, layer, rounded vertex)
        vtx: Dict[Tuple[int, str, float, float], List[Tuple[float, float]]] = {}
        for seg in segments:
            if seg["layer"] not in copper:
                continue
            for a, b in ((seg["p1"], seg["p2"]), (seg["p2"], seg["p1"])):
                d = (b[0] - a[0], b[1] - a[1])
                mag = math.hypot(*d)
                if mag < 1e-6:
                    continue
                key = (seg["net"], seg["layer"], round(a[0], 3), round(a[1], 3))
                vtx.setdefault(key, []).append((d[0] / mag, d[1] / mag))
        seen_corner = set()
        for (nid, layer, vx, vy), vecs in vtx.items():
            if len(vecs) < 2:
                continue
            flagged = False
            for i in range(len(vecs)):
                for j in range(i + 1, len(vecs)):
                    dot = max(-1.0, min(1.0, vecs[i][0] * vecs[j][0]
                                        + vecs[i][1] * vecs[j][1]))
                    ang = math.degrees(math.acos(dot))
                    if abs(ang - 90.0) <= tol:
                        flagged = True
                        break
                if flagged:
                    break
            if flagged and (nid, vx, vy) not in seen_corner:
                seen_corner.add((nid, vx, vy))
                add(net_name.get(nid, f"net{nid}"), "corner_90",
                    f"90-degree corner at ({vx:.1f}, {vy:.1f}) on {layer}")

    # --- aggregate ---
    counts = {"error": 0, "warning": 0, "info": 0, "review": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w = counts["error"], counts["warning"]
    routed_count = len(routed_nets)

    if e == 0 and w == 0:
        verdict = vt.get("clean", "ROUTING VERIFIED — {n} nets").format(n=routed_count)
        ok = True
    else:
        verdict = vt.get("issues",
                         "ROUTING NOT CLEAN — {e} error(s), {w} warning(s)"
                         ).format(e=e, w=w)
        ok = (e == 0)

    icon = {"error": "✗", "warning": "!", "info": "·", "review": "?"}
    lines = [f"# Routing audit — {pcb.name}",
             f"  {routed_count}/{total_nets} nets routed · {len(segments)} tracks · "
             f"outline {'yes' if outline is not None else 'MISSING'}"]
    for sev in ["error", "warning", "info"]:
        group = [f for f in findings if f["severity"] == sev]
        if not group:
            continue
        lines.append("")
        lines.append(f"  {sev.upper()} ({len(group)})")
        for f in group[:cap]:
            lines.append(f"    {icon[sev]} {f['scope']}: {f['msg']}")
        if len(group) > cap:
            lines.append(f"    … +{len(group) - cap} more")
    lines.append("")
    lines.append(f"**{verdict}**")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": ok,
        "pcb": str(pcb).replace("\\", "/"),
        "routed": True,
        "routed_nets": routed_count,
        "total_nets": total_nets,
        "tracks": len(segments),
        "verdict": verdict,
        "counts": counts,
        "findings": findings,
    }
