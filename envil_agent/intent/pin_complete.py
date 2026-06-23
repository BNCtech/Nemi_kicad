"""ONE data-driven engine that connects a FLOATING component pin to its correct
net -- replacing the per-component repair functions (LED cathode, crystal cap,
ground pin, power pin, ...).

The insight the user pushed for: you cannot write a Python function per part. A
floating pin's correct connection is determined by its ROLE -- and a role is just
(component lib_id pattern) x (pin name / electrical type). So we express the
knowledge as DATA (config: pin_completion.rules) and apply it with ONE generic
loop. Adding a relay coil return, a Hall-sensor ground, an opto-isolator anode, a
new MCU's VBAT -- ALL become a single JSON rule, ZERO new code.

A rule matches a pin by any of {lib_id_contains, pin_name_in, pin_name_contains,
pin_etype_in} (+ optional guards two_pin_only / require_other_pin_wired) and
connects a matching FLOATING pin to `connect_to` (GND | DOMINANT_RAIL | an
explicit rail name). First matching rule wins; list order = priority.

"Floating" = the pin is in NO net, OR it is the lone pin on a single-pin
NET_FLOATING stub (the CAN_LED_K case). Strictly additive + symbol-grounded:
only ever connects a pin the validator already calls floating, so it cannot
change a working topology or short anything. Config-gated; never raises.
"""
from __future__ import annotations

from typing import Any, List, Optional


def _cfg() -> dict:
    try:
        from .engine import _load_layout_config
        return _load_layout_config().get("pin_completion") or {}
    except Exception:
        return {}


def _rule_matches(rule: dict, lib_id: str, pin) -> bool:
    lib_u = (lib_id or "").upper()
    inc = rule.get("lib_id_contains")
    if inc and not any(str(s).upper() in lib_u for s in inc):
        return False
    pname = (getattr(pin, "name", "") or "").upper()
    names_in = rule.get("pin_name_in")
    if names_in and pname not in {str(s).upper() for s in names_in}:
        return False
    name_has = rule.get("pin_name_contains")
    if name_has and not any(str(s).upper() in pname for s in name_has):
        return False
    nums_in = rule.get("pin_number_in")
    if nums_in and str(getattr(pin, "number", "")) not in {str(s) for s in nums_in}:
        return False
    et_in = rule.get("pin_etype_in")
    if et_in and (getattr(pin, "etype", "") or "") not in et_in:
        return False
    # at least one positive matcher must be present, else the rule is a no-op
    return bool(inc or names_in or name_has or nums_in or et_in)


def _get_or_make(ir, name: str, power: bool):
    for n in ir.nets:
        if n.name == name:
            return n
    from .ir import IRNet
    net = IRNet(name=name, pins=[], is_power=power)
    ir.nets.append(net)
    return net


def complete_floating_pins(ir, rules: Optional[List[dict]] = None) -> int:
    """Apply the pin_completion rules to every floating pin. Returns the number
    of pins connected. `rules` overrides config (for tests)."""
    from ..kicad.symbol_geom import load_symbol
    try:
        from .normalize import _ground_aliases, _dominant_logic_rail
    except Exception:                              # pragma: no cover
        _ground_aliases = lambda: {"GND", "VSS", "AGND", "DGND", "PGND"}
        _dominant_logic_rail = lambda _ir: None

    cfg = _cfg()
    if rules is None:
        if not cfg.get("enabled", True):
            return 0
        rules = cfg.get("rules") or []
    if not rules:
        return 0

    gnd_aliases = _ground_aliases()
    gnd_net = next((n for n in ir.nets if n.name.upper() in gnd_aliases), None)
    if gnd_net is None:
        gnd_net = _get_or_make(ir, "GND", power=True)
    dom_rail = _dominant_logic_rail(ir)

    def pin_nets(ref, pin):
        toks = {f"{ref}.{pin.number}", f"{ref}.{pin.name}"}
        return [n for n in ir.nets if any(t in n.pins for t in toks)]

    def is_floating(nets):
        return (not nets) or (len(nets) == 1 and len(nets[0].pins) == 1)

    def target_for(connect_to):
        ct = str(connect_to or "").upper()
        if ct == "GND":
            return gnd_net
        if ct == "DOMINANT_RAIL":
            return _get_or_make(ir, dom_rail, power=True) if dom_rail else None
        return _get_or_make(ir, connect_to, power=True) if connect_to else None

    fixed = 0
    emptied_ids: set = set()
    for comp in ir.components:
        try:
            geom = load_symbol(comp.lib_id)
        except Exception:
            continue
        pins = geom.pins or []
        for pin in pins:
            pn = pin_nets(comp.ref, pin)
            if not is_floating(pn):
                continue
            # pick the first rule that matches + passes its guards
            chosen = None
            for rule in rules:
                if not _rule_matches(rule, comp.lib_id, pin):
                    continue
                if rule.get("two_pin_only") and len(pins) != 2:
                    continue
                if rule.get("require_other_pin_wired"):
                    others = [p for p in pins if p is not pin]
                    other_ok = any(
                        any(len(n.pins) >= 2 for n in pin_nets(comp.ref, o))
                        for o in others
                    )
                    if not other_ok:
                        continue
                chosen = rule
                break
            if chosen is None:
                continue
            target = target_for(chosen.get("connect_to"))
            if target is None:
                continue
            if not pn:                              # pin unconnected
                if any(t in target.pins for t in
                       (f"{comp.ref}.{pin.number}", f"{comp.ref}.{pin.name}")):
                    continue
                target.pins.append(f"{comp.ref}.{pin.number}")
                fixed += 1
            else:                                   # single-pin stub
                stub = pn[0]
                if stub is target:
                    continue
                target.pins.append(stub.pins[0])
                stub.pins = []
                emptied_ids.add(id(stub))
                fixed += 1
    if emptied_ids:
        ir.nets = [n for n in ir.nets if id(n) not in emptied_ids]
    return fixed
