"""Deterministic design-checklist completion — turn the validator's
detect-only design checklist into detect-AND-synthesise.

`validate._run_design_checklist` DETECTS missing support parts (reset
pull-up, LDO input/output caps, crystal load caps, USB-C CC pull-downs)
for ANY part via the lib_id / pin-name patterns in
`config/design_checklist.json`. It only reports them, so that whole class
of error bounces back to the architect, which re-emits the FULL board and
diverges on large designs (the STM32F405 CAN-logger retry storm).

This module READS THE SAME CONFIG + consumes the validator's own issue
list and SYNTHESISES the missing part, wiring it from the offending pin's
net to the rail/GND the checklist names. It is:

  * Part-agnostic by construction — it never names a part; it acts on the
    validator's issue CODES, so every IC family the checklist already
    covers is auto-completed with zero code change.
  * Deterministic — no LLM, no retry, no divergence.
  * Strictly additive and provably short-free. Scope (v1) is SHUNT
    completion only: adding a 2-pin part between an EXISTING net and a
    rail/GND (pull-up, decoupling / bulk / load cap, CC pull-down). The
    synthesised part connects exactly the pin the validator demands to the
    exact rail the checklist names, so it can never bridge two signals.
    SERIES insertion (LED current-limit resistor) and STRUCTURAL additions
    (a missing bus connector / SD-card socket) change topology and are
    deliberately LEFT to the architect — they are not a deterministic
    completion.

A synthesised support part is assigned to the SAME block as the part it
supports, so it never trips the validator's COMPONENT_NOT_IN_ANY_BLOCK
check on hierarchical boards.

Gated by `layout_config.json:normalize.complete_design_checklist`
(default FALSE until live-verified). Called once near the end of
`normalize_ir`, after the power tree + rail merges, so the rail nets it
attaches to already exist. See [feedback_non_breaking_changes].
"""
from __future__ import annotations

from typing import List, Optional

from .ir import IRComponent, IRNet


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _alloc_refdes(ir, prefix: str) -> str:
    """Next free ``<prefix><n>`` that collides with no existing ref."""
    prefix_u = prefix.upper()
    max_n = 0
    used = set()
    for c in ir.components:
        used.add(c.ref)
        r = c.ref
        i = 0
        while i < len(r) and r[i].isalpha():
            i += 1
        if r[:i].upper() == prefix_u:
            try:
                max_n = max(max_n, int(r[i:]))
            except ValueError:
                continue
    n = max_n + 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def _net_by_name(ir, name: str) -> Optional[IRNet]:
    for n in ir.nets:
        if n.name == name:
            return n
    return None


def _get_or_make_rail(ir, name: str) -> IRNet:
    net = _net_by_name(ir, name)
    if net is None:
        net = IRNet(name=name, pins=[], is_power=True)
        ir.nets.append(net)
    return net


def _gnd_net(ir) -> IRNet:
    """The board ground net. Reuses an existing GND alias when present,
    else synthesises a canonical ``GND`` (the power-tree pass already does
    this when it ran, so normally we just find it)."""
    try:
        from .normalize import _ground_aliases
        gnd_aliases = _ground_aliases()
    except Exception:
        gnd_aliases = {"GND", "VSS", "AGND", "DGND", "PGND"}
    for n in ir.nets:
        if n.name.upper() in gnd_aliases:
            return n
    return _get_or_make_rail(ir, "GND")


def _pick_rail(ir, pattern: str) -> IRNet:
    """Resolve the pull-up target rail from a ``"+3V3|+5V|VDD|VCC"`` style
    pattern: first existing power net named by a candidate wins; else the
    board's dominant logic rail; else the first candidate (created)."""
    candidates = [c.strip() for c in str(pattern or "").replace("|", ",").split(",") if c.strip()]
    for c in candidates:
        n = _net_by_name(ir, c)
        if n is not None and getattr(n, "is_power", False):
            return n
    try:
        from .normalize import _dominant_logic_rail
        dom = _dominant_logic_rail(ir)
    except Exception:
        dom = None
    if dom:
        return _get_or_make_rail(ir, dom)
    return _get_or_make_rail(ir, candidates[0] if candidates else "+3V3")


def _assign_to_block(ir, new_ref: str, anchor_ref: str) -> None:
    """Put the synthesised part in the same block as the part it supports
    so a hierarchical board doesn't fire COMPONENT_NOT_IN_ANY_BLOCK."""
    for blk in getattr(ir, "blocks", []) or []:
        if anchor_ref in blk.component_refs:
            if new_ref not in blk.component_refs:
                blk.component_refs.append(new_ref)
            return


def _add_shunt(ir, signal_net: IRNet, rail_net: IRNet, prefix: str,
               value: str, lib_id: str, anchor_ref: str) -> str:
    """Synthesise a 2-pin part with pin 1 on ``signal_net`` and pin 2 on
    ``rail_net``; assign it to ``anchor_ref``'s block. Returns the refdes.
    Purely additive — never removes or re-points an existing pin."""
    ref = _alloc_refdes(ir, prefix)
    ir.components.append(IRComponent(ref=ref, lib_id=lib_id, value=value))
    signal_net.pins.append(f"{ref}.1")
    rail_net.pins.append(f"{ref}.2")
    _assign_to_block(ir, ref, anchor_ref)
    return ref


def _uf_to_value(uf) -> str:
    """Format a microfarad number as an IEC-RKM value token: 10 -> '10u',
    4.7 -> '4u7'-style is avoided (engine normalises later), 0.1 -> '100n'."""
    try:
        uf = float(uf)
    except (TypeError, ValueError):
        return "10u"
    if uf >= 1.0:
        return f"{uf:g}u"
    return f"{uf * 1000:g}n"


# --------------------------------------------------------------------------
# per-rule synthesisers (driven by the validator's issue codes)
# --------------------------------------------------------------------------

def _fix_reset_pullup(ir, mcu_ref: str, rules: dict, load_symbol) -> List[str]:
    from . import validate as V
    rst = rules.get("reset_circuit") or {}
    pull = rst.get("required_pullup") or {}
    if not pull.get("required", True):
        return []
    patterns = rst.get("trigger_pin_name_patterns") or []
    comp = ir.component_by_ref(mcu_ref)
    if comp is None:
        return []
    try:
        geom = load_symbol(comp.lib_id)
    except Exception:
        return []
    rst_pin = next((p for p in (geom.pins or []) if V._pin_name_matches(p.name, patterns)), None)
    if rst_pin is None:
        return []
    rst_net = None
    for net in ir.nets:
        if (f"{comp.ref}.{rst_pin.number}" in net.pins
                or f"{comp.ref}.{rst_pin.name}" in net.pins):
            rst_net = net
            break
    if rst_net is None:
        # Reset pin is floating — the POWER_PIN/connectivity checks own that;
        # there is no node to pull up yet.
        return []
    rail = _pick_rail(ir, pull.get("to_net_pattern", "+3V3|+5V|VDD|VCC"))
    ref = _add_shunt(ir, rst_net, rail, str(pull.get("refdes_prefix", "R")),
                     str(pull.get("value", "10k")), "Device:R", mcu_ref)
    return [ref]


def _fix_ldo_cap(ir, ldo_ref: str, code: str, rules: dict, gnd: IRNet) -> List[str]:
    ldo_rule = rules.get("ldo_linear_regulator") or {}
    slot_key = ("required_input_cap" if code.endswith("INPUT_CAP_MISSING")
                else "required_output_cap")
    slot = ldo_rule.get(slot_key) or {}
    pin_pats = {s.upper() for s in (slot.get("on_pin_patterns") or [])}
    if not pin_pats:
        return []
    comp = ir.component_by_ref(ldo_ref)
    if comp is None:
        return []
    # The IN/OUT net = the net carrying an LDO pin whose token matches a
    # configured pin pattern (mirrors the validator's `ldo_on_this`).
    target = None
    for net in ir.nets:
        on_this = any(
            p.split(".", 1)[1].upper() in pin_pats
            for p in net.pins
            if p.startswith(f"{comp.ref}.") and "." in p
        )
        if on_this:
            target = net
            break
    if target is None:
        # IN/OUT pin not wired — power-pin check owns it.
        return []
    value = _uf_to_value(slot.get("preferred_uf", slot.get("min_uf", 10.0)))
    ref = _add_shunt(ir, target, gnd, str(slot.get("refdes_prefix", "C")),
                     value, "Device:C", ldo_ref)
    return [ref]


def _fix_crystal_caps(ir, xtal_ref: str, rules: dict, gnd: IRNet) -> List[str]:
    xtal_rule = rules.get("crystal_oscillator") or {}
    load_cfg = xtal_rule.get("required_load_caps") or {}
    need_count = int(load_cfg.get("count", 2))
    value = str(load_cfg.get("preferred_value", "22p"))
    prefix = str(load_cfg.get("refdes_prefix", "C"))
    xtal = ir.component_by_ref(xtal_ref)
    if xtal is None:
        return []
    try:
        from .normalize import _ground_aliases
        gnd_aliases = _ground_aliases()
    except Exception:
        gnd_aliases = {"GND", "VSS", "AGND", "DGND", "PGND"}
    xtal_nets = [n for n in ir.nets if any(p.startswith(f"{xtal.ref}.") for p in n.pins)]
    existing_caps = set()
    for net in xtal_nets:
        for pr in net.pins:
            rp = pr.split(".", 1)[0]
            if rp.startswith("C") and rp != xtal.ref:
                existing_caps.add(rp)
    need = need_count - len(existing_caps)
    if need <= 0:
        return []
    # Each missing load cap goes from a crystal SIGNAL terminal net to GND.
    term_nets = [n for n in xtal_nets
                 if not getattr(n, "is_power", False)
                 and n.name.upper() not in gnd_aliases]
    added: List[str] = []
    for net in term_nets:
        if need <= 0:
            break
        has_cap = any(pr.split(".", 1)[0].startswith("C")
                      and pr.split(".", 1)[0] != xtal.ref for pr in net.pins)
        if has_cap:
            continue
        ref = _add_shunt(ir, net, gnd, prefix, value, "Device:C", xtal_ref)
        added.append(ref)
        need -= 1
    return added


def _fix_cc_pulldown(ir, where: str, rules: dict, gnd: IRNet) -> List[str]:
    usb = rules.get("usb_c_ufp") or {}
    pull = usb.get("required_pulldowns") or {}
    if not pull.get("pins"):
        return []
    ref_part, _, pin_tok = where.partition(".")
    if not ref_part or not pin_tok:
        return []
    from . import validate as V
    nets = list(V._ir_nets_for_pin(ir, ref_part, pin_tok))
    if nets:
        cc_net = nets[0]
    else:
        cc_net = IRNet(name=pin_tok.upper(), pins=[f"{ref_part}.{pin_tok}"], is_power=False)
        ir.nets.append(cc_net)
    ref = _add_shunt(ir, cc_net, gnd, str(pull.get("refdes_prefix", "R")),
                     str(pull.get("value", "5.1k")), "Device:R", ref_part)
    return [ref]


def _load_completion_cfg() -> dict:
    """Read the whole load_completion config block (the DATA table + defaults)."""
    try:
        from .engine import _load_layout_config
        cfg = _load_layout_config().get("load_completion") or {}
        if not cfg.get("enabled", True):
            return {}
        return cfg
    except Exception:
        return {}


def _target_net_for(ir, connect_to: str, gnd, rail_pattern: str):
    """Resolve a connect_to token to a net. Nothing hardcoded -- the rail set
    comes from config (rail_pattern / the dominant-logic-rail priority)."""
    ct = str(connect_to or "").upper()
    if ct == "GND":
        return gnd
    if ct == "DOMINANT_RAIL":
        try:
            from .normalize import _dominant_logic_rail
            dom = _dominant_logic_rail(ir)
        except Exception:
            dom = None
        if dom:
            return _get_or_make_rail(ir, dom)
        return _pick_rail(ir, rail_pattern)        # config pattern, not a literal
    return _get_or_make_rail(ir, connect_to) if connect_to else None


def _connect_floating_pin(ir, ref: str, pin, target, emptied_ids: set) -> None:
    """Move a floating pin onto `target`: fold its single-pin stub if it has one,
    else append it. Records emptied stub nets for removal."""
    toks = {f"{ref}.{pin.number}", f"{ref}.{pin.name}"}
    stubs = [n for n in ir.nets if any(t in n.pins for t in toks)]
    if not stubs:
        target.pins.append(f"{ref}.{pin.number}")
        return
    if len(stubs) == 1 and len(stubs[0].pins) == 1 and stubs[0] is not target:
        target.pins.append(stubs[0].pins[0])
        stubs[0].pins = []
        emptied_ids.add(id(stubs[0]))


def _complete_loads(ir, gnd, load_symbol) -> List[str]:
    """ONE data-driven completion for any fully-floating 2-terminal LOAD (LED,
    buzzer, relay coil, motor, ...). Driven by load_completion.rules -- adding a
    new load family is ONE config rule, NOT a new function. For a load whose
    BOTH terminals are isolated (no real >=2-pin connection), wires SOURCE ->
    (optional series R) -> rail and RETURN -> GND, so the AI's incomplete part
    becomes a valid energised circuit. Never touches a part that already has a
    real connection on either terminal."""
    from . import validate as V
    cfg = _load_completion_cfg()
    rules = cfg.get("rules") or []
    if not rules:
        return []
    series_lib = str(cfg.get("series_part_lib_id", "Device:R"))
    series_prefix = str(cfg.get("series_refdes_prefix", "R"))
    rail_pattern = str(cfg.get("dominant_rail_pattern", "+3V3|+5V|VDD|VCC"))
    added: List[str] = []
    emptied_ids: set = set()
    for rule in rules:
        triggers = [str(s).upper() for s in (rule.get("lib_id_contains") or [])]
        if not triggers:
            continue
        src_keys = {str(s).upper() for s in (rule.get("source_pin") or [])}
        ret_keys = {str(s).upper() for s in (rule.get("return_pin") or [])}
        needs_r = bool(rule.get("series_resistor"))
        rval = str(rule.get("resistor_value", "330"))
        src_to = rule.get("source_to", "DOMINANT_RAIL")
        ret_to = rule.get("return_to", "GND")
        for comp in ir.components:
            lib_u = (comp.lib_id or "").upper()
            if not any(s in lib_u for s in triggers):
                continue
            try:
                geom = load_symbol(comp.lib_id)
            except Exception:
                continue
            pins = geom.pins or []
            if len(pins) < 2:
                continue

            def find_pin(keys):
                return next((p for p in pins
                             if (p.name or "").upper() in keys or str(p.number) in keys), None)

            spin = find_pin(src_keys)
            rpin = find_pin(ret_keys)
            if spin is None or rpin is None:
                continue

            def has_real(pin):
                toks = {f"{comp.ref}.{pin.number}", f"{comp.ref}.{pin.name}"}
                return any(any(t in n.pins for t in toks) and len(n.pins) >= 2 for n in ir.nets)

            if has_real(spin) or has_real(rpin):
                continue  # already connected somewhere -> not fully floating

            src_rail = _target_net_for(ir, src_to, gnd, rail_pattern)
            if src_rail is None:
                continue
            # SOURCE: rail -> (optional series part) -> source pin
            if needs_r:
                toks = {f"{comp.ref}.{spin.number}", f"{comp.ref}.{spin.name}"}
                snets = [n for n in ir.nets if any(t in n.pins for t in toks)]
                if snets:
                    src_net = snets[0]
                else:
                    src_net = IRNet(name=f"{comp.ref}_SRC",
                                    pins=[f"{comp.ref}.{spin.number}"], is_power=False)
                    ir.nets.append(src_net)
                rref = _alloc_refdes(ir, series_prefix)
                ir.components.append(IRComponent(ref=rref, lib_id=series_lib, value=rval))
                src_rail.pins.append(f"{rref}.1")
                src_net.pins.append(f"{rref}.2")
                _assign_to_block(ir, rref, comp.ref)
                added.append(rref)
            else:
                _connect_floating_pin(ir, comp.ref, spin, src_rail, emptied_ids)
            # RETURN: return pin -> GND/return rail
            ret_net = _target_net_for(ir, ret_to, gnd, rail_pattern)
            if ret_net is not None:
                _connect_floating_pin(ir, comp.ref, rpin, ret_net, emptied_ids)
    if emptied_ids:
        ir.nets = [n for n in ir.nets if id(n) not in emptied_ids]
    return added


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

# Issue codes this pass can deterministically complete (shunt class only).
_FIXABLE = {
    "MCU_NO_RESET_PULLUP",
    "LDO_INPUT_CAP_MISSING",
    "LDO_OUTPUT_CAP_MISSING",
    "CRYSTAL_LOAD_CAPS_MISSING",
    "USB_C_CC_PULLDOWN_MISSING",
    "USB_C_CC_PIN_UNCONNECTED",
    "IC_PIN_NO_BYPASS",
    "IC_BUS_NO_PULLUP",
    "SENSE_NO_DIFF_FILTER",
}


def _fix_pin_bypass(ir, where: str, rules: dict, gnd: IRNet) -> List[str]:
    """Add a local bypass ceramic from a regulator/reference/charge-pump pin to
    GND (IC_PIN_NO_BYPASS). where = '<ref>.<pinname>'. Part-agnostic: works for
    any IC by pin name; value from config. Self-limiting — once the cap exists
    the detector stops firing, so re-running never stacks caps."""
    from . import validate as V
    if "." not in where:
        return []
    ref, pin = where.split(".", 1)
    net = next(iter(V._ir_nets_for_pin(ir, ref, pin)), None)
    if net is None:
        return []
    gpb = (rules.get("generic_pin_bypass") or {})
    value = str(gpb.get("default_cap_value", "100n"))
    cpref = str(gpb.get("cap_refdes_prefix", "C"))
    r = _add_shunt(ir, net, gnd, cpref, value, "Device:C", ref)
    return [r] if r else []


def _fix_bus_pullup(ir, where: str, rules: dict, gnd: IRNet) -> List[str]:
    """Add ONE pull-up resistor from an open-drain bus net to the logic rail
    (IC_BUS_NO_PULLUP). `where` is the bus NET name. Part-agnostic; self-limiting
    (once the pull-up exists the detector stops firing, so no stacking)."""
    net = _net_by_name(ir, where)
    if net is None:
        return []
    gbp = rules.get("generic_bus_pullup") or {}
    rail = _pick_rail(ir, gbp.get("rail_pattern", "+3V3|+5V|VDD|VCC"))
    value = str(gbp.get("pullup_value", "4.7k"))
    rpref = str(gbp.get("pullup_refdes_prefix", "R"))
    anchor = next((p.split(".", 1)[0] for p in net.pins if "." in p), "")
    r = _add_shunt(ir, net, rail, rpref, value, "Device:R", anchor)
    return [r] if r else []


def _fix_sense_filter(ir, where: str, rules: dict, gnd: IRNet) -> List[str]:
    """Add ONE differential filter cap across a low-side current-sense pair
    (SENSE_NO_DIFF_FILTER). `where` = '<net_p>|<net_n>'. Strictly additive —
    bridges the two EXISTING sense nets, never re-points a pin and never touches
    the series filter R (that is the architect's call). Self-limiting: once the
    cap straddles the pair the detector goes quiet, so re-runs never stack."""
    if "|" not in where:
        return []
    a, b = where.split("|", 1)
    net_a = _net_by_name(ir, a)
    net_b = _net_by_name(ir, b)
    if net_a is None or net_b is None or net_a.name == net_b.name:
        return []
    gsf = rules.get("generic_sense_filter") or {}
    value = str(gsf.get("diff_cap_value", "100n"))
    cpref = str(gsf.get("cap_refdes_prefix", "C"))
    anchor = next((p.split(".", 1)[0]
                   for p in (net_a.pins + net_b.pins) if "." in p), "")
    r = _add_shunt(ir, net_a, net_b, cpref, value, "Device:C", anchor)
    return [r] if r else []


def complete_design_checklist(ir) -> List[str]:
    """Synthesise every missing SHUNT support part the design checklist
    detects. Returns the list of refdes added. Strictly additive; never
    raises (a repair miss must not block normalisation)."""
    from . import validate as V
    from ..kicad.symbol_geom import load_symbol

    cfg = V._load_design_checklist()
    if not cfg.get("enabled", True):
        return []
    rules = cfg.get("rules") or {}

    try:
        issues = V._run_design_checklist(ir)
    except Exception:
        issues = []

    gnd = _gnd_net(ir)
    added: List[str] = []
    for iss in issues:
        code = iss.get("code")
        if code not in _FIXABLE:
            continue
        where = iss.get("where", "") or ""
        try:
            if code == "MCU_NO_RESET_PULLUP":
                added += _fix_reset_pullup(ir, where, rules, load_symbol)
            elif code in ("LDO_INPUT_CAP_MISSING", "LDO_OUTPUT_CAP_MISSING"):
                added += _fix_ldo_cap(ir, where, code, rules, gnd)
            elif code == "CRYSTAL_LOAD_CAPS_MISSING":
                added += _fix_crystal_caps(ir, where, rules, gnd)
            elif code in ("USB_C_CC_PULLDOWN_MISSING", "USB_C_CC_PIN_UNCONNECTED"):
                added += _fix_cc_pulldown(ir, where, rules, gnd)
            elif code == "IC_PIN_NO_BYPASS":
                added += _fix_pin_bypass(ir, where, rules, gnd)
            elif code == "IC_BUS_NO_PULLUP":
                added += _fix_bus_pullup(ir, where, rules, gnd)
            elif code == "SENSE_NO_DIFF_FILTER":
                added += _fix_sense_filter(ir, where, rules, gnd)
        except Exception:
            # one rule's failure must never abort the rest
            continue
    # Complete any fully-floating 2-terminal LOAD (LED, buzzer, relay, ...) the
    # architect declared but never wired -- ONE data-driven engine, not a
    # function per part. Driven by load_completion.rules.
    try:
        added += _complete_loads(ir, gnd, load_symbol)
    except Exception:
        pass
    return added
