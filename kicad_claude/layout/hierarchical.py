"""Step 6 of the universal layout engine: hierarchical sheet splitter.

When a design has more than `min_components` parts, packing them all onto
one sheet produces an unreadable wall. This module partitions a placement
by `block.role`, emits one CHILD .kicad_sch per role-group, and writes a
PARENT sheet that contains (sheet ...) stubs referencing each child plus
(hierarchical_label ...) instances on every net that crosses a sheet
boundary.

Public surface:

  should_split(placement, cfg)        -> bool
  split_placement(placement, routed)  -> SplitResult
  emit_hierarchical(split, source, out_dir)
                                      -> {parent_path, child_paths, ...}

The splitter is LAYOUT-AGNOSTIC: it operates on placement.json + routed.json
artifacts only. Tying it to the emitter is a thin call from the caller
(usually pipeline.py in Step 10).

Cross-sheet detection: a net that has pin members in two or more role-
groups is a HIERARCHICAL net. The splitter promotes it to a hierarchical
label on BOTH sides (and a top-sheet bus implied by the parent's hierarchy
mechanism — KiCad handles same-named hierarchical labels as electrically
joined when the parent connects matching sheet pins).

Limitations of v1 (documented for honesty):
  - Parent sheet lays children in a uniform N-column grid; advanced
    placement (where each child sits in its semantic zone on the parent)
    is left for a future iteration.
  - Same-name net joining relies on (hierarchical_label) name matching;
    we do NOT auto-emit (sheet_pin) entries on the parent yet — that
    field needs every child's port list to be enumerated. Without it,
    eeschema will warn 'no matching sheet pin' but the schematic still
    loads and renders.

These limitations are real but non-blocking for the user's stated goal
(visual readability for large designs); Step 10's quality.py will surface
the warnings so they don't pass silently.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from ai_backend.kicad_claude.schematic_modifier import (
        SchematicDocument, _head, _to_str, _sym, _make_at,
        _gen_uuid_node, _make_property,
    )
except ImportError:  # repo-on-sys.path variant
    from kicad_claude.schematic_modifier import (  # type: ignore
        SchematicDocument, _head, _to_str, _sym, _make_at,
        _gen_uuid_node, _make_property,
    )

from . import load_config


SplitResult = Dict[str, Any]


def should_split(placement: Dict[str, Any], cfg: Dict[str, Any],
                  classified: Optional[Dict[str, Any]] = None,
                  routed: Optional[Dict[str, Any]] = None) -> bool:
    """Circuit-shape-driven hierarchical-split decision. Fires when ANY of:

      1. Multi-MCU — ≥ `min_main_controllers` (default 2) parts have role
         MAIN_CONTROLLER. Multiple MCUs is the canonical hierarchical
         case: each MCU gets its own sheet so cross-MCU traces become
         hierarchical labels.
      2. Bulk threshold — ≥ `min_components` (default 40) parts AND
         ≥ `min_roles` (default 3) distinct roles.
      3. Single-role overload — any one role bucket has ≥
         `single_role_overflow` (default 40) members. Catches the
         "big GENERIC trash bucket" case.

    Pure topology — no part numbers. Each rule is config-tunable; adding
    a new trigger is a single conditional + config key."""
    if not cfg.get("enabled", False):
        return False
    components = placement.get("components", []) or []
    if not components:
        return False

    min_mcu = int(cfg.get("min_main_controllers", 2))
    min_comp = int(cfg.get("min_components", 40))
    min_roles = int(cfg.get("min_roles", 3))
    single_role_max = int(cfg.get("single_role_overflow", 40))
    wireless_mcu_pins = int(cfg.get("wireless_mcu_min_pins", 12))

    # Rule 1 — multi-MCU. Counts MAIN_CONTROLLERs plus any WIRELESS /
    # MEMORY / DISPLAY part with high pin count: an ESP32-S3 module is
    # WIRELESS by lib_id family but functionally an MCU; same for big
    # memory or display controllers. Each high-pin "system anchor"
    # earns its own sheet in the hierarchical view.
    _MCU_ANCHOR_ROLES = {"WIRELESS", "MEMORY", "DISPLAY"}
    def _is_mcu_like(c):
        role = c.get("role", "")
        if role == "MAIN_CONTROLLER":
            return True
        if role in _MCU_ANCHOR_ROLES and int(c.get("pin_count", 0)) >= wireless_mcu_pins:
            return True
        return False
    mcus = sum(1 for c in components if _is_mcu_like(c))
    if mcus >= min_mcu:
        return True

    # Rule 2 — bulk size + diversity
    roles_set = {c.get("role") for c in components if c.get("role")}
    if len(components) >= min_comp and len(roles_set) >= min_roles:
        return True

    # Rule 3 — single role overflow
    from collections import Counter
    role_counts = Counter(c.get("role") for c in components if c.get("role"))
    if any(n >= single_role_max for n in role_counts.values()):
        return True

    # Rule 4 — UNIFIED COMPLEXITY SCORE. Combines component count, net
    # density, cross-block traffic, and label density into a single
    # scalar. Fires when complexity / max_score exceeds threshold.
    # This catches "tricky-but-small" designs that the bucket rules miss
    # (e.g. 20-component RF board with 8 distinct sub-systems).
    try:
        from . import complexity as _cx
        complexity_cfg = cfg.get("complexity") or {}
        threshold = float(complexity_cfg.get("split_threshold", 0.55))
        result = _cx.compute_complexity(
            placement=placement, classified=classified, routed=routed,
            cfg=complexity_cfg,
        )
        if result.get("max_score", 0) > 0:
            normalised = result["score"] / result["max_score"]
            if normalised >= threshold:
                return True
    except Exception:
        pass  # complexity scoring is additive — never blocks the older rules

    return False


def _role_to_zone_name(role: str) -> str:
    """Sanitize a role to a filesystem-safe sheet name."""
    out = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in role)
    return out or "MISC"


# Canonical reading order for sheet numbering — mirrors the signal-flow
# convention: power first, processing second, comms third, IO last.
# Roles not in this list fall to the end alphabetically. Universal: any
# new role added in classifier_config.json gets a deterministic position
# without code changes.
_SHEET_ORDER = [
    "POWER", "POWER_REGULATOR", "PROTECTION",
    "MAIN_CONTROLLER",
    "CRYSTAL", "RESET", "BOOT",
    "MEMORY", "WIRELESS",
    "ANALOG", "SENSOR",
    "USB", "UART", "SPI", "I2C", "CAN", "DEBUG",
    "DISPLAY", "MOTOR",
    "CONNECTOR",
    "GENERIC",
]


def _ordered_role(role: str) -> Tuple[int, str]:
    """Sort key: position in _SHEET_ORDER, then alphabetical tie-break."""
    base = role.split("_")[0]  # handle ROLE_1, ROLE_2 sub-splits
    for i, r in enumerate(_SHEET_ORDER):
        if role == r or role.startswith(r + "_"):
            return (i, role)
    return (len(_SHEET_ORDER), role)


def _project_stem(source_path) -> str:
    """Derive the project stem (filesystem-safe) from the source schematic
    path. `Documents/foo/foo.kicad_sch` → `foo`. Used for sheet titles
    in the `<Project>_<Block>` professional format."""
    from pathlib import Path as _P
    return _P(str(source_path)).stem


def split_placement(
    placement: Dict[str, Any], routed: Dict[str, Any],
    classified: Optional[Dict[str, Any]] = None,
    source_schematic: Optional[str] = None,
) -> SplitResult:
    """Partition placement + routed into per-cluster child specs and surface
    the set of nets crossing cluster boundaries.

    Two partitioning strategies, selected by config:
      - `community_partition.enabled = true`: Louvain modularity community
        detection on the connectivity-weighted graph (the architecturally
        correct path — clusters reflect electrical reality, not a brittle
        single classifier role bucket).
      - else (default for back-compat): the original role-bucket split.

    Returns:
      {
        "children": {cluster_name: {
          "placement": <child placement json>,
          "routed":    <child routed json>,
          "cross_sheet_nets": [{net, side, x_mm, y_mm, angle}, ...]
        }, ...},
        "cross_sheet_nets": {net_name: {cluster_a, cluster_b, ...}, ...}
      }

    Cross-sheet nets are converted to hierarchical labels — same name on
    BOTH child sheets, KiCad joins them via the parent's sheet-pin map."""
    cfg_root = load_config("layout_config")
    cp_cfg = cfg_root.get("community_partition") or {}
    use_community = bool(cp_cfg.get("enabled", False))

    if use_community and source_schematic:
        return _split_placement_by_community(
            placement, routed, source_schematic, classified, cp_cfg,
        )
    comps_by_role: Dict[str, List[Dict[str, Any]]] = {}
    role_by_ref: Dict[str, str] = {}
    for c in placement.get("components", []):
        role = c.get("role", "GENERIC") or "GENERIC"
        comps_by_role.setdefault(role, []).append(c)
        role_by_ref[c["ref"]] = role

    # Which roles does each net touch?
    nets_to_roles: Dict[str, Set[str]] = {}
    for lb in routed.get("net_labels", []):
        role = role_by_ref.get(lb.get("ref", ""))
        if role:
            nets_to_roles.setdefault(lb.get("text", ""), set()).add(role)
    # Power-port rails are by definition shared — they cross every sheet
    # they appear on, but KiCad handles power nets via power-port symbols,
    # not hierarchical labels. We DO NOT promote them.
    cross = {n: rs for n, rs in nets_to_roles.items() if len(rs) >= 2}

    children: Dict[str, Dict[str, Any]] = {}
    for role, comps in comps_by_role.items():
        refs = {c["ref"] for c in comps}
        # Keep only labels and ports whose ref is in this role.
        child_labels = [lb for lb in routed.get("net_labels", [])
                        if lb.get("ref") in refs]
        child_ports = [pp for pp in routed.get("power_ports", [])
                       if pp.get("ref") in refs]
        child_stubs = [w for w in routed.get("stub_wires", [])]  # geometric — pass through

        cross_sheet_nets = []
        for lb in child_labels:
            if lb.get("text") in cross:
                cross_sheet_nets.append({
                    "net": lb.get("text"),
                    "x_mm": lb.get("x_mm"),
                    "y_mm": lb.get("y_mm"),
                    "angle": lb.get("angle", 0),
                })

        child_placement = {
            "sheet": placement.get("sheet", {}),
            "main_controller": placement.get("main_controller") if any(
                c["ref"] == placement.get("main_controller") for c in comps
            ) else None,
            "blocks": [b for b in placement.get("blocks", [])
                       if b.get("role") == role],
            "components": comps,
        }
        child_routed = {
            "net_labels":  child_labels,
            "power_ports": child_ports,
            "stub_wires":  child_stubs,
            "junctions":   routed.get("junctions", []),
            "stats":       {"role": role,
                            "components": len(comps),
                            "cross_sheet_nets": len(cross_sheet_nets)},
        }
        children[role] = {
            "placement": child_placement,
            "routed":    child_routed,
            "cross_sheet_nets": cross_sheet_nets,
        }

    return {"children": children, "cross_sheet_nets": cross}


def _split_placement_by_community(
    placement: Dict[str, Any],
    routed: Dict[str, Any],
    source_schematic: str,
    classified: Optional[Dict[str, Any]],
    cp_cfg: Dict[str, Any],
) -> SplitResult:
    """Community-detection-driven partitioning. Buckets components by the
    connectivity-weighted cluster they belong to instead of by classifier
    role. Cluster names come from plurality vote of member roles.

    Why this works better than role-bucket: the role classifier sees one
    role per component (the cap belongs to POWER, the SDA-line resistor
    belongs to GENERIC, etc.) — splitting by that fragments related
    sub-circuits. The connectivity graph sees the sub-circuit as a single
    cluster regardless of per-part role, so the resulting sheets match
    "what an EE would draw" instead of "what the classifier guessed"."""
    from . import community_partition as _cp

    partition = _cp.partition_schematic(
        source_schematic, classified=classified, config_root={"community_partition": cp_cfg},
    )

    # Build ref -> cluster_name map from the partition.
    ref_to_cluster: Dict[str, str] = {}
    cluster_names: List[str] = []
    for c in partition["communities"]:
        cluster_names.append(c["name"])
        for ref in c["members"]:
            ref_to_cluster[ref] = c["name"]

    # Any component the partitioner didn't see (e.g. a singleton with no
    # neighbours, or a #PWR that was filtered out) — funnel to a MISC
    # bucket so the emitter doesn't drop it.
    misc_cluster = "MISC"
    for c in placement.get("components", []):
        ref = c.get("ref") or ""
        if ref and ref not in ref_to_cluster:
            ref_to_cluster[ref] = misc_cluster
    if misc_cluster in ref_to_cluster.values() and misc_cluster not in cluster_names:
        cluster_names.append(misc_cluster)

    comps_by_cluster: Dict[str, List[Dict[str, Any]]] = {n: [] for n in cluster_names}
    for c in placement.get("components", []):
        cluster = ref_to_cluster.get(c.get("ref", ""), misc_cluster)
        comps_by_cluster.setdefault(cluster, []).append(c)

    # Cross-sheet nets: labels that cross cluster boundaries. Filter out
    # rail-name labels — the parent shouldn't expose VCC/GND as sheet pins
    # (power-port symbols handle that natively).
    POWER_NAMES = {"GND", "VSS", "VCC", "VDD", "VEE", "VBUS", "VBAT", "VSYS",
                    "AGND", "DGND", "PGND", "SGND", "EGND", "AVCC", "AVDD"}
    def _is_power_label(name: str) -> bool:
        up = (name or "").upper()
        return up in POWER_NAMES or up.startswith(("+", "-"))

    nets_to_clusters: Dict[str, Set[str]] = {}
    for lb in routed.get("net_labels", []):
        ref = lb.get("ref", "")
        cluster = ref_to_cluster.get(ref)
        net = lb.get("text", "")
        if not cluster or not net or _is_power_label(net):
            continue
        nets_to_clusters.setdefault(net, set()).add(cluster)
    cross = {n: cs for n, cs in nets_to_clusters.items() if len(cs) >= 2}

    children: Dict[str, Dict[str, Any]] = {}
    for cluster, comps in comps_by_cluster.items():
        if not comps:
            continue
        refs = {c["ref"] for c in comps}
        # Tag cross-sheet labels with `hierarchical=True` so the emitter
        # writes them as (hierarchical_label ...) instead of (label ...).
        # KiCad pairs each hierarchical_label on the child sheet with a
        # matching (pin ...) on the parent's (sheet ...) block by NAME —
        # without the tag, ERC would warn "no matching sheet pin" and the
        # cross-sheet net would be electrically incomplete at the project
        # level.
        child_labels = []
        for lb in routed.get("net_labels", []):
            if lb.get("ref") not in refs:
                continue
            lb_copy = dict(lb)
            if lb_copy.get("text") in cross:
                lb_copy["hierarchical"] = True
                # `passive` is the safe default — KiCad accepts any-to-passive
                # joins. Inferring input/output from the pin's electrical_type
                # is a future improvement.
                lb_copy["hier_shape"] = "passive"
            child_labels.append(lb_copy)
        child_ports = [pp for pp in routed.get("power_ports", [])
                       if pp.get("ref") in refs]
        # Stub-wire filtering. Without this, EVERY child sheet gets ALL
        # stub_wires from the flat routed.json — producing identical
        # floating wire forests across every sheet (the visible "5 sheets
        # all showing the same bus" bug). A stub belongs to this cluster
        # iff at least one of its endpoints sits on a label position OR
        # a power-port position OR a pin position belonging to this
        # cluster's components.
        cluster_anchor_set = set()
        for lb in child_labels:
            cluster_anchor_set.add(
                (round(float(lb.get("x_mm", 0)), 2),
                 round(float(lb.get("y_mm", 0)), 2)))
            # Also the original pin coord (from before label-placer moved
            # the label out from the pin) — stubs run pin→label, so the
            # pin endpoint is the "from" side.
            if lb.get("pin_x_mm") is not None and lb.get("pin_y_mm") is not None:
                cluster_anchor_set.add(
                    (round(float(lb["pin_x_mm"]), 2),
                     round(float(lb["pin_y_mm"]), 2)))
        for pp in child_ports:
            cluster_anchor_set.add(
                (round(float(pp.get("x_mm", 0)), 2),
                 round(float(pp.get("y_mm", 0)), 2)))

        child_stubs = []
        for w in (routed.get("stub_wires") or []):
            try:
                p1 = (round(float(w["x1"]), 2), round(float(w["y1"]), 2))
                p2 = (round(float(w["x2"]), 2), round(float(w["y2"]), 2))
            except (KeyError, TypeError, ValueError):
                continue
            if p1 in cluster_anchor_set or p2 in cluster_anchor_set:
                child_stubs.append(w)

        cross_sheet_nets: List[Dict[str, Any]] = []
        for lb in child_labels:
            if lb.get("text") in cross:
                cross_sheet_nets.append({
                    "net": lb.get("text"),
                    "x_mm": lb.get("x_mm"),
                    "y_mm": lb.get("y_mm"),
                    "angle": lb.get("angle", 0),
                    "ref": lb.get("ref"),
                    "pin_number": lb.get("pin_number"),
                })

        child_placement = {
            "sheet": placement.get("sheet", {}),
            "main_controller": placement.get("main_controller") if any(
                c["ref"] == placement.get("main_controller") for c in comps
            ) else None,
            "blocks": [b for b in placement.get("blocks", [])
                       if any(m in refs for m in (b.get("members") or []))],
            "components": comps,
        }
        child_routed = {
            "net_labels":  child_labels,
            "power_ports": child_ports,
            "stub_wires":  child_stubs,
            "junctions":   routed.get("junctions", []),
            "stats":       {"cluster": cluster,
                            "components": len(comps),
                            "cross_sheet_nets": len(cross_sheet_nets)},
        }
        children[cluster] = {
            "placement": child_placement,
            "routed":    child_routed,
            "cross_sheet_nets": cross_sheet_nets,
        }

    # P13 — flow-direction grid planning. Convert the partition's
    # `cross_cluster_edges` (which carry numeric community ids) into
    # name-tagged edges, then derive a (col, row) cell per block so
    # source/ingress blocks land top-left, processing centre, sinks
    # bottom-right. `emit_hierarchical` consumes the grid to override
    # the default row-major sheet layout.
    block_grid: Dict[str, Tuple[int, int]] = {}
    flow_dag: Dict[str, Any] = {}
    try:
        from . import flow_direction as _flow
        cluster_id_to_name = {c["id"]: c["name"]
                                for c in partition["communities"]}
        enriched_edges: List[Dict[str, Any]] = []
        for e in partition.get("cross_cluster_edges", []):
            a_blk = cluster_id_to_name.get(e.get("a_community"))
            b_blk = cluster_id_to_name.get(e.get("b_community"))
            if not a_blk or not b_blk:
                continue
            enriched_edges.append({
                "a_block": a_blk,
                "b_block": b_blk,
                "weight":  e.get("weight", 0.0),
            })
        max_cols = int(((cp_cfg.get("flow_direction") or {})
                          .get("parent_grid_cols")
                          or load_config("layout_config")
                          .get("hierarchical_split", {})
                          .get("parent_grid_cols", 3)))
        plan = _flow.plan_sheet_grid(
            list(comps_by_cluster.keys()), enriched_edges,
            max_cols=max_cols,
        )
        block_grid = plan.get("grid", {})
        flow_dag = plan.get("dag", {})
    except Exception as _exc:  # pragma: no cover — flow is advisory
        block_grid = {}
        flow_dag = {"error": str(_exc)}

    return {
        "children": children,
        "cross_sheet_nets": cross,
        "block_grid": block_grid,
        "flow_dag": flow_dag,
        "partition_meta": {
            "algorithm": "community",
            "graph_stats": partition["graph_stats"],
            "cross_cluster_edge_count": len(partition["cross_cluster_edges"]),
        },
    }


def _build_sheet_node(name: str, file_basename: str,
                      x: float, y: float, w: float, h: float,
                      pins: Optional[List[Dict[str, Any]]] = None) -> list:
    """Construct a (sheet ...) sexpr entry for the parent.

    KiCad's `(sheet ...)` syntax expects `(at x y)` with EXACTLY two values
    — NO rotation. `_make_at` emits a third `(at x y rot)` value which
    eeschema parses fine for symbols but rejects for sheets with
    'Expecting ")" line N offset 17' (the rotation value's `.` is at
    that offset).

    Bypassing `_make_at` here to emit the 2-value form. Properties keep
    the 3-value form because property (at) does take rotation.

    Optional `pins` list: [{"name": str, "direction": "input"|"output"|
    "bidirectional"|"passive", "side": "left"|"right"|"top"|"bottom"}].
    Each becomes a (pin ...) sub-entry on the sheet's edge. Position is
    auto-distributed along the chosen side; KiCad joins these to matching
    hierarchical_label entries in the child .kicad_sch by name."""
    node: list = [
        _sym("sheet"),
        [_sym("at"), float(x), float(y)],   # 2-value form for sheets
        [_sym("size"), float(w), float(h)],
        [_sym("fields_autoplaced")],
        [_sym("stroke"), [_sym("width"), 0.1524],
         [_sym("type"), _sym("solid")]],
        [_sym("fill"), [_sym("color"), 0, 0, 0, 0.0]],
        _gen_uuid_node(),
        _make_property("Sheetname", name, float(x), float(y) - 0.71, hide=False),
        _make_property("Sheetfile", file_basename, float(x), float(y) + h + 0.71, hide=False),
    ]
    if pins:
        # Distribute pins along sides. Default: split between LEFT and RIGHT
        # (the canonical sheet-pin layout — inputs left, outputs right).
        # Side-explicit pins win; the rest auto-pack on the LEFT.
        by_side: Dict[str, List[Dict[str, Any]]] = {
            "left": [], "right": [], "top": [], "bottom": [],
        }
        for p in pins:
            side = p.get("side") or "left"
            if side not in by_side:
                side = "left"
            by_side[side].append(p)

        def _place_side(side: str, plist: List[Dict[str, Any]]):
            if not plist:
                return
            if side == "left":
                px = float(x)
                start_y = float(y) + 2.54
                spacing = max(2.54, (h - 5.08) / max(1, len(plist) - 1)) if len(plist) > 1 else 0
                angle = 180  # arrow points INTO the sheet from the left edge
                for i, p in enumerate(plist):
                    py = start_y + i * spacing
                    node.append(_make_sheet_pin(p["name"], p.get("direction", "passive"),
                                                  px, py, angle))
            elif side == "right":
                px = float(x) + w
                start_y = float(y) + 2.54
                spacing = max(2.54, (h - 5.08) / max(1, len(plist) - 1)) if len(plist) > 1 else 0
                angle = 0
                for i, p in enumerate(plist):
                    py = start_y + i * spacing
                    node.append(_make_sheet_pin(p["name"], p.get("direction", "passive"),
                                                  px, py, angle))
            elif side == "top":
                py = float(y)
                start_x = float(x) + 2.54
                spacing = max(2.54, (w - 5.08) / max(1, len(plist) - 1)) if len(plist) > 1 else 0
                angle = 90
                for i, p in enumerate(plist):
                    px = start_x + i * spacing
                    node.append(_make_sheet_pin(p["name"], p.get("direction", "passive"),
                                                  px, py, angle))
            else:  # bottom
                py = float(y) + h
                start_x = float(x) + 2.54
                spacing = max(2.54, (w - 5.08) / max(1, len(plist) - 1)) if len(plist) > 1 else 0
                angle = 270
                for i, p in enumerate(plist):
                    px = start_x + i * spacing
                    node.append(_make_sheet_pin(p["name"], p.get("direction", "passive"),
                                                  px, py, angle))

        for side, plist in by_side.items():
            _place_side(side, plist)
    return node


def _make_sheet_pin(name: str, direction: str,
                    x: float, y: float, angle: int) -> list:
    """Emit a single (pin ...) sub-node for a sheet block.

    KiCad sheet-pin shape: (pin "NAME" <electrical-type> (at x y angle)
                            (effects ...) (uuid ...))
    electrical-type ∈ {input, output, bidirectional, tri_state, passive}.
    `passive` is the safe default when intent isn't clear — KiCad accepts
    any-to-passive at the parent join. Angle defines the side: 180 = left
    edge, 0 = right, 90 = top, 270 = bottom (mirrors the wire-pin
    convention)."""
    elec = direction.lower() if direction else "passive"
    if elec not in {"input", "output", "bidirectional", "tri_state", "passive"}:
        elec = "passive"
    return [
        _sym("pin"), str(name), _sym(elec),
        [_sym("at"), float(x), float(y), int(angle) % 360],
        [_sym("effects"),
         [_sym("font"), [_sym("size"), 1.27, 1.27]],
         [_sym("justify"), _sym("right" if angle == 180 else "left")]],
        _gen_uuid_node(),
    ]


def emit_hierarchical(
    split: SplitResult,
    source_path,
    parent_out_path,
    child_out_dir,
) -> Dict[str, Any]:
    """Write parent + N child .kicad_sch files. Each child uses the standard
    emitter; the parent gets one (sheet ...) entry per child laid out in a
    uniform grid.

    Returns:
      {parent_path, child_paths: {role: path},
       stats: {children, cross_sheet_nets, ...}}
    """
    # Local import to avoid circular load (emitter imports from this module
    # via Step 10's pipeline, not directly).
    from . import emitter as _emit

    cfg = load_config("layout_config")
    h_cfg = cfg.get("hierarchical_split") or {}
    grid_cols = int(h_cfg.get("parent_grid_cols", 3))
    cell_w = float(h_cfg.get("parent_cell_w_mm", 80.0))
    cell_h = float(h_cfg.get("parent_cell_h_mm", 50.0))
    cell_gap = float(h_cfg.get("parent_cell_gap_mm", 12.0))
    origin_x = float(h_cfg.get("parent_origin_x_mm", 20.0))
    origin_y = float(h_cfg.get("parent_origin_y_mm", 20.0))

    parent_out_p = Path(parent_out_path)
    child_dir = Path(child_out_dir)
    child_dir.mkdir(parents=True, exist_ok=True)

    # Order children by canonical sheet-reading flow (power → processing
    # → IO → connectors), then assign 2-digit numeric prefixes for
    # filesystem ordering + visual navigation: 01_POWER, 02_MCU, ...
    project_stem = _project_stem(source_path)
    # P13 — when the split carries a `block_grid` (computed by
    # `flow_direction.plan_sheet_grid`), order sheets by the grid's
    # row-major reading order: top row left-to-right, then next row,
    # etc. This makes the filesystem 01_/02_/... prefixes follow the
    # SAME signal-flow order the parent's visual layout will use.
    # Falls back to the static `_SHEET_ORDER` table when no grid was
    # computed (legacy / role-bucket path).
    block_grid: Dict[str, Tuple[int, int]] = split.get("block_grid") or {}
    if block_grid:
        def _flow_key(role: str) -> Tuple[int, int, str]:
            cell = block_grid.get(role)
            if cell is None:
                # Unplanned blocks (e.g. MISC) get pushed to the end.
                return (999, 999, role)
            col, row = cell
            return (row, col, role)
        ordered_roles = sorted(split["children"].keys(), key=_flow_key)
    else:
        ordered_roles = sorted(split["children"].keys(), key=_ordered_role)
    role_to_label: Dict[str, str] = {}
    for idx, role in enumerate(ordered_roles, start=1):
        zone = _role_to_zone_name(role)
        # Sheet title format: "<Project>_<Block>" — e.g. "dual_MCU_STM_MAIN_CONTROLLER"
        role_to_label[role] = f"{idx:02d}_{zone}"

    child_paths: Dict[str, str] = {}
    for role in ordered_roles:
        spec = split["children"][role]
        zone = role_to_label[role]
        child_path = child_dir / f"{zone}.kicad_sch"
        _emit.emit(spec["placement"], spec["routed"], source_path, child_path)
        child_paths[role] = str(child_path)

    # Parent sheet: start from a clean copy of source for header parity
    # (version, generator, uuid, paper, lib_symbols) — then strip the
    # placement layer entirely and replace with (sheet ...) entries.
    shutil.copy(source_path, parent_out_p)
    doc = SchematicDocument(parent_out_p)
    _emit._strip_layout(doc, tuple(cfg["ignored_refdes_prefixes"]["prefixes"]))
    # Drop every symbol too — parent has only sheets + cross-sheet labels.
    doc.tree[:] = [c for c in doc.tree
                   if not (isinstance(c, list) and _head(c) == "symbol"
                           and _to_str(c[1][1] if len(c) > 1 and isinstance(c[1], list)
                                       and _head(c[1]) == "lib_id" and len(c[1]) > 1
                                       else "") != "")]

    # Per-child sheet-pin sets: each cross-cluster net becomes a hierarchical
    # label on every child that touches it, AND a matching (pin ...) on the
    # parent's (sheet ...) block for that child. Without these, KiCad warns
    # "no matching sheet pin" and the cross-sheet net is electrically broken
    # at the project level (per-sheet ERC passes but project ERC fails).
    cross = split.get("cross_sheet_nets") or {}
    # P14.5 / P14.6 — bus-aware pin ordering. Detect buses from the
    # cross-sheet net names; sheet pins for the SAME bus emit
    # CONSECUTIVELY (so I2C SDA/SCL land adjacent, DATA0..7 land in
    # numeric order). Within a bus, sort by role/index; non-bus pins
    # fall to the end alphabetically. Pure ordering tweak — every net
    # still gets its own pin (KiCad's bus-sheet-pin display needs
    # matching bus wires inside the child, deferred to P14.4).
    try:
        from . import bus_semantics as _bus
        synth_nets = [{"name": n, "members": []} for n in cross.keys()]
        detected_buses = _bus.detect_buses(synth_nets)
        net_to_bus = _bus.buses_by_net(detected_buses)
    except Exception:
        detected_buses = []
        net_to_bus = {}

    def _pin_sort_key(name: str) -> Tuple[int, str, int, str]:
        b = net_to_bus.get(name)
        if not b:
            return (1, "", 0, name)
        # Position within the bus: prefer numeric `index` (DATA bus
        # bit); else role-canonical order (sda/scl/mosi/miso); else
        # member position in the detected order.
        canonical_role_order = {
            "sda": 0, "scl": 1,
            "mosi": 0, "miso": 1, "sck": 2, "cs": 3,
            "tx": 0, "rx": 1,
            "dp": 0, "dm": 1,
            "canh": 0, "canl": 1,
            "p": 0, "n": 1,
        }
        for i, m in enumerate(b["members"]):
            if m["net"] != name:
                continue
            if m.get("index") is not None:
                return (0, b["name"], int(m["index"]), name)
            role = (m.get("role") or "").lower()
            if role in canonical_role_order:
                return (0, b["name"], canonical_role_order[role], name)
            return (0, b["name"], i, name)
        return (0, b["name"], 0, name)

    ordered_cross = sorted(cross.keys(), key=_pin_sort_key)
    child_pins: Dict[str, List[Dict[str, Any]]] = {role: [] for role in ordered_roles}
    for net_name in ordered_cross:
        clusters_for_net = cross[net_name]
        for role in ordered_roles:
            if role in clusters_for_net:
                # Tag the pin with its bus name so downstream emitters
                # (and future P14.4 spine synthesis) can group them.
                bus = net_to_bus.get(net_name)
                child_pins[role].append({
                    "name": str(net_name),
                    "direction": "passive",  # safe default; KiCad accepts join
                    "side": "left",
                    "_bus_name": bus["name"] if bus else None,
                    "_bus_kind": bus["kind"] if bus else None,
                })

    # Lay sheets in an N-column grid. P13 path: use the flow planner's
    # (col, row) per block so signal direction reads left-to-right,
    # top-to-bottom on the parent. Legacy path (no `block_grid`): pack
    # row-major using the canonical-order list.
    for i, role in enumerate(ordered_roles):
        cell = block_grid.get(role) if block_grid else None
        if cell is not None:
            col, row = cell
        else:
            row, col = divmod(i, grid_cols)
        x = origin_x + col * (cell_w + cell_gap)
        y = origin_y + row * (cell_h + cell_gap)
        numbered_zone = role_to_label[role]
        child_path = child_dir / f"{numbered_zone}.kicad_sch"
        try:
            rel_ref = child_path.relative_to(parent_out_p.parent).as_posix()
        except ValueError:
            rel_ref = str(child_path).replace("\\", "/")
        # Sheetname displayed in parent: "<Project>_<Block>"
        display_name = f"{project_stem}_{role}"
        # Cap pin count per sheet — too many sheet pins on a small block
        # overflow the bbox and clip visually. The pins beyond the cap
        # still exist as hierarchical labels on the child; users that need
        # them just see them inside the sub-sheet.
        pin_cap = max(4, int(cell_h // 2.54) - 2)
        pins = (child_pins.get(role) or [])[:pin_cap]
        doc.tree.append(_build_sheet_node(
            name=display_name, file_basename=rel_ref,
            x=x, y=y, w=cell_w, h=cell_h, pins=pins,
        ))

    doc.save(parent_out_p, backup=False)
    return {
        "parent_path": str(parent_out_p),
        "child_paths": child_paths,
        "stats": {
            "children": len(child_paths),
            "cross_sheet_nets": len(split["cross_sheet_nets"]),
            "cross_sheet_net_names": sorted(split["cross_sheet_nets"].keys())[:50],
        },
    }
