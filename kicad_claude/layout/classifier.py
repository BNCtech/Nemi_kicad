"""Step 2 of the universal layout engine: functional-role classification.

Reads the graph JSON produced by Step 1 (connectivity_graph) and assigns
each node a functional role (MAIN_CONTROLLER, POWER, CRYSTAL, RESET,
I2C, SPI, UART, USB, ...). Every pattern lives in classifier_config.json
— this module is pure dispatch over those rules. No component-specific
logic in code; new IC families ship by editing JSON only.

Nodes that fail every rule come out as GENERIC. That set is the natural
hand-off point for an LLM fallback later: it's small, bounded, and only
the genuinely unknown parts need a Claude call.

CLI accepts either a graph.json from Step 1 or a raw .kicad_sch (in
which case Step 1 runs in-memory first).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import load_config


def _compile_net_patterns(raw: Dict[str, Any]) -> Dict[str, List[re.Pattern]]:
    """A pattern starting with `^` becomes a prefix match; otherwise a plain
    substring. Both are case-insensitive. We compile once so role-voting per
    node is a single regex.search per pattern. Keys starting with '_' are
    config-file comments (`_comment`, `_about`) and are skipped."""
    out: Dict[str, List[re.Pattern]] = {}
    for role, patterns in raw.items():
        if role.startswith("_") or not isinstance(patterns, list):
            continue
        compiled: List[re.Pattern] = []
        for p in patterns:
            if p.startswith("^"):
                rgx = re.compile(p, re.IGNORECASE)
            else:
                rgx = re.compile(re.escape(p), re.IGNORECASE)
            compiled.append(rgx)
        out[role] = compiled
    return out


def _signal_nets_per_node(graph_dict: Dict[str, Any]) -> Dict[str, Set[str]]:
    """For every node, the set of non-power nets it touches (signal edges
    only — power nets were already filtered out in Step 1)."""
    nets_by_node: Dict[str, Set[str]] = {n["ref"]: set() for n in graph_dict["nodes"]}
    for e in graph_dict["edges"]:
        for net in e.get("nets") or []:
            if not net:
                continue
            nets_by_node.setdefault(e["a"], set()).add(net)
            nets_by_node.setdefault(e["b"], set()).add(net)
    return nets_by_node


def _vote_from_nets(nets: Set[str], compiled: Dict[str, List[re.Pattern]]) -> Dict[str, int]:
    votes: Dict[str, int] = {}
    for role, regexes in compiled.items():
        hits = 0
        for net in nets:
            if any(rgx.search(net) for rgx in regexes):
                hits += 1
        if hits:
            votes[role] = votes.get(role, 0) + hits
    return votes


def _vote_from_substring(haystack: str, table: Dict[str, Any]) -> Dict[str, int]:
    """Keys starting with '_' are config comments and skipped."""
    votes: Dict[str, int] = {}
    if not haystack:
        return votes
    hs = haystack.lower()
    for role, patterns in table.items():
        if role.startswith("_") or not isinstance(patterns, list):
            continue
        for p in patterns:
            if p.lower() in hs:
                votes[role] = votes.get(role, 0) + 1
    return votes


def _vote_from_refdes(ref: str, table: Dict[str, Any]) -> Optional[str]:
    """Longest-prefix-wins single vote. Keys starting with '_' are skipped."""
    if not ref:
        return None
    best_role: Optional[str] = None
    best_len = -1
    for prefix, role in table.items():
        if prefix.startswith("_") or not isinstance(role, str):
            continue
        if ref.startswith(prefix) and len(prefix) > best_len:
            best_role = role
            best_len = len(prefix)
    return best_role


def _lib_id_short(lib_id: str) -> str:
    """Drop the library namespace ('pic_programmer:BC307' -> 'BC307'). Custom
    libraries often shadow the standard ones with the same part-suffix; the
    short form is what most family-table substring rules expect."""
    if ":" in lib_id:
        return lib_id.split(":", 1)[1]
    return lib_id


def _vote_from_pin_geometry(
    node: Dict[str, Any], rules: List[Dict[str, Any]],
) -> Optional[str]:
    """First-match-wins over an ORDERED rule list. Each rule is a dict:
      {"min_pins": int, "max_pins": int, "requires_power": bool,
       "role": str, "refdes_prefix": optional str}

    Pin geometry alone never disambiguates passive-vs-discrete (a 2-pin
    body could be R, C, L, or D); rules are designed to be CONSERVATIVE —
    only emit a role when the geometry plus optional refdes hint genuinely
    nails it (e.g. 'U-prefix + ≥8 pins + power-rail-touching' is solid
    evidence of an IC even when lib_id is unknown)."""
    pin_count = int(node.get("pin_count", 0))
    has_power = bool(node.get("power_rails"))
    ref = node.get("ref", "")
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if pin_count < int(rule.get("min_pins", 0)):
            continue
        if pin_count > int(rule.get("max_pins", 10_000)):
            continue
        if rule.get("requires_power") and not has_power:
            continue
        pfx = rule.get("refdes_prefix")
        if pfx and not ref.startswith(pfx):
            continue
        role = rule.get("role")
        if isinstance(role, str):
            return role
    return None


def _vote_from_value_regex(value: str, table: Dict[str, Any]) -> Optional[str]:
    """role -> list of regex strings. First role with any match wins (caller
    can pre-order roles by specificity). Returns None on no match."""
    if not value:
        return None
    for role, patterns in table.items():
        if role.startswith("_") or not isinstance(patterns, list):
            continue
        for p in patterns:
            try:
                if re.search(p, value, re.IGNORECASE):
                    return role
            except re.error:
                continue
    return None


def _classify_layers(
    node: Dict[str, Any], signal_nets: Set[str],
    cfg: Dict[str, Any],
    net_compiled: Dict[str, List[re.Pattern]],
    lib_table: Dict[str, Any], refdes_table: Dict[str, Any],
    value_table: Dict[str, Any], value_regex_table: Dict[str, Any],
    pin_rules: List[Dict[str, Any]],
) -> List[Tuple[str, float, str]]:
    """Run every signal layer and return (role, trust, layer_name) tuples
    for layers that produced a verdict. Empty list means no layer fired —
    the caller routes the node to GENERIC + LLM fallback."""
    trust = (cfg.get("trust_layers") or {}).get("weights") or {}
    t_net    = float(trust.get("net",          1.0))
    t_lib    = float(trust.get("lib_id",       1.0))
    t_refdes = float(trust.get("refdes",       0.9))
    t_geom   = float(trust.get("pin_geometry", 0.8))
    t_val    = float(trust.get("value",        0.7))

    out: List[Tuple[str, float, str]] = []

    net_votes = _vote_from_nets(signal_nets, net_compiled)
    if net_votes:
        out.append((max(net_votes, key=net_votes.get), t_net, "net"))

    lib_id_full = node.get("lib_id", "")
    lib_votes = _vote_from_substring(lib_id_full, lib_table)
    if not lib_votes:  # retry against short form for custom-namespace libs
        lib_votes = _vote_from_substring(_lib_id_short(lib_id_full), lib_table)
    if lib_votes:
        out.append((max(lib_votes, key=lib_votes.get), t_lib, "lib_id"))

    refdes_role = _vote_from_refdes(node.get("ref", ""), refdes_table)
    if refdes_role:
        out.append((refdes_role, t_refdes, "refdes"))

    geom_role = _vote_from_pin_geometry(node, pin_rules)
    if geom_role:
        out.append((geom_role, t_geom, "pin_geometry"))

    val_role = _vote_from_value_regex(node.get("value", ""), value_regex_table)
    if not val_role:
        val_votes = _vote_from_substring(node.get("value", ""), value_table)
        if val_votes:
            val_role = max(val_votes, key=val_votes.get)
    if val_role:
        out.append((val_role, t_val, "value"))
    return out


def _resolve_layered(
    layered: List[Tuple[str, float, str]],
    priority: List[str],
) -> Tuple[Optional[str], float, Dict[str, Any]]:
    """Highest-trust layer's role wins. Ties (multiple layers at the top
    trust) break by per-role agreement count (a role voted by lib_id AND
    refdes outranks a role from refdes alone), then by config priority.

    Returns (role, max_trust, breakdown_for_audit)."""
    if not layered:
        return None, 0.0, {"layers": []}
    by_trust: Dict[float, List[Tuple[str, str]]] = {}
    for role, trust, layer in layered:
        by_trust.setdefault(trust, []).append((role, layer))
    top_trust = max(by_trust.keys())
    top_roles = [r for r, _ in by_trust[top_trust]]
    # Count agreements ACROSS all layers (not just top trust) so a role
    # consistently voted across lib_id + refdes + geometry beats a one-shot
    # net-pattern hit.
    role_score: Dict[str, float] = {}
    for role, trust, _ in layered:
        role_score[role] = role_score.get(role, 0.0) + trust
    # Restrict to roles present at top trust, then pick the best score; if
    # still tied, fall back to priority order.
    candidates = [r for r in top_roles if r in role_score]
    if len(set(candidates)) == 1:
        return candidates[0], top_trust, {"layers": layered, "score": role_score}
    best = max(set(candidates), key=lambda r: role_score[r])
    tied = [r for r in set(candidates) if abs(role_score[r] - role_score[best]) < 1e-6]
    if len(tied) == 1:
        return tied[0], top_trust, {"layers": layered, "score": role_score}
    for role in priority:
        if role in tied:
            return role, top_trust, {"layers": layered, "score": role_score}
    return tied[0], top_trust, {"layers": layered, "score": role_score}


def _meets_main_gates(node: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """Pin-count + power+ground threshold check. Role exclusion is a separate
    layer applied in pick_main_controller — kept apart so the gates can be
    evaluated both before and after LLM role refinement."""
    if node["pin_count"] < int(cfg["min_pin_count"]):
        return False
    if not cfg.get("require_power_and_ground"):
        return True
    rails_up = {r.upper() for r in node.get("power_rails") or []}
    gnd_names = {g.upper() for g in cfg.get("ground_net_names") or []}
    has_gnd = any(any(g in r for g in gnd_names) for r in rails_up)
    has_pwr = any(r and not any(g in r for g in gnd_names) for r in rails_up)
    return has_gnd and has_pwr


def pick_main_controller(classified: Dict[str, Any]) -> Optional[str]:
    """Promote the highest-scoring eligible node to MAIN_CONTROLLER and return
    its refdes (or None for sheets with no central controller — power-only
    sheets are the canonical case). Mutates `classified["nodes"]` and
    `classified["main_controller"]` in place.

    Strips any prior MAIN_CONTROLLER label first (restoring the node's role
    from `heuristic_role_pre_main` if stored), so this function is idempotent
    and safe to call again after LLM refinement reshuffles roles."""
    main_cfg = load_config("classifier_config")["main_controller"]
    excluded = set(main_cfg.get("excluded_roles") or [])

    for n in classified["nodes"]:
        if n.get("role") == "MAIN_CONTROLLER":
            n["role"] = n.pop("heuristic_role_pre_main", "GENERIC")

    candidates = sorted(classified["nodes"], key=lambda x: -x.get("score", 0.0))
    for n in candidates:
        if n["role"] in excluded:
            continue
        if not _meets_main_gates(n, main_cfg):
            continue
        n["heuristic_role_pre_main"] = n["role"]
        n["role"] = "MAIN_CONTROLLER"
        classified["main_controller"] = n["ref"]
        return n["ref"]

    classified["main_controller"] = None
    return None


def _resolve_role(
    votes: Dict[str, int],
    refdes_vote: Optional[str],
    priority: List[str],
) -> Optional[str]:
    """Highest vote count wins; ties broken by priority order. Refdes vote
    adds 1 to whatever it picked (weak signal but enough to disambiguate)."""
    if refdes_vote:
        votes[refdes_vote] = votes.get(refdes_vote, 0) + 1
    if not votes:
        return None
    top = max(votes.values())
    tied = [r for r, v in votes.items() if v == top]
    if len(tied) == 1:
        return tied[0]
    for role in priority:
        if role in tied:
            return role
    return tied[0]


def classify(graph_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Annotate every node in `graph_dict` with `role` and `role_votes`.
    Returns a NEW dict (input is not mutated).

    When config `trust_layers.enabled` is true (default), uses the 4-layer
    trust resolver: net + lib_id (1.0), refdes (0.9), pin geometry (0.8),
    value (0.7). Below `trust_fallback_threshold` the node is forced to
    GENERIC and flagged for LLM rescue. Legacy vote-sum path remains as
    the disabled branch.

    MAIN_CONTROLLER selection runs as a SEPARATE step after role
    classification so a power-regulator cluster cannot accidentally win
    the main slot just because it scored highest."""
    cfg = load_config("classifier_config")
    net_compiled = _compile_net_patterns(cfg["net_patterns"])
    lib_table = cfg["lib_id_patterns"]
    value_table = cfg["value_patterns"]
    value_regex_table = cfg.get("value_regex_patterns") or {}
    refdes_table = cfg["refdes_patterns"]
    priority = cfg["role_priority"]["order"]
    fallback_power = bool(cfg.get("power_only_fallback", {}).get("enabled", True))
    trust_cfg = cfg.get("trust_layers") or {}
    trust_mode = bool(trust_cfg.get("enabled", True))
    trust_threshold = float(trust_cfg.get("fallback_threshold", 0.7))
    pin_rules = (cfg.get("pin_geometry_rules") or {}).get("rules") or []

    nets_by_node = _signal_nets_per_node(graph_dict)

    out_nodes: List[Dict[str, Any]] = []
    for node in graph_dict["nodes"]:
        ref = node["ref"]
        signal_nets = nets_by_node.get(ref, set())

        if trust_mode:
            layered = _classify_layers(
                node, signal_nets, cfg, net_compiled, lib_table,
                refdes_table, value_table, value_regex_table, pin_rules,
            )
            role, max_trust, breakdown = _resolve_layered(layered, priority)
            llm_candidate = max_trust < trust_threshold or role is None
            if role is None:
                if fallback_power and not signal_nets and node.get("power_rails"):
                    role = "POWER"
                    llm_candidate = False
                else:
                    role = "GENERIC"

            # Seed heuristic_role_pre_main so pick_main_controller's
            # strip-and-revert leaves the role unchanged for trust-mode
            # MAIN_CONTROLLERs. Without this, the pop() default of
            # "GENERIC" silently demotes every classified MAIN_CONTROLLER
            # before the picker re-promotes ONE — breaking multi-MCU
            # detection because all but one MCU end up as GENERIC.
            entry = {
                **node,
                "signal_nets": sorted(signal_nets),
                "role": role,
                "role_trust": max_trust,
                "role_breakdown": breakdown,
                "llm_candidate": llm_candidate,
            }
            if role == "MAIN_CONTROLLER":
                entry["heuristic_role_pre_main"] = "MAIN_CONTROLLER"
            out_nodes.append(entry)
            continue

        # Legacy vote-sum path (kept for A/B comparison; disable via
        # trust_layers.enabled=false in classifier_config.json).
        net_votes = _vote_from_nets(signal_nets, net_compiled)
        lib_votes = _vote_from_substring(node.get("lib_id", ""), lib_table)
        val_votes = _vote_from_substring(node.get("value", ""), value_table)
        refdes_vote = _vote_from_refdes(ref, refdes_table)

        combined: Dict[str, int] = {}
        for src in (net_votes, lib_votes, val_votes):
            for role, count in src.items():
                combined[role] = combined.get(role, 0) + count

        role = _resolve_role(dict(combined), refdes_vote, priority)
        if role is None:
            if fallback_power and not signal_nets and node.get("power_rails"):
                role = "POWER"
            else:
                role = "GENERIC"

        out_nodes.append({
            **node,
            "signal_nets": sorted(signal_nets),
            "role": role,
            "role_votes": combined,
        })

    result = {
        "main_controller": None,
        "nodes": out_nodes,
        "edges": graph_dict["edges"],
    }
    pick_main_controller(result)
    return result


def _build_graph_dict_from_schematic(path) -> Dict[str, Any]:
    from .connectivity_graph import build_graph, graph_to_dict
    return graph_to_dict(build_graph(path))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.classifier")
    ap.add_argument("input", help="graph.json (Step 1 output) or a .kicad_sch")
    ap.add_argument("--out", help="write classified JSON here (default: stdout)")
    ap.add_argument(
        "--summary", action="store_true",
        help="print a one-line-per-node summary to stderr",
    )
    args = ap.parse_args(argv)

    in_path = Path(args.input)
    if in_path.suffix == ".kicad_sch":
        graph_dict = _build_graph_dict_from_schematic(in_path)
    else:
        with open(in_path, "r", encoding="utf-8") as f:
            graph_dict = json.load(f)

    classified = classify(graph_dict)
    payload = json.dumps(classified, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        roles: Dict[str, int] = {}
        for n in classified["nodes"]:
            roles[n["role"]] = roles.get(n["role"], 0) + 1
        role_summary = ", ".join(f"{k}={v}" for k, v in sorted(roles.items(), key=lambda x: -x[1]))
        print(
            f"wrote {args.out}  main={classified['main_controller']}  "
            f"roles: {role_summary}"
        )
    else:
        sys.stdout.write(payload + "\n")

    if args.summary:
        for n in sorted(classified["nodes"], key=lambda x: (x["role"], -x.get("score", 0))):
            sys.stderr.write(
                f"  {n['ref']:6s} {n['role']:16s} pins={n['pin_count']:2d} "
                f"deg={n['degree']:2d} score={n.get('score', 0):.2f}  "
                f"lib={n.get('lib_id', '')[:32]}\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
