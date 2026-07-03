"""Tool: route_pcb_astar — Tier-2 MULTI-LAYER A* cost router.

The harder-tier companion to route_pcb_simple. For each still-unrouted net it runs
the (x, y, layer) A* in ``layout/router_astar.py`` — a real cost search that can
spend a via to cross to the other copper layer when that beats a long on-layer
detour. Emits segments AND vias, stays clearance-aware (other nets' pads/tracks/
vias + the board edge are obstacles inflated by copper+clearance), and lays copper
incrementally so later nets see earlier ones.

Board parsing, the serializer, MST, net-class width/clearance and via/segment
builders are REUSED from route_pcb_simple (one source of truth — no duplicate
s-expression code). This module adds only: per-layer obstacle rasterisation, pad
copper-layer detection, and the multi-net driver. Universal, config-driven
(config/router_astar.json), never raises.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool

from .route_pcb_simple import (
    _head, _at, _prop, _layer_of, _pad_number, _pad_size, _pad_net, _rotate,
    _net_table, _pad_bbox, _make_segment, _make_via, _emit, _mst_edges,
    _load_netclass_widths, _load_netclass_patterns, _load_netclass_clearance,
    _track_width_for, _edge_bbox, _edge_walls,
)
from ..layout.router_astar import find_path, waypoints_to_tracks, path_length_mm


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _load_cfg() -> Dict[str, Any]:
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "router_astar.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _pad_layers(pad: list) -> List[str]:
    """Copper layers a pad connects to. '*.Cu' -> both sides (THT); else the
    explicit F.Cu / B.Cu (SMD). Empty -> caller assumes all layers."""
    for ch in pad[1:]:
        if isinstance(ch, list) and _head(ch) == "layers":
            names = [str(x) for x in ch[1:]]
            if any(n == "*.Cu" for n in names):
                return ["*"]
            return [n for n in names if n.endswith(".Cu")]
    return []


def _allowed_layers(pad_layers: List[str], all_layers: List[str]) -> List[str]:
    if not pad_layers or pad_layers == ["*"]:
        return list(all_layers)
    got = [l for l in pad_layers if l in all_layers]
    return got or list(all_layers)


def _rect_overlaps_bounds(r: Tuple[float, float, float, float],
                          b: Tuple[float, float, float, float]) -> bool:
    return not (r[2] < b[0] or r[0] > b[2] or r[3] < b[1] or r[1] > b[3])


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #

@tool(
    name="route_pcb_astar",
    description=(
        "TIER-2 multi-layer A* router for a .kicad_pcb — routes the harder nets "
        "route_pcb_simple leaves as ratsnest, using a real cost search that can "
        "cross to the back layer through a VIA when that's shorter/clearer than a "
        "long detour. Emits tracks AND vias, stays clearance-aware (never shorts), "
        "skips GND (poured). Use for 'route the rest', 'autoroute the remaining "
        "nets', 'route across two layers', or after route_pcb_simple.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}                  # required\n'
        '  {"pcb_path": "...", "preview_only": true}              # dry-run\n'
        '  {"pcb_path": "...", "only_unrouted": true}             # skip nets already fully routed\n'
        '  {"pcb_path": "...", "via_penalty_mm": 10}              # discourage vias\n'
        '  {"pcb_path": "...", "force_route_nets": ["GND"]}       # route a skipped net\n'
        "All defaults in config/router_astar.json. Run AFTER auto_place_pcb."
    ),
    input_schema={"pcb_path": str},
)
async def route_pcb_astar(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
                "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "route_pcb_astar disabled"}],
                "is_error": True}

    layers = list(cfg.get("layers", ["F.Cu", "B.Cu"]))
    layer_axis = dict(cfg.get("layer_axis", {"F.Cu": "h", "B.Cu": "v"}))
    astar_cfg = {
        "grid_pitch_mm":     float(cfg.get("grid_pitch_mm", 0.5)),
        "bend_penalty_mm":   float(cfg.get("bend_penalty_mm", 0.5)),
        "via_penalty_mm":    float(args.get("via_penalty_mm", cfg.get("via_penalty_mm", 5.0))),
        "off_axis_penalty_mm": float(cfg.get("off_axis_penalty_mm", 0.15)),
        "bounds_margin_mm":  float(cfg.get("bounds_margin_mm", 8.0)),
        "max_nodes":         int(cfg.get("max_nodes", 120000)),
    }
    default_w   = float(cfg.get("default_track_width_mm", 0.25))
    clearance   = _load_netclass_clearance(pcb_path) or float(cfg.get("default_clearance_mm", 0.2))
    via_drill   = float(cfg.get("via_drill_mm", 0.3))
    via_size    = float(cfg.get("via_size_mm", 0.6))
    max_pads    = int(cfg.get("max_pads_per_net", 8))
    use_class_w = bool(cfg.get("use_net_class_width", True))
    skip_nets   = set(s.upper() for s in cfg.get("skip_nets", []))
    force_route = set(s.upper() for s in (args.get("force_route_nets") or []))
    preview     = bool(args.get("preview_only", cfg.get("preview_only", False)))
    only_unrouted = bool(args.get("only_unrouted", True))
    margin      = astar_cfg["bounds_margin_mm"]

    try:
        root = sexpdata.loads(pcb_path.read_text(encoding="utf-8"))
    except Exception as exc:                                  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: parse failed: {exc}"}],
                "is_error": True}
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: not a kicad_pcb"}],
                "is_error": True}

    # ---- Pad extraction (with copper-layer detection) ----
    # entry: (net_id, net_name, ref, num, x, y, pw, ph, allowed_layers)
    pads: List[Tuple[int, str, str, str, float, float, float, float, List[str]]] = []
    # obstacle: (net_id, bbox, layer_set) — a pad blocks copper only on the layers
    # it actually occupies. An SMD pad blocks its ONE side (so a track may pass
    # under it on the other layer); a THT/'*' pad blocks every copper layer.
    pad_obstacles: List[Tuple[int, Tuple[float, float, float, float], frozenset]] = []
    for fp in root[1:]:
        if not (isinstance(fp, list) and _head(fp) == "footprint"):
            continue
        ref = _prop(fp, "Reference") or ""
        fx, fy, frot = _at(fp)
        for ch in fp[1:]:
            if not (isinstance(ch, list) and _head(ch) == "pad"):
                continue
            num = _pad_number(ch)
            px, py, prot = _at(ch)
            pw, ph = _pad_size(ch)
            nid, nname = _pad_net(ch)
            dx, dy = _rotate(px, py, -frot)
            if int(round(frot + prot)) % 180 == 90:
                pw, ph = ph, pw
            ax, ay = fx + dx, fy + dy
            raw_layers = _pad_layers(ch)
            allowed = _allowed_layers(raw_layers, layers)
            pads.append((nid, nname, ref, num, ax, ay, pw, ph, allowed))
            if pw > 0 and ph > 0:
                # THT / '*.Cu' pad blocks all copper; SMD blocks only its side(s).
                lset = (frozenset(["*"]) if raw_layers in ([], ["*"])
                        else frozenset(allowed))
                pad_obstacles.append((nid, _pad_bbox(ax, ay, pw, ph, 0.0), lset))

    if not pads:
        return {"content": [{"type": "text", "text": "PCB has no pads. Run F8 first."}],
                "is_error": True}

    xs = [p[4] for p in pads]
    ys = [p[5] for p in pads]
    diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) if xs else 60.0
    _span = float(cfg.get("max_net_span_mm", 0) or 0)
    max_span = _span if _span > 0 else max(diag, 30.0)

    # ---- Bucket pads by net; fall back to net table for names ----
    by_net: Dict[int, Dict[str, Any]] = {}
    for (nid, nname, ref, num, x, y, pw, ph, allowed) in pads:
        b = by_net.setdefault(nid, {"name": nname, "pads": []})
        b["pads"].append({"xy": (x, y), "layers": allowed})
        if not b["name"] and nname:
            b["name"] = nname
    nt = _net_table(root)
    for nid, b in by_net.items():
        if not b["name"]:
            b["name"] = nt.get(nid, "")

    # ---- Seed obstacles from EXISTING copper (tracks per-layer, vias all-layer) ----
    # entry: (layer, centerline_bbox, half_width, net_id); layer '*' = all layers.
    track_obstacles: List[Tuple[str, Tuple[float, float, float, float], float, int]] = []
    routed_pts_by_net: Dict[int, int] = {}
    if cfg.get("respect_existing_copper", True):
        def _pt(node, key):
            for c in node[1:]:
                if isinstance(c, list) and _head(c) == key and len(c) >= 3:
                    try:
                        return (float(c[1]), float(c[2]))
                    except (TypeError, ValueError):
                        return None
            return None

        def _val(node, key):
            for c in node[1:]:
                if isinstance(c, list) and _head(c) == key and len(c) >= 2:
                    try:
                        return float(c[1])
                    except (TypeError, ValueError):
                        return None
            return None

        def _netof(node):
            for c in node[1:]:
                if isinstance(c, list) and _head(c) == "net" and len(c) >= 2:
                    try:
                        return int(c[1])
                    except (TypeError, ValueError):
                        return 0
            return 0

        for ch in root[1:]:
            if not isinstance(ch, list):
                continue
            h = _head(ch)
            if h == "segment":
                s = _pt(ch, "start"); e = _pt(ch, "end"); w = _val(ch, "width")
                lyr = _layer_of(ch); onid = _netof(ch)
                if s and e and w:
                    bb = (min(s[0], e[0]), min(s[1], e[1]),
                          max(s[0], e[0]), max(s[1], e[1]))
                    track_obstacles.append((lyr or "F.Cu", bb, w / 2.0, onid))
                    routed_pts_by_net[onid] = routed_pts_by_net.get(onid, 0) + 1
            elif h == "via":
                at = _pt(ch, "at"); sz = _val(ch, "size"); onid = _netof(ch)
                if at and sz:
                    track_obstacles.append(("*", (at[0], at[1], at[0], at[1]),
                                            sz / 2.0, onid))

    # ---- Board-edge walls (all layers) ----
    edge_walls: List[Tuple[float, float, float, float]] = []
    if cfg.get("edge_clearance_enabled", True):
        eb = _edge_bbox(root)
        if eb is not None:
            edge_walls = _edge_walls(eb, float(cfg.get("edge_clearance_mm", 0.5)))

    netclass_widths = _load_netclass_widths(pcb_path) if use_class_w else {}
    netclass_patterns = _load_netclass_patterns(pcb_path) if use_class_w else []

    # ---- Route each net ----
    routed_nets: List[str] = []
    skipped: List[Dict[str, Any]] = []
    new_nodes: List[list] = []
    total_seg = 0
    total_via = 0
    total_len = 0.0

    def _make_is_blocked(nid: int, half: float,
                         bounds: Tuple[float, float, float, float]):
        """Predicate over cells for one edge, pre-filtered to the search bounds
        so A* isn't O(all-copper) per node. A cell is blocked on `layer` when its
        centre lies within (half + clearance + obstacle_extent) of any other net's
        pad/track/via, or within (half) of an edge wall."""
        pad_gap = half + clearance
        p_rects = [((bb[0] - pad_gap, bb[1] - pad_gap, bb[2] + pad_gap, bb[3] + pad_gap),
                    lset)
                   for (onid, bb, lset) in pad_obstacles
                   if onid != nid and _rect_overlaps_bounds(bb, bounds)]
        t_rects: List[Tuple[str, Tuple[float, float, float, float]]] = []
        for (lyr, bb, ohw, onid) in track_obstacles:
            if onid == nid or not _rect_overlaps_bounds(bb, bounds):
                continue
            g = half + clearance + ohw
            t_rects.append((lyr, (bb[0] - g, bb[1] - g, bb[2] + g, bb[3] + g)))
        e_rects = [(bb[0] - half, bb[1] - half, bb[2] + half, bb[3] + half)
                   for bb in edge_walls if _rect_overlaps_bounds(bb, bounds)]

        def is_blocked(x: float, y: float, layer: str) -> bool:
            for ((x1, y1, x2, y2), lset) in p_rects:
                if "*" not in lset and layer not in lset:
                    continue  # SMD pad on another layer — copper may pass here
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return True
            for (lyr, (x1, y1, x2, y2)) in t_rects:
                if lyr != "*" and lyr != layer:
                    continue
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return True
            for (x1, y1, x2, y2) in e_rects:
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return True
            return False
        return is_blocked

    def _make_is_via_blocked(nid: int,
                             bounds: Tuple[float, float, float, float]):
        """A via spans every copper layer and is wider than a track, so it needs
        (via_radius + clearance) from ANY other-net copper on ANY layer — not the
        track-width gap is_blocked uses. Under-clearing vias was the source of the
        via-to-track clearance DRC errors the benchmark caught."""
        via_gap = via_size / 2.0 + clearance
        p_rects = [(bb[0] - via_gap, bb[1] - via_gap, bb[2] + via_gap, bb[3] + via_gap)
                   for (onid, bb, _lset) in pad_obstacles
                   if onid != nid and _rect_overlaps_bounds(bb, bounds)]
        t_rects = []
        for (lyr, bb, ohw, onid) in track_obstacles:
            if onid == nid or not _rect_overlaps_bounds(bb, bounds):
                continue
            g = via_size / 2.0 + clearance + ohw
            t_rects.append((bb[0] - g, bb[1] - g, bb[2] + g, bb[3] + g))
        e_rects = [(bb[0] - via_size / 2.0, bb[1] - via_size / 2.0,
                    bb[2] + via_size / 2.0, bb[3] + via_size / 2.0)
                   for bb in edge_walls if _rect_overlaps_bounds(bb, bounds)]

        def is_via_blocked(x: float, y: float) -> bool:
            for (x1, y1, x2, y2) in p_rects:   # pads: via touches all layers
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return True
            for (x1, y1, x2, y2) in t_rects:   # tracks on ANY layer
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return True
            for (x1, y1, x2, y2) in e_rects:
                if x1 <= x <= x2 and y1 <= y <= y2:
                    return True
            return False
        return is_via_blocked

    for nid, info in sorted(by_net.items()):
        name = info["name"]
        net_pads = info["pads"]
        if nid == 0 or not name:
            continue
        up = name.upper()
        if up in skip_nets and up not in force_route:
            skipped.append({"net": name, "reason": "in skip_nets (poured)"})
            continue
        if len(net_pads) < 2:
            continue
        if len(net_pads) > max_pads:
            skipped.append({"net": name, "reason": f"{len(net_pads)} pads > max ({max_pads})"})
            continue
        if only_unrouted and routed_pts_by_net.get(nid, 0) > 0:
            skipped.append({"net": name, "reason": "already has copper"})
            continue
        coords = [p["xy"] for p in net_pads]
        sxs = [c[0] for c in coords]; sys_ = [c[1] for c in coords]
        if (max(sxs) - min(sxs)) > max_span or (max(sys_) - min(sys_)) > max_span:
            skipped.append({"net": name, "reason": "span too wide for A*"})
            continue

        width = _track_width_for(name, netclass_widths, netclass_patterns, default_w)
        half = width / 2.0
        mst = _mst_edges(coords)
        net_ok = True
        net_segments: List[list] = []
        net_vias: List[list] = []
        for (i, j) in mst:
            a = coords[i]; b = coords[j]
            la = net_pads[i]["layers"]; lb = net_pads[j]["layers"]
            bnds = (min(a[0], b[0]) - margin, min(a[1], b[1]) - margin,
                    max(a[0], b[0]) + margin, max(a[1], b[1]) + margin)
            is_blocked = _make_is_blocked(nid, half, bnds)
            is_via_blocked = _make_is_via_blocked(nid, bnds)
            wp = find_path(a, b, layers, is_blocked, astar_cfg,
                           start_layers=la, goal_layers=lb, layer_axis=layer_axis,
                           is_via_blocked=is_via_blocked)
            if not wp:
                net_ok = False
                break
            segs, vias = waypoints_to_tracks(wp)
            total_len += path_length_mm(wp)
            for (lyr, p1, p2) in segs:
                seg_node = _make_segment(p1, p2, width, lyr, nid)
                net_segments.append(seg_node)
                # Feed this edge's copper back as an obstacle for later edges/nets.
                track_obstacles.append((lyr, (min(p1[0], p2[0]), min(p1[1], p2[1]),
                                              max(p1[0], p2[0]), max(p1[1], p2[1])),
                                        half, nid))
            for (vx, vy, l1, l2) in vias:
                via_node = _make_via(vx, vy, via_drill, via_size, nid, l1, l2)
                net_vias.append(via_node)
                track_obstacles.append(("*", (vx, vy, vx, vy), via_size / 2.0, nid))

        if not net_ok:
            skipped.append({"net": name, "reason": "no clearance-safe A* path"})
            # roll back this net's obstacle contributions is unnecessary — partial
            # copper wasn't emitted (we only emit on full success below).
            continue
        new_nodes.extend(net_segments)
        new_nodes.extend(net_vias)
        total_seg += len(net_segments)
        total_via += len(net_vias)
        routed_nets.append(name)

    # ---- Write ----
    if not preview and new_nodes:
        root = list(root) + new_nodes
        try:
            pcb_path.write_text(_emit(root), encoding="utf-8")
        except Exception as exc:                              # noqa: BLE001
            return {"content": [{"type": "text", "text": f"ERROR: write failed: {exc}"}],
                    "is_error": True}

    lines = [f"route_pcb_astar -> {pcb_path.name}"
             + ("  (preview — not written)" if preview else ""),
             f"  routed {len(routed_nets)} net(s): {total_seg} segments, "
             f"{total_via} vias, {total_len:.0f} mm total"]
    if routed_nets:
        lines.append("  nets: " + ", ".join(routed_nets[:12])
                     + (" …" if len(routed_nets) > 12 else ""))
    if skipped:
        shown = [s for s in skipped if "already has copper" not in s["reason"]][:8]
        if shown:
            lines.append(f"  skipped {len(skipped)}:")
            for s in shown:
                lines.append(f"    {s['net']}: {s['reason']}")
    if not routed_nets and not new_nodes:
        lines.append("  nothing to route (all nets already routed, poured, or "
                     "beyond A* limits).")

    return {"content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": True, "path": str(pcb_path),
            "routed": len(routed_nets), "segments": total_seg, "vias": total_via,
            "length_mm": round(total_len, 1),
            "routed_nets": routed_nets, "skipped": skipped,
            "preview_only": preview}
