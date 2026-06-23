"""Tool: best-effort L-shape Manhattan router for short single-layer nets.

NOT a replacement for FreeRouting. Designed to lay down the EASY tracks
the user otherwise has to draw by hand:
  - decoupling cap to IC VDD pin
  - pull-up resistor to MCU GPIO
  - short connector fanout
  - two-pad signal nets within a single block

Algorithm
---------
1. Read every footprint, build pad table (ref, pad#, absolute xy, net id/name).
2. Bucket pads by net.
3. For each net (excluding GND/skip-list, excluding nets with too many
   pads or too wide a bbox):
   a. Build a minimum spanning tree (Prim's) connecting all pads.
   b. For each MST edge, try the two L-shape candidates (horizontal-then-
      vertical vs vertical-then-horizontal) — pick the first that does
      not cross any non-endpoint footprint pad/body.
   c. If both candidates fail, skip that edge and report it.
4. Emit (segment ...) s-expressions on the configured layer.

Universal — every parameter lives in `layout_config.json:route_pcb_simple`.
Never throws — failures are reported as skipped routes so the user can
inspect.
"""
from __future__ import annotations

import math
import re
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool


# ---------------------------------------------------------------------------
# Shared s-expr helpers
# ---------------------------------------------------------------------------

def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _at(node: list) -> Tuple[float, float, float]:
    for child in node[1:]:
        if isinstance(child, list) and _head(child) == "at":
            try:
                x = float(child[1]); y = float(child[2])
                r = float(child[3]) if len(child) > 3 else 0.0
                return (x, y, r)
            except (IndexError, TypeError, ValueError):
                return (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def _prop(node: list, name: str) -> Optional[str]:
    for child in node[1:]:
        if (isinstance(child, list) and _head(child) == "property"
                and len(child) >= 3 and str(child[1]) == name):
            return str(child[2])
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
        return _load_layout_config().get("route_pcb_simple", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Serializer (matches the other PCB tools)
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
# Pad / net extraction
# ---------------------------------------------------------------------------

def _rotate(dx: float, dy: float, rot_deg: float) -> Tuple[float, float]:
    if rot_deg == 0.0:
        return dx, dy
    a = math.radians(rot_deg)
    c, s = math.cos(a), math.sin(a)
    return c * dx - s * dy, s * dx + c * dy


def _pad_number(pad: list) -> str:
    for child in pad[1:]:
        if isinstance(child, str):
            return child
    return ""


def _pad_size(pad: list) -> Tuple[float, float]:
    for child in pad[1:]:
        if (isinstance(child, list) and _head(child) == "size"
                and len(child) >= 3):
            try:
                return (float(child[1]), float(child[2]))
            except (TypeError, ValueError):
                return (0.0, 0.0)
    return (0.0, 0.0)


def _pad_net(pad: list) -> Tuple[int, str]:
    for child in pad[1:]:
        if (isinstance(child, list) and _head(child) == "net"
                and len(child) >= 2):
            try:
                nid = int(child[1])
            except (TypeError, ValueError):
                return (0, "")
            name = str(child[2]) if len(child) >= 3 else ""
            return (nid, name)
    return (0, "")


def _net_table(root: list) -> Dict[int, str]:
    """Top-level (net N "name") entries — net 0 is the implicit
    'no-connect' net per KiCad convention."""
    out: Dict[int, str] = {}
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "net"):
            continue
        if len(child) < 3:
            continue
        try:
            nid = int(child[1])
            out[nid] = str(child[2])
        except (TypeError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# Net-class table from .kicad_pro
# ---------------------------------------------------------------------------

def _load_netclass_widths(pcb_path: Path) -> Dict[str, float]:
    """Pull {class_name: track_width_mm} from the sibling .kicad_pro.
    Empty dict when the project file is missing or schema-incompatible
    — caller falls back to default_track_width_mm."""
    pro = pcb_path.with_suffix(".kicad_pro")
    if not pro.exists():
        return {}
    try:
        import json as _json
        data = _json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: Dict[str, float] = {}
    for cls in (data.get("net_settings", {}) or {}).get("classes", []) or []:
        name = cls.get("name") or ""
        try:
            w = float(cls.get("track_width", 0.0))
        except (TypeError, ValueError):
            continue
        if name and w > 0.0:
            out[name] = w
    return out


def _load_netclass_patterns(pcb_path: Path) -> List[Tuple[str, str]]:
    """Pull the [(pattern, classname), ...] assignment rules from
    `.kicad_pro -> net_settings.netclass_patterns`. Pattern syntax is
    KiCad's wildcard (* / ?)."""
    pro = pcb_path.with_suffix(".kicad_pro")
    if not pro.exists():
        return []
    try:
        import json as _json
        data = _json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: List[Tuple[str, str]] = []
    for rule in ((data.get("net_settings", {}) or {})
                  .get("netclass_patterns", []) or []):
        p = rule.get("pattern") or ""
        c = rule.get("netclass") or "Default"
        if p:
            out.append((p, c))
    return out


def _wildcard_to_re(pattern: str) -> re.Pattern:
    rx = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return re.compile("^" + rx + "$", re.IGNORECASE)


def _track_width_for(net_name: str,
                      netclass_widths: Dict[str, float],
                      patterns: List[Tuple[str, str]],
                      default_mm: float) -> float:
    for pat, cls in patterns:
        if _wildcard_to_re(pat).match(net_name):
            if cls in netclass_widths:
                return netclass_widths[cls]
    return netclass_widths.get("Default", default_mm)


# ---------------------------------------------------------------------------
# Geometry — segment / bbox tests
# ---------------------------------------------------------------------------

def _chamfer_corners(segs: List[Tuple[Tuple[float, float],
                                        Tuple[float, float]]],
                      chamfer_mm: float,
                      obstacles: List[Tuple[float, float, float, float]]
                      ) -> List[Tuple[Tuple[float, float],
                                        Tuple[float, float]]]:
    """R2.3 (2026-05-27). Replace each right-angle corner with a 45°
    bevel of length `chamfer_mm`. Per rule "Avoid 90-degree bends; use
    45-degree bends" — improves visual quality and slightly reduces
    high-frequency reflection at the corner.

    Algorithm: walk segment pairs. When two consecutive segments meet
    at a perpendicular 90° corner, pull back from the corner by
    `chamfer_mm` on each side and insert a 45° diagonal between the
    two new endpoints. The diagonal is rejected (corner kept square)
    if it crosses any obstacle or if either pulled-back length is
    longer than half the segment — keeps stub corners safe.

    Universal — works on any route shape (L, Z, A* multi-corner).
    """
    if chamfer_mm <= 0.0 or len(segs) < 2:
        return list(segs)
    out: List[Tuple[Tuple[float, float], Tuple[float, float]]] = list(segs)
    # Iterate over corners; rebuild in place. Single pass left-to-right;
    # each corner consumed once because new shorter segments replace the
    # adjacent originals so we step over them.
    i = 0
    while i < len(out) - 1:
        s1_a, s1_b = out[i]
        s2_a, s2_b = out[i + 1]
        # Corner is at s1_b == s2_a
        if s1_b != s2_a:
            i += 1
            continue
        cx, cy = s1_b
        # Direction vectors as unit signs
        dx1 = (s1_b[0] - s1_a[0])
        dy1 = (s1_b[1] - s1_a[1])
        dx2 = (s2_b[0] - s2_a[0])
        dy2 = (s2_b[1] - s2_a[1])
        # Must be axis-aligned, non-zero, and perpendicular
        if not ((dx1 == 0 or dy1 == 0) and (dx2 == 0 or dy2 == 0)):
            i += 1; continue
        if dx1 == 0 and dx2 == 0:
            i += 1; continue   # collinear vertical
        if dy1 == 0 and dy2 == 0:
            i += 1; continue   # collinear horizontal
        len1 = abs(dx1) + abs(dy1)
        len2 = abs(dx2) + abs(dy2)
        L = min(chamfer_mm, len1 / 2.0, len2 / 2.0)
        if L < 0.05:
            i += 1; continue   # too small to bother
        # Unit step toward the corner along seg1
        ux1 = (dx1 / len1) if len1 > 0 else 0.0
        uy1 = (dy1 / len1) if len1 > 0 else 0.0
        # Unit step away from the corner along seg2
        ux2 = (dx2 / len2) if len2 > 0 else 0.0
        uy2 = (dy2 / len2) if len2 > 0 else 0.0
        pt1 = (round((cx - ux1 * L) / 0.01) * 0.01,
               round((cy - uy1 * L) / 0.01) * 0.01)
        pt2 = (round((cx + ux2 * L) / 0.01) * 0.01,
               round((cy + uy2 * L) / 0.01) * 0.01)
        # Reject the chamfer if the diagonal crosses any obstacle
        new_diag = (pt1, pt2)
        # Simple obstacle test — sample midpoint (chamfers are short).
        mx = (pt1[0] + pt2[0]) / 2.0
        my = (pt1[1] + pt2[1]) / 2.0
        crosses = any(ox1 < mx < ox2 and oy1 < my < oy2
                       for (ox1, oy1, ox2, oy2) in obstacles)
        if crosses:
            i += 1; continue
        # Apply: shrink seg1 to (s1_a → pt1), insert diagonal, shrink
        # seg2 to (pt2 → s2_b)
        out[i] = (s1_a, pt1)
        out.insert(i + 1, new_diag)
        out[i + 2] = (pt2, s2_b)
        i += 2   # skip past the inserted diagonal
    # Drop any zero-length segments produced by chamfering a corner
    # whose adjacent segment was already exactly L long.
    return [(s, e) for (s, e) in out
            if abs(s[0] - e[0]) > 1e-6 or abs(s[1] - e[1]) > 1e-6]


def _segments_for_L(a: Tuple[float, float], b: Tuple[float, float],
                     prefer_axis: str = "any"
                     ) -> List[List[Tuple[Tuple[float, float],
                                          Tuple[float, float]]]]:
    """Two L-shape candidates between two points. Each is a list of
    (start, end) pairs.

    `prefer_axis` controls candidate ordering per the layer-axis policy
    (R1.2): when "h" preferred (F.Cu by convention), the horizontal-
    first L variant is tried before vertical-first. "v" for B.Cu. "any"
    keeps the legacy [horizontal, vertical] order. Universal — works
    for any net, only changes WHICH L is tested first when both are
    obstacle-free.
    """
    (ax, ay), (bx, by) = a, b
    h_first = [(a, (bx, ay)), ((bx, ay), b)]          # horizontal first
    v_first = [(a, (ax, by)), ((ax, by), b)]          # vertical first
    if prefer_axis == "v":
        return [v_first, h_first]
    # "h" or "any" → horizontal first (legacy default)
    return [h_first, v_first]


def _segments_for_Z(a: Tuple[float, float], b: Tuple[float, float],
                     obstacles: List[Tuple[float, float, float, float]],
                     step_mm: float = 2.54,
                     max_offset_mm: float = 25.0
                     ) -> List[List[Tuple[Tuple[float, float],
                                          Tuple[float, float]]]]:
    """Generate Z-shape (3-segment) candidates between a and b that
    go AROUND obstacles. The Z has a mid-axis perpendicular to the
    main travel direction.

    For horizontal travel (|dx| >= |dy|), the Z is:
      a -> (a.x, midY) -> (b.x, midY) -> b
    midY is searched outward from the y-midpoint in steps until either
    the candidate clears all obstacles or `max_offset_mm` is reached.

    For vertical travel, the symmetric form with midX.

    Returns the list of candidate paths (each a list of segment tuples).
    Caller picks the first that fully clears the obstacle list.
    """
    (ax, ay), (bx, by) = a, b
    dx = abs(bx - ax)
    dy = abs(by - ay)
    cands: List[List[Tuple[Tuple[float, float], Tuple[float, float]]]] = []
    if dx >= dy:
        # Horizontal-dominant: bend around obstacles in the Y direction.
        midY0 = (ay + by) / 2
        steps = int(max_offset_mm / step_mm)
        for k in range(1, steps + 1):
            for direction in (-1, 1):
                midY = midY0 + direction * k * step_mm
                path = [(a, (ax, midY)),
                        ((ax, midY), (bx, midY)),
                        ((bx, midY), b)]
                if all(not any(_seg_intersects_rect(s, e, rect)
                                for rect in obstacles)
                        for (s, e) in path):
                    cands.append(path)
                    if len(cands) >= 2:
                        return cands
    else:
        # Vertical-dominant: bend around obstacles in the X direction.
        midX0 = (ax + bx) / 2
        steps = int(max_offset_mm / step_mm)
        for k in range(1, steps + 1):
            for direction in (-1, 1):
                midX = midX0 + direction * k * step_mm
                path = [(a, (midX, ay)),
                        ((midX, ay), (midX, by)),
                        ((midX, by), b)]
                if all(not any(_seg_intersects_rect(s, e, rect)
                                for rect in obstacles)
                        for (s, e) in path):
                    cands.append(path)
                    if len(cands) >= 2:
                        return cands
    return cands


def _make_via(x: float, y: float, drill_mm: float, size_mm: float,
               net_id: int,
               top_layer: str = "F.Cu",
               bottom_layer: str = "B.Cu") -> list:
    """Build a (via ...) s-expression. Spans top<->bottom layer pair."""
    return [
        sexpdata.Symbol("via"),
        [sexpdata.Symbol("at"), x, y],
        [sexpdata.Symbol("size"), size_mm],
        [sexpdata.Symbol("drill"), drill_mm],
        [sexpdata.Symbol("layers"), top_layer, bottom_layer],
        [sexpdata.Symbol("net"), net_id],
        [sexpdata.Symbol("uuid"), str(_uuid.uuid4())],
    ]


def _seg_intersects_rect(p1: Tuple[float, float], p2: Tuple[float, float],
                          rect: Tuple[float, float, float, float]) -> bool:
    """True if axis-aligned Manhattan segment overlaps the rectangle.
    `rect` = (xmin, ymin, xmax, ymax). Both endpoints excluded is fine
    because we only care about *crossing* obstacles."""
    x1, y1 = p1
    x2, y2 = p2
    xmin, ymin, xmax, ymax = rect
    # Horizontal segment
    if y1 == y2:
        y = y1
        if y < ymin or y > ymax:
            return False
        lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
        return lo <= xmax and hi >= xmin
    # Vertical segment
    if x1 == x2:
        x = x1
        if x < xmin or x > xmax:
            return False
        lo, hi = (y1, y2) if y1 <= y2 else (y2, y1)
        return lo <= ymax and hi >= ymin
    # Diagonal segments aren't produced by L-routing
    return False


def _pad_bbox(pad_abs_x: float, pad_abs_y: float,
               pw: float, ph: float, inflate: float
               ) -> Tuple[float, float, float, float]:
    return (pad_abs_x - pw / 2 - inflate,
            pad_abs_y - ph / 2 - inflate,
            pad_abs_x + pw / 2 + inflate,
            pad_abs_y + ph / 2 + inflate)


# ---------------------------------------------------------------------------
# R1.1: A* fallback on a Manhattan grid. Used when L/Z/via all fail.
# Pure Python — small grid, only invoked for the residual ~5–10% of nets
# the cheaper tiers can't route. Returns a list of (start, end) segments
# or None when even A* can't find a path.
# ---------------------------------------------------------------------------

def _astar_route(a: Tuple[float, float], b: Tuple[float, float],
                  obstacles: List[Tuple[float, float, float, float]],
                  step_mm: float,
                  prefer_axis: str,
                  bounds_margin_mm: float = 5.0,
                  max_nodes: int = 50_000,
                  ) -> Optional[List[Tuple[Tuple[float, float],
                                            Tuple[float, float]]]]:
    """A* Manhattan-grid path search from `a` to `b`, avoiding any cell
    that lies inside an obstacle rectangle.

    Grid pitch = `step_mm`. Search bounded by the bbox of a + b inflated
    by `bounds_margin_mm` so it never wanders the whole board. Nodes
    explored capped at `max_nodes` — when exceeded, returns None and the
    caller skips the edge.

    Turns are penalised slightly so paths with fewer corners win when
    cost ties. The `prefer_axis` first-step bias matches the layer-axis
    policy from R1.2.

    Pure Python heapq, ~120 LOC, no external dependencies. JSON-driven
    via `route_pcb_simple.astar_*` so tuning needs no code change.
    """
    import heapq as _heapq

    ax, ay = a
    bx, by = b
    # Snap endpoints to grid
    def snap(v: float) -> float:
        return round(v / step_mm) * step_mm
    sa = (snap(ax), snap(ay))
    sb = (snap(bx), snap(by))
    if sa == sb:
        return [(a, b)]

    xmin = min(sa[0], sb[0]) - bounds_margin_mm
    xmax = max(sa[0], sb[0]) + bounds_margin_mm
    ymin = min(sa[1], sb[1]) - bounds_margin_mm
    ymax = max(sa[1], sb[1]) + bounds_margin_mm

    def in_obstacle(x: float, y: float) -> bool:
        for (ox1, oy1, ox2, oy2) in obstacles:
            # Strict interior — pad edges are reachable. Endpoints sit
            # on pad centres so the start/goal cells are explicitly let
            # through below.
            if ox1 < x < ox2 and oy1 < y < oy2:
                return True
        return False

    def is_endpoint(x: float, y: float) -> bool:
        return ((abs(x - sa[0]) < 1e-6 and abs(y - sa[1]) < 1e-6)
                or (abs(x - sb[0]) < 1e-6 and abs(y - sb[1]) < 1e-6))

    def manhattan(x: float, y: float) -> float:
        return abs(x - sb[0]) + abs(y - sb[1])

    # Direction vectors. Prefer the policy axis first → ties resolve in
    # its favour because Python's heap is stable on equal keys.
    if prefer_axis == "v":
        moves = [(0, step_mm), (0, -step_mm), (step_mm, 0), (-step_mm, 0)]
    else:
        moves = [(step_mm, 0), (-step_mm, 0), (0, step_mm), (0, -step_mm)]

    # (f, g, x, y, came_from_key, came_dir)
    open_heap: List[Tuple[float, float, float, float, Optional[str], Optional[Tuple[float, float]]]] = []
    _heapq.heappush(open_heap, (manhattan(*sa), 0.0, sa[0], sa[1], None, None))
    came_from: Dict[str, Tuple[str, Tuple[float, float]]] = {}
    g_score: Dict[str, float] = {f"{sa[0]:.4f},{sa[1]:.4f}": 0.0}
    nodes_visited = 0
    turn_penalty = step_mm * 0.5

    while open_heap:
        if nodes_visited > max_nodes:
            return None
        nodes_visited += 1
        f, g, x, y, parent_key, last_dir = _heapq.heappop(open_heap)
        key = f"{x:.4f},{y:.4f}"
        if abs(x - sb[0]) < 1e-6 and abs(y - sb[1]) < 1e-6:
            # Reconstruct path
            path_pts: List[Tuple[float, float]] = [(x, y)]
            cur_key = key
            while cur_key in came_from:
                pk, ppt = came_from[cur_key]
                path_pts.append(ppt)
                cur_key = pk
            path_pts.reverse()
            # Collapse colinear consecutive points to fewer segments
            if len(path_pts) < 2:
                return [(a, b)]
            simplified: List[Tuple[float, float]] = [path_pts[0]]
            for p in path_pts[1:]:
                if len(simplified) >= 2:
                    p0, p1 = simplified[-2], simplified[-1]
                    if ((p0[0] == p1[0] == p[0]) or (p0[1] == p1[1] == p[1])):
                        simplified[-1] = p
                        continue
                simplified.append(p)
            # Replace the snapped first / last endpoints with the
            # original pad-centre coordinates so the route lands on the
            # actual pad, not on the grid cell next to it.
            if len(simplified) >= 1:
                simplified[0] = a
                simplified[-1] = b
            segs = [(simplified[i], simplified[i + 1])
                     for i in range(len(simplified) - 1)]
            return segs
        for (dx, dy) in moves:
            nx, ny = x + dx, y + dy
            if nx < xmin or nx > xmax or ny < ymin or ny > ymax:
                continue
            if not is_endpoint(nx, ny) and in_obstacle(nx, ny):
                continue
            nkey = f"{nx:.4f},{ny:.4f}"
            new_g = g + step_mm
            # Turn penalty: discourage zigzag paths when straight is OK
            this_dir = (dx, dy)
            if last_dir is not None and this_dir != last_dir:
                new_g += turn_penalty
            if nkey in g_score and g_score[nkey] <= new_g:
                continue
            g_score[nkey] = new_g
            came_from[nkey] = (key, (x, y))
            nf = new_g + manhattan(nx, ny)
            _heapq.heappush(open_heap, (nf, new_g, nx, ny, key, this_dir))

    return None


# ---------------------------------------------------------------------------
# MST
# ---------------------------------------------------------------------------

def _mst_edges(pads: List[Tuple[float, float]]) -> List[Tuple[int, int]]:
    """Prim's MST on euclidean distance. Returns (i, j) index pairs."""
    n = len(pads)
    if n < 2:
        return []
    in_tree = [False] * n
    dist = [float("inf")] * n
    parent = [-1] * n
    in_tree[0] = True
    for j in range(1, n):
        dx = pads[j][0] - pads[0][0]
        dy = pads[j][1] - pads[0][1]
        dist[j] = dx * dx + dy * dy
        parent[j] = 0
    edges: List[Tuple[int, int]] = []
    for _ in range(n - 1):
        best = -1
        bd = float("inf")
        for k in range(n):
            if not in_tree[k] and dist[k] < bd:
                bd = dist[k]
                best = k
        if best < 0:
            break
        in_tree[best] = True
        edges.append((parent[best], best))
        for k in range(n):
            if not in_tree[k]:
                dx = pads[k][0] - pads[best][0]
                dy = pads[k][1] - pads[best][1]
                d = dx * dx + dy * dy
                if d < dist[k]:
                    dist[k] = d
                    parent[k] = best
    return edges


# ---------------------------------------------------------------------------
# Segment builder
# ---------------------------------------------------------------------------

def _make_segment(p1: Tuple[float, float], p2: Tuple[float, float],
                   width_mm: float, layer: str, net_id: int) -> list:
    return [
        sexpdata.Symbol("segment"),
        [sexpdata.Symbol("start"), p1[0], p1[1]],
        [sexpdata.Symbol("end"),   p2[0], p2[1]],
        [sexpdata.Symbol("width"), width_mm],
        [sexpdata.Symbol("layer"), layer],
        [sexpdata.Symbol("net"), net_id],
        [sexpdata.Symbol("uuid"), str(_uuid.uuid4())],
    ]


# ---------------------------------------------------------------------------
# Tool entry
# ---------------------------------------------------------------------------

@tool(
    name="route_pcb_simple",
    description=(
        "Best-effort L-shape router for short nets on a .kicad_pcb. "
        "Connects 2-to-N-pad nets on a single layer (F.Cu by default) "
        "using a minimum spanning tree of L-shaped traces. Skips GND/VSS "
        "(handled by auto_zones_pcb), skips dense or wide-spanning nets "
        "(left to a real autorouter). Not a replacement for FreeRouting — "
        "covers the easy cases so the user only has to hand-route the "
        "hard ones.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}        # required\n'
        '  {"pcb_path": "...", "preview_only": true}     # dry-run, no mutation\n'
        '  {"pcb_path": "...", "layer": "B.Cu"}          # route on back\n'
        '  {"pcb_path": "...", "replace": true}          # drop existing tracks first\n'
        "All defaults in layout_config.json:route_pcb_simple. Run AFTER "
        "auto_place_pcb so pad positions are stable. Skipped nets are "
        "listed in the result so the user knows what still needs routing."
    ),
    input_schema={"pcb_path": str},
)
async def route_pcb_simple(args: dict[str, Any]) -> dict[str, Any]:
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
                              "text": "route_pcb_simple disabled"}],
                 "is_error": True}

    layer        = str(args.get("layer", cfg.get("layer", "F.Cu")))
    back_layer   = str(args.get("back_layer", cfg.get("back_layer", "B.Cu")))
    max_pads     = int(args.get("max_pads_per_net",
                                  cfg.get("max_pads_per_net", 6)))
    # max_net_span_mm: if 0 (or "auto"), derive from board diagonal so the
    # cap scales with board size — a 60 mm fixed cap skips legitimate
    # power rails on any board bigger than ~80 x 80 mm. Per [P2.1].
    _span_cfg = args.get("max_net_span_mm", cfg.get("max_net_span_mm", 0.0))
    try:
        max_span = float(_span_cfg)
    except (TypeError, ValueError):
        max_span = 0.0
    if max_span <= 0.0:
        max_span = -1.0  # sentinel → compute from board bbox after pad scan
    default_w    = float(args.get("default_track_width_mm",
                                    cfg.get("default_track_width_mm", 0.25)))
    skip_nets    = set(s.upper() for s in (
        args.get("skip_nets") or cfg.get("skip_nets",
            ["GND", "AGND", "DGND", "PGND", "EGND", "SGND", "VSS", "0", ""])))
    use_class_w  = bool(cfg.get("use_net_class_width", True))
    inflate      = float(cfg.get("obstacle_inflate_mm", 0.5))
    preview_only = bool(args.get("preview_only",
                                   cfg.get("preview_only", False)))
    replace      = bool(args.get("replace",
                                   cfg.get("replace_existing", False)))
    enable_z     = bool(args.get("enable_z_detour",
                                    cfg.get("enable_z_detour", True)))
    # R1.2: layer axis policy — F.Cu prefers horizontal traces, B.Cu
    # prefers vertical (standard PCB convention to minimise track
    # crossings on two-layer boards). JSON-driven so 4-layer or rotated
    # boards override per project. Map layer name → "h"/"v"/"any".
    _axis_map = cfg.get("layer_axis_policy", {"F.Cu": "h", "B.Cu": "v"})
    front_axis = _axis_map.get(layer, "any")
    back_axis  = _axis_map.get(cfg.get("back_layer", "B.Cu"), "v")
    enable_vias  = bool(args.get("enable_vias",
                                    cfg.get("enable_vias", True)))
    via_drill_mm = float(cfg.get("via_drill_mm", 0.3))
    via_size_mm  = float(cfg.get("via_size_mm", 0.6))
    max_vias_per_net = int(cfg.get("max_vias_per_net", 4))
    # R1.3: per-net-class via budget. Power rails often need stitching
    # vias to bridge zone-to-zone returns; the global cap (4) starves
    # them. JSON map class_name → cap; default just bumps POWER to 16.
    max_vias_per_class = dict(cfg.get("max_vias_per_class", {
        "Power": 16, "POWER": 16, "GND": 16,
    }))
    z_step_mm    = float(cfg.get("z_step_mm", 2.54))
    z_max_offset = float(cfg.get("z_max_offset_mm", 25.0))
    # R1.1: A* fallback knobs
    enable_astar      = bool(cfg.get("enable_astar", True))
    # R2.3: 45° corner bevels. Default 0.5 mm chamfer — keeps the visual
    # benefit without lengthening short stubs. 0 disables.
    corner_chamfer_mm = float(cfg.get("corner_chamfer_mm", 0.5))
    astar_step_mm     = float(cfg.get("astar_step_mm", 1.27))
    astar_margin_mm   = float(cfg.get("astar_bounds_margin_mm", 8.0))
    astar_max_nodes   = int(cfg.get("astar_max_nodes", 50_000))

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

    # ---- Pad extraction ----
    # Pad list: (net_id, net_name, ref, pad_num, abs_x, abs_y, pw, ph)
    pads: List[Tuple[int, str, str, str, float, float, float, float]] = []
    for fp in root[1:]:
        if not (isinstance(fp, list) and _head(fp) == "footprint"):
            continue
        ref = _prop(fp, "Reference") or ""
        fp_x, fp_y, fp_rot = _at(fp)
        for child in fp[1:]:
            if not (isinstance(child, list) and _head(child) == "pad"):
                continue
            num = _pad_number(child)
            pad_x, pad_y, _ = _at(child)
            pw, ph = _pad_size(child)
            net_id, net_name = _pad_net(child)
            dx, dy = _rotate(pad_x, pad_y, fp_rot)
            pads.append((net_id, net_name, ref, num,
                          fp_x + dx, fp_y + dy, pw, ph))

    if not pads:
        return {"content": [{"type": "text",
                              "text": "PCB has no pads. Run F8 first."}],
                 "is_error": True}

    # Auto max_span = board pad-bbox diagonal × factor. Keeps the
    # skip-on-huge-span guard active without locking out legitimate rails
    # on bigger boards. Factor from JSON (max_net_span_diag_factor,
    # default 1.0 = full diagonal).
    if max_span < 0.0:
        xs = [p[4] for p in pads]
        ys = [p[5] for p in pads]
        if xs and ys:
            diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
        else:
            diag = 60.0
        factor = float(cfg.get("max_net_span_diag_factor", 1.0))
        max_span = max(diag * factor, 30.0)  # floor at 30 mm for tiny boards

    # Bucket pads by net
    by_net: Dict[int, Dict[str, Any]] = {}
    for (nid, nname, ref, num, x, y, pw, ph) in pads:
        b = by_net.setdefault(nid, {"name": nname, "pads": []})
        b["pads"].append({"ref": ref, "num": num,
                           "xy": (x, y), "size": (pw, ph)})
        # Pad-side name may differ from the net table; keep the longer
        if not b["name"] and nname:
            b["name"] = nname
    # Fall back to top-level net table for empty names
    nt = _net_table(root)
    for nid, b in by_net.items():
        if not b["name"]:
            b["name"] = nt.get(nid, "")

    # Net-class lookup tables (read once)
    netclass_widths = _load_netclass_widths(pcb_path) if use_class_w else {}
    netclass_patterns = _load_netclass_patterns(pcb_path) if use_class_w else []

    # ---- Optional: clear existing tracks on this layer ----
    removed = 0
    if replace:
        kept: List[Any] = [root[0]]
        for child in root[1:]:
            if (isinstance(child, list) and _head(child) == "segment"
                    and _layer_of(child) == layer):
                removed += 1
                continue
            kept.append(child)
        root = kept

    # ---- Per-net routing ----
    routed_nets: List[str] = []
    skipped_nets: List[Dict[str, Any]] = []
    new_segments: List[list] = []
    total_segments = 0
    total_length_mm = 0.0

    # All pad bboxes for obstacle checking (we exclude same-net pads
    # from the obstacle set when routing each net).
    all_pad_bboxes: List[Tuple[int, Tuple[float, float, float, float]]] = []
    for (nid, _nname, _ref, _num, x, y, pw, ph) in pads:
        if pw > 0 and ph > 0:
            all_pad_bboxes.append((nid, _pad_bbox(x, y, pw, ph, inflate)))

    for nid, info in sorted(by_net.items()):
        name = info["name"]
        net_pads = info["pads"]
        if nid == 0 or not name:
            skipped_nets.append({"net": name or f"(net {nid})",
                                  "reason": "no net (unconnected)"})
            continue
        if name.upper() in skip_nets:
            skipped_nets.append({"net": name, "reason": "in skip_nets"})
            continue
        if len(net_pads) < 2:
            skipped_nets.append({"net": name, "reason": "single pad"})
            continue
        if len(net_pads) > max_pads:
            skipped_nets.append({"net": name,
                                  "reason": f"{len(net_pads)} pads > max ({max_pads})"})
            continue
        xs = [p["xy"][0] for p in net_pads]
        ys = [p["xy"][1] for p in net_pads]
        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
        if span_x > max_span or span_y > max_span:
            skipped_nets.append({"net": name,
                                  "reason": f"span {span_x:.0f}x{span_y:.0f} > "
                                            f"{max_span:.0f} mm"})
            continue

        width = _track_width_for(name, netclass_widths, netclass_patterns,
                                   default_w)
        coords = [p["xy"] for p in net_pads]
        edges = _mst_edges(coords)
        # P2.3: route the LONGEST MST edges first. Prim's emits edges in
        # discovery order (shortest-first); short edges then fill up the
        # open channels and the long ones get squeezed out. Sorting
        # long-first lets the difficult spans grab their preferred L-route
        # before the cheap edges claim the same corridor.
        edges_with_len = [
            (i, j, math.hypot(coords[i][0] - coords[j][0],
                                coords[i][1] - coords[j][1]))
            for (i, j) in edges
        ]
        edges_with_len.sort(key=lambda e: -e[2])
        edges = [(i, j) for (i, j, _l) in edges_with_len]
        # Pre-compute the obstacle set: every pad NOT on this net
        obstacles = [bb for (other_nid, bb) in all_pad_bboxes
                     if other_nid != nid]

        edges_routed = 0
        edges_skipped = 0
        vias_used = 0
        # Per-net via cap so a single dense net can't punch the whole
        # board full of stitching. R1.3: power-class nets get a bigger
        # budget because they often legitimately need 8-16 stitching
        # vias across the board.
        net_class = None
        for pat, cls in netclass_patterns:
            if _wildcard_to_re(pat).match(name):
                net_class = cls
                break
        net_via_cap = int(max_vias_per_class.get(net_class or "",
                                                   max_vias_per_net))
        if net_via_cap < max_vias_per_net:
            net_via_cap = max_vias_per_net
        for (i, j) in edges:
            a = coords[i]; b = coords[j]
            picked_layer = layer
            picked: Optional[List[Tuple[Tuple[float, float],
                                         Tuple[float, float]]]] = None
            picked_via: Optional[Tuple[float, float]] = None

            # ---- Tier 1: L-shape on the primary layer ----
            # Prefer the layer-axis-policy ordering (F.Cu → horizontal-first)
            # so non-piercing L variants on the natural axis are picked
            # before their cross-axis sibling.
            for cand in _segments_for_L(a, b, prefer_axis=front_axis):
                if all(not any(_seg_intersects_rect(s, e, rect)
                                for rect in obstacles)
                        for (s, e) in cand):
                    picked = cand
                    break

            # ---- Tier 2: Z-detour on the primary layer ----
            if picked is None and enable_z:
                z_cands = _segments_for_Z(a, b, obstacles,
                                            step_mm=z_step_mm,
                                            max_offset_mm=z_max_offset)
                if z_cands:
                    picked = z_cands[0]

            # ---- Tier 3: via swap — route on F.Cu to a midpoint,
            # drop a via, finish on B.Cu. Useful when the primary layer
            # is completely blocked. We pick the via location as the
            # midpoint of the L's corner — gives the best chance of
            # both halves routing cleanly.
            via_layer_obstacles = obstacles  # for now, share — most
                                              # pads exist on both layers
            if (picked is None and enable_vias
                    and vias_used < net_via_cap):
                # Try BOTH L-corner positions as the via drop point.
                # Use the front-axis preference for the front-half so
                # the first via-swap candidate respects the layer policy.
                for cand_L in _segments_for_L(a, b, prefer_axis=front_axis):
                    via_pt = cand_L[0][1]   # the corner of the L
                    # Round to grid
                    via_pt = (round(via_pt[0] / 0.05) * 0.05,
                              round(via_pt[1] / 0.05) * 0.05)
                    # Front-layer half (a -> via_pt). Then back-layer
                    # half (via_pt -> b). Each half is a single
                    # straight segment so guaranteed-Manhattan.
                    front_seg = (a, via_pt)
                    back_seg = (via_pt, b)
                    if (not any(_seg_intersects_rect(front_seg[0], front_seg[1], rect)
                                 for rect in obstacles)
                            and not any(_seg_intersects_rect(back_seg[0], back_seg[1], rect)
                                          for rect in via_layer_obstacles)):
                        picked = [front_seg]
                        picked_via = via_pt
                        # back_seg drawn on B.Cu below
                        back_path = [back_seg]
                        break
                else:
                    back_path = None
            else:
                back_path = None

            # ---- Tier 4: A* fallback on a Manhattan grid (R1.1) ----
            # When L, Z, AND via-swap all fail, fall through to a real
            # grid search. Only fires for the residual nets the cheaper
            # tiers can't route — typical board sees ≤5% of edges reach
            # this tier. Returns multi-corner path that the L-router
            # can't produce.
            if picked is None and enable_astar:
                a_path = _astar_route(
                    a, b, obstacles,
                    step_mm=astar_step_mm,
                    prefer_axis=front_axis,
                    bounds_margin_mm=astar_margin_mm,
                    max_nodes=astar_max_nodes,
                )
                if a_path:
                    picked = a_path

            if picked is None:
                edges_skipped += 1
                continue
            # ONE successful MST edge — increment once regardless of
            # how many segments the route took (L=2, Z=3, via path=2+).
            edges_routed += 1

            # R2.3: chamfer right-angle corners on the front-layer path
            # before emission. Pure post-pass — leaves length unchanged
            # to ±diag, never adds new obstacle crossings (rejects the
            # chamfer when the diagonal would clip a pad). Same for the
            # back-half path below.
            if corner_chamfer_mm > 0.0 and len(picked) >= 2:
                picked = _chamfer_corners(picked, corner_chamfer_mm, obstacles)

            # Emit front-layer segments
            for (s, e) in picked:
                if s == e:
                    continue
                new_segments.append(_make_segment(s, e, width, layer, nid))
                total_segments += 1
                total_length_mm += math.hypot(e[0] - s[0], e[1] - s[1])

            # Emit via + back-layer half if Tier 3 fired
            if picked_via is not None and back_path:
                new_segments.append(_make_via(picked_via[0], picked_via[1],
                                                via_drill_mm, via_size_mm,
                                                nid,
                                                top_layer=layer,
                                                bottom_layer=back_layer))
                vias_used += 1
                if corner_chamfer_mm > 0.0 and len(back_path) >= 2:
                    back_path = _chamfer_corners(back_path, corner_chamfer_mm,
                                                   obstacles)
                for (s, e) in back_path:
                    if s == e:
                        continue
                    new_segments.append(_make_segment(s, e, width,
                                                       back_layer, nid))
                    total_segments += 1
                    total_length_mm += abs(e[0] - s[0]) + abs(e[1] - s[1])

        if edges_routed:
            routed_nets.append(f"{name}({edges_routed}/{len(edges)})"
                                + (f"+{vias_used}V" if vias_used else ""))
        if edges_skipped:
            skipped_nets.append({"net": name,
                                  "reason": f"{edges_skipped}/{len(edges)} "
                                            f"edge(s) blocked even after Z+vias"})

    # ---- Write back (unless preview_only) ----
    if not preview_only and new_segments:
        for seg in new_segments:
            root.append(seg)
        try:
            pcb_path.write_text(_emit(root), encoding="utf-8")
        except Exception as exc:
            return {"content": [{"type": "text",
                                  "text": f"ERROR: write failed: {exc}"}],
                     "is_error": True}

    # ---- Report ----
    lines = [
        f"route_pcb_simple → {pcb_path.name}",
        f"  layer:        {layer}",
        f"  segments:     {total_segments}"
        + ("  (preview only — file unchanged)" if preview_only else ""),
        f"  length total: {total_length_mm:.1f} mm",
        f"  removed:      {removed} existing track(s) on {layer}",
    ]
    if routed_nets:
        lines.append("  routed nets:  " + ", ".join(routed_nets[:15])
                      + (f" (+{len(routed_nets) - 15} more)"
                          if len(routed_nets) > 15 else ""))
    if skipped_nets:
        lines.append(f"  skipped:      {len(skipped_nets)} net(s):")
        for s in skipped_nets[:8]:
            lines.append(f"    - {s['net']}: {s['reason']}")
        if len(skipped_nets) > 8:
            lines.append(f"    ... (+{len(skipped_nets) - 8} more)")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": True,
        "path": str(pcb_path),
        "layer": layer,
        "segments_added": total_segments,
        "length_mm": round(total_length_mm, 2),
        "removed": removed,
        "routed_nets": routed_nets,
        "skipped_nets": skipped_nets,
        "preview_only": preview_only,
    }
