"""layout/router_astar.py — Tier-2 MULTI-LAYER cost-based A* router.

Phase 3 of the Universal PCB AI Engine. Where route_pcb_simple is Tier 1 (fast
L/Z/single-layer-A* Manhattan, great for the easy 90 %), this module is the
harder tier: a grid A* over a **3-D state space (x, y, layer)** with a real cost
function so it can trade a via for a shorter/clearer path —

    cost = distance
         + bend_penalty        (each 90 deg corner)
         + off_axis_penalty     (travelling against a layer's preferred axis)
         + via_penalty          (each layer change; a via is expensive but allowed)

This is the algorithm ONLY — it knows nothing about KiCad s-expressions. The
caller (tools/route_pcb_astar.py) supplies a board-derived ``is_blocked(x, y,
layer)`` predicate + bounds and turns the returned way-points into segments and
vias. Pure Python heapq, no dependencies, fully config-driven. Never raises.

Public API:
    find_path(start, goal, layers, is_blocked, cfg, ...) -> [(x, y, layer), ...] | None
    waypoints_to_tracks(waypoints) -> (segments_by_layer, via_points)
"""
from __future__ import annotations

import heapq
import math
from typing import Callable, Dict, List, Optional, Tuple

# A way-point is (x_mm, y_mm, layer_name).
Waypoint = Tuple[float, float, str]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def default_cfg() -> Dict[str, float]:
    """Algorithm constants — the caller overrides from config/router_astar.json.
    All penalties are in millimetres so they compose with the distance term."""
    return {
        "grid_pitch_mm": 0.5,
        "bend_penalty_mm": 0.5,
        "via_penalty_mm": 5.0,
        "off_axis_penalty_mm": 0.15,
        "bounds_margin_mm": 8.0,
        "max_nodes": 120_000,
    }


# --------------------------------------------------------------------------- #
# Core A*
# --------------------------------------------------------------------------- #

def find_path(start: Tuple[float, float],
              goal: Tuple[float, float],
              layers: List[str],
              is_blocked: Callable[[float, float, str], bool],
              cfg: Optional[Dict[str, float]] = None,
              *,
              start_layers: Optional[List[str]] = None,
              goal_layers: Optional[List[str]] = None,
              layer_axis: Optional[Dict[str, str]] = None,
              is_via_blocked: Optional[Callable[[float, float], bool]] = None,
              ) -> Optional[List[Waypoint]]:
    """A* from `start` to `goal` across `layers`, returning way-points or None.

    is_blocked(x, y, layer) -> True when that grid cell is copper-forbidden on
    that layer (other-net pad/track/via/edge, already inflated by clearance by the
    caller). The start and goal cells are always allowed (an endpoint sits on its
    own pad).

    start_layers / goal_layers restrict which layer(s) the path may begin / end
    on (a THT pad = both; an SMD pad = its one side). Default: all layers.

    layer_axis maps a layer -> "h"/"v"; travelling against it costs
    off_axis_penalty_mm (the classic F.Cu-horizontal / B.Cu-vertical convention,
    which keeps two-layer boards from crossing). Absent -> no axis bias.

    is_via_blocked(x, y) -> True when a VIA cannot be dropped at that cell (a via
    is bigger than a track, so it needs its own, larger, all-layer clearance).
    When absent, a via is allowed wherever the track cell is free — which
    under-clears vias against nearby copper. The caller should pass it.
    """
    c = dict(default_cfg())
    if cfg:
        c.update({k: float(v) for k, v in cfg.items()
                  if k in default_cfg() and isinstance(v, (int, float))})
    pitch = float(c["grid_pitch_mm"])
    if pitch <= 0 or not layers:
        return None
    bend_pen = float(c["bend_penalty_mm"])
    via_pen = float(c["via_penalty_mm"])
    axis_pen = float(c["off_axis_penalty_mm"])
    margin = float(c["bounds_margin_mm"])
    max_nodes = int(c["max_nodes"])
    layer_axis = layer_axis or {}
    start_layers = start_layers or list(layers)
    goal_layers = goal_layers or list(layers)

    def snap(v: float) -> float:
        return round(v / pitch) * pitch

    sx, sy = snap(start[0]), snap(start[1])
    gx, gy = snap(goal[0]), snap(goal[1])

    xmin = min(sx, gx) - margin
    xmax = max(sx, gx) + margin
    ymin = min(sy, gy) - margin
    ymax = max(sy, gy) + margin

    def near(a: float, b: float) -> bool:
        return abs(a - b) < 1e-6

    def is_start_cell(x: float, y: float) -> bool:
        return near(x, sx) and near(y, sy)

    def is_goal_cell(x: float, y: float) -> bool:
        return near(x, gx) and near(y, gy)

    def blocked(x: float, y: float, layer: str) -> bool:
        # Endpoints are always reachable (own pad copper).
        if is_start_cell(x, y) or is_goal_cell(x, y):
            return False
        try:
            return bool(is_blocked(x, y, layer))
        except Exception:                                   # noqa: BLE001
            return True  # be conservative on a bad predicate

    def h(x: float, y: float) -> float:
        return abs(x - gx) + abs(y - gy)

    planar = [(pitch, 0.0), (-pitch, 0.0), (0.0, pitch), (0.0, -pitch)]

    def off_axis(layer: str, dx: float, dy: float) -> float:
        pref = layer_axis.get(layer)
        if not pref:
            return 0.0
        if pref == "h" and dy != 0.0:
            return axis_pen
        if pref == "v" and dx != 0.0:
            return axis_pen
        return 0.0

    # Node key includes layer AND incoming direction so the bend penalty is exact
    # (a cell reached going east is a different search state than reached going
    # north). dir index: 0..3 planar, -1 = none (start / just-vias).
    def dkey(x: float, y: float, layer: str, di: int) -> str:
        return f"{x:.3f},{y:.3f},{layer},{di}"

    open_heap: List[Tuple[float, float, float, float, str, int]] = []
    g_score: Dict[str, float] = {}
    came_from: Dict[str, Tuple[str, Waypoint]] = {}

    for L in start_layers:
        if L not in layers:
            continue
        k = dkey(sx, sy, L, -1)
        g_score[k] = 0.0
        heapq.heappush(open_heap, (h(sx, sy), 0.0, sx, sy, L, -1))

    nodes = 0
    while open_heap:
        if nodes > max_nodes:
            return None
        nodes += 1
        f, g, x, y, layer, di = heapq.heappop(open_heap)
        key = dkey(x, y, layer, di)
        if g > g_score.get(key, math.inf):
            continue  # stale heap entry

        if is_goal_cell(x, y) and layer in goal_layers:
            # Reconstruct
            pts: List[Waypoint] = [(x, y, layer)]
            cur = key
            while cur in came_from:
                pcur, ppt = came_from[cur]
                pts.append(ppt)
                cur = pcur
            pts.reverse()
            # Land exactly on the true pad centres, not the snapped grid cell.
            if pts:
                pts[0] = (start[0], start[1], pts[0][2])
                pts[-1] = (goal[0], goal[1], pts[-1][2])
            return _simplify(pts)

        li = layers.index(layer)
        # Planar moves on this layer.
        for ndir, (dx, dy) in enumerate(planar):
            nx, ny = x + dx, y + dy
            if nx < xmin or nx > xmax or ny < ymin or ny > ymax:
                continue
            if blocked(nx, ny, layer):
                continue
            step = pitch + off_axis(layer, dx, dy)
            if di != -1 and ndir != di:
                step += bend_pen
            ng = g + step
            nk = dkey(nx, ny, layer, ndir)
            if ng >= g_score.get(nk, math.inf):
                continue
            g_score[nk] = ng
            came_from[nk] = (key, (x, y, layer))
            heapq.heappush(open_heap, (ng + h(nx, ny), ng, nx, ny, layer, ndir))
        # Via move: change layer at the same (x, y). Direction resets (-1).
        # A via is a physical object bigger than a track, so it has its own,
        # larger, all-layer clearance — gate the whole via move on it (endpoints
        # sit on their own pad and are exempt).
        via_here_ok = True
        if (is_via_blocked is not None
                and not (is_start_cell(x, y) or is_goal_cell(x, y))):
            try:
                via_here_ok = not is_via_blocked(x, y)
            except Exception:                               # noqa: BLE001
                via_here_ok = False
        if via_here_ok and via_pen >= 0 and len(layers) > 1:
            for L2 in layers:
                if L2 == layer:
                    continue
                if blocked(x, y, L2):
                    continue
                ng = g + via_pen
                nk = dkey(x, y, L2, -1)
                if ng >= g_score.get(nk, math.inf):
                    continue
                g_score[nk] = ng
                came_from[nk] = (key, (x, y, layer))
                heapq.heappush(open_heap, (ng + h(x, y), ng, x, y, L2, -1))

    return None


def _simplify(pts: List[Waypoint]) -> List[Waypoint]:
    """Collapse consecutive collinear same-layer way-points; keep layer changes."""
    if len(pts) < 3:
        return pts
    out: List[Waypoint] = [pts[0]]
    for p in pts[1:]:
        if len(out) >= 2:
            a, b = out[-2], out[-1]
            same_layer = (a[2] == b[2] == p[2])
            collinear = ((a[0] == b[0] == p[0]) or (a[1] == b[1] == p[1]))
            if same_layer and collinear:
                out[-1] = p
                continue
        out.append(p)
    return out


# --------------------------------------------------------------------------- #
# Way-points -> board geometry
# --------------------------------------------------------------------------- #

def waypoints_to_tracks(waypoints: List[Waypoint]
                        ) -> Tuple[List[Tuple[str, Tuple[float, float],
                                              Tuple[float, float]]],
                                   List[Tuple[float, float, str, str]]]:
    """Turn A* way-points into (segments, vias).

    segments: [(layer, (x1, y1), (x2, y2)), ...] — one per travelled edge.
    vias:     [(x, y, from_layer, to_layer), ...] — one per layer change.
    """
    segments: List[Tuple[str, Tuple[float, float], Tuple[float, float]]] = []
    vias: List[Tuple[float, float, str, str]] = []
    for i in range(len(waypoints) - 1):
        x1, y1, l1 = waypoints[i]
        x2, y2, l2 = waypoints[i + 1]
        if l1 != l2:
            # Layer change at the same point -> via.
            vias.append((x1, y1, l1, l2))
            continue
        if abs(x1 - x2) > 1e-6 or abs(y1 - y2) > 1e-6:
            segments.append((l1, (x1, y1), (x2, y2)))
    return segments, vias


def path_length_mm(waypoints: List[Waypoint]) -> float:
    total = 0.0
    for i in range(len(waypoints) - 1):
        x1, y1, l1 = waypoints[i]
        x2, y2, l2 = waypoints[i + 1]
        if l1 == l2:
            total += math.hypot(x2 - x1, y2 - y1)
    return total
