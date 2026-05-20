"""Unified circuit-complexity scoring — the single number that drives
sheet-split decisions, density expansion, and future routing-budget calls.

The score combines four signals, each with a config-tunable weight:

    score =   w_count   * normalized_component_count
            + w_density * net_density
            + w_cross   * cross_connections_ratio
            + w_text    * text_density

All four components are NORMALISED into [0, 1] before weighting so
weights have a consistent meaning regardless of circuit class. The
final score lives in [0, sum_of_weights] — caller treats it as
a relative scalar.

Pure data — no part-number knowledge, no role-specific branches.
Works for any circuit class.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional


def _normalize_count(n: int, soft_cap: int = 100) -> float:
    """Map raw component count into [0, 1] with saturating curve.
    Values up to `soft_cap` scale linearly; beyond, asymptote to 1.0.
    A 50-component design scores 0.5, a 200-component design scores ~0.9."""
    if n <= 0:
        return 0.0
    return min(1.0, n / float(soft_cap))


def _net_density(nodes: int, edges: int) -> float:
    """Edge-per-node ratio normalised. A pure star (1 hub + N leaves)
    has density ~1; a richly interconnected graph (mesh) ~2-3.
    Result clipped to [0, 1] with /3.0 normalization."""
    if nodes <= 0:
        return 0.0
    return min(1.0, (edges / nodes) / 3.0)


def _cross_connections_ratio(role_counts: Counter, total_components: int) -> float:
    """Approximation of cross-block traffic. Higher role diversity (many
    distinct functional blocks each with a few members) means more
    cross-block nets; a single-role circuit means everything's intra-block.

    Formula: (distinct_roles / total_components) capped at 0.5, then
    scaled. A 6-role 30-component board scores ~0.2; a 12-role 50-
    component board ~0.24; pure-passive (1 role) ~0."""
    if total_components <= 0:
        return 0.0
    distinct_roles = len(role_counts)
    raw = distinct_roles / total_components
    return min(1.0, raw / 0.4)


def _text_density(label_count: int, total_components: int) -> float:
    """Net-label-per-component ratio. Designs with many named signals
    (named MCU pins, bus protocols) crowd the sheet more than designs
    with just power and discretes. Normalised by /5.0 (a typical MCU
    sheet has 2-4 labels per component)."""
    if total_components <= 0:
        return 0.0
    return min(1.0, (label_count / total_components) / 5.0)


def compute_complexity(
    *,
    placement: Optional[Dict[str, Any]] = None,
    classified: Optional[Dict[str, Any]] = None,
    routed: Optional[Dict[str, Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compute a single complexity scalar plus its breakdown.

    Inputs are optional — the function uses whatever data is available:
      - placement.components → component count + role counts
      - classified.edges    → net density (edges per node)
      - routed.net_labels   → text density

    Returns dict with:
      score       — total scalar (0..sum_of_weights)
      components  — N
      roles       — distinct role count
      net_density — edges/node
      breakdown   — per-signal contribution
    """
    cfg = cfg or {}
    weights = cfg.get("weights") or {}
    w_count   = float(weights.get("count",   2.0))
    w_density = float(weights.get("density", 1.5))
    w_cross   = float(weights.get("cross",   1.5))
    w_text    = float(weights.get("text",    1.0))
    soft_cap  = int(cfg.get("soft_cap_components", 100))

    components: List[Dict[str, Any]] = (
        (placement or {}).get("components", []) or []
    )
    n_components = len(components)
    role_counts = Counter(c.get("role", "GENERIC") for c in components
                          if c.get("role"))

    nodes_in_graph = 0
    edges_in_graph = 0
    if classified is not None:
        nodes_in_graph = len(classified.get("nodes") or [])
        edges_in_graph = len(classified.get("edges") or [])

    label_count = 0
    if routed is not None:
        label_count = len(routed.get("net_labels") or [])

    s_count   = _normalize_count(n_components, soft_cap)
    s_density = _net_density(nodes_in_graph, edges_in_graph)
    s_cross   = _cross_connections_ratio(role_counts, n_components)
    s_text    = _text_density(label_count, n_components)

    contributions = {
        "count":   w_count   * s_count,
        "density": w_density * s_density,
        "cross":   w_cross   * s_cross,
        "text":    w_text    * s_text,
    }
    total = sum(contributions.values())

    return {
        "score":       round(total, 4),
        "max_score":   round(w_count + w_density + w_cross + w_text, 4),
        "components":  n_components,
        "roles":       len(role_counts),
        "net_density": round(s_density, 4),
        "breakdown":   {k: round(v, 4) for k, v in contributions.items()},
    }
