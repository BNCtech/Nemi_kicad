"""Push the Envil/IPC DEFAULT rules INTO the KiCad project.

Companion to user_rules.py. Where user_rules.py is the per-user OVERLAY, this
module is the DEFAULT layer that is always present: it takes the IPC defaults
(ipc_constraints.json) and turns them into things KiCad's Design Rule Editor can
show —

  * flat acceptance-class floors  -> board.design_settings.rules (Constraints page)
  * IPC-2221B voltage clearance   -> custom rules in <project>.kicad_dru

Generated live from ipc_constraints.json (no numbers duplicated here) and gated
by config/default_rules.json. Sits UNDER the user overlay (USER > DEFAULT).
"""
from __future__ import annotations

from typing import Any, Dict, List

from .user_rules import _load_json  # shared loader


def load_cfg() -> Dict[str, Any]:
    return _load_json("default_rules.json")


def is_enabled() -> bool:
    return bool(load_cfg().get("enabled", False))


def emit_to_project() -> bool:
    c = load_cfg()
    return bool(c.get("enabled", False) and c.get("emit_to_project", False))


def apply_on_create_project() -> bool:
    c = load_cfg()
    return bool(c.get("enabled", False) and c.get("apply_on_create_project", False))


def _ipc() -> Dict[str, Any]:
    return _load_json("ipc_constraints.json")


def class_floors() -> Dict[str, float]:
    """Acceptance-class minimums as {kicad_rules_key: value}, to be max()'d onto
    the Constraints page. Uses ipc_constraints default_acceptance_class."""
    cfg = (load_cfg().get("acceptance_class_floors") or {})
    if not cfg.get("enabled", False):
        return {}
    ipc = _ipc()
    cls = str(ipc.get("default_acceptance_class", 2))
    body = (ipc.get("acceptance_classes", {}) or {}).get(cls, {}) or {}
    out: Dict[str, float] = {}
    for ipc_field, kicad_key in (cfg.get("map") or {}).items():
        if ipc_field in body:
            try:
                v = float(body[ipc_field])
            except (TypeError, ValueError):
                continue
            if v > 0:
                out[kicad_key] = v
    return out


def voltage_clearance_dru_rules() -> List[Dict[str, Any]]:
    """Generate IPC-2221B clearance-by-voltage rules as directive dicts (same
    shape user_rules.emit_dru consumes). One per distinct clearance tier, keyed
    on a high-voltage net class the user assigns their net to."""
    cfg = (load_cfg().get("voltage_clearance_rules") or {})
    if not cfg.get("enabled", False):
        return []
    bands = ((_ipc().get("clearance_by_voltage", {}) or {}).get("bands") or [])
    if not bands:
        return []
    field_key = str(cfg.get("condition_field", "external_uncoated")) + "_mm"
    min_v = float(cfg.get("min_voltage_v", 30))
    prefix = str(cfg.get("netclass_prefix", "HV_"))

    # For each distinct clearance value, the highest voltage that still uses it
    # -> one clean tier (e.g. 0.6mm up to 150V, 1.25mm up to 300V, 2.5mm to 500V).
    tier_top_v: Dict[float, int] = {}
    for b in bands:
        try:
            v = int(b.get("max_v"))
            val = float(b.get(field_key))
        except (TypeError, ValueError):
            continue
        if v <= min_v:
            continue
        if val not in tier_top_v or v > tier_top_v[val]:
            tier_top_v[val] = v

    rules: List[Dict[str, Any]] = []
    for val, topv in sorted(tier_top_v.items()):
        netclass = f"{prefix}{topv}V"
        rules.append({
            "name": f"ipc2221_{netclass}",
            "condition": f"A.NetClass == '{netclass}'",
            "dru": "clearance",
            "bound": "min",
            "value": val,
            "unit": "mm",
            "disallow": None,
            "text": (f"IPC-2221B clearance for nets up to {topv} V "
                     f"(assign such a net to net class '{netclass}')"),
        })
    return rules
