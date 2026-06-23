"""Tool: auto-arrange footprints on a .kicad_pcb so they don't all
stack at (0,0) after `Update PCB from Schematic`.

Algorithm:
  1. Parse the PCB via sexpdata
  2. Find every (footprint ...) entry + its (at X Y [rot]) clause
  3. Group by refdes prefix (U / R / C / J / D / Q ...) so related
     parts cluster — ICs in one row, passives below, connectors at
     the edges
  4. Place each group on a grid; spacing + cell pitch + origin all
     come from layout_config.json:auto_place_pcb
  5. Write the .kicad_pcb back

Universal — works on ANY .kicad_pcb regardless of circuit. No
per-component hardcoding."""
from __future__ import annotations

import math
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


def _ref_of_footprint(fp: list) -> Optional[str]:
    """Return the Reference property of a (footprint ...) node."""
    for child in fp[1:]:
        if (isinstance(child, list) and _head(child) == "property"
                and len(child) >= 3 and str(child[1]) == "Reference"):
            return str(child[2])
        # Older format: (fp_text reference REF ...)
        if (isinstance(child, list) and _head(child) == "fp_text"
                and len(child) >= 3
                and isinstance(child[1], sexpdata.Symbol)
                and child[1].value() == "reference"):
            return str(child[2])
    return None


def _at_of_footprint(fp: list) -> Optional[list]:
    """Return the top-level (at X Y [R]) clause for the footprint
    position. Different from any (at ...) inside child fp_text/pad
    nodes — those are relative to the footprint origin."""
    for child in fp[1:]:
        if isinstance(child, list) and _head(child) == "at":
            return child
    return None


def _prefix_of_ref(ref: str) -> str:
    """Strip digits from the right to get the ref prefix.
    R1 -> R, U10 -> U, NetU1Pad7 -> NetU1Pad. KiCad refs always
    end in digits after the alphabetic prefix."""
    i = len(ref) - 1
    while i >= 0 and ref[i].isdigit():
        i -= 1
    return ref[: i + 1] if i >= 0 else ref


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("auto_place_pcb", {}) or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# KiCad-style serializer (lifted from apply_ops — same s-expr conventions)
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
        # Order matters: backslashes first (so the escapes we add next
        # don't get double-escaped), then quotes, then control chars.
        # Without the newline/tab/CR escapes, a multi-line gr_text would
        # write a raw newline INSIDE the string literal — kicad-cli's
        # parser then rejects the file with "Failed to load board".
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
        # 10 decimals covers every KiCad-emitted value we've seen:
        # arc midpoints use 6 decimals, but `roundrect_rratio` and a
        # handful of pad ratios use up to 10. Truncating any of those
        # produces "Failed to load board" from kicad-cli. Trailing
        # zeros stripped so normal coordinates still look clean.
        return f"{node:.10f}".rstrip("0").rstrip(".")
    return str(node)


def _emit(node: Any, indent: int = 0) -> str:
    """Multi-line emit. Keeps leading atom children on the head line so
    `(symbol "Lib:Part" ...)` doesn't split the name onto its own line —
    kicad-cli rejects that form."""
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
# Phase 23 — block-aware placement (uses the IR-block sidecar)
# ---------------------------------------------------------------------------

def _load_blocks_sidecar(pcb_path: Path) -> Optional[Dict[str, Any]]:
    """Read `<basename>.envil-blocks.json` next to the .kicad_pcb.

    Returns the parsed sidecar dict, or None if the file is missing /
    unreadable / has the wrong schema. Caller falls back to legacy
    prefix-grouped placement in those cases."""
    side = pcb_path.with_suffix(".envil-blocks.json")
    if not side.exists():
        # Also try next to the .kicad_sch sibling
        sib = pcb_path.with_suffix(".envil-blocks.json")
        if not sib.exists():
            return None
        side = sib
    try:
        import json as _json
        data = _json.loads(side.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("version", 0)) < 1:
        return None
    if not isinstance(data.get("blocks"), list) or not data["blocks"]:
        return None
    return data


def _load_role_zone_maps() -> Tuple[Dict[str, str], Dict[str, Tuple[float, float]]]:
    """Pull the role→zone and zone→sheet-fraction maps out of
    layout_config.json. Returns ({role: zone_name}, {zone_name: (fx, fy)}).
    Falls back to empty dicts if config is missing."""
    try:
        from ..intent.engine import _load_layout_config
        cfg = _load_layout_config() or {}
    except Exception:
        return {}, {}
    # role_zone_map (fallback) + role_zone_maps[circuit_type] (preferred)
    role_map = dict(cfg.get("role_zone_map", {}) or {})
    # zones is {name: [fx, fy]} fractional positions on sheet
    zones_raw = cfg.get("zones", {}) or {}
    zones: Dict[str, Tuple[float, float]] = {}
    for name, val in zones_raw.items():
        if isinstance(val, (list, tuple)) and len(val) >= 2:
            try:
                zones[name] = (float(val[0]), float(val[1]))
            except (TypeError, ValueError):
                pass
    return role_map, zones


def _role_for_block(b: Dict[str, Any]) -> str:
    """Pick the role label used to look up a zone. Architect's
    block.name (POWER / MCU / USB) takes priority because it's the most
    specific; falls back to block_type (power / mcu / io) uppercased."""
    name = (b.get("name") or "").strip().upper()
    if name:
        return name
    bt = (b.get("block_type") or "").strip().upper()
    return bt or "GENERIC"


def _zone_for_role(role: str, role_map: Dict[str, str]) -> str:
    """Map role -> zone name. Direct hit first, then a few common
    aliases that don't appear in the existing schematic-side
    role_zone_map (POWER_REGULATOR == POWER for placement, IO == CONNECTOR)."""
    if role in role_map:
        return role_map[role]
    aliases = {
        "POWER_REGULATOR": "POWER",
        "REGULATOR":       "POWER",
        "PROTECTION":      "POWER",
        "IO":              "CONNECTOR",
        "COMM":            "CONNECTOR",
        "SENSE":           "SENSOR",
        "DEBUG":           "DEBUG",
        "MCU":             "MAIN_CONTROLLER",
    }
    for src, dst in aliases.items():
        if role == src and dst in role_map:
            return role_map[dst]
    # Last resort: dump in the generic zone
    return role_map.get("GENERIC", "center")


def _compute_block_positions(blocks: List[Dict[str, Any]],
                              ref_to_prefix: Dict[str, str],
                              cfg: Dict[str, Any],
                              role_map: Dict[str, str],
                              zones: Dict[str, Tuple[float, float]]
                              ) -> Dict[str, Tuple[float, float]]:
    """Lay every ref in `blocks` on the board, using one rectangular
    sub-grid per block whose top-left corner is the zone fraction (from
    layout_config.json:zones) scaled into a board area.

    Board area defaults: 100 mm wide x 80 mm tall, anchored at
    `origin_x/y_mm`. Tunable in layout_config.json:auto_place_pcb under
    `board_w_mm` / `board_h_mm`.

    Within a block, ICs come first (U prefix), then connectors, then
    crystals, then transistors / diodes, then passives — mirroring the
    schematic-side reading order so an IC sits at the block's top-left
    and its decoupling caps cluster around it.
    """
    origin_x = float(cfg.get("origin_x_mm", 50.0))
    origin_y = float(cfg.get("origin_y_mm", 50.0))
    board_w  = float(cfg.get("board_w_mm", 100.0))
    board_h  = float(cfg.get("board_h_mm", 80.0))
    cell_x   = float(cfg.get("cell_pitch_x_mm", 12.0))
    cell_y   = float(cfg.get("cell_pitch_y_mm", 8.0))

    # Sort refs inside a block by prefix priority then numeric suffix
    prefix_order = cfg.get("group_order",
                            ["U", "Q", "D", "J", "Y", "R", "C", "L", "FB", "TP", "SW"])

    def _ref_rank(ref: str) -> Tuple[int, int]:
        pfx = ref_to_prefix.get(ref, "")
        if pfx in prefix_order:
            r = prefix_order.index(pfx)
        else:
            r = len(prefix_order) + (ord(pfx[0]) if pfx else 90)
        n = int("".join(c for c in ref if c.isdigit()) or "0")
        return (r, n)

    positions: Dict[str, Tuple[float, float]] = {}
    for b in blocks:
        refs = list(b.get("component_refs") or [])
        if not refs:
            continue
        refs.sort(key=_ref_rank)
        role = _role_for_block(b)
        zone_name = _zone_for_role(role, role_map)
        fx, fy = zones.get(zone_name, (0.5, 0.5))
        # Anchor for this block: zone-fraction * board area + origin.
        # Reserve a small margin so blocks near the right/bottom don't
        # spill off the board.
        margin = float(cfg.get("zone_margin_mm", 3.0))
        anchor_x = origin_x + fx * (board_w - margin)
        anchor_y = origin_y + fy * (board_h - margin)
        # Sub-grid: ceil(sqrt(N)) cols
        n = len(refs)
        cols = max(1, math.ceil(math.sqrt(n)))
        for i, ref in enumerate(refs):
            col = i % cols
            row = i // cols
            x = anchor_x + col * cell_x
            y = anchor_y + row * cell_y
            # Round to 0.1 mm to match KiCad's display precision
            positions[ref] = (round(x / 0.1) * 0.1, round(y / 0.1) * 0.1)

    return positions


# ---------------------------------------------------------------------------
# Legacy placement core (refdes-prefix grouping)
# ---------------------------------------------------------------------------

def _compute_grid_positions(refs_by_group: Dict[str, List[str]],
                             cfg: Dict[str, Any]
                             ) -> Dict[str, Tuple[float, float]]:
    """For each (group, refs) pair, lay refs out in a sub-grid and place
    sub-grids in a row across the board. Returns {ref: (x, y)}.

    Sub-grid layout: ceil(sqrt(N)) cols, ceil(N / cols) rows."""
    origin_x = float(cfg.get("origin_x_mm", 50.0))
    origin_y = float(cfg.get("origin_y_mm", 50.0))
    cell_x   = float(cfg.get("cell_pitch_x_mm", 12.0))
    cell_y   = float(cfg.get("cell_pitch_y_mm", 8.0))
    group_gap_x = float(cfg.get("group_gap_x_mm", 8.0))
    group_order = cfg.get("group_order", ["U", "Q", "D", "J", "R", "C", "L"])

    # Sort groups by configured order; unknown prefixes go to the end.
    def grp_key(g: str) -> int:
        return group_order.index(g) if g in group_order else len(group_order) + ord(g[0] if g else "Z")
    ordered = sorted(refs_by_group.keys(), key=grp_key)

    positions: Dict[str, Tuple[float, float]] = {}
    cursor_x = origin_x
    for grp in ordered:
        refs = sorted(refs_by_group[grp],
                       key=lambda r: int("".join(c for c in r if c.isdigit()) or "0"))
        n = len(refs)
        if n == 0:
            continue
        cols = max(1, math.ceil(math.sqrt(n)))
        for i, ref in enumerate(refs):
            col = i % cols
            row = i // cols
            x = cursor_x + col * cell_x
            y = origin_y + row * cell_y
            positions[ref] = (round(x / 0.1) * 0.1, round(y / 0.1) * 0.1)
        # Advance cursor past this group's column span + gap
        cursor_x += cols * cell_x + group_gap_x

    return positions


# ---------------------------------------------------------------------------
# P1.2 — IC-anchor placement (no-sidecar / single-IC case)
# ---------------------------------------------------------------------------

def _pads_of_footprint(fp: list) -> List[list]:
    return [c for c in fp[1:] if isinstance(c, list) and _head(c) == "pad"]


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


def _pad_local_at(pad: list) -> Tuple[float, float]:
    """Pad position relative to the footprint origin (the inner `(at ...)`
    on the pad itself, not the footprint's outer `(at ...)`)."""
    for child in pad[1:]:
        if isinstance(child, list) and _head(child) == "at":
            try:
                x = float(child[1])
                y = float(child[2]) if len(child) >= 3 else 0.0
                return (x, y)
            except (TypeError, ValueError):
                return (0.0, 0.0)
    return (0.0, 0.0)


def _pad_number(pad: list) -> str:
    if len(pad) >= 2:
        return str(pad[1])
    return ""


def _anchor_pin_axes(fp: list) -> Dict[str, Tuple[float, float]]:
    """For every pad of the anchor, return a unit outward vector based on
    the pad's position relative to the footprint centre. Used to fan
    satellites outward from the IC along the pin they connect to."""
    pads = _pads_of_footprint(fp)
    if not pads:
        return {}
    axes: Dict[str, Tuple[float, float]] = {}
    for p in pads:
        num = _pad_number(p)
        if not num:
            continue
        lx, ly = _pad_local_at(p)
        mag = math.hypot(lx, ly)
        if mag < 0.01:
            axes[num] = (1.0, 0.0)
        else:
            axes[num] = (lx / mag, ly / mag)
    return axes


def _canonical_power_rails() -> set:
    try:
        from ..intent.engine import _load_layout_config
        rails = _load_layout_config().get("validate", {}).get(
            "canonical_power_rails", [])
        return {r.upper() for r in rails}
    except Exception:
        return {"+3V3", "+3.3V", "+5V", "+12V", "+9V", "+15V", "+24V",
                "GND", "AGND", "DGND", "PGND", "VBUS", "VBAT",
                "VCC", "VDD", "VEE", "VSS"}


def _classify_connector_edge(fp: list, net_names: List[str],
                              value_str: str,
                              edge_cfg: Dict[str, Any]
                              ) -> Optional[str]:
    """Same logic as the schematic-side connector_edge_pass: walk the
    connector's net names + Value field, return 'left' / 'right' /
    'bottom' from regex match priority. None when nothing matches."""
    import re as _re
    try:
        left_pats   = [_re.compile(p, _re.IGNORECASE)
                       for p in edge_cfg.get("left_edge_net_patterns", [])]
        right_pats  = [_re.compile(p, _re.IGNORECASE)
                       for p in edge_cfg.get("right_edge_net_patterns", [])]
        bottom_pats = [_re.compile(p, _re.IGNORECASE)
                       for p in edge_cfg.get("bottom_right_net_patterns", [])]
    except _re.error:
        return None
    candidates = [value_str] + list(net_names) if value_str else list(net_names)
    for nm in candidates:
        if any(p.search(nm) for p in left_pats):
            return "left"
    for nm in candidates:
        if any(p.search(nm) for p in right_pats):
            return "right"
    for nm in candidates:
        if any(p.search(nm) for p in bottom_pats):
            return "bottom"
    return None


def _value_of_footprint(fp: list) -> str:
    for child in fp[1:]:
        if (isinstance(child, list) and _head(child) == "property"
                and len(child) >= 3 and str(child[1]) == "Value"):
            return str(child[2])
    return ""


def _compute_ic_anchor_positions(ref_to_fp: Dict[str, list],
                                   ref_to_prefix: Dict[str, str],
                                   cfg: Dict[str, Any]
                                   ) -> Optional[Dict[str, Tuple[float, float]]]:
    """Single-IC placement: anchor = footprint with the most pads, others
    fan out along the anchor pin they share a net with. Decouplers (caps
    on a power net) land close to the matching power pad; signal-net
    satellites land further out along the pin's outward axis. Connectors
    snap to sheet edges by net-name regex (mirrors the schematic-side
    edge-snap so the PCB layout matches the .kicad_sch).

    Returns None when no clear anchor exists (caller falls back to the
    legacy refdes-grouped grid). Trigger condition: largest pad count >=
    `auto_place_pcb.ic_anchor.min_anchor_pads` (default 3) AND there is
    only ONE candidate at that pad count (ties = ambiguous → fall back).
    """
    ic_cfg = cfg.get("ic_anchor", {}) or {}
    if not ic_cfg.get("enabled", True):
        return None
    min_pads = int(ic_cfg.get("min_anchor_pads", 3))
    max_components = int(ic_cfg.get("max_components", 15))
    if len(ref_to_fp) > max_components:
        return None  # too big for single-IC layout; defer to sidecar/legacy
    pad_counts = {r: len(_pads_of_footprint(fp)) for r, fp in ref_to_fp.items()}
    if not pad_counts:
        return None
    max_count = max(pad_counts.values())
    if max_count < min_pads:
        return None
    candidates = [r for r, n in pad_counts.items() if n == max_count]
    if len(candidates) != 1:
        return None  # ambiguous — multiple "ICs", fall back to legacy
    anchor_ref = candidates[0]
    anchor_fp = ref_to_fp[anchor_ref]
    anchor_pads = _pads_of_footprint(anchor_fp)
    anchor_pad_nets: Dict[str, str] = {}
    for p in anchor_pads:
        num = _pad_number(p)
        _, name = _pad_net(p)
        if num and name:
            anchor_pad_nets[num] = name
    pin_axes = _anchor_pin_axes(anchor_fp)

    origin_x = float(cfg.get("origin_x_mm", 50.0))
    origin_y = float(cfg.get("origin_y_mm", 50.0))
    board_w  = float(cfg.get("board_w_mm", 100.0))
    board_h  = float(cfg.get("board_h_mm", 80.0))
    center_x = origin_x + board_w / 2.0
    center_y = origin_y + board_h / 2.0
    decoup_radius   = float(ic_cfg.get("decoupling_radius_mm", 4.0))
    cluster_radius  = float(ic_cfg.get("cluster_radius_mm", 8.0))
    edge_inset      = float(ic_cfg.get("edge_inset_mm", 6.0))
    stack_step      = float(ic_cfg.get("stack_step_mm", 4.0))

    power_rails = _canonical_power_rails()
    positions: Dict[str, Tuple[float, float]] = {anchor_ref: (center_x, center_y)}
    occupancy: Dict[str, int] = {}  # anchor_pad -> N satellites placed there

    # Connector edge-snap config from the SAME source as schematic side
    try:
        from ..intent.engine import _load_layout_config
        edge_cfg = _load_layout_config().get("connector_edge_placement", {}) or {}
    except Exception:
        edge_cfg = {}
    conn_ref_prefixes = list(edge_cfg.get("connector_refdes_prefixes", ["J", "P"]))

    for ref, fp in ref_to_fp.items():
        if ref == anchor_ref:
            continue
        pads = _pads_of_footprint(fp)
        if not pads:
            continue
        # Net names this footprint touches
        net_names = []
        for p in pads:
            _, n = _pad_net(p)
            if n:
                net_names.append(n)

        # Connectors: edge-snap by regex against net names + Value
        if (any(ref.startswith(p) for p in conn_ref_prefixes)
                and edge_cfg.get("enabled")):
            value = _value_of_footprint(fp)
            edge = _classify_connector_edge(fp, net_names, value, edge_cfg)
            if edge == "left":
                positions[ref] = (round((origin_x + edge_inset) / 0.1) * 0.1,
                                  round(center_y / 0.1) * 0.1)
                continue
            if edge == "right":
                positions[ref] = (round((origin_x + board_w - edge_inset) / 0.1) * 0.1,
                                  round(center_y / 0.1) * 0.1)
                continue
            if edge == "bottom":
                positions[ref] = (round(center_x / 0.1) * 0.1,
                                  round((origin_y + board_h - edge_inset) / 0.1) * 0.1)
                continue
            # No edge match → fall through to anchor-axis placement

        # Anchor-axis placement: find the anchor pad whose net this
        # footprint shares. A 2-pin part whose pads ALL land on power
        # rails is treated as a decoupler (close placement) — pure
        # topology, no refdes-prefix hardcoding (per
        # [feedback_no_hardcode_json_config]: must work for any
        # circuit, including caps named "BYP1" or resistors named
        # "RFB3" that act as bypass elements).
        is_two_pin = (len(pads) == 2)
        all_pad_nets_powered = (
            is_two_pin
            and all((n or "").upper() in power_rails for n in net_names))
        shared_pad: Optional[str] = None
        shared_net: Optional[str] = None
        if all_pad_nets_powered:
            for ap, an in anchor_pad_nets.items():
                if an.upper() in power_rails and an in net_names:
                    shared_pad, shared_net = ap, an
                    break
        if shared_pad is None:
            for ap, an in anchor_pad_nets.items():
                if an in net_names:
                    shared_pad, shared_net = ap, an
                    break
        if shared_pad is None or shared_pad not in pin_axes:
            continue  # orphan — caller's legacy grid handles it
        dx, dy = pin_axes[shared_pad]
        base_r = decoup_radius if all_pad_nets_powered else cluster_radius
        slot = occupancy.get(shared_pad, 0)
        r = base_r + slot * stack_step
        x = center_x + dx * r
        y = center_y + dy * r
        positions[ref] = (round(x / 0.1) * 0.1, round(y / 0.1) * 0.1)
        occupancy[shared_pad] = slot + 1

    return positions


@tool(
    name="auto_place_pcb",
    description=(
        "Auto-arrange footprints on a .kicad_pcb so they fan out in a "
        "readable grid instead of stacking at the origin.\n"
        "Preferred: BLOCK-AWARE placement. When the project has an "
        "`.envil-blocks.json` sidecar (written by build_circuit), the "
        "tool places each block in its own board region per the "
        "role-to-zone map in layout_config.json — POWER top-left, MCU "
        "center, CONNECTOR right edge, etc. — and clusters the block's "
        "components inside that region.\n"
        "Fallback: when no sidecar is found (older projects, hand-built "
        "PCBs, or `use_blocks=false`), the tool groups by refdes prefix "
        "(U/Q/D/J/R/C/L) and lays out a single grid. Universal — works "
        "on any board.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}      # required\n'
        '  {"pcb_path": "...", "use_blocks": false}   # force legacy mode\n'
        '  {"pcb_path": "...", "origin_x_mm": 80}     # custom start X\n'
        '  {"pcb_path": "...", "cell_pitch_x_mm": 15} # tighter/looser grid\n'
        "All defaults (origin, cell pitch, group ordering, board area) "
        "come from layout_config.json:auto_place_pcb. Run AFTER Update "
        "PCB from Schematic (F8) — placing on an empty board is a no-op. "
        "Result includes `mode`: 'blocks' (sidecar honoured) or 'prefix' "
        "(fallback used)."
    ),
    input_schema={"pcb_path": str},
)
async def auto_place_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: .kicad_pcb not found: {pcb_path}"}],
            "is_error": True,
        }
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: expected .kicad_pcb, got {pcb_path.suffix}"}],
            "is_error": True,
        }

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {
            "content": [{"type": "text",
                          "text": "auto_place_pcb disabled in layout_config.json"}],
            "is_error": True,
        }
    # Per-call overrides win over config defaults
    for k in ("origin_x_mm", "origin_y_mm", "cell_pitch_x_mm",
               "cell_pitch_y_mm", "group_gap_x_mm"):
        if k in args:
            cfg = {**cfg, k: args[k]}

    # refine_only: skip the grid/anchor/zone placement entirely and ONLY run
    # the de-collision + wirelength refine on the existing footprint positions.
    # Used by drc_autofix to clear `courtyard_overlap` without re-arranging a
    # board the user already hand-placed.
    refine_only = bool(args.get("refine_only", False))

    try:
        text = pcb_path.read_text(encoding="utf-8")
        root = sexpdata.loads(text)
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: parse failed: {type(exc).__name__}: {exc}"}],
                 "is_error": True}
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        return {"content": [{"type": "text",
                              "text": f"ERROR: not a kicad_pcb file"}],
                 "is_error": True}

    # Collect footprints
    refs_by_group: Dict[str, List[str]] = {}
    ref_to_fp: Dict[str, list] = {}
    ref_to_prefix: Dict[str, str] = {}
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "footprint"):
            continue
        ref = _ref_of_footprint(child)
        if not ref:
            continue
        ref_to_fp[ref] = child
        prefix = _prefix_of_ref(ref)
        ref_to_prefix[ref] = prefix
        refs_by_group.setdefault(prefix, []).append(ref)

    if not ref_to_fp:
        return {"content": [{"type": "text",
                              "text": ("PCB has no footprints. Run "
                                        "Update PCB from Schematic (F8) "
                                        "in eeschema first, then re-run "
                                        "this tool.")}],
                 "is_error": True}

    # Phase 23 — try block-aware placement first. The sidecar
    # `<basename>.envil-blocks.json` (written by build_circuit) carries
    # the architect's functional blocks; when present we honour them.
    # The opt-out `use_blocks=false` falls back to the legacy
    # prefix-grouped grid. Refs not covered by any block (e.g. a part
    # the user added by hand after the build) are placed afterwards
    # using the legacy grid in any free space.
    sidecar = (None if args.get("use_blocks") is False
                else _load_blocks_sidecar(pcb_path))
    placement_mode = "prefix"
    positions: Dict[str, Tuple[float, float]] = {}
    block_summary = ""
    if sidecar:
        role_map, zones = _load_role_zone_maps()
        # Filter blocks to refs that actually exist on the PCB — sidecar
        # might reference parts the user hasn't pushed yet.
        active_blocks: List[Dict[str, Any]] = []
        covered: set = set()
        for b in sidecar.get("blocks", []):
            refs = [r for r in (b.get("component_refs") or [])
                     if r in ref_to_fp]
            if not refs:
                continue
            active_blocks.append({**b, "component_refs": refs})
            covered.update(refs)
        if active_blocks:
            positions = _compute_block_positions(
                active_blocks, ref_to_prefix, cfg, role_map, zones)
            placement_mode = "blocks"
            block_summary = ", ".join(
                f"{_role_for_block(b)}={len(b['component_refs'])}"
                for b in active_blocks)
            # Place leftover refs (not in any block) using the legacy
            # grid in a strip below the block area so nothing is lost.
            leftover = sorted(set(ref_to_fp) - covered)
            if leftover:
                leftover_groups: Dict[str, List[str]] = {}
                for r in leftover:
                    leftover_groups.setdefault(ref_to_prefix[r], []).append(r)
                leftover_cfg = {
                    **cfg,
                    "origin_x_mm": float(cfg.get("origin_x_mm", 50.0)),
                    "origin_y_mm": (float(cfg.get("origin_y_mm", 50.0))
                                     + float(cfg.get("board_h_mm", 80.0))
                                     + 10.0),
                }
                leftover_pos = _compute_grid_positions(
                    leftover_groups, leftover_cfg)
                positions.update(leftover_pos)

    if placement_mode == "prefix":
        # P1.2: try IC-anchor placement first (single-IC / small flat
        # circuits get the canonical "IC at centre, decouplers adjacent,
        # connectors at edges" layout, mirroring the schematic side). The
        # fn returns None when the board doesn't look like a single-IC
        # design (no anchor with >= min_anchor_pads, multiple equal
        # candidates, or too many components for the IC-anchor heuristic)
        # — caller drops to the legacy prefix-grouped grid.
        ic_positions = _compute_ic_anchor_positions(
            ref_to_fp, ref_to_prefix, cfg)
        if ic_positions:
            positions = ic_positions
            placement_mode = "ic_anchor"
            # Fill in any refs the IC-anchor pass didn't place (orphans
            # with no shared net to the anchor) using the legacy grid.
            unplaced = set(ref_to_fp) - set(positions)
            if unplaced:
                groups: Dict[str, List[str]] = {}
                for r in unplaced:
                    groups.setdefault(ref_to_prefix[r], []).append(r)
                leftover_cfg = {
                    **cfg,
                    "origin_y_mm": (float(cfg.get("origin_y_mm", 50.0))
                                     + float(cfg.get("board_h_mm", 80.0))
                                     + 10.0),
                }
                positions.update(_compute_grid_positions(groups, leftover_cfg))
        else:
            # Legacy path — no sidecar AND no clear IC anchor.
            positions = _compute_grid_positions(refs_by_group, cfg)

    # refine_only short-circuit: discard any computed grid positions so the
    # apply loop is a no-op; the refine post-pass below does all the work.
    if refine_only:
        positions = {}
        placement_mode = "refine_only"

    # Apply positions — mutate the (at X Y [R]) clause of each footprint
    moved = 0
    for ref, (x, y) in positions.items():
        fp = ref_to_fp.get(ref)
        if fp is None:
            continue
        at = _at_of_footprint(fp)
        if at is None:
            continue
        # Preserve rotation if present
        if len(at) >= 4:
            at[1] = x
            at[2] = y
            # rot stays in at[3]
        else:
            at[1] = x
            at[2] = y
            # Pad with rotation 0 so KiCad doesn't complain
            if len(at) == 3:
                at.append(0.0)
        moved += 1

    # P24 — placement refinement post-pass. The three modes above place on
    # grids / radii without checking whether footprints physically overlap or
    # whether the wiring is short. `layout.place_refine` (a) pushes overlapping
    # courtyards apart and (b) does a few conservative wirelength hill-climb
    # sweeps. Gated by `auto_place_pcb.refine.enabled`; when off this block is
    # skipped and the written file is byte-identical to the pre-refine output.
    refine_report: Dict[str, Any] = {}
    refine_cfg = cfg.get("refine", {}) if isinstance(cfg.get("refine"), dict) else {}
    if refine_only or refine_cfg.get("enabled", True):
        try:
            from ..layout.place_refine import refine_placement
            refine_report = refine_placement(root, refine_cfg) or {}
        except Exception as exc:                            # noqa: BLE001 — never block a placement on refine
            refine_report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        pcb_path.write_text(_emit(root), encoding="utf-8")
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: write failed: {type(exc).__name__}: {exc}"}],
                 "is_error": True}

    if placement_mode == "blocks":
        groups_line = f"  blocks: {block_summary}"
    elif placement_mode == "ic_anchor":
        anchor_ref = max(((r, len(_pads_of_footprint(fp)))
                            for r, fp in ref_to_fp.items()),
                          key=lambda kv: kv[1])[0]
        groups_line = (f"  anchor: {anchor_ref} "
                       f"({len(_pads_of_footprint(ref_to_fp[anchor_ref]))} pads)")
    else:
        group_summary = ", ".join(
            f"{g}={len(refs_by_group[g])}" for g in sorted(refs_by_group.keys()))
        groups_line = f"  groups: {group_summary}"

    refine_line = ""
    if refine_report.get("refined"):
        refine_line = (f"\n  refine: overlaps {refine_report.get('overlaps_before', 0)}"
                       f"->{refine_report.get('overlaps_after', 0)}, "
                       f"moved {refine_report.get('moved', 0)}")
    elif refine_report.get("error"):
        refine_line = f"\n  refine: skipped ({refine_report['error']})"

    return {
        "content": [{"type": "text",
                      "text": (f"placed {moved} footprints on {pcb_path.name}\n"
                                f"  mode: {placement_mode}\n"
                                f"{groups_line}\n"
                                f"  origin: ({cfg.get('origin_x_mm', 50)}, "
                                f"{cfg.get('origin_y_mm', 50)}) mm\n"
                                f"  pitch: {cfg.get('cell_pitch_x_mm', 12)} × "
                                f"{cfg.get('cell_pitch_y_mm', 8)} mm"
                                f"{refine_line}")}],
        "ok": True,
        "path": str(pcb_path),
        "placed": moved,
        "mode": placement_mode,
        "refine": refine_report,
        "groups": {g: len(refs_by_group[g]) for g in refs_by_group},
        "blocks": [_role_for_block(b) for b in (sidecar or {}).get("blocks", [])
                    if any(r in ref_to_fp for r in (b.get("component_refs") or []))]
                  if placement_mode == "blocks" else [],
    }
