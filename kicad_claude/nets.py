"""Net topology walker.

Builds the connectivity graph of a single sheet: every coordinate that has
a wire endpoint, junction, label anchor, or pin tip becomes a node;
union-find groups them into connected components (= nets). Each net carries:
  - members: list of {kind, ref, ...} describing what's on the net
  - name:    label/power-port name if any, else auto-named (Net-(<refdes>-Pad<n>))
  - drivers: count of pins whose electrical_type is output / power_out / bidirectional

Used by net-aware L1 checks (real orphan-label, missing junctions) and
eventually by L3 functional checks (decoupling cap on every VCC pin, etc.).

Per-sheet only; cross-sheet hierarchical-label binding is a v2 task.

All thresholds (coord rounding, etc.) come from conventions.json — same
zero-hardcode rule.
"""

import math
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ._config_loader import load as _load_config
from .schematic_extractor import SchematicExtractor


def _conv_cfg() -> Dict[str, Any]:
    return _load_config("conventions")


# ---------------------------------------------------------------------------
# Coord snapping
# ---------------------------------------------------------------------------

def _snap(x: float, y: float, tol: float) -> Tuple[int, int]:
    """Snap a (x, y) coordinate to a grid-tolerance bucket so floating-point
    drift doesn't fragment connected nodes."""
    return (int(round(x / tol)), int(round(y / tol)))


# ---------------------------------------------------------------------------
# Pin geometry: lib-local coords -> world coords
# ---------------------------------------------------------------------------

def _pin_tip_local(pin: Dict[str, Any]) -> Tuple[float, float]:
    """Pin tip (the wire-attach point) in symbol-local coords.

    KLC convention: the pin's `(at x y rot)` IS the tip — the outer endpoint
    where wires attach. The pin extends INWARD by `length` toward the body
    (opposite to rot direction). So the tip is just (x, y). length and rot
    only matter for body-side computations.
    """
    return (float(pin["x"]), float(pin["y"]))


def _rot_xy(x: float, y: float, deg: float) -> Tuple[float, float]:
    """Rotate a point (x, y) by 0/90/180/270 degrees around origin."""
    q = (int(round(deg)) % 360) // 90
    for _ in range(q):
        x, y = -y, x
    return (x, y)


def placed_pin_endpoints(component: Dict[str, Any], pin_defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Resolve every pin of a placed component to its world-space tip coordinate.

    Returns a list of {x, y, electrical_type, name, number, ref}. Coords are
    in the schematic's mm coordinate system (Y-down).
    """
    if not component.get("at") or not pin_defs:
        return []
    cx, cy = float(component["at"][0]), float(component["at"][1])
    crot = float(component["at"][2]) if len(component["at"]) >= 3 else 0.0
    ref = component.get("reference") or ""
    out: List[Dict[str, Any]] = []
    for p in pin_defs:
        tx, ty = _pin_tip_local(p)
        rx, ry = _rot_xy(tx, ty, crot)
        # KiCad schematic Y is inverted relative to lib-symbol Y.
        wx, wy = cx + rx, cy - ry
        out.append({
            "x": wx, "y": wy,
            "electrical_type": p["electrical_type"],
            "name": p["name"],
            "number": p["number"],
            "ref": ref,
        })
    return out


# ---------------------------------------------------------------------------
# Union-find
# ---------------------------------------------------------------------------

class _UF:
    def __init__(self) -> None:
        self.parent: Dict[Any, Any] = {}

    def add(self, x: Any) -> None:
        if x not in self.parent:
            self.parent[x] = x

    def find(self, x: Any) -> Any:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # path compression
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


# ---------------------------------------------------------------------------
# Sheet net build
# ---------------------------------------------------------------------------

def _wire_segments(wire: List[Tuple[float, float]]) -> Iterable[Tuple[Tuple[float, float], Tuple[float, float]]]:
    for i in range(len(wire) - 1):
        yield (tuple(wire[i]), tuple(wire[i + 1]))


def _segment_contains_point(p1, p2, q, tol: float) -> bool:
    """True iff q lies on the closed segment p1-p2 (axis-aligned only) within tol."""
    x1, y1 = p1; x2, y2 = p2; qx, qy = q
    if abs(y1 - y2) <= tol * 0.5:  # horizontal
        if abs(qy - y1) > tol:
            return False
        return min(x1, x2) - tol <= qx <= max(x1, x2) + tol
    if abs(x1 - x2) <= tol * 0.5:  # vertical
        if abs(qx - x1) > tol:
            return False
        return min(y1, y2) - tol <= qy <= max(y1, y2) + tol
    # diagonal: parametric check
    dx, dy = x2 - x1, y2 - y1
    if abs(dx) < tol and abs(dy) < tol:
        return abs(qx - x1) <= tol and abs(qy - y1) <= tol
    if abs(dx) >= abs(dy):
        t = (qx - x1) / dx
    else:
        t = (qy - y1) / dy
    if t < -1e-6 or t > 1 + 1e-6:
        return False
    return abs(x1 + t * dx - qx) <= tol and abs(y1 + t * dy - qy) <= tol


def build_sheet_nets(extractor: SchematicExtractor) -> Dict[str, Any]:
    """Build the per-sheet net list from one extractor.

    Returns:
      {
        "nets": [
          {"id": int, "name": str, "members": [...], "drivers": int,
           "label_count": int, "pin_count": int}
        ],
        "junctions": [(x, y), ...],
        "wires_by_endpoint": {snapped_pt: [(seg, wire_idx), ...]}
      }
    """
    grid = float(_conv_cfg()["grid"]["schematic_mm"])
    # Tolerance for joining nodes — KiCad default is the grid step itself, but
    # we use a fraction so off-grid drift catches bugs (basic_checks already
    # flags off-grid endpoints separately).
    tol = max(grid * 0.49, 0.01)

    uf = _UF()
    members: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)

    components = extractor.components()
    labels = extractor.labels()
    wires = extractor.wires()
    junctions = extractor.junctions()
    lib_pins = extractor.lib_symbol_pins()

    # 1. Wire endpoints — every endpoint joins the wire's other endpoints.
    for wi, w in enumerate(wires):
        if len(w) < 2:
            continue
        snapped = [_snap(x, y, tol) for x, y in w]
        for s in snapped:
            uf.add(s)
        for k in range(1, len(snapped)):
            uf.union(snapped[0], snapped[k])

    # 2. Junctions — connect all wires that pass THROUGH this point (not just
    # those that end here). KiCad junctions stitch wires that visually meet
    # at a T or 4-way; without the junction, only wires sharing an endpoint
    # are connected (which is why missing junctions are a real defect).
    for jx, jy in junctions:
        jp = _snap(jx, jy, tol)
        uf.add(jp)
        for wi, w in enumerate(wires):
            for p1, p2 in _wire_segments(w):
                if _segment_contains_point(p1, p2, (jx, jy), tol):
                    s1 = _snap(p1[0], p1[1], tol)
                    s2 = _snap(p2[0], p2[1], tol)
                    uf.union(jp, s1)
                    uf.union(jp, s2)

    # 3. Labels — anchor binds the label-name to whatever else is at that point.
    # Global labels with the same name are one net regardless of position;
    # union them via a virtual "global_label::<name>" node.
    for lb in labels:
        at = lb.get("at")
        if not at:
            continue
        s = _snap(float(at[0]), float(at[1]), tol)
        uf.add(s)
        kind = lb.get("kind", "label")
        name = lb.get("name") or ""
        members[s].append({"kind": kind, "name": name})
        if kind == "global_label" and name:
            virt = ("__global_label__", name)
            uf.add(virt)
            uf.union(s, virt)

    # 4. Pins — every pin tip is a member of its net. Multi-unit aware:
    # the placed instance has a `unit` field; we take that unit's pins plus
    # the always-present unit-0 (common: VDD/GND on multi-channel ICs).
    for c in components:
        lib_id = c.get("lib_id", "")
        by_unit = lib_pins.get(lib_id) or {}
        if not by_unit:
            continue
        unit_no = int(c.get("unit", 1))
        pin_defs: List[Dict[str, Any]] = []
        if 0 in by_unit:
            pin_defs.extend(by_unit[0])
        if unit_no in by_unit and unit_no != 0:
            pin_defs.extend(by_unit[unit_no])
        if not pin_defs:
            # Single-unit symbols often store pins under key 1 even if instance unit is 1
            # (which is the common path), but if both 0 and unit_no missed, fall back to all.
            for u_pins in by_unit.values():
                pin_defs.extend(u_pins)
        for ep in placed_pin_endpoints(c, pin_defs):
            s = _snap(ep["x"], ep["y"], tol)
            uf.add(s)
            members[s].append({
                "kind": "pin",
                "ref": ep["ref"],
                "pin_name": ep["name"],
                "pin_number": ep["number"],
                "electrical_type": ep["electrical_type"],
            })

    # 5. Power-port symbols — value is the rail name (VCC, GND, +3V3, ...).
    # All power ports with the same rail name are ONE net regardless of where
    # they sit, just like global labels. Union via "power::<name>" virtual node.
    for c in components:
        lib_id = c.get("lib_id", "") or ""
        # Treat both `power:` library symbols AND any symbol whose value is a
        # power-rail name with a leading '#' refdes (KiCad hides power-port
        # references behind '#PWR...' and '#PWR_FLAG').
        is_power = lib_id.startswith("power:") or (c.get("reference", "") or "").startswith("#PWR")
        if not is_power:
            continue
        at = c.get("at")
        if not at:
            continue
        rail = c.get("value", "") or ""
        s = _snap(float(at[0]), float(at[1]), tol)
        uf.add(s)
        members[s].append({
            "kind": "power",
            "ref": c.get("reference", ""),
            "name": rail,
        })
        if rail:
            virt = ("__power__", rail)
            uf.add(virt)
            uf.union(s, virt)

    # Bucket nodes by their union-find root.
    by_root: Dict[Any, List[Any]] = defaultdict(list)
    for node in list(uf.parent.keys()):
        by_root[uf.find(node)].append(node)

    nets: List[Dict[str, Any]] = []
    driver_types = {"output", "power_out", "bidirectional", "tri_state"}
    for nid, (root, nodes) in enumerate(sorted(by_root.items())):
        net_members: List[Dict[str, Any]] = []
        for node in nodes:
            net_members.extend(members.get(node, []))
        # Net name: prefer power-port, else any label, else auto-name.
        name = ""
        for m in net_members:
            if m.get("kind") == "power" and m.get("name"):
                name = m["name"]
                break
        if not name:
            for m in net_members:
                if m.get("kind") in ("label", "global_label", "hierarchical_label") and m.get("name"):
                    name = m["name"]
                    break
        if not name:
            for m in net_members:
                if m.get("kind") == "pin":
                    name = f"Net-({m['ref']}-Pad{m['pin_number']})"
                    break
        if not name:
            name = f"Net-#{nid}"

        drivers = sum(1 for m in net_members if m.get("electrical_type") in driver_types)
        label_count = sum(1 for m in net_members
                          if m.get("kind") in ("label", "global_label", "hierarchical_label"))
        pin_count = sum(1 for m in net_members if m.get("kind") == "pin")

        nets.append({
            "id": nid,
            "name": name,
            "members": net_members,
            "drivers": drivers,
            "label_count": label_count,
            "pin_count": pin_count,
            "node_count": len(nodes),
        })

    return {
        "nets": nets,
        "junctions": junctions,
        "wires": wires,
    }


def build_project_nets(schematic_path) -> Dict[str, Any]:
    """Build per-sheet nets across the whole project (no cross-sheet binding yet).

    Returns {sheets: {hpath: <build_sheet_nets result>}}.
    """
    from . import hierarchy as _hier
    out: Dict[str, Any] = {"sheets": {}}
    for hpath, path in _hier.iter_sheet_instances(schematic_path):
        try:
            ext = SchematicExtractor(path)
        except Exception:
            continue
        out["sheets"][hpath] = build_sheet_nets(ext)
    return out
