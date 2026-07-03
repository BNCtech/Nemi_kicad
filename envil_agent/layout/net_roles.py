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
from typing import Dict, List, Optional, Set

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


async def _load_nets(sch_path: str, timeout: float):
    """Run KiCad's netlister once and yield (net_name, pintypes, pinfuncs) for
    every REAL net. Returns [] on any failure so every caller degrades to a
    name-pattern-only fallback rather than raising."""
    text = await _run_netlist_text(sch_path, timeout)
    if not text:
        return []
    try:
        root = sexpdata.loads(text)
    except Exception:
        return []
    if _head(root) != "export":
        return []
    nets_node = _kid(root, "nets")
    if not nets_node:
        return []
    out = []
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
        out.append((nm, pintypes, pinfuncs))
    return out


def _num_from_match(m) -> Optional[float]:
    """Pull a magnitude out of a regex match. Handles the split "3V3"/"12V5" form
    (two numeric groups -> 3.3 / 12.5) and the plain decimal/whole form ("48",
    "3.3", "400" in the last non-empty group). Returns None on no match."""
    if m is None:
        return None
    if m.lastindex and m.lastindex >= 2 and m.group(1) and m.group(2):
        frac = m.group(2)
        try:
            return int(m.group(1)) + int(frac) / (10 ** len(frac))
        except ValueError:
            return None
    g = next((x for x in reversed(m.groups()) if x), None)
    try:
        return float(g) if g is not None else None
    except ValueError:
        return None


def _parse_magnitude(name: str, sub_cfg: Dict) -> Optional[float]:
    """Search `name` with the regex in a `voltage`/`current` sub-config block and
    return the parsed magnitude (V or A). Ignores the block's enable flag — that
    gates ROLE assignment, not raw parsing (the power-integrity report parses even
    when auto-assign is off). Returns None on no regex / no match / bad regex."""
    import re
    rx = (sub_cfg or {}).get("name_regex")
    if not name or not rx:
        return None
    try:
        return _num_from_match(re.search(str(rx), name))
    except re.error:
        return None


def _auto_elec_cfg() -> Dict:
    try:
        from ..intent import default_rules as _dr
        return (_dr.load_cfg().get("auto_assign_electrical") or {})
    except Exception:
        return {}


def parse_voltage_from_name(name: str) -> Optional[float]:
    """Volts parsed from a net name (e.g. '+48V'->48, '3V3'->3.3, '400VDC'->400),
    or None. Public — reused by the power-integrity report for the rail voltage."""
    return _parse_magnitude(name, _auto_elec_cfg().get("voltage") or {})


def parse_current_from_name(name: str) -> Optional[float]:
    """Amps parsed from a net name (e.g. 'MOTOR_5A'->5), or None. Public — reused
    by the power-integrity report to seed a net's design current from its name."""
    return _parse_magnitude(name, _auto_elec_cfg().get("current") or {})


def _electrical_class(name: str) -> str:
    """Parse a voltage/current magnitude embedded in a NET NAME and map it to the
    covering HV_*/I_* net class, so the IPC clearance/width DRU rules activate
    without hand-assignment. Regexes + enable flags come from
    default_rules.json:auto_assign_electrical (config-no-hardcode). Voltage wins
    over current when both parse. Returns "" when nothing parses / feature off."""
    if not name:
        return ""
    try:
        from ..intent import default_rules as _dr
    except Exception:
        return ""
    cfg = _auto_elec_cfg()
    vcfg = cfg.get("voltage") or {}
    if bool(vcfg.get("enabled", False)) and vcfg.get("name_regex"):
        volts = _parse_magnitude(name, vcfg)
        if volts is not None:
            cls = _dr.netclass_for_voltage(volts)
            if cls:
                return cls
    ccfg = cfg.get("current") or {}
    if bool(ccfg.get("enabled", False)) and ccfg.get("name_regex"):
        amps = _parse_magnitude(name, ccfg)
        if amps is not None:
            cls = _dr.netclass_for_current(amps)
            if cls:
                return cls
    return ""


async def classify_project_nets(sch_path: str, timeout: float = 60.0
                                ) -> Dict[str, str]:
    """Run KiCad's netlister and return {net_name: netclass} for every net that
    resolves to a non-Default role from its name + connected pin types."""
    order = _role_order()
    if not order:
        return {}
    nets = await _load_nets(sch_path, timeout)
    if not nets:
        return {}

    sig_cfg = _sig_cfg()
    try:
        from ..tools.pcb_reasoning import _matches_any as match
    except Exception:
        def match(pats, names, _cfg):       # substring fallback (length-guarded)
            up = [n.upper() for n in names]
            return any(len(p) >= 3 and p.upper() in n for p in pats for n in up)

    out: Dict[str, str] = {}
    for nm, pintypes, pinfuncs in nets:
        # Match signatures against the net name AND every connected pin function
        # -> classification follows the actual circuit, not naming convention.
        names = [nm] + sorted(pinfuncs)
        cls = _classify_one(names, pintypes, sig_cfg, order, match)
        if cls:
            out[nm] = cls
    return out


async def classify_project_all(sch_path: str, timeout: float = 60.0
                               ) -> Dict[str, Dict[str, str]]:
    """One netlist pass -> both role classes (POWER/ANALOG/…) and IPC electrical
    classes (HV_*/I_* parsed from net names). Returns
    {"roles": {net: class}, "electrical": {net: class}}. Electrical wins on
    conflict at the caller since it encodes a hard IPC clearance/width floor."""
    order = _role_order()
    nets = await _load_nets(sch_path, timeout)
    if not nets:
        return {"roles": {}, "electrical": {}}

    roles: Dict[str, str] = {}
    electrical: Dict[str, str] = {}
    if order:
        sig_cfg = _sig_cfg()
        try:
            from ..tools.pcb_reasoning import _matches_any as match
        except Exception:
            def match(pats, names, _cfg):
                up = [n.upper() for n in names]
                return any(len(p) >= 3 and p.upper() in n for p in pats for n in up)
        for nm, pintypes, pinfuncs in nets:
            cls = _classify_one([nm] + sorted(pinfuncs), pintypes, sig_cfg,
                                order, match)
            if cls:
                roles[nm] = cls

    for nm, _pt, _pf in nets:
        ecls = _electrical_class(nm)
        if ecls:
            electrical[nm] = ecls
    return {"roles": roles, "electrical": electrical}
