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


def _voltage_tiers() -> List[tuple]:
    """Collapse the IPC-2221B clearance-by-voltage table into distinct clearance
    tiers as [(top_v, clearance_mm), …] sorted by top_v. Single source of truth
    shared by voltage_clearance_dru_rules() (which emits one .kicad_dru rule per
    tier) and netclass_for_voltage() (which maps a measured net voltage to the
    matching tier's class). Returns [] when the feature is disabled/unconfigured."""
    cfg = (load_cfg().get("voltage_clearance_rules") or {})
    if not cfg.get("enabled", False):
        return []
    bands = ((_ipc().get("clearance_by_voltage", {}) or {}).get("bands") or [])
    if not bands:
        return []
    field_key = str(cfg.get("condition_field", "external_uncoated")) + "_mm"
    min_v = float(cfg.get("min_voltage_v", 30))
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
    return sorted(((topv, val) for val, topv in tier_top_v.items()))


def _voltage_prefix() -> str:
    return str((load_cfg().get("voltage_clearance_rules") or {}).get(
        "netclass_prefix", "HV_"))


def netclass_for_voltage(volts: float) -> str:
    """Map a measured/parsed net voltage to the HV_* net class whose clearance
    tier covers it (smallest tier with top_v >= volts). Returns "" for voltages
    at/below min_voltage_v — those are already covered by the fab/default
    clearance and need no HV class. Config-consistent with the DRU emitter."""
    try:
        v = abs(float(volts))
    except (TypeError, ValueError):
        return ""
    min_v = float((load_cfg().get("voltage_clearance_rules") or {}).get(
        "min_voltage_v", 30))
    if v <= min_v:
        return ""
    prefix = _voltage_prefix()
    for topv, _val in _voltage_tiers():
        if v <= topv:
            return f"{prefix}{topv}V"
    # Above the highest tabulated tier: assign the top tier (most conservative
    # available) rather than dropping it to Default.
    tiers = _voltage_tiers()
    return f"{prefix}{tiers[-1][0]}V" if tiers else ""


def voltage_clearance_dru_rules() -> List[Dict[str, Any]]:
    """Generate IPC-2221B clearance-by-voltage rules as directive dicts (same
    shape user_rules.emit_dru consumes). One per distinct clearance tier, keyed
    on a high-voltage net class the user assigns their net to."""
    tiers = _voltage_tiers()
    if not tiers:
        return []
    prefix = _voltage_prefix()
    rules: List[Dict[str, Any]] = []
    for topv, val in tiers:
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


def width_by_current_dru_rules() -> List[Dict[str, Any]]:
    """Generate IPC-2152 minimum-track-WIDTH-by-current rules as directive dicts
    (same shape user_rules.emit_dru consumes). KiCad's flat Constraints page has
    a single global min-track-width; it cannot say 'a 3 A rail must be >= 1.7 mm'.
    One rule per current band, keyed on a current net class the user assigns their
    power net to (e.g. I_3A). Read live from ipc_constraints.json:width_by_current
    so the copper-weight / temperature-rise assumptions live in one place."""
    cfg = (load_cfg().get("width_by_current_rules") or {})
    if not cfg.get("enabled", False):
        return []
    bands = ((_ipc().get("width_by_current", {}) or {}).get("bands") or [])
    if not bands:
        return []
    prefix = str(cfg.get("netclass_prefix", "I_"))
    min_a = float(cfg.get("min_current_a", 0.0))

    rules: List[Dict[str, Any]] = []
    seen: set = set()
    for b in bands:
        try:
            amps = float(b.get("max_a"))
            width = float(b.get("width_mm"))
        except (TypeError, ValueError):
            continue
        if amps <= min_a or width <= 0:
            continue
        # Format the amp label without a trailing .0 so 1.0 A -> "1A".
        amp_label = (f"{amps:g}").rstrip(".")
        netclass = f"{prefix}{amp_label}A"
        if netclass in seen:
            continue
        seen.add(netclass)
        rules.append({
            "name": f"ipc2152_{netclass}",
            "condition": f"A.NetClass == '{netclass}'",
            "dru": "track_width",
            "bound": "min",
            "value": width,
            "unit": "mm",
            "disallow": None,
            "text": (f"IPC-2152 min track width for nets up to {amp_label} A "
                     f"(assign such a net to net class '{netclass}')"),
        })
    return rules


def _current_bands() -> List[tuple]:
    """IPC-2152 current bands as [(max_a, width_mm), …] sorted by current, above
    the configured min_current_a. Shared by width_by_current_dru_rules() and
    netclass_for_current(). Returns [] when disabled/unconfigured."""
    cfg = (load_cfg().get("width_by_current_rules") or {})
    if not cfg.get("enabled", False):
        return []
    bands = ((_ipc().get("width_by_current", {}) or {}).get("bands") or [])
    min_a = float(cfg.get("min_current_a", 0.0))
    out: List[tuple] = []
    for b in bands:
        try:
            amps = float(b.get("max_a"))
            width = float(b.get("width_mm"))
        except (TypeError, ValueError):
            continue
        if amps <= min_a or width <= 0:
            continue
        out.append((amps, width))
    return sorted(out)


def netclass_for_current(amps: float) -> str:
    """Map a measured/parsed net current to the I_* net class whose IPC-2152 band
    covers it (smallest band with max_a >= amps). Returns "" for currents below
    the lowest band — already covered by the fab/default min width. Nets above the
    top band get the top band (widest available) rather than dropping to Default."""
    try:
        a = abs(float(amps))
    except (TypeError, ValueError):
        return ""
    ccfg = (load_cfg().get("width_by_current_rules") or {})
    if a <= float(ccfg.get("min_current_a", 0.0)):
        return ""
    bands = _current_bands()
    if not bands:
        return ""
    prefix = str(ccfg.get("netclass_prefix", "I_"))
    for max_a, _w in bands:
        if a <= max_a:
            lbl = (f"{max_a:g}").rstrip(".")
            return f"{prefix}{lbl}A"
    lbl = (f"{bands[-1][0]:g}").rstrip(".")
    return f"{prefix}{lbl}A"


def pcb_rule_severities() -> Dict[str, str]:
    """PCB DRC rule-severity overrides written into board.design_settings of the
    .kicad_pro so KiCad's stored DRC profile reflects fab/manufacturability intent
    rather than stock defaults. Config-driven (default_rules.json:pcb_rule_severities);
    returns {} when disabled so the project keeps KiCad's defaults untouched."""
    cfg = (load_cfg().get("pcb_rule_severities") or {})
    if not cfg.get("enabled", False):
        return {}
    sev = cfg.get("severities") or {}
    return {str(k): str(v) for k, v in sev.items()
            if str(v) in ("error", "warning", "ignore")}
