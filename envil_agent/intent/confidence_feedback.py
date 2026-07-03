"""intent/confidence_feedback.py — close the reasoning->outcome loop.

pcb_reasoning gives every component a role + confidence (Phase 1). This module
feeds the ROUTING outcome back: after a board is routed and DRC'd, the routing-
category violations are attributed to the components on the offending nets, and
those components' role confidence is reduced. The penalty is persisted keyed by
lib_id, so a part whose role keeps causing routing conflicts is trusted less on
the next board — adaptive reasoning.

Design (per the review):
  * ONLY routing-category DRC counts against a role. A starved_thermal is the
    zone engine's fault, not evidence the role was misread — so it never lowers
    confidence. Category map = config/drc_categories.json.
  * The learned prior is keyed by lib_id (stable across boards); refdes is not.
  * Everything is gated. apply_learned_bias off, or an empty store, leaves
    pcb_reasoning byte-identical. Never raises.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_REFDES = re.compile(r"\b([A-Z]{1,3}\d+)\b")


def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _load_json(name: str) -> Dict[str, Any]:
    try:
        return json.loads((_config_dir() / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def load_cfg() -> Dict[str, Any]:
    return _load_json("confidence_feedback.json")


def is_enabled() -> bool:
    return bool(load_cfg().get("enabled", False))


def _catmap() -> Dict[str, str]:
    return _load_json("drc_categories.json").get("type_to_category", {}) or {}


# --------------------------------------------------------------------------- #
# Attribution: which components does each routing failure implicate?
# --------------------------------------------------------------------------- #

def attribute_failures(components: List[Dict[str, Any]],
                       drc_issues: List[Dict[str, Any]]) -> Dict[str, int]:
    """Return {ref: implicated_routing_error_count}. A component is implicated by
    a routing-category error when its refdes appears in the violation's item text
    OR one of its nets does. Uses the reasoning report's per-component `nets`."""
    cfg = load_cfg().get("attribution", {}) or {}
    penal_cats = set(cfg.get("penalize_categories", ["routing"]))
    catmap = _catmap()

    net_to_refs: Dict[str, set] = {}
    ref_set = set()
    for c in components:
        ref = c.get("ref")
        if not ref:
            continue
        ref_set.add(ref)
        for n in c.get("nets", []) or []:
            net_to_refs.setdefault(str(n), set()).add(ref)

    counts: Dict[str, int] = {}
    for v in drc_issues:
        cat = catmap.get(v.get("type", ""), "other")
        if cat not in penal_cats:
            continue
        if (v.get("severity") or "").lower() != "error":
            continue
        blob = " ".join(str(it) for it in (v.get("items") or []))
        implicated: set = set()
        for m in _REFDES.findall(blob):
            if m in ref_set:
                implicated.add(m)
        for net, refs in net_to_refs.items():
            if net and net in blob:
                implicated |= refs
        for ref in implicated:
            counts[ref] = counts.get(ref, 0) + 1
    return counts


def compute_adjustments(report: Dict[str, Any],
                        drc_issues: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per implicated component: {ref: {role, lib_id, old, new, penalty, reason}}.
    Only components with >=1 attributed routing error appear."""
    cfg = load_cfg().get("attribution", {}) or {}
    per = float(cfg.get("penalty_per_error", 0.12))
    cap = float(cfg.get("max_penalty", 0.4))
    floor = float(cfg.get("floor", 0.1))
    comps = report.get("components", []) or []
    counts = attribute_failures(comps, drc_issues)
    by_ref = {c.get("ref"): c for c in comps}
    out: Dict[str, Dict[str, Any]] = {}
    for ref, n in counts.items():
        c = by_ref.get(ref) or {}
        old = float(c.get("confidence", 0.0) or 0.0)
        penalty = min(cap, per * n)
        new = max(floor, round(old - penalty, 3))
        out[ref] = {
            "role": c.get("role", ""),
            "lib_id": c.get("lib_id", ""),
            "old": old, "new": new, "penalty": round(penalty, 3),
            "errors": n,
            "reason": f"{c.get('role','role')} classification implicated in "
                      f"{n} routing conflict(s)",
        }
    return out


# --------------------------------------------------------------------------- #
# Persistence: the learned per-lib_id prior
# --------------------------------------------------------------------------- #

def _store_path() -> Optional[Path]:
    ps = load_cfg().get("persist", {}) or {}
    if not ps.get("enabled", False):
        return None
    return _config_dir() / str(ps.get("store", "learned_confidence.json"))


def _load_store() -> Dict[str, Any]:
    p = _store_path()
    if not p or not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_store(store: Dict[str, Any]) -> bool:
    p = _store_path()
    if not p:
        return False
    try:
        p.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        return True
    except OSError:
        return False


def learned_bias(lib_id: str) -> float:
    """The persisted confidence bias (<= 0) for a lib_id, or 0.0. Applied on top
    of a fresh classification so a repeatedly-troublesome part reads less certain."""
    if not lib_id:
        return 0.0
    ap = load_cfg().get("apply_learned_bias", {}) or {}
    if not ap.get("enabled", False):
        return 0.0
    entry = _load_store().get(lib_id)
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get("bias", 0.0))
    except (TypeError, ValueError):
        return 0.0


def record_observations(report: Dict[str, Any],
                        drc_issues: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Persist this board's routing-failure evidence into the learned store,
    keyed by lib_id. Each implicated observation nudges the part's bias toward
    -max_bias by bias_per_observation; existing bias decays first so stale bad
    boards fade. Returns a summary {lib_id: {observations, bias}}. No-op (returns
    {}) when persistence is disabled."""
    ps = load_cfg().get("persist", {}) or {}
    if not ps.get("enabled", False):
        return {}
    step = float(ps.get("bias_per_observation", 0.06))
    max_bias = float(ps.get("max_bias", 0.3))
    decay = float(ps.get("decay", 0.9))

    adjustments = compute_adjustments(report, drc_issues)
    if not adjustments:
        return {}
    store = _load_store()
    summary: Dict[str, Any] = {}
    for ref, adj in adjustments.items():
        lib = adj.get("lib_id") or ""
        if not lib:
            continue
        entry = store.get(lib) or {"observations": 0, "bias": 0.0}
        prior = float(entry.get("bias", 0.0)) * decay      # decay old evidence
        new_bias = max(-max_bias, round(prior - step * int(adj.get("errors", 1)), 3))
        entry["observations"] = int(entry.get("observations", 0)) + 1
        entry["bias"] = new_bias
        entry["last_role"] = adj.get("role", "")
        entry["last_reason"] = adj.get("reason", "")
        store[lib] = entry
        summary[lib] = {"observations": entry["observations"], "bias": new_bias}
    _save_store(store)
    return summary
