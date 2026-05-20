"""P13 — net flow direction & sheet coordinate planning.

The community partition (P12) tells us WHICH blocks exist. This module
tells us WHERE to put them on the parent sheet so the reading order
follows signal flow:

    POWER  →  PROCESSING  →  SINK
    INPUT (left/top)         OUTPUT (right/bottom)
    side-channels (debug/reset/crystal) on the periphery

Three stages:

  Stage 1 — Block flow-role classification. Map every block name to a
            `flow_role` (source / ingress / transform / processing /
            bidirectional / sink / side_channel) and a `flow_rank` (0=
            top, larger = lower). Defaults are declarative & extensible
            via `flow_direction_config.json`.

  Stage 2 — DAG ordering. Build an inter-block directed graph from the
            partition's `cross_cluster_edges`: edges run from the
            lower-rank block to the higher-rank block (source → sink).
            A topological sort breaks ties when two blocks share the
            same default rank.

  Stage 3 — Grid assignment. Each block lands at a `(col, row)` in the
            parent grid using its `flow_role`-preferred lane (sources
            left, processing centre, sinks right; side-channels at the
            periphery). Collisions resolve by walking outward inside
            the same row band.

Output: `{block_name: (col, row)}` consumed by `hierarchical.emit_hierarchical`
to lay the parent's (sheet ...) entries in a flow-meaningful grid
instead of the alphabetical / canonical-static order.

Pure algorithm. No file I/O. Composable — caller passes block names +
cross-cluster edges, gets back a placement dict."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# Stage 1 — declarative flow-role table. `flow_rank` is the canonical
# row-band (0 = top). `lane_pref` is the preferred column band relative
# to a max-cols grid: -1 = left, 0 = center, +1 = right. Blocks not in
# this map fall through to `("processing", 5, 0)`. Universal — any new
# functional block name introduced by P12 can be wired in via JSON
# config without touching code.
_DEFAULT_FLOW_TABLE: Dict[str, Tuple[str, int, int]] = {
    # name              (flow_role,     flow_rank, lane_pref)
    "POWER_INPUT":      ("source",       0,        -1),
    "POWER_REGULATOR":  ("transform",    1,         0),
    "POWER":            ("source",       0,        -1),  # legacy alias
    "PROTECTION":       ("transform",    1,        -1),
    "USB":              ("ingress",      2,        -1),
    "UART_IFACE":       ("ingress",      2,        -1),
    "CAN_BUS":          ("ingress",      2,        -1),
    "ETHERNET":         ("ingress",      2,        -1),
    "MAIN_CONTROLLER":  ("processing",   4,         0),
    "WIRELESS":         ("processing",   4,         1),
    "MEMORY":           ("processing",   4,         1),
    "ANALOG":           ("bidirectional",5,         0),
    "I2C_BUS":          ("bidirectional",5,         0),
    "SPI_BUS":          ("bidirectional",5,         0),
    "SENSOR":           ("bidirectional",5,         1),
    "MOTOR":            ("sink",         6,         1),
    "DISPLAY":          ("sink",         6,         1),
    "LED_INDICATOR":    ("sink",         6,         1),
    "CRYSTAL_SECT":     ("side_channel", 3,        -1),
    "CRYSTAL":          ("side_channel", 3,        -1),
    "RESET_SECT":       ("side_channel", 3,        -1),
    "RESET":            ("side_channel", 3,        -1),
    "BOOT":             ("side_channel", 3,        -1),
    "DEBUG":            ("side_channel", 7,         1),
    "CONNECTOR":        ("ingress",      2,        -1),
    "GENERIC":          ("processing",   5,         0),
    "MISC":             ("processing",   8,         1),
}

# Direction priority: lower-rank → higher-rank edges win (source flows
# toward sink). Used to orient cross-cluster edges in the DAG.
_FLOW_ROLE_PRIORITY = {
    "source":         0,
    "ingress":        1,
    "transform":      2,
    "side_channel":   3,  # neutral — doesn't drive direction strongly
    "processing":     4,
    "bidirectional":  5,
    "sink":           6,
}


def _normalize_block_name(name: str) -> str:
    """Strip duplicate suffixes ("USB_2" → "USB") and uppercase. Lets the
    flow table match cluster names that the partitioner tagged for
    duplicate-detection."""
    if not name:
        return ""
    up = name.upper()
    # Drop trailing _<digit>+ (the duplicate-name marker from
    # `name_communities`).
    parts = up.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return up


def classify_block_flow(
    block_name: str,
    flow_table: Optional[Dict[str, Tuple[str, int, int]]] = None,
) -> Tuple[str, int, int]:
    """Stage 1 helper — return `(flow_role, flow_rank, lane_pref)` for
    a block. Falls back to processing/center for unknowns."""
    table = flow_table if flow_table is not None else _DEFAULT_FLOW_TABLE
    norm = _normalize_block_name(block_name)
    if norm in table:
        return table[norm]
    return ("processing", 5, 0)


def build_block_dag(
    block_names: Iterable[str],
    cross_edges: Iterable[Dict[str, Any]],
    flow_table: Optional[Dict[str, Tuple[str, int, int]]] = None,
) -> Dict[str, Any]:
    """Stage 2 — derive a directed inter-block graph.

    `cross_edges` is the `cross_cluster_edges` list from
    `community_partition.partition_schematic`: each entry has
    `a_community`/`b_community` (numeric ids) and `a`/`b` (ref names).
    We don't get community-id-to-name here directly, but the partition
    result already includes the mapping; callers pass an enriched edge
    list with `a_block`/`b_block` strings (the block names) instead.

    Edge direction: lower flow_role priority → higher. Same-role edges
    are recorded but contribute no orientation (their two blocks are
    "siblings" — placement falls back to lane_pref).

    Returns:
      {
        "rank":     {block: int},     # toposorted rank (0 = source-most)
        "successors": {block: [...]}, # outgoing directed edges
        "predecessors": {block: [...]},
        "siblings": {block: [...]},   # same-rank ties
      }"""
    names = list(block_names)
    role_rank: Dict[str, Tuple[str, int, int]] = {
        b: classify_block_flow(b, flow_table) for b in names
    }

    successors: Dict[str, Set[str]] = defaultdict(set)
    predecessors: Dict[str, Set[str]] = defaultdict(set)
    siblings: Dict[str, Set[str]] = defaultdict(set)
    for e in cross_edges or ():
        a = e.get("a_block") or e.get("a") or ""
        b = e.get("b_block") or e.get("b") or ""
        if not a or not b or a == b or a not in role_rank or b not in role_rank:
            continue
        ra = _FLOW_ROLE_PRIORITY.get(role_rank[a][0], 4)
        rb = _FLOW_ROLE_PRIORITY.get(role_rank[b][0], 4)
        if ra < rb:
            successors[a].add(b)
            predecessors[b].add(a)
        elif rb < ra:
            successors[b].add(a)
            predecessors[a].add(b)
        else:
            siblings[a].add(b)
            siblings[b].add(a)

    # Topological rank: combine the static flow_rank with the DAG
    # depth-from-source. flow_rank dominates; DAG depth breaks ties.
    rank: Dict[str, float] = {}
    for b in names:
        base = role_rank[b][1] * 100.0
        # BFS from this block's predecessor closure to count depth.
        depth = 0
        visited: Set[str] = set()
        frontier: Set[str] = set(predecessors.get(b, ()))
        while frontier:
            depth += 1
            new_frontier: Set[str] = set()
            for p in frontier:
                if p in visited:
                    continue
                visited.add(p)
                new_frontier.update(predecessors.get(p, ()))
            frontier = new_frontier - visited
            if depth > len(names):
                break  # cycle guard
        rank[b] = base + depth

    return {
        "rank":         {b: rank[b] for b in names},
        "successors":   {b: sorted(successors[b]) for b in names},
        "predecessors": {b: sorted(predecessors[b]) for b in names},
        "siblings":     {b: sorted(siblings[b]) for b in names},
        "role_rank":    {b: list(role_rank[b]) for b in names},
    }


def plan_sheet_grid(
    block_names: Iterable[str],
    cross_edges: Optional[Iterable[Dict[str, Any]]] = None,
    *,
    max_cols: int = 3,
    flow_table: Optional[Dict[str, Tuple[str, int, int]]] = None,
) -> Dict[str, Any]:
    """Stage 3 — assign each block a `(col, row)` cell in a flow-shaped
    grid.

    Algorithm:
      1. Compute `(flow_role, flow_rank, lane_pref)` per block.
      2. Group blocks by `flow_rank`. Each rank becomes one ROW BAND
         (multiple ranks may share a parent-grid row when the schematic
         has few blocks — see step 5).
      3. Within each rank, sort by `(lane_pref, dag_rank, name)`:
         left-lane sources before right-lane sinks; DAG depth breaks
         ties so an upstream sibling ends up west of its downstream
         sibling.
      4. Pack each rank into one row of width `max_cols`. When a rank
         has more blocks than `max_cols`, overflow rolls to the next
         row and bumps subsequent ranks down.
      5. Collapse empty ranks — if rank 3 has zero blocks but rank 4
         has one, rank 4 moves up to fill the visual row. Prevents huge
         vertical gaps on schematics that don't use every flow band.

    Returns:
      {
        "grid":       {block: (col, row)},
        "rows_used":  int,
        "cols_used":  int,
        "dag":        <build_block_dag output>,
      }"""
    names = list(block_names)
    if not names:
        return {"grid": {}, "rows_used": 0, "cols_used": 0, "dag": {}}

    dag = build_block_dag(names, cross_edges or (), flow_table)
    role_rank_map: Dict[str, Tuple[str, int, int]] = {
        b: tuple(dag["role_rank"][b]) for b in names
    }

    # Group blocks per flow_rank band.
    band_to_blocks: Dict[int, List[str]] = defaultdict(list)
    for b in names:
        band_to_blocks[role_rank_map[b][1]].append(b)

    # Sort blocks within each band by (lane_pref, DAG rank, name) so
    # left-lane sources come before right-lane sinks, and upstream
    # comes before downstream within the same lane.
    for band in band_to_blocks:
        band_to_blocks[band].sort(key=lambda b: (
            role_rank_map[b][2],      # lane_pref: -1, 0, 1
            dag["rank"].get(b, 0),    # DAG depth for tie-break
            _normalize_block_name(b), # stable alphabetic fallback
        ))

    # Compute a column for each block within its band. Lane preference
    # is mapped to a STARTING column index: -1 → 0, 0 → max_cols//2,
    # +1 → max_cols - 1. Blocks with the same lane pile up from that
    # start position, walking inward (left-lane fills 0,1,2; right-
    # lane fills max-1, max-2, ...).
    sorted_bands = sorted(band_to_blocks.keys())
    grid: Dict[str, Tuple[int, int]] = {}
    used_cells: Set[Tuple[int, int]] = set()
    row_cursor = 0
    for band in sorted_bands:
        blocks_in_band = band_to_blocks[band]
        # Bucket by lane.
        by_lane: Dict[int, List[str]] = {-1: [], 0: [], 1: []}
        for b in blocks_in_band:
            lane = role_rank_map[b][2]
            by_lane.setdefault(lane, []).append(b)
        # Assign cells: left lane fills cols 0..; centre fills around
        # mid; right lane fills cols max-1..0.
        left_col = 0
        right_col = max_cols - 1
        centre_col = max_cols // 2
        # Assign right lane first so it claims rightmost cells before
        # centre/left compete.
        right_blocks = list(by_lane.get(1, []))
        left_blocks = list(by_lane.get(-1, []))
        centre_blocks = list(by_lane.get(0, []))
        cur_row = row_cursor
        for b in right_blocks:
            col = right_col
            while (col, cur_row) in used_cells and col >= 0:
                col -= 1
            if col < 0:
                cur_row += 1
                col = right_col
            grid[b] = (col, cur_row)
            used_cells.add((col, cur_row))
            right_col = col - 1 if col > 0 else max_cols - 1
            if right_col < 0:
                right_col = max_cols - 1
        for b in left_blocks:
            col = left_col
            while (col, cur_row) in used_cells and col < max_cols:
                col += 1
            if col >= max_cols:
                cur_row += 1
                col = left_col
            grid[b] = (col, cur_row)
            used_cells.add((col, cur_row))
            left_col = col + 1 if col < max_cols - 1 else 0
            if left_col >= max_cols:
                left_col = 0
        for b in centre_blocks:
            col = centre_col
            while (col, cur_row) in used_cells:
                # Walk outward — try col+1, col-1, col+2, ...
                offset = 1
                placed = False
                while offset <= max_cols:
                    for c in (centre_col + offset, centre_col - offset):
                        if 0 <= c < max_cols and (c, cur_row) not in used_cells:
                            col = c
                            placed = True
                            break
                    if placed:
                        break
                    offset += 1
                if not placed:
                    cur_row += 1
                    col = centre_col
                    break
            grid[b] = (col, cur_row)
            used_cells.add((col, cur_row))
        row_cursor = cur_row + 1

    # Step 5 — compact rows: if no block uses row N but row N+1 does,
    # shift everything in N+1..end up by one. Repeat until stable.
    while True:
        rows_used = {r for _c, r in grid.values()}
        if not rows_used:
            break
        max_row = max(rows_used)
        compacted = False
        for r in range(max_row):
            if r in rows_used:
                continue
            # Shift every block at row >= r+1 up by 1.
            for b, (c, br) in list(grid.items()):
                if br >= r + 1:
                    grid[b] = (c, br - 1)
            compacted = True
            break
        if not compacted:
            break

    rows_used = max((r for _c, r in grid.values()), default=-1) + 1
    cols_used = max((c for c, _r in grid.values()), default=-1) + 1
    return {
        "grid":      grid,
        "rows_used": rows_used,
        "cols_used": cols_used,
        "dag":       dag,
    }
