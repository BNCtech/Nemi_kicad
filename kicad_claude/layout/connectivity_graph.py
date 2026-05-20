"""Step 1 of the universal layout engine.

Builds a connectivity graph from a KiCad schematic and ranks components by
structural importance. The resulting JSON artifact is the single input
consumed by Step 2 (classifier), Step 3 (placer), etc.

Pure-Python and deterministic — no Claude/LLM calls. Run standalone:

    python -m ai_backend.kicad_layout.connectivity_graph path/to/design.kicad_sch --out graph.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import networkx as nx

try:
    from ai_backend.kicad_claude.schematic_extractor import SchematicExtractor
    from ai_backend.kicad_claude import nets as _nets
except ImportError:  # supports both `f:/Ki_CAD` and `ai_backend/` on sys.path
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore
    from kicad_claude import nets as _nets  # type: ignore

from . import load_config


def _is_power_net(net: Dict[str, Any], power_patterns: List[str]) -> bool:
    """Power if it carries a power-port member, or its name matches a configured
    rail pattern. Both checks are needed: bare labels like `+3V3` without a
    power-port symbol still need power treatment."""
    for m in net.get("members", []):
        if m.get("kind") == "power":
            return True
    name_up = (net.get("name") or "").upper()
    return any(name_up == p or name_up.startswith(p) for p in power_patterns)


def _component_refs_on_net(net: Dict[str, Any], ignored_prefixes: List[str]) -> List[str]:
    refs: Set[str] = set()
    for m in net.get("members", []):
        if m.get("kind") != "pin":
            continue
        ref = m.get("ref") or ""
        if not ref or any(ref.startswith(p) for p in ignored_prefixes):
            continue
        refs.add(ref)
    return sorted(refs)


def _pin_counts(extractor: SchematicExtractor) -> Dict[str, int]:
    """Pin count per refdes (sum across unit-0 + instance's unit). Multi-unit
    parts only count their own slice — matches how the placer will reason
    about them later."""
    lib_pins = extractor.lib_symbol_pins()
    out: Dict[str, int] = {}
    for c in extractor.components():
        ref = c.get("reference") or ""
        if not ref:
            continue
        lib_id = c.get("lib_id") or ""
        by_unit = lib_pins.get(lib_id) or {}
        if not by_unit:
            out[ref] = max(out.get(ref, 0), 0)
            continue
        unit_no = int(c.get("unit", 1))
        pins = list(by_unit.get(0, [])) + (list(by_unit.get(unit_no, [])) if unit_no != 0 else [])
        out[ref] = max(out.get(ref, 0), len(pins))
    return out


def build_graph(schematic_path) -> nx.Graph:
    """Build a networkx.Graph from a .kicad_sch file.

    Nodes = components (one per refdes — multi-unit parts collapse to one
    node with unit_count > 1). Edges = shared non-power net.

    Node attrs: lib_id, value, pin_count, power_rails, unit_count.
    Edge attrs: nets (list of shared net names), weight (count).
    """
    cfg = load_config()
    power_patterns = [p.upper() for p in cfg["power_net_patterns"]["patterns"]]
    ignored = cfg["ignored_refdes_prefixes"]["prefixes"]
    exclude_pwr = bool(cfg["edge_rules"]["exclude_power_nets"])
    min_pin = int(cfg["edge_rules"]["min_pin_count_for_edge"])

    ext = SchematicExtractor(schematic_path)
    net_data = _nets.build_sheet_nets(ext)
    pin_counts = _pin_counts(ext)
    # Body bboxes carry the REAL symbol dimensions from the lib_symbols cache;
    # without them the placer falls back to a pin-count formula that doubles a
    # 45-pin BGA's width and breaks zone fits on A4. Optional — if a lib_id has
    # no body primitives (rare), the placer's pin-count estimate is used.
    body_bboxes = ext.lib_symbol_bodies()
    lib_pins_by_id = ext.lib_symbol_pins()

    def _outline_dims(lib_id: str) -> Tuple[float, float]:
        """Outline = body bbox UNIONed with every pin endpoint. Without pin
        coords the body bbox under-reports the symbol's true visual footprint
        (pin overhangs extend 2.54 mm+ beyond the body line per KLC), and the
        placer's zone borders end up clipping pins off the side."""
        xs: List[float] = []
        ys: List[float] = []
        body = body_bboxes.get(lib_id)
        if body:
            xs.extend([body[0], body[2]])
            ys.extend([body[1], body[3]])
        for unit_pins in (lib_pins_by_id.get(lib_id) or {}).values():
            for p in unit_pins:
                xs.append(float(p["x"]))
                ys.append(float(p["y"]))
        if not xs or not ys:
            return (0.0, 0.0)
        return (max(xs) - min(xs), max(ys) - min(ys))

    g = nx.Graph()

    units_per_ref: Dict[str, Set[int]] = defaultdict(set)
    for c in ext.components():
        ref = c.get("reference") or ""
        if not ref or any(ref.startswith(p) for p in ignored):
            continue
        units_per_ref[ref].add(int(c.get("unit", 1)))
        if not g.has_node(ref):
            lib_id = c.get("lib_id", "")
            body = body_bboxes.get(lib_id)
            body_w = float(body[2] - body[0]) if body else 0.0
            body_h = float(body[3] - body[1]) if body else 0.0
            outline_w, outline_h = _outline_dims(lib_id)
            g.add_node(
                ref,
                lib_id=lib_id,
                value=c.get("value", ""),
                pin_count=pin_counts.get(ref, 0),
                power_rails=[],
                unit_count=1,
                body_w=body_w,
                body_h=body_h,
                outline_w=outline_w,
                outline_h=outline_h,
            )
    for ref, units in units_per_ref.items():
        if g.has_node(ref):
            g.nodes[ref]["unit_count"] = len(units)

    for net in net_data["nets"]:
        is_power = _is_power_net(net, power_patterns)
        refs = _component_refs_on_net(net, ignored)
        if is_power:
            for r in refs:
                if not g.has_node(r):
                    continue
                rails = g.nodes[r]["power_rails"]
                if net["name"] and net["name"] not in rails:
                    rails.append(net["name"])
            if exclude_pwr:
                continue
        if net.get("pin_count", 0) < min_pin:
            continue
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                if not (g.has_node(a) and g.has_node(b)):
                    continue
                if g.has_edge(a, b):
                    g[a][b]["nets"].append(net["name"])
                    g[a][b]["weight"] += 1
                else:
                    g.add_edge(a, b, nets=[net["name"]], weight=1)

    return g


def rank_importance(g: nx.Graph) -> Dict[str, float]:
    """Score each node; higher = more central to the circuit."""
    w = load_config()["importance_weights"]
    w_pins = float(w["pin_count"])
    w_deg = float(w["degree"])
    w_bet = float(w["betweenness"])

    if g.number_of_nodes() > 1:
        bet = nx.betweenness_centrality(g)
    else:
        bet = {n: 0.0 for n in g.nodes}

    return {
        n: w_pins * attrs.get("pin_count", 0)
        + w_deg * g.degree(n)
        + w_bet * bet.get(n, 0.0)
        for n, attrs in g.nodes(data=True)
    }


def identify_main_controller(g: nx.Graph, scores: Optional[Dict[str, float]] = None) -> Optional[str]:
    if g.number_of_nodes() == 0:
        return None
    if scores is None:
        scores = rank_importance(g)
    return max(scores, key=scores.get)


def graph_to_dict(g: nx.Graph, scores: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    if scores is None:
        scores = rank_importance(g)
    return {
        "main_controller": identify_main_controller(g, scores),
        "nodes": [
            {
                "ref": n,
                "lib_id": attrs.get("lib_id", ""),
                "value": attrs.get("value", ""),
                "pin_count": attrs.get("pin_count", 0),
                "power_rails": list(attrs.get("power_rails", [])),
                "unit_count": attrs.get("unit_count", 1),
                "body_w": round(attrs.get("body_w", 0.0), 3),
                "body_h": round(attrs.get("body_h", 0.0), 3),
                "outline_w": round(attrs.get("outline_w", 0.0), 3),
                "outline_h": round(attrs.get("outline_h", 0.0), 3),
                "degree": g.degree(n),
                "score": round(scores.get(n, 0.0), 4),
            }
            for n, attrs in g.nodes(data=True)
        ],
        "edges": [
            {"a": a, "b": b, "nets": list(d.get("nets", [])), "weight": d.get("weight", 1)}
            for a, b, d in g.edges(data=True)
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.connectivity_graph")
    ap.add_argument("schematic", help="path to .kicad_sch")
    ap.add_argument("--out", help="write graph JSON here (default: stdout)")
    args = ap.parse_args(argv)

    g = build_graph(args.schematic)
    data = graph_to_dict(g)
    payload = json.dumps(data, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(
            f"wrote {args.out}  "
            f"({len(data['nodes'])} nodes, {len(data['edges'])} edges, "
            f"main={data['main_controller']})"
        )
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
