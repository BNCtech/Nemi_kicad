"""Verified-block expansion — deterministic instantiation of stored, known-good
support sub-circuits anchored on a specific IC.

GENERAL mechanism (NOT part-specific in code): every template lives in
``config/verified_blocks/*.json``. When the IR contains a component whose lib_id
matches a template's ``anchor``, this pass injects the template's support parts
and PRE-VERIFIED internal wiring, so the LLM never has to wire that front-end.
Adding a verified block for ANY part = one JSON file, zero code.

Discipline (mirrors intent/checklist_repair.py): detect-existing -> skip, else
inject, strictly ADDITIVE (never removes/repoints a pin -> can never short),
never raises. Internal net names are namespaced by the anchor refdes so two
instances of the same part never bond their internals via same-named global
labels (the Phase 0.2 cross-sheet failure mode). Rails (V+/GND) and the
architect's boundary nets (OUT/REF) are left untouched.

Gated by layout_config.json:normalize.expand_verified_blocks (default OFF);
called from normalize_ir alongside complete_design_checklist.
"""
from __future__ import annotations

import fnmatch
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ir import IRComponent, IRNet, TopologyIR

# Optional symbol resolution so a template pin NAME ('+', 'V+') matches an
# architect token emitted as a NUMBER ('8', '6'), and vice-versa. Wrapped so a
# missing/partial library never breaks the pass (literal token match still works).
try:
    from ..kicad.symbol_geom import load_symbol
except Exception:                                   # noqa: BLE001
    def load_symbol(_lib_id):                       # type: ignore
        raise ValueError("no symbol library")


_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config" / "verified_blocks"


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _load_templates() -> tuple:
    """Glob + parse every enabled template. Returns a tuple (hashable/cacheable)
    of dicts. A malformed file is skipped, never fatal."""
    out: List[dict] = []
    try:
        for p in sorted(_CONFIG_DIR.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and d.get("enabled", True) and d.get("anchor"):
                out.append(d)
    except Exception:                               # noqa: BLE001
        return tuple()
    return tuple(out)


# ---------------------------------------------------------------------------
# Helpers copied from checklist_repair (kept local so this module is
# standalone-testable with a stubbed library, like tests/_verify_design_checks).
# ---------------------------------------------------------------------------
def _lib_matches(lib_id: str, patterns) -> bool:
    lib = lib_id or ""
    for pat in patterns or []:
        if pat and (fnmatch.fnmatchcase(lib, pat)
                    or fnmatch.fnmatchcase(lib.lower(), str(pat).lower())):
            return True
    return False


def _alloc_refdes(ir, prefix: str) -> str:
    prefix_u = prefix.upper()
    max_n, used = 0, set()
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


def _gnd_net(ir) -> IRNet:
    try:
        from .normalize import _ground_aliases
        gnd_aliases = _ground_aliases()
    except Exception:                               # noqa: BLE001
        gnd_aliases = {"GND", "VSS", "AGND", "DGND", "PGND"}
    for n in ir.nets:
        if n.name.upper() in gnd_aliases:
            return n
    net = IRNet(name="GND", pins=[], is_power=True)
    ir.nets.append(net)
    return net


def _pick_rail(ir, pattern: str) -> IRNet:
    candidates = [c.strip() for c in str(pattern or "").replace("|", ",").split(",") if c.strip()]
    for c in candidates:
        n = _net_by_name(ir, c)
        if n is not None and getattr(n, "is_power", False):
            return n
    name = candidates[0] if candidates else "+3V3"
    net = _net_by_name(ir, name)
    if net is None:
        net = IRNet(name=name, pins=[], is_power=True)
        ir.nets.append(net)
    return net


def _assign_to_block(ir, new_ref: str, anchor_ref: str) -> None:
    for blk in getattr(ir, "blocks", []) or []:
        if anchor_ref in blk.component_refs:
            if new_ref not in blk.component_refs:
                blk.component_refs.append(new_ref)
            return


def _pin_tokens(ir, ref: str, pin: str) -> set:
    """Every token form `ref.pin` could take — the literal plus, when the symbol
    resolves, the matching pin NAME and NUMBER. Lets a template pin name match an
    architect-emitted pin number and vice-versa."""
    if not pin or pin == "~":
        return set()
    toks = {f"{ref}.{pin}"}
    comp = ir.component_by_ref(ref)
    if comp is not None:
        try:
            geom = load_symbol(comp.lib_id)
        except Exception:                           # noqa: BLE001
            geom = None
        if geom is not None:
            pl = pin.lower()
            for p in (geom.pins or []):
                if (p.name or "").lower() == pl or (p.number or "").lower() == pl:
                    nm = (p.name or "").strip()
                    # NEVER add an empty/unnamed ('~') pin-name token: KiCad
                    # gives an unnamed pin the name "" / "~" (INA240 OUT pin 5),
                    # and a bare "<ref>." / "<ref>.~" token would match an
                    # unrelated net and attach support to the WRONG net
                    # (adversarial finding). Match such pins by NUMBER only.
                    if nm and nm != "~":
                        toks.add(f"{ref}.{nm}")
                    if p.number:
                        toks.add(f"{ref}.{p.number}")
    return toks


def _net_carrying_pin(ir, ref: str, pin: str) -> Optional[IRNet]:
    toks = _pin_tokens(ir, ref, pin)
    if not toks:
        return None
    for net in ir.nets:
        if any(p in toks for p in net.pins):
            return net
    return None


def _parse_ohms(value: str) -> Optional[float]:
    try:
        from .validate import _parse_resistance_to_ohms
        return _parse_resistance_to_ohms(value)
    except Exception:                               # noqa: BLE001
        try:
            return float(str(value).replace("R", "").replace("r", ""))
        except (TypeError, ValueError):
            return None


def _bridges(ir, net_a: IRNet, net_b: IRNet, prefix: str,
             max_ohm: Optional[float] = None) -> bool:
    """A part of `prefix` whose pins straddle net_a and net_b. When `max_ohm` is
    given, the part's value must parse to <= max_ohm (resistor case); when None,
    any part of the prefix bridging the two nets counts (cap/decoupling case)."""
    a = {p.split(".", 1)[0] for p in net_a.pins if "." in p}
    b = {p.split(".", 1)[0] for p in net_b.pins if "." in p}
    for ref in (a & b):
        if not ref.startswith(prefix):
            continue
        comp = ir.component_by_ref(ref)
        if comp is None:
            continue
        if max_ohm is None:
            return True
        r = _parse_ohms(comp.value or "")
        if r is not None and r <= max_ohm + 1e-9:
            return True
    return False


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------
def _should_skip(ir, aref: str, tmpl: dict) -> bool:
    """Skip expansion when a template-declared support sub-circuit is already
    present. General + part-agnostic: each `skip_if.any_part_bridges` entry says
    "if a part of <prefix> (optionally <= max_ohm) already straddles the nets on
    anchor pins <pin_a> and <pin_b>, the architect already built this — skip."
    e.g. a shunt already across IN+/IN-, or a decoupling cap already on V+/GND."""
    sk = tmpl.get("skip_if", {}) or {}
    for cond in sk.get("any_part_bridges", []) or []:
        na = _net_carrying_pin(ir, aref, cond.get("pin_a", ""))
        nb = _net_carrying_pin(ir, aref, cond.get("pin_b", ""))
        if na is None or nb is None or na.name == nb.name:
            continue
        mo = cond.get("max_ohm")
        if _bridges(ir, na, nb, cond.get("prefix", "R"),
                    float(mo) if mo is not None else None):
            return True
    return False


def _expand_one(ir, aref: str, tmpl: dict) -> List[str]:
    if _should_skip(ir, aref, tmpl):
        return []
    anchor_role = (tmpl.get("anchor") or {}).get("anchor_role", "anchor")
    role_ref: Dict[str, str] = {anchor_role: aref}
    added: List[str] = []

    # inject support parts (immediate append so the next _alloc_refdes sees them)
    for part in tmpl.get("support_parts", []) or []:
        role = part.get("role")
        if not role:
            continue
        ref = _alloc_refdes(ir, part.get("ref_prefix", "U"))
        ir.components.append(IRComponent(
            ref=ref, lib_id=part.get("lib_id", ""), value=str(part.get("value", ""))))
        _assign_to_block(ir, ref, aref)
        role_ref[role] = ref
        added.append(ref)

    # instantiate internal nets, namespaced by the anchor refdes
    for net in tmpl.get("internal_nets", []) or []:
        nm = net.get("name")
        if not nm:
            continue
        pins: List[str] = []
        for tok in net.get("pins", []) or []:
            role, _, pin = str(tok).partition(".")
            r = role_ref.get(role)
            if r and pin:
                pins.append(f"{r}.{pin}")
        if len(pins) >= 2:
            ir.nets.append(IRNet(name=f"{aref}_{nm}", pins=pins, is_power=False))

    # join support pins to the anchor's EXISTING rails (never duplicate/rename)
    for pa in tmpl.get("power_attach", []) or []:
        srole, _, spin = str(pa.get("support", "")).partition(".")
        sref = role_ref.get(srole)
        if not sref or not spin:
            continue
        target = None
        fap = pa.get("follow_anchor_pin")
        if fap:
            target = _net_carrying_pin(ir, aref, fap)
        if target is None:
            if pa.get("fallback_rail_role") == "GND":
                target = _gnd_net(ir)
            elif pa.get("fallback_rail_pattern"):
                target = _pick_rail(ir, pa["fallback_rail_pattern"])
        if target is not None:
            tok = f"{sref}.{spin}"
            if tok not in target.pins:
                target.pins.append(tok)
    return added


def expand_verified_blocks(ir: TopologyIR) -> List[str]:
    """Instantiate every matching verified template into `ir` in place. Returns
    the refdes added (for logging/tests). Never raises."""
    added: List[str] = []
    try:
        templates = _load_templates()
    except Exception:                               # noqa: BLE001
        return added
    # IDEMPOTENCY (adversarial finding): normalize_ir runs MANY times per build
    # (validate / block_repair / incremental nodes + the retry loop), so a
    # template with no dedup would re-inject its support on EVERY call -> stacked
    # caps (C12, C13, C14...). Track which (anchor, block) pairs were already
    # expanded on THIS ir object and skip them on re-runs. In-memory attribute
    # only (not in to_dict), so it never leaks into the IR JSON; the graph passes
    # the SAME ir object through the repeated normalize calls, so the marker
    # survives and re-expansion is suppressed.
    done = getattr(ir, "_verified_expanded", None)
    if not isinstance(done, set):
        done = set()
        try:
            setattr(ir, "_verified_expanded", done)
        except Exception:                           # noqa: BLE001
            pass
    for tmpl in templates:
        anchor = tmpl.get("anchor") or {}
        bid = str(tmpl.get("block_id", "") or anchor.get("anchor_role", "block"))
        lib_pats = anchor.get("lib_id_patterns") or []
        val_pats = anchor.get("value_patterns") or []
        # snapshot refs first — expansion mutates ir.components
        anchors = [c.ref for c in list(ir.components)
                   if _lib_matches(c.lib_id, lib_pats)
                   and (not val_pats or _lib_matches(c.value or "", val_pats))]
        for aref in anchors:
            key = f"{aref}::{bid}"
            if key in done:
                continue                            # already expanded on this ir
            try:
                refs = _expand_one(ir, aref, tmpl)
                done.add(key)
                if refs:
                    added += refs
                    # Provenance (Phase 5): record which refdes came from this
                    # verified block, so the approval card can tell the engineer
                    # which parts are KNOWN-GOOD (template) vs architect-generated
                    # (review those). In-memory, not serialised.
                    prov = getattr(ir, "_verified_provenance", None)
                    if not isinstance(prov, dict):
                        prov = {}
                        try:
                            setattr(ir, "_verified_provenance", prov)
                        except Exception:           # noqa: BLE001
                            pass
                    for r in refs:
                        prov[r] = bid
            except Exception:                       # noqa: BLE001
                continue
    return added
