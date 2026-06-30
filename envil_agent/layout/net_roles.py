"""Classify each net in a schematic by its electrical ROLE — from BOTH the net
NAME and the connected pins' TYPES — so net-class assignment is robust to nets
whose names don't follow convention (a power rail called "RAIL", an analog line
called "SENSOR_OUT", a clock called "NODE7"…).

Name-only patterns (`config/net_classes.json -> assignment_patterns`) already
catch conventionally-named nets (+5V, GND, *CLK*, USB_D±, ADC*…). What they miss
is a net whose name carries no hint. KiCad's netlist exposes every pin's
`pintype` (power_in / power_out / input / output / passive…), so a rail still
classifies as POWER even when its name doesn't, by looking at the pins on it.

NOTHING is hard-coded here (config-no-hardcode policy):
- the role → class mapping + order + the power-pin pintypes come from
  `net_classes.json -> role_assignment`;
- the name signatures come from `pcb_constraint_model.json -> pin_signatures`.
So a new role, a repointed class, or a different power-pin definition is a JSON
edit, not a code change.

Returns ``{net_name: netclass}`` only for nets that resolve to a NON-Default
class, so `set_design_rules` adds an explicit assignment exactly where the
wildcard patterns would otherwise drop the net to Default. Pure, config-driven,
never raises (returns {} on any failure -> caller falls back to name patterns).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Set

import sexpdata

from ..kicad.netlist_ir import _run_netlist_text, _head, _kid, _kids, _val


def _sig_cfg() -> Dict:
    """Name signatures (pin_signatures) — same source the router uses."""
    try:
        from ..tools.pcb_reasoning import _load_cfg
        return _load_cfg() or {}
    except Exception:
        return {}


def _role_order() -> List[Dict]:
    """Ordered role → class mapping from net_classes.json:role_assignment.order.
    Empty list -> classifier becomes a no-op (caller keeps name patterns)."""
    try:
        path = Path(__file__).resolve().parent.parent / "config" / "net_classes.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        return list((cfg.get("role_assignment", {}) or {}).get("order", []) or [])
    except Exception:
        return []


def _sig(cfg: Dict, key: str) -> List[str]:
    return list((cfg.get("pin_signatures", {}) or {}).get(key, []) or [])


def _classify_one(names: List[str], pintypes: Set[str], sig_cfg: Dict,
                  order: List[Dict], match) -> str:
    """Role class name, or "" (= Default / leave to name patterns). `names` is
    the net name PLUS every connected pin's function (e.g. 'OSC_IN', 'ADC0',
    'VREF') — so the role is driven by what the net actually CONNECTS TO, not
    just how it was named. The first `order` entry that matches — by any of those
    names against its signature, or by an optional connected `pintypes` — wins."""
    for entry in order:
        sig_key = str(entry.get("signature") or "")
        cls = str(entry.get("class") or "")
        if not sig_key or not cls:
            continue
        if match(_sig(sig_cfg, sig_key), names, sig_cfg):
            return cls
        pts = [str(p).lower() for p in (entry.get("pintypes") or [])]
        if pts and any(pt in have for have in pintypes for pt in pts):
            return cls
    return ""


async def classify_project_nets(sch_path: str, timeout: float = 60.0
                                ) -> Dict[str, str]:
    """Run KiCad's netlister and return {net_name: netclass} for every net that
    resolves to a non-Default role from its name + connected pin types."""
    order = _role_order()
    if not order:
        return {}
    text = await _run_netlist_text(sch_path, timeout)
    if not text:
        return {}
    try:
        root = sexpdata.loads(text)
    except Exception:
        return {}
    if _head(root) != "export":
        return {}
    nets_node = _kid(root, "nets")
    if not nets_node:
        return {}

    sig_cfg = _sig_cfg()
    try:
        from ..tools.pcb_reasoning import _matches_any as match
    except Exception:
        def match(pats, names, _cfg):       # substring fallback (length-guarded)
            up = [n.upper() for n in names]
            return any(len(p) >= 3 and p.upper() in n for p in pats for n in up)

    out: Dict[str, str] = {}
    for net in _kids(nets_node, "net"):
        nm = _val(net, "name")
        if not nm or nm.lower().startswith("unconnected-"):
            continue  # KiCad's single-pad pseudo-nets — not real connections
        pintypes: Set[str] = set()
        pinfuncs: Set[str] = set()
        for node in _kids(net, "node"):
            pt = _val(node, "pintype")
            if pt:
                pintypes.add(pt.lower())
            pf = _val(node, "pinfunction")
            if pf:
                pinfuncs.add(pf)
        # Match signatures against the net name AND every connected pin function
        # -> classification follows the actual circuit, not naming convention.
        names = [nm] + sorted(pinfuncs)
        cls = _classify_one(names, pintypes, sig_cfg, order, match)
        if cls:
            out[nm] = cls
    return out
