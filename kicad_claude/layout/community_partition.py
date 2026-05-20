"""Community-detection-based hierarchical partitioning.

Replaces the role-bucket split (`hierarchical.split_placement`) with a
weighted-graph community partition. The weighted graph captures the
three connectivity strengths the project's instrumentation revealed:

    wire edges   (explicit segment between two pins)    weight = 1.0
    label edges  (same-name label merges two pins)      weight = 0.6
    power edges  (same power-rail across multiple pins) weight = 0.2

Louvain or greedy-modularity then groups components into clusters that
maximise intra-cluster connectivity / minimise cross-cluster nets — the
natural sheet boundaries of a real schematic.

Pure NetworkX. No Claude calls. No hardcoded part rules — every weight,
threshold, and community-naming hint is config-driven via
`layout_config.json:community_partition`. Deterministic for a given
graph + seed.

Public:
  build_weighted_graph(schematic_path)   -> networkx.Graph
  partition_into_communities(graph, cfg) -> List[Set[ref]]
  name_communities(communities, classified) -> Dict[community_id, role_name]
  balance_communities(communities, cfg)  -> List[Set[ref]]
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional, Set, Tuple

import networkx as nx

try:
    from ai_backend.kicad_claude.schematic_extractor import SchematicExtractor
    from ai_backend.kicad_claude import nets as _nets
except ImportError:  # repo-on-sys.path variant
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore
    from kicad_claude import nets as _nets  # type: ignore

from . import load_config


_DEFAULT_WEIGHTS = {
    "wire": 1.0,
    "label": 0.6,
    "power": 0.2,
}

# Power-net detection. Same convention as connectivity_graph: any net with
# a `kind: power` member, OR a name starting with one of these prefixes.
_POWER_NET_PREFIXES = ("GND", "VSS", "VCC", "VDD", "VEE", "VBUS", "VBAT",
                       "VSYS", "+", "-", "AGND", "DGND", "PGND", "SGND",
                       "EGND", "AVCC", "AVDD")


def _is_power_net(net: Dict[str, Any]) -> bool:
    for m in net.get("members", []):
        if m.get("kind") == "power":
            return True
    name = (net.get("name") or "").upper()
    return any(name == p or name.startswith(p) for p in _POWER_NET_PREFIXES)


def _has_label_member(net: Dict[str, Any]) -> bool:
    label_kinds = {"label", "global_label", "hierarchical_label"}
    return any(m.get("kind") in label_kinds for m in net.get("members", []))


def _component_refs_on_net(net: Dict[str, Any]) -> List[str]:
    refs: Set[str] = set()
    for m in net.get("members", []):
        if m.get("kind") != "pin":
            continue
        ref = m.get("ref") or ""
        if not ref or ref.startswith("#"):
            continue  # exclude #PWR / #FLG symbols
        refs.add(ref)
    return sorted(refs)


_ANCHOR_ROLES = frozenset({
    "MAIN_CONTROLLER", "POWER_REGULATOR", "WIRELESS", "MEMORY", "DISPLAY",
    "MOTOR", "SENSOR", "ANALOG", "RF",
})


def boost_anchor_satellite_weights(
    g: nx.Graph,
    classified: Optional[Dict[str, Any]],
    boost_multiplier: float = 2.5,
) -> int:
    """Pre-Louvain weight boost. For every anchor component (MCU,
    regulator, sensor, etc.), multiply the weight of every edge from
    the anchor to its DIRECT NEIGHBOURS. Result: Louvain sees a much
    stronger pull binding each anchor to its support passives, so the
    subsystem (anchor + decoupling caps + biasing resistors + crystal +
    pull-ups) stays together in one community.

    Why it works: without boost, an MCU shares
      - wire-net (1.0) with its crystal Y1
      - label-net (0.6) with the I2C bus going to the sensor
    The label-edge to the sensor matches the wire-edge to its OWN
    crystal — Louvain has no reason to keep the crystal with the MCU
    over the sensor. With anchor boost, the crystal-MCU edge becomes
    2.5 — modularity now strictly prefers MCU+crystal stay together.

    Mutates `g` in place. Returns the count of boosted edges so callers
    can log the effect."""
    if not classified:
        return 0
    role_by_ref = {n.get("ref"): n.get("role")
                   for n in classified.get("nodes", [])
                   if n.get("ref")}
    boosted = 0
    for ref, role in role_by_ref.items():
        if role not in _ANCHOR_ROLES:
            continue
        if not g.has_node(ref):
            continue
        for neighbor in list(g.neighbors(ref)):
            edge = g[ref][neighbor]
            old_w = edge.get("weight", 1.0)
            edge["weight"] = old_w * boost_multiplier
            boosted += 1
    return boosted


def build_weighted_graph(
    schematic_path,
    weights: Optional[Dict[str, float]] = None,
) -> nx.Graph:
    """Construct a weighted networkx.Graph from a single sheet.

    Edge weight = sum of per-net weights for every (ref_a, ref_b) pair the
    components share. A pair that shares a wire-net (weight 1.0) AND a
    label-net (weight 0.6) carries 1.6 — naturally strong cluster
    membership. A pair sharing only the GND rail carries 0.2.

    Nodes = component refdes (real parts; #PWR/#FLG excluded).
    Node attrs: pin_count (best-effort from net membership)."""
    w_cfg = {**_DEFAULT_WEIGHTS, **(weights or {})}
    extractor = SchematicExtractor(schematic_path)
    net_data = _nets.build_sheet_nets(extractor)

    g = nx.Graph()

    pin_count_by_ref: Dict[str, int] = {}
    for net in net_data["nets"]:
        for m in net.get("members", []):
            if m.get("kind") != "pin":
                continue
            ref = m.get("ref") or ""
            if not ref or ref.startswith("#"):
                continue
            pin_count_by_ref[ref] = pin_count_by_ref.get(ref, 0) + 1

    for ref, pc in pin_count_by_ref.items():
        g.add_node(ref, pin_count=pc)

    for net in net_data["nets"]:
        refs = _component_refs_on_net(net)
        if len(refs) < 2:
            continue
        if _is_power_net(net):
            weight = w_cfg["power"]
        elif _has_label_member(net):
            weight = w_cfg["label"]
        else:
            weight = w_cfg["wire"]
        # Pairwise edges. Same pair across multiple nets: weights accumulate.
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                if g.has_edge(a, b):
                    g[a][b]["weight"] += weight
                    g[a][b]["nets"].append(net.get("name", ""))
                else:
                    g.add_edge(a, b, weight=weight, nets=[net.get("name", "")])

    return g


def partition_into_communities(
    graph: nx.Graph,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Set[str]]:
    """Run weighted Louvain community detection on the graph. Returns a list
    of communities (each a set of component refs). Singleton communities
    (1 ref, no neighbours) come back as their own sets — caller decides
    whether to absorb them via `balance_communities`.

    For very small graphs (< 4 components) returns one community
    containing every node — community detection is meaningless below that.
    For disconnected components, each connected piece gets its own
    community trivially.

    Universal: no part-specific knowledge. Algorithm operates purely on
    the edge weight matrix."""
    cfg = cfg or {}
    if graph.number_of_nodes() < 4:
        return [set(graph.nodes())] if graph.number_of_nodes() else []

    algo = (cfg.get("algorithm") or "louvain").lower()
    seed = int(cfg.get("seed", 42))

    if algo == "louvain":
        comms = nx.community.louvain_communities(
            graph, weight="weight", seed=seed,
        )
    elif algo == "greedy_modularity":
        comms = nx.community.greedy_modularity_communities(
            graph, weight="weight",
        )
    elif algo == "label_propagation":
        comms = nx.community.label_propagation_communities(graph)
    else:
        # Unknown algorithm — degenerate to "one community per connected component"
        # so callers can't accidentally fail when config is mis-set.
        comms = [set(c) for c in nx.connected_components(graph)]

    out: List[Set[str]] = []
    for c in comms:
        s = set(c)
        if s:
            out.append(s)
    # Isolated nodes (no neighbours) won't appear in any community; capture them.
    seen: Set[str] = set().union(*out) if out else set()
    for n in graph.nodes():
        if n not in seen:
            out.append({n})
    return out


# Cluster-naming priority. Higher position = wins over lower regardless of
# plurality. Without this, a cluster containing the MCU + 3 connectors +
# 5 protection parts gets named "PROTECTION" by plurality vote — wrong,
# because the MCU is the SUBSYSTEM ANCHOR. The fix: priority order treats
# role classes the way an EE would name a sub-sheet ("STM32 + decoupling
# is the MCU sheet", not the "CAP sheet").
#
# Power-supply boards (no MCU) fall to POWER_REGULATOR; analog/RF/etc.
# pick the highest-priority role that's actually present in the cluster.
_CLUSTER_NAME_PRIORITY = [
    "MAIN_CONTROLLER",
    "WIRELESS",
    "MEMORY",
    "DISPLAY",
    "MOTOR",
    "RF",
    "POWER_REGULATOR",
    "SENSOR",
    "ANALOG",
    "USB",
    "DEBUG",
    "PROTECTION",
    "I2C", "SPI", "UART", "CAN",
    "RESET", "BOOT", "CRYSTAL",
    "POWER",
    "CONNECTOR",
    "GENERIC",
]


def name_communities(
    communities: List[Set[str]],
    classified: Optional[Dict[str, Any]] = None,
    block_by_ref: Optional[Dict[str, str]] = None,
) -> Dict[int, str]:
    """Pick a semantic name for each community.

    Naming priority:
      1. P12 functional block — if ≥`fb_majority_frac` of a community's
         members all share a functional-block tag (USB / CAN / I2C /
         CRYSTAL_SECT / DEBUG / POWER_INPUT / PROTECTION / ...), use the
         block name. This is the highest-signal name: block detection
         already disambiguated by lib_id + net patterns.
      2. Role priority — fallback for un-blocked clusters. Highest-
         priority role in `_CLUSTER_NAME_PRIORITY` wins, even if other
         roles have plurality (the MCU in a cluster of 10 caps should
         still name the cluster MAIN_CONTROLLER).
      3. GENERIC — absolute fallback.

    Duplicate-name resolution: when the same priority name appears in
    multiple communities, the SECOND community's members get re-tagged
    with `<NAME>_2` so the caller can collapse them via
    `merge_duplicate_named_communities`."""
    role_by_ref: Dict[str, str] = {}
    if classified:
        for n in classified.get("nodes", []):
            ref = n.get("ref")
            role = n.get("role")
            if ref and role:
                role_by_ref[ref] = role
    block_by_ref = block_by_ref or {}
    # A community is named by its functional block when at least
    # `fb_majority_frac` of its members carry the same block tag. 0.4
    # = 40% — generous enough that a USB-block cluster with a few
    # power-rail caps mixed in still gets the USB name.
    fb_majority_frac = 0.4

    priority_pos = {r: i for i, r in enumerate(_CLUSTER_NAME_PRIORITY)}
    out: Dict[int, str] = {}
    for i, comm in enumerate(communities):
        # Step 1 — functional block majority.
        if block_by_ref:
            counts: Dict[str, int] = {}
            for ref in comm:
                blk = block_by_ref.get(ref)
                if blk:
                    counts[blk] = counts.get(blk, 0) + 1
            if counts:
                best_blk, best_count = max(counts.items(),
                                             key=lambda kv: kv[1])
                if best_count >= max(2, fb_majority_frac * len(comm)):
                    out[i] = best_blk
                    continue
        if not role_by_ref:
            out[i] = f"CLUSTER_{i + 1}"
            continue
        # Step 2 — role priority.
        best_pos = len(_CLUSTER_NAME_PRIORITY) + 1
        best_role = "GENERIC"
        for ref in comm:
            role = role_by_ref.get(ref)
            if not role:
                continue
            pos = priority_pos.get(role, len(_CLUSTER_NAME_PRIORITY))
            if pos < best_pos:
                best_pos = pos
                best_role = role
        out[i] = best_role

    # Two communities both winning the same name (e.g. both have an MCU)
    # means the partition spuriously split a single sub-system. Tag with
    # `_2` so the caller can collapse them via `_merge_duplicate_named_communities`.
    seen: Dict[str, int] = {}
    final: Dict[int, str] = {}
    for i in sorted(out.keys()):
        name = out[i]
        seen[name] = seen.get(name, 0) + 1
        final[i] = name if seen[name] == 1 else f"{name}_{seen[name]}"
    return final


def merge_duplicate_named_communities(
    communities: List[Set[str]], names: Dict[int, str],
) -> Tuple[List[Set[str]], Dict[int, str]]:
    """When two communities ended up with the same priority-name (e.g.
    PROTECTION + PROTECTION_2 — they appear as different Louvain
    components but represent the same subsystem-class), merge them
    back into one community. Result has tighter naming and fewer
    fragmented sheets.

    Universal: works regardless of cluster count or names. Merges
    `<NAME>` + `<NAME>_2` + `<NAME>_3` ... into one community keyed
    by the base name."""
    import re as _re_merge
    # Group community indices by base-name (strip _2 / _3 suffixes).
    by_base: Dict[str, List[int]] = {}
    for i, n in names.items():
        base = _re_merge.sub(r"_\d+$", "", n)
        by_base.setdefault(base, []).append(i)

    merged_communities: List[Set[str]] = []
    merged_names: Dict[int, str] = {}
    new_idx = 0
    for base, idxs in by_base.items():
        union: Set[str] = set()
        for i in idxs:
            union |= communities[i]
        merged_communities.append(union)
        merged_names[new_idx] = base
        new_idx += 1
    return merged_communities, merged_names


def balance_communities(
    communities: List[Set[str]],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Set[str]]:
    """Apply min/max-size balancing so the parent sheet doesn't end up
    showing "1 huge + 5 tiny" panels.

      min_members  — communities below this absorb into the largest
                      neighbour. Default 3.
      max_members  — communities above this stay as-is for now; the
                      hierarchy emitter splits them inside the same
                      sheet rather than across sheets. Default 40.
      max_sheets   — hard cap on total sheet count after absorption.
                      Default 8. Excess small communities merge into
                      a final MISC bucket.

    Conservative — only absorbs the smallest-into-largest, never the
    other way around. Universal: works for any graph shape."""
    cfg = cfg or {}
    min_n = int(cfg.get("min_members", 3))
    max_sheets = int(cfg.get("max_sheets", 8))

    # Sort by size descending. Walk from the end; small communities
    # absorb into the LAST (largest) community.
    ordered = sorted(communities, key=lambda c: -len(c))
    if not ordered:
        return []

    kept: List[Set[str]] = []
    for c in ordered:
        if len(c) >= min_n:
            kept.append(set(c))
        elif kept:
            # Absorb into the largest existing community.
            kept[0].update(c)
        else:
            # First community is below min — promote it anyway so the
            # graph isn't lost.
            kept.append(set(c))

    if len(kept) > max_sheets:
        # Merge the smallest (len-wise) communities into a MISC bucket
        # at the tail.
        kept.sort(key=lambda c: -len(c))
        head = kept[:max_sheets - 1]
        tail: Set[str] = set()
        for c in kept[max_sheets - 1:]:
            tail.update(c)
        head.append(tail)
        kept = head

    return kept


def partition_schematic(
    schematic_path,
    classified: Optional[Dict[str, Any]] = None,
    config_root: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """End-to-end partition: build the weighted graph, run community
    detection, name + balance the result. Returns:

      {
        "graph_stats": {nodes, edges, total_weight, ...},
        "communities": [{"id": i, "name": "MCU", "members": [...refs...],
                          "size": int, "intra_weight": float}, ...],
        "cross_cluster_edges": [{"a": refA, "b": refB, "weight": w,
                                  "nets": [...]}, ...],
      }

    The cross_cluster_edges list is the natural input to the
    hierarchical-port synthesis stage — every entry becomes a sheet pin
    pair (one on each parent-side child)."""
    cfg_root = config_root or load_config("layout_config")
    cp_cfg = (cfg_root.get("community_partition") or {})
    weights = cp_cfg.get("weights") or {}
    balance_cfg = cp_cfg.get("balance") or {}
    detect_cfg = cp_cfg.get("detection") or {}

    g = build_weighted_graph(schematic_path, weights=weights)
    # Pre-Louvain anchor-satellite boost — every MCU/regulator/sensor
    # edge to its direct neighbours gets multiplied so the subsystem
    # binds tightly during modularity optimization. Caller can disable
    # by setting community_partition.anchor_boost.enabled = false.
    boost_cfg = cp_cfg.get("anchor_boost") or {}
    boosted_edges = 0
    if boost_cfg.get("enabled", True):
        boosted_edges = boost_anchor_satellite_weights(
            g, classified,
            boost_multiplier=float(boost_cfg.get("multiplier", 2.5)),
        )
    # P12 — functional block edges. Detect USB / I2C / SPI / CAN /
    # crystal / debug / power sub-systems by lib_id + net patterns,
    # then inject virtual edges between same-block members so Louvain
    # naturally groups them even when raw connectivity is weak (e.g.
    # USBLC6 ESD that label-connects rather than wire-connects to the
    # USB-C). Pure additive: the existing connectivity graph is
    # preserved; we only strengthen the binding inside detected blocks.
    fb_cfg = cp_cfg.get("functional_blocks") or {}
    fb_stats: Dict[str, Any] = {}
    fb_block_by_ref: Dict[str, str] = {}
    if fb_cfg.get("enabled", True) and classified is not None:
        try:
            from . import functional_blocks as _fb
            fb_result = _fb.assign_functional_blocks(
                schematic_path, classified,
                config_root={"functional_blocks": fb_cfg},
            )
            fb_block_by_ref = fb_result.get("block_by_ref", {})
            fb_stats = fb_result.get("stats", {})
            fb_edges = fb_result.get("intra_block_edges", [])
            for a, b, w, _name in fb_edges:
                if not g.has_node(a) or not g.has_node(b):
                    continue
                if g.has_edge(a, b):
                    # Add to existing edge weight rather than replace —
                    # a wire edge that's ALSO an intra-block edge stays
                    # at least as strong as before.
                    g[a][b]["weight"] = float(g[a][b].get("weight", 0)) + w
                else:
                    g.add_edge(a, b, weight=w, nets=[],
                                kind="functional_block")
            fb_stats["edges_injected"] = len(fb_edges)
        except Exception as _exc:  # pragma: no cover — never block partition
            fb_stats = {"error": str(_exc)}
    # P15.3 — analog-domain binding. Build the power DAG, derive each
    # ref's dominant power-rail domain, and inject same-domain edges
    # so analog components cluster apart from digital ones during
    # Louvain. Mirrors the functional-block edge-injection pattern;
    # digital is the implicit default (boosting it everywhere would
    # be a no-op for partitioning).
    pi_cfg = cp_cfg.get("power_intent") or {}
    pi_stats: Dict[str, Any] = {}
    if pi_cfg.get("enabled", True) and classified is not None:
        try:
            from . import power_intent as _pi
            tree = _pi.build_power_tree(schematic_path, classified=classified)
            domain_by_ref = _pi.tag_component_domains(tree, schematic_path)
            domain_edges = _pi.domain_intra_edges(
                domain_by_ref,
                same_domain_weight=float(pi_cfg.get(
                    "analog_domain_weight", 2.0,
                )),
            )
            for a, b, w, _name in domain_edges:
                if not g.has_node(a) or not g.has_node(b):
                    continue
                if g.has_edge(a, b):
                    g[a][b]["weight"] = float(g[a][b].get("weight", 0)) + w
                else:
                    g.add_edge(a, b, weight=w, nets=[],
                                kind="power_domain")
            pi_stats = {
                "rails_typed":    tree["stats"]["rails_typed"],
                "regulators":     tree["stats"]["regulators"],
                "sources":        tree["stats"]["sources"],
                "analog_refs":    sum(1 for d in domain_by_ref.values()
                                       if d == "analog"),
                "mixed_refs":     sum(1 for d in domain_by_ref.values()
                                       if d == "mixed"),
                "edges_injected": len(domain_edges),
            }
        except Exception as _exc:  # pragma: no cover
            pi_stats = {"error": str(_exc)}
    raw_comms = partition_into_communities(g, detect_cfg)
    balanced = balance_communities(raw_comms, balance_cfg)
    names = name_communities(balanced, classified, block_by_ref=fb_block_by_ref)
    # Collapse PROTECTION + PROTECTION_2 (and similar splits) back into
    # one community — the priority-naming step tags duplicates, this
    # step actually merges them.
    if cp_cfg.get("merge_duplicate_names", True):
        balanced, names = merge_duplicate_named_communities(balanced, names)

    ref_to_comm: Dict[str, int] = {}
    for i, comm in enumerate(balanced):
        for ref in comm:
            ref_to_comm[ref] = i

    communities_out: List[Dict[str, Any]] = []
    for i, comm in enumerate(balanced):
        intra_w = 0.0
        for a in comm:
            for b in g.neighbors(a):
                if b in comm:
                    intra_w += g[a][b]["weight"] / 2  # halve double-count
        communities_out.append({
            "id": i,
            "name": names.get(i, f"CLUSTER_{i + 1}"),
            "members": sorted(comm),
            "size": len(comm),
            "intra_weight": round(intra_w, 2),
        })

    cross: List[Dict[str, Any]] = []
    for a, b, data in g.edges(data=True):
        ca = ref_to_comm.get(a)
        cb = ref_to_comm.get(b)
        if ca is None or cb is None or ca == cb:
            continue
        cross.append({
            "a": a, "b": b,
            "a_community": ca, "b_community": cb,
            "weight": round(data["weight"], 2),
            "nets": data.get("nets") or [],
        })

    return {
        "graph_stats": {
            "nodes": g.number_of_nodes(),
            "edges": g.number_of_edges(),
            "total_weight": round(sum(d["weight"] for _a, _b, d in g.edges(data=True)), 2),
            "raw_communities": len(raw_comms),
            "balanced_communities": len(balanced),
            "anchor_edges_boosted": boosted_edges,
            "functional_blocks": fb_stats,
            "power_intent":      pi_stats,
        },
        "communities": communities_out,
        "cross_cluster_edges": cross,
    }
