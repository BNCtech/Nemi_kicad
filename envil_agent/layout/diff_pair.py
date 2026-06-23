"""Differential-pair detection + length math.

Reads a `.kicad_pcb` s-expression, walks `(net ...)` and `(segment ...)`
entries, and produces per-net length totals. Pair detection uses the
patterns in `config/diff_pair_rules.json` (positive/negative exact names,
or positive_suffix/negative_suffix with optional name_prefix).

v1 is detection + reporting only. Routing is a follow-up that needs
deeper integration with `tools/route_pcb_simple.py`.

Universal --- no per-board hardcoding. All thresholds live in JSON.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata


_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "diff_pair_rules.json"
)


def load_rules(path: Optional[Path] = None) -> dict:
    """Read diff_pair_rules.json. Returns {} on read or parse error so
    callers can fall back to the empty pattern list."""
    try:
        return json.loads((path or _CONFIG_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _seg_length_mm(seg: list) -> float:
    """Pull (start x y) and (end x y) from a (segment ...) node and
    return its Euclidean length in mm. KiCad stores coords in mm."""
    sx = sy = ex = ey = 0.0
    have_start = have_end = False
    for child in seg[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)
        if h == "start" and len(child) >= 3:
            try:
                sx, sy = float(child[1]), float(child[2])
                have_start = True
            except (TypeError, ValueError):
                pass
        elif h == "end" and len(child) >= 3:
            try:
                ex, ey = float(child[1]), float(child[2])
                have_end = True
            except (TypeError, ValueError):
                pass
    if not (have_start and have_end):
        return 0.0
    return math.hypot(ex - sx, ey - sy)


def _seg_net_id(seg: list) -> Optional[int]:
    for child in seg[1:]:
        if isinstance(child, list) and _head(child) == "net" and len(child) >= 2:
            try:
                return int(child[1])
            except (TypeError, ValueError):
                return None
    return None


def parse_pcb(sch_path: Path) -> Tuple[Dict[int, str], Dict[str, float], Dict[str, str]]:
    """Walk a .kicad_pcb. Returns:
      (net_id_to_name, net_name_to_total_length_mm, net_name_to_layer)

    Layer is the layer of the FIRST segment encountered for that net
    --- good enough for the v1 report which just wants to flag pairs
    routed on different layers (a length-matching red flag)."""
    text = sch_path.read_text(encoding="utf-8")
    root = sexpdata.loads(text)
    if not isinstance(root, list) or _head(root) != "kicad_pcb":
        return {}, {}, {}

    net_id_to_name: Dict[int, str] = {}
    net_total_mm: Dict[str, float] = defaultdict(float)
    net_layer: Dict[str, str] = {}
    for child in root[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)
        if h == "net" and len(child) >= 3:
            try:
                nid = int(child[1])
                # net name comes through as a string OR sexpdata.Symbol
                raw = child[2]
                if isinstance(raw, sexpdata.Symbol):
                    name = raw.value()
                else:
                    name = str(raw).strip('"')
                net_id_to_name[nid] = name
            except (TypeError, ValueError):
                continue
        elif h == "segment":
            nid = _seg_net_id(child)
            if nid is None:
                continue
            name = net_id_to_name.get(nid)
            # The `net` declarations appear before segments in the
            # file, so the lookup above usually succeeds. When it
            # doesn't (malformed file), skip the segment.
            if not name:
                continue
            net_total_mm[name] += _seg_length_mm(child)
            if name not in net_layer:
                # Layer field on a segment is `(layer "F.Cu")` etc.
                for sub in child[1:]:
                    if (isinstance(sub, list) and _head(sub) == "layer"
                            and len(sub) >= 2):
                        raw = sub[1]
                        net_layer[name] = (
                            raw.value()
                            if isinstance(raw, sexpdata.Symbol)
                            else str(raw).strip('"')
                        )
                        break
    return net_id_to_name, dict(net_total_mm), net_layer


def _match_pattern(rule: dict, all_net_names: List[str]) -> List[Tuple[str, str, str]]:
    """Apply ONE rule from diff_pair_rules.json to the net list.
    Returns [(positive_net, negative_net, class_name), ...].

    Supported pattern shapes:
      a) exact name pair:
         {"positive": "USB_DP", "negative": "USB_DM", "class": "DIFF_USB"}
      b) suffix pair with optional name_prefix:
         {"positive_suffix": "_P", "negative_suffix": "_N",
          "name_prefix": "ETH_", "class": "DIFF_ETH"}
    """
    cls = str(rule.get("class") or "DIFF_GENERIC")
    pos_exact = rule.get("positive")
    neg_exact = rule.get("negative")
    if pos_exact and neg_exact:
        if pos_exact in all_net_names and neg_exact in all_net_names:
            return [(str(pos_exact), str(neg_exact), cls)]
        return []

    pos_suffix = rule.get("positive_suffix")
    neg_suffix = rule.get("negative_suffix")
    if not (pos_suffix and neg_suffix):
        return []
    name_prefix = rule.get("name_prefix") or ""
    out: List[Tuple[str, str, str]] = []
    for name in all_net_names:
        if name_prefix and not name.startswith(name_prefix):
            continue
        if not name.endswith(pos_suffix):
            continue
        stem = name[: -len(pos_suffix)]
        complement = stem + neg_suffix
        if complement in all_net_names:
            out.append((name, complement, cls))
    return out


def detect_pairs(
    net_names: List[str], rules: Optional[dict] = None,
) -> List[Tuple[str, str, str, dict]]:
    """Run every pattern in `diff_pair_rules.json:patterns` against the
    PCB's net list. Returns [(pos, neg, class, rule), ...] preserving
    rule order so earlier rules take precedence."""
    cfg = rules if rules is not None else load_rules()
    patterns = cfg.get("patterns") or []
    seen: set = set()
    out: List[Tuple[str, str, str, dict]] = []
    for rule in patterns:
        for pos, neg, cls in _match_pattern(rule, net_names):
            key = tuple(sorted((pos, neg)))
            if key in seen:
                continue
            seen.add(key)
            out.append((pos, neg, cls, rule))
    return out


def plan_routes(pcb_path: Path) -> Dict[str, Any]:
    """Plan-only sibling of `audit_pcb`. Builds a routing PROPOSAL per
    detected diff-pair --- recommended width/gap/skew/layer based on
    the rule class --- WITHOUT mutating the PCB.

    Useful for human-in-the-loop routing: the operator can review the
    plan, then route by hand in pcbnew. A future v2 tool can execute
    the plan via `route_pcb_simple` primitives.

    Result shape:
      {
        "path": "...",
        "plan": [
          {
            "positive": "USB_DP",
            "negative": "USB_DM",
            "class": "DIFF_USB",
            "current_state": {
              "positive_length_mm": 17.85,
              "negative_length_mm": 17.91,
              "skew_mm": 0.06,
              "positive_layer": "F.Cu",
              "negative_layer": "F.Cu",
              "same_layer": true
            },
            "recommendation": {
              "preferred_width_mm": 0.18,
              "preferred_gap_mm": 0.10,
              "max_skew_mm": 0.15,
              "preferred_layer": "F.Cu",
              "rationale": "DIFF_USB class --- 90 ohm differential, tight skew."
            },
            "action": "ok" | "rework_skew" | "rework_layer" | "rework_both"
          },
          ...
        ]
      }
    """
    try:
        net_id_to_name, net_total_mm, net_layer = parse_pcb(pcb_path)
    except Exception as exc:
        return {
            "path": str(pcb_path),
            "plan": [],
            "errors": [f"PCB parse failed: {type(exc).__name__}: {exc}"],
        }
    if not net_id_to_name:
        return {
            "path": str(pcb_path),
            "plan": [],
            "errors": ["no net declarations found"],
        }
    rules_cfg = load_rules()
    pairs = detect_pairs(list(net_id_to_name.values()), rules_cfg)

    plan: List[Dict[str, Any]] = []
    for pos, neg, cls, rule in pairs:
        pos_len = net_total_mm.get(pos, 0.0)
        neg_len = net_total_mm.get(neg, 0.0)
        skew = abs(pos_len - neg_len)
        max_skew = float(rule.get("max_skew_mm", 0.25))
        pos_layer = net_layer.get(pos, "")
        neg_layer = net_layer.get(neg, "")
        same_layer = bool(pos_layer) and pos_layer == neg_layer
        rework_skew = skew > max_skew
        rework_layer = bool(pos_layer) and bool(neg_layer) and not same_layer
        if rework_skew and rework_layer:
            action = "rework_both"
        elif rework_skew:
            action = "rework_skew"
        elif rework_layer:
            action = "rework_layer"
        else:
            action = "ok"
        rationale = (rule.get("_comment") or f"{cls} class").strip()
        plan.append({
            "positive": pos,
            "negative": neg,
            "class": cls,
            "current_state": {
                "positive_length_mm": round(pos_len, 3),
                "negative_length_mm": round(neg_len, 3),
                "skew_mm": round(skew, 3),
                "positive_layer": pos_layer,
                "negative_layer": neg_layer,
                "same_layer": same_layer,
            },
            "recommendation": {
                "preferred_width_mm": float(rule.get("preferred_width_mm", 0.2)),
                "preferred_gap_mm": float(rule.get("preferred_gap_mm", 0.15)),
                "max_skew_mm": max_skew,
                "preferred_layer": pos_layer or "F.Cu",
                "rationale": rationale,
            },
            "action": action,
        })
    return {
        "path": str(pcb_path),
        "plan": plan,
        "errors": [],
    }


def audit_pcb(pcb_path: Path) -> Dict[str, Any]:
    """Top-level audit: read PCB, detect pairs, compute skew, classify
    pass/fail per pair against the rule's `max_skew_mm`.

    Result shape (JSON-serialisable):
      {
        "path": "...",
        "pair_count": N,
        "pairs": [
          {
            "positive": "USB_DP",
            "negative": "USB_DM",
            "class": "DIFF_USB",
            "positive_length_mm": 17.85,
            "negative_length_mm": 17.91,
            "skew_mm": 0.06,
            "max_skew_mm": 0.15,
            "ok": true,
            "positive_layer": "F.Cu",
            "negative_layer": "F.Cu",
            "same_layer": true
          },
          ...
        ],
        "errors": [str, ...],
        "warnings": [str, ...]
      }
    """
    errors: List[str] = []
    warnings: List[str] = []
    try:
        net_id_to_name, net_total_mm, net_layer = parse_pcb(pcb_path)
    except Exception as exc:
        return {
            "path": str(pcb_path),
            "pair_count": 0,
            "pairs": [],
            "errors": [f"PCB parse failed: {type(exc).__name__}: {exc}"],
            "warnings": [],
        }
    if not net_id_to_name:
        return {
            "path": str(pcb_path),
            "pair_count": 0,
            "pairs": [],
            "errors": ["no net declarations found --- is this a real .kicad_pcb?"],
            "warnings": [],
        }
    rules_cfg = load_rules()
    all_names = list(net_id_to_name.values())
    pairs = detect_pairs(all_names, rules_cfg)

    out_pairs: List[Dict[str, Any]] = []
    for pos, neg, cls, rule in pairs:
        pos_len = net_total_mm.get(pos, 0.0)
        neg_len = net_total_mm.get(neg, 0.0)
        skew = abs(pos_len - neg_len)
        max_skew = float(rule.get("max_skew_mm", 0.25))
        pos_layer = net_layer.get(pos, "")
        neg_layer = net_layer.get(neg, "")
        same_layer = bool(pos_layer) and pos_layer == neg_layer
        ok = skew <= max_skew and same_layer
        out_pairs.append({
            "positive": pos,
            "negative": neg,
            "class": cls,
            "positive_length_mm": round(pos_len, 3),
            "negative_length_mm": round(neg_len, 3),
            "skew_mm": round(skew, 3),
            "max_skew_mm": max_skew,
            "ok": ok,
            "positive_layer": pos_layer,
            "negative_layer": neg_layer,
            "same_layer": same_layer,
        })
        if not same_layer and pos_layer and neg_layer:
            warnings.append(
                f"pair {pos}/{neg} routed on different layers "
                f"({pos_layer} / {neg_layer}) -- impedance mismatch risk"
            )
        if skew > max_skew:
            warnings.append(
                f"pair {pos}/{neg} skew {skew:.2f} mm > tolerance "
                f"{max_skew:.2f} mm (class {cls})"
            )

    return {
        "path": str(pcb_path),
        "pair_count": len(out_pairs),
        "pairs": out_pairs,
        "errors": errors,
        "warnings": warnings,
    }
