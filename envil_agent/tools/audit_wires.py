"""Tool: audit a .kicad_sch for wires that pierce component bodies.

Enforces wiring rule R2 from `WIRING_RULES.md` — wires must connect
pin tips only, never cross over a component body. Reports any
violation with the offending wire endpoints + the component pierced.

Universal — works on any .kicad_sch, single-sheet or hierarchy child."""
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


def _audit_sch(sch_path: Path) -> Dict[str, Any]:
    from ..kicad.symbol_geom import load_symbol, place_pin
    text = sch_path.read_text(encoding="utf-8")
    root = sexpdata.loads(text)
    if not isinstance(root, list) or _head(root) != "kicad_sch":
        return {"ok": False, "error": "not a kicad_sch"}

    bboxes: List[Tuple[str, float, float, float, float]] = []
    pin_positions: List[Tuple[float, float, str]] = []
    for child in root[1:]:
        if not (isinstance(child, list) and _head(child) == "symbol"):
            continue
        ref = lib_id = ""
        atx = aty = atrot = 0.0
        for c in child[1:]:
            if not isinstance(c, list): continue
            if _head(c) == "lib_id" and len(c) >= 2:
                lib_id = str(c[1])
            elif _head(c) == "at":
                try:
                    atx = float(c[1]); aty = float(c[2])
                    if len(c) >= 4: atrot = float(c[3])
                except (TypeError, ValueError): pass
            elif _head(c) == "property" and len(c) >= 3 and str(c[1]) == "Reference":
                ref = str(c[2])
        if not ref or ref.startswith("#") or not lib_id:
            continue
        try:
            g = load_symbol(lib_id)
        except Exception:
            continue
        x1, y1, x2, y2 = g.outer_bbox
        corners = [(x1, y1), (x1, y2), (x2, y1), (x2, y2)]
        corners = [(x, -y) for x, y in corners]
        rad = math.radians(atrot)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        corners = [(x * cos_r - y * sin_r, x * sin_r + y * cos_r) for x, y in corners]
        corners = [(atx + x, aty + y) for x, y in corners]
        xs = [p[0] for p in corners]; ys = [p[1] for p in corners]
        bboxes.append((ref, min(xs), min(ys), max(xs), max(ys)))
        for pin in g.pins:
            px, py, _r = place_pin(pin, atx, aty, atrot)
            pin_positions.append((px, py, ref))

    n_wires = n_labels = n_junctions = n_pwr = 0
    violations: List[Dict[str, Any]] = []
    # R4 — count wires terminating at each grid point so we can detect
    # 4-way meetings (potentially ambiguous between junction + crossing).
    from collections import defaultdict
    wire_endpoints: Dict[Tuple[float, float], int] = defaultdict(int)
    # R9 — collect label names so we can suggest bus notation when
    # 4+ nets share a prefix+digit pattern.
    label_names: List[str] = []
    for child in root[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)
        if h == "wire":
            n_wires += 1
            pts = next((c for c in child[1:]
                         if isinstance(c, list) and _head(c) == "pts"), None)
            if not pts: continue
            try:
                ax, ay = float(pts[1][1]), float(pts[1][2])
                bx, by = float(pts[2][1]), float(pts[2][2])
            except Exception:
                continue
            # R4 — record both endpoints
            wire_endpoints[(round(ax, 3), round(ay, 3))] += 1
            wire_endpoints[(round(bx, 3), round(by, 3))] += 1
            for ref, x1, y1, x2, y2 in bboxes:
                eps = 0.5
                pin_on_this = any(
                    ref == pref and (
                        (abs(px - ax) < 0.5 and abs(py - ay) < 0.5) or
                        (abs(px - bx) < 0.5 and abs(py - by) < 0.5))
                    for px, py, pref in pin_positions)
                if pin_on_this:
                    continue
                if max(ax, bx) <= x1 or min(ax, bx) >= x2: continue
                if max(ay, by) <= y1 or min(ay, by) >= y2: continue
                inside = lambda x, y: x1 + eps < x < x2 - eps and y1 + eps < y < y2 - eps
                midx, midy = (ax + bx) / 2, (ay + by) / 2
                if inside(ax, ay) or inside(bx, by) or inside(midx, midy):
                    violations.append({
                        "wire_start": [round(ax, 2), round(ay, 2)],
                        "wire_end":   [round(bx, 2), round(by, 2)],
                        "pierces":    ref,
                        "bbox": [round(x1, 2), round(y1, 2),
                                 round(x2, 2), round(y2, 2)],
                    })
                    break
        elif h in ("label", "global_label", "hierarchical_label"):
            n_labels += 1
            if len(child) >= 2:
                label_names.append(str(child[1]))
        elif h == "junction":
            n_junctions += 1
        elif h == "symbol":
            for c in child[1:]:
                if (isinstance(c, list) and _head(c) == "lib_id"
                        and len(c) >= 2 and str(c[1]).startswith("power:")):
                    n_pwr += 1
                    break

    # R4 — count grid points where 4+ wire endpoints meet
    four_ways = sum(1 for c in wire_endpoints.values() if c >= 4)

    # R9 — suggest buses where 4+ labels share `<prefix><digit>` pattern
    import re as _re
    pat = _re.compile(r"^([A-Za-z_]+)(\d+)$")
    groups: Dict[str, List[int]] = {}
    for n in set(label_names):
        m = pat.match(n)
        if m:
            groups.setdefault(m.group(1), []).append(int(m.group(2)))
    bus_candidates = []
    for prefix, idxs in groups.items():
        if len(idxs) >= 4:
            idxs.sort()
            bus_candidates.append({
                "prefix": prefix,
                "count": len(idxs),
                "range": f"{prefix}[{min(idxs)}..{max(idxs)}]",
            })

    return {
        "ok": True,
        "components":     len(bboxes),
        "wires":          n_wires,
        "labels":         n_labels,
        "junctions":      n_junctions,
        "power_ports":    n_pwr,
        "body_crossings": len(violations),         # R2
        "four_way_junctions": four_ways,            # R4
        "bus_candidates": bus_candidates,           # R9 suggestion
        "violations":     violations[:20],
    }


@tool(
    name="audit_wires",
    description=(
        "Audit a .kicad_sch for wires piercing component bodies (rule "
        "R2 from WIRING_RULES.md). Reports counts + first 20 violations "
        "with wire coords + offending component. Use when the user asks "
        "'check wire routing', 'find body-piercing wires', 'audit the "
        "schematic', 'wire crossing check pannu' (Tanglish).\n"
        "Args:\n"
        '  {"sch_path": "C:/.../proj.kicad_sch"}\n'
    ),
    input_schema={"sch_path": str},
)
async def audit_wires(args: dict[str, Any]) -> dict[str, Any]:
    sch = Path(str(args.get("sch_path", "")).strip()).expanduser()
    if not sch.exists():
        return {"content": [{"type": "text",
                              "text": f"ERROR: not found: {sch}"}],
                 "is_error": True}
    if sch.suffix.lower() != ".kicad_sch":
        return {"content": [{"type": "text",
                              "text": "ERROR: expected .kicad_sch"}],
                 "is_error": True}

    try:
        result = _audit_sch(sch)
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: {type(exc).__name__}: {exc}"}],
                 "is_error": True}

    lines = [f"Wire audit: {sch.name}"]
    lines.append(f"  components:    {result.get('components')}")
    lines.append(f"  wires:         {result.get('wires')}")
    lines.append(f"  labels:        {result.get('labels')}")
    lines.append(f"  junctions:     {result.get('junctions')}")
    lines.append(f"  power ports:   {result.get('power_ports')}")
    crossings = result.get("body_crossings", 0)
    four_ways = result.get("four_way_junctions", 0)
    buses     = result.get("bus_candidates", [])
    lines.append("")
    if crossings == 0:
        lines.append("  R2  ✓ no wires pierce component bodies")
    else:
        lines.append(f"  R2  ✗ {crossings} body-pierce violations")
        for v in result.get("violations", []):
            ax, ay = v["wire_start"]; bx, by = v["wire_end"]
            lines.append(f"    - wire ({ax}, {ay}) -> ({bx}, {by}) pierces {v['pierces']}")
    if four_ways == 0:
        lines.append("  R4  ✓ no 4-way wire convergences")
    else:
        lines.append(f"  R4  ⓘ {four_ways} grid point(s) with 4+ wire endpoints "
                      f"(may be intentional same-net joins; verify visually)")
    if not buses:
        lines.append("  R9  ✓ no bus-suggestion candidates (label spread is uniform)")
    else:
        lines.append(f"  R9  ⓘ {len(buses)} label group(s) could become buses:")
        for b in buses:
            lines.append(f"    - {b['range']} ({b['count']} signals)")
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": crossings == 0,
        "result": result,
    }
