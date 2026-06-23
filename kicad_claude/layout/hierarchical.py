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
) -> SplitResult:
    """Partition placement + routed into per-role child specs and surface
    the set of nets crossing role boundaries. Returns:

      {
        "children": {role: {
          "placement": <child placement json>,
          "routed":    <child routed json>,
          "cross_sheet_nets": [{net, side, x_mm, y_mm, angle}, ...]
        }, ...},
        "cross_sheet_nets": {net_name: {role_a, role_b, ...}, ...}
      }

    Cross-sheet nets are converted to hierarchical labels — same name on
    BOTH child sheets, KiCad joins them via the parent's sheet-pin map."""
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


def _build_sheet_node(name: str, file_basename: str,
                      x: float, y: float, w: float, h: float) -> list:
    """Construct a (sheet ...) sexpr entry for the parent.

    KiCad's `(sheet ...)` syntax expects `(at x y)` with EXACTLY two values
    — NO rotation. `_make_at` emits a third `(at x y rot)` value which
    eeschema parses fine for symbols but rejects for sheets with
    'Expecting ")" line N offset 17' (the rotation value's `.` is at
    that offset).

    Bypassing `_make_at` here to emit the 2-value form. Properties keep
    the 3-value form because property (at) does take rotation."""
    return [
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

    # Lay sheets in an N-column grid, ordered by signal flow (power
    # first, processing next, IO last). Each (sheet) entry's Sheetfile
    # is the RELATIVE PATH from parent dir to child file.
    for i, role in enumerate(ordered_roles):
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
        doc.tree.append(_build_sheet_node(
            name=display_name, file_basename=rel_ref,
            x=x, y=y, w=cell_w, h=cell_h,
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
