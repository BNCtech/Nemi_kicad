"""Per-block retry core — attribute errors to blocks, extract a block's
interface, and splice a regenerated block back into the full IR.

Motivation (see [[project_canlogger_retry_fix]]): on a 30+-part board the
architect can't re-emit a clean FULL TopologyIR on retry — instruction-
following degrades as the generation grows, so each whole-board retry
DIVERGES (5 -> 8 -> 27 errors) and the architect Claude call times out. The
fix is to regenerate ONLY the block that failed, keeping every passing block
BYTE-FROZEN, then splice the small regenerated block back in. This cuts the
regenerated surface from ~37 parts to ~5 and removes the "break a good block
while fixing a bad one" divergence.

This module is the DETERMINISTIC core (no LLM): three pure helpers the graph
node composes around a single block-scoped architect call —

  localize_issues(ir, issues)  -> which block owns each error
  block_interface(ir, block)   -> internal vs FROZEN boundary nets
  splice_block(ir, ...)        -> replace the block, re-assert the interface

Splice safety contract (so a regenerated block can never silently short the
board): external pins on a boundary net are NEVER touched — only the block's
own pin memberships are replaced; boundary net NAMES are frozen; refdes are
frozen (a regenerated component that collides with a frozen external ref is
dropped, not merged). `normalize_ir` runs after every splice as the final
net (dedupe / merge / multinet resolve), so the splice only ever has to be
*approximately* right — the deterministic repair layer cleans the seams.
See [[feedback_non_breaking_changes]].
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Token / ref helpers
# ---------------------------------------------------------------------------

def ref_of_token(token: str) -> str:
    """`<ref>.<pin>` -> `<ref>`; a bare ref or rail token -> itself.

    "C11.2" -> "C11", "U3.VDD" -> "U3", "U3" -> "U3", "GND" -> "GND"."""
    return token.split(".", 1)[0] if "." in token else token


def block_of_ref(ir, ref: str) -> Optional[str]:
    """Name of the block owning component `ref`, or None if it's in no block
    (a part the architect left out of every block[] entry)."""
    for b in getattr(ir, "blocks", []) or []:
        if ref in (b.component_refs or []):
            return b.name
    return None


def _block_by_name(ir, name: str):
    for b in getattr(ir, "blocks", []) or []:
        if b.name == name:
            return b
    return None


def _net_by_name(ir, name: str):
    for n in ir.nets:
        if n.name == name:
            return n
    return None


def _blocks_touched_by_net(ir, net) -> Set[str]:
    """Set of block names whose components appear on `net`."""
    out: Set[str] = set()
    for pin in net.pins:
        bn = block_of_ref(ir, ref_of_token(pin))
        if bn:
            out.add(bn)
    return out


# ---------------------------------------------------------------------------
# 1. Error -> block attribution
# ---------------------------------------------------------------------------

def issue_block(ir, issue: Dict[str, Any]) -> Optional[str]:
    """The single block that owns `issue`, or None when it can't be pinned to
    exactly one block (a cross-block net, a global rail, a part outside every
    block). None => the caller must fall back to a FULL architect retry, since
    no single-block regeneration can fix it.

    `where` is one of: a pin ref (``C11.2``), a component ref (``U3``), a net
    name (``SWD_GND2``, ``CANH``), or a block name (``COMM`` from BLOCK_*).
    Resolution order:
      1. component/pin ref -> its block (the common case: PIN_IN_MULTIPLE_NETS,
         PIN_NOT_ON_SYMBOL, MCU_NO_RESET_PULLUP all key on a part);
      2. else a block name (BLOCK_TOO_SMALL / BLOCK_NOT_JUSTIFIED);
      3. else a net name -> the block its pins live in, but ONLY when every
         pin sits in ONE block (a single-pin floating stub, or a net wholly
         inside a block). A net spanning >=2 blocks is interface-level and
         left to the full retry.
    """
    where = str(issue.get("where") or "").strip()
    if not where:
        return None
    # (1) component / pin ref
    ref = ref_of_token(where)
    bn = block_of_ref(ir, ref)
    if bn is not None:
        return bn
    # (2) a literal block name
    if _block_by_name(ir, where) is not None:
        return where
    # (3) a net name -> its owning block, only if unambiguous
    net = _net_by_name(ir, where)
    if net is not None:
        touched = _blocks_touched_by_net(ir, net)
        if len(touched) == 1:
            return next(iter(touched))
    return None


def localize_issues(ir, issues: List[Dict[str, Any]]
                    ) -> Tuple[Dict[str, List[Dict[str, Any]]],
                               List[Dict[str, Any]]]:
    """Bucket ERROR-severity issues by owning block.

    Returns ``(by_block, unlocalized)`` where ``by_block[name]`` is the list of
    errors that block owns and ``unlocalized`` is every error that maps to no
    single block (=> full retry). Warnings are ignored — they don't block.
    """
    by_block: Dict[str, List[Dict[str, Any]]] = {}
    unlocalized: List[Dict[str, Any]] = []
    for iss in issues:
        if iss.get("severity") != "error":
            continue
        bn = issue_block(ir, iss)
        if bn is None:
            unlocalized.append(iss)
        else:
            by_block.setdefault(bn, []).append(iss)
    return by_block, unlocalized


# ---------------------------------------------------------------------------
# 2. Block interface — internal vs FROZEN boundary
# ---------------------------------------------------------------------------

def block_interface(ir, block) -> Dict[str, Any]:
    """Split everything touching `block` into the data a block-scoped
    architect call needs:

      components     : the block's IRComponent objects (frozen refs)
      internal_nets  : nets whose pins are ALL inside the block AND not a
                       global power rail -> the block may rewire these freely
      boundary_nets  : [{name, is_power, my_pins, external_pins}] -> the FROZEN
                       interface. `name` and `external_pins` must survive the
                       regeneration unchanged; the block reconnects its own
                       pins (`my_pins`) to `name`. A global power rail (is_power)
                       is always boundary even if it has no external pin, so the
                       block can't rename GND/+3V3.
    """
    refs: Set[str] = set(block.component_refs or [])
    comps = [c for c in ir.components if c.ref in refs]
    internal: List[Any] = []
    boundary: List[Dict[str, Any]] = []
    for net in ir.nets:
        my_pins = [p for p in net.pins if ref_of_token(p) in refs]
        if not my_pins:
            continue  # net doesn't touch this block
        ext_pins = [p for p in net.pins if ref_of_token(p) not in refs]
        if ext_pins or bool(getattr(net, "is_power", False)):
            boundary.append({
                "name": net.name,
                "is_power": bool(getattr(net, "is_power", False)),
                "my_pins": list(my_pins),
                "external_pins": list(ext_pins),
            })
        else:
            internal.append(net)
    return {"components": comps,
            "internal_nets": internal,
            "boundary_nets": boundary}


# ---------------------------------------------------------------------------
# 3. Splice a regenerated block back into the full IR
# ---------------------------------------------------------------------------

def _splice_rename_cfg():
    """(on, suffix_template, [compiled weak patterns]) for the Phase 0.2 splice
    collision guard, from layout_config.json:cross_block_guard. Default
    (False, '__{block}', []) -> the union below is byte-identical to the
    pre-guard behaviour.

    The rename fires ONLY for a name matching a weak (bus-index) pattern, so a
    DESCRIPTIVE emergent shared signal (CAN_TX, SIGNAL_A) the architect added but
    didn't declare in the plan is UNIONED (its connection preserved) rather than
    wrongly split — the fix for the 2026-06-11 adversarial-review hole."""
    import re
    try:
        from .engine import _load_layout_config
        g = _load_layout_config().get("cross_block_guard", {}) or {}
        pats = []
        for p in (g.get("weak_signal_name_patterns") or []):
            try:
                pats.append(re.compile(p))
            except re.error:
                continue
        return (bool(g.get("splice_rename_incidental", False)),
                str(g.get("splice_rename_suffix", "__{block}")),
                pats)
    except Exception:
        return (False, "__{block}", [])


def _unique_collision_name(base: str, block_name: str, suffix_tmpl: str,
                           taken) -> str:
    """A collision-free rename for an INCIDENTAL same-name net: `base` + a
    block-scoped suffix, bumped with a numeric tail until unique in `taken`."""
    try:
        suf = suffix_tmpl.format(block=block_name)
    except Exception:
        suf = "__" + str(block_name)
    cand = base + suf
    i = 2
    while cand in taken:
        cand = f"{base}{suf}_{i}"
        i += 1
    return cand


def splice_block(ir, block_name: str,
                 new_components: List[Any],
                 new_nets: List[Any]) -> Dict[str, Any]:
    """Replace `block_name`'s components + internal nets with the regenerated
    ones, re-asserting the frozen interface. Mutates `ir` in place and returns
    a small report ``{"replaced_refs", "added_refs", "dropped_refs",
    "internal_nets", "boundary_reconnected", "ref_collisions"}``.

    Guarantees (the splice can NEVER short the board on its own):
      * external pins on a boundary net are never touched — only the block's
        OWN pins are stripped and re-added;
      * boundary net NAMES are frozen (a regenerated net whose name matches a
        boundary net merges into it; it cannot rename GND/CANH);
      * refdes are frozen — a regenerated component whose ref collides with a
        FROZEN external part is dropped (reported in ref_collisions), never
        merged over it;
      * a regenerated net only ever contributes the block's OWN pins; any
        external pin the LLM put in a net it shouldn't own is discarded.
    Run `normalize_ir(ir)` after this to clean the seams.
    """
    block = _block_by_name(ir, block_name)
    if block is None:
        raise ValueError(f"splice_block: no block named {block_name!r}")

    iface = block_interface(ir, block)
    old_refs: Set[str] = set(block.component_refs or [])
    internal_names: Set[str] = {n.name for n in iface["internal_nets"]}
    boundary_names: Set[str] = {b["name"] for b in iface["boundary_nets"]}

    report = {
        "replaced_refs": sorted(old_refs),
        "added_refs": [],
        "dropped_refs": [],
        "internal_nets": [],
        "boundary_reconnected": [],
        "ref_collisions": [],
        "collision_renames": [],
    }

    # --- components: drop the block's old parts, add the regenerated ones ---
    ir.components = [c for c in ir.components if c.ref not in old_refs]
    existing_refs: Set[str] = {c.ref for c in ir.components}  # frozen external
    new_block_refs: Set[str] = set()
    for c in new_components:
        if c.ref in existing_refs:
            # collides with a FROZEN external part -> keep the external one
            report["ref_collisions"].append(c.ref)
            continue
        ir.components.append(c)
        existing_refs.add(c.ref)
        new_block_refs.add(c.ref)
        report["added_refs"].append(c.ref)
    block.component_refs = sorted(new_block_refs)
    report["dropped_refs"] = sorted(old_refs - new_block_refs)

    # --- nets: strip the old block's pins everywhere, drop now-empty internal
    # nets, keep boundary nets (with their external pins intact) ---
    surviving: List[Any] = []
    for net in ir.nets:
        was_internal = net.name in internal_names
        net.pins = [p for p in net.pins if ref_of_token(p) not in old_refs]
        if was_internal and not any(ref_of_token(p) not in new_block_refs
                                    for p in net.pins):
            # a pure-internal net of this block -> drop; the regenerated
            # block re-emits its internal nets below.
            if not net.pins or all(ref_of_token(p) in new_block_refs
                                   for p in net.pins):
                continue
        surviving.append(net)
    ir.nets = surviving
    by_name = {n.name: n for n in ir.nets}

    # --- merge the regenerated block's nets back in ---
    # Phase 0.2 cross-block collision guard (gated OFF by default): a
    # regenerated net whose name matches an EXISTING net that is NOT a declared
    # boundary (and where neither side is a power rail) is an INCIDENTAL
    # collision — two blocks chose the same generic name for DIFFERENT signals.
    # Unioning them silently shorts the board, so rename this block's net to a
    # unique block-scoped name instead. When the gate is OFF, `_rename` is
    # always False and the union path below is byte-identical to the pre-guard
    # code (boundary_names already protects shared rails like GND/CANH).
    _splice_rename_on, _suffix_tmpl, _weak_pats = _splice_rename_cfg()
    for nn in new_nets:
        own_pins = [p for p in nn.pins if ref_of_token(p) in new_block_refs]
        if not own_pins:
            continue  # nothing of this block's -> ignore (LLM noise)
        target = by_name.get(nn.name)
        # Rename ONLY a weak (bus-index) incidental collision: a descriptive
        # emergent shared signal (CAN_TX) the architect added off-plan must be
        # UNIONED here, not split. The any(...) short-circuits after
        # _splice_rename_on, so the gate-OFF path is byte-identical.
        _rename = (target is not None and _splice_rename_on
                   and nn.name not in boundary_names
                   and not bool(getattr(nn, "is_power", False))
                   and not bool(getattr(target, "is_power", False))
                   and any(p.search(nn.name) for p in _weak_pats))
        if target is not None and not _rename:
            # boundary (or already-present) net -> union ONLY the block's pins;
            # external pins already live on `target` and stay frozen.
            seen = set(target.pins)
            for p in own_pins:
                if p not in seen:
                    target.pins.append(p)
                    seen.add(p)
            if getattr(nn, "is_power", False):
                target.is_power = True
            if nn.name in boundary_names:
                report["boundary_reconnected"].append(nn.name)
        else:
            # a fresh internal net -> keep only the block's own pins. An
            # incidental collision is renamed first so it stays a SEPARATE node.
            if _rename:
                _old = nn.name
                nn.name = _unique_collision_name(_old, block_name,
                                                 _suffix_tmpl, by_name)
                report["collision_renames"].append(
                    {"from": _old, "to": nn.name, "block": block_name})
            nn.pins = list(dict.fromkeys(own_pins))
            ir.nets.append(nn)
            by_name[nn.name] = nn
            report["internal_nets"].append(nn.name)

    return report
