"""P18.5 — repair feasibility filter.

Sits between P18's plan synthesis and the (future) execution layer:

    CausalReport (P17)
        ↓
    RepairPlan (P18: roots + ops)
        ↓
    FeasibilityFilter (P18.5)  ← THIS MODULE
        ↓
    executable ops (future P19)

Per the user's note: "the next bottleneck is preventing valid designs
from being repaired incorrectly". This filter implements that gate.

For every proposed op, run two checks:

  1. **Structural feasibility** — does the schematic ALREADY contain
     what we're about to insert? E.g.
       - INSERT_REGULATOR for +3V3 but the design has an LDO whose
         output is `+3V3` (P15 found it AND we want to insert another?
         skip — likely a misclassification).
       - INSERT_CRYSTAL for MCU U2's OSC pins but Y2 is already on
         those pins. skip.
       - INSERT_PULLUP for I2C bus but a pullup R on SDA exists.
         skip.
       - RECONNECT_NET targeting 'unknown' net. soften — needs more
         context.
       - LABEL_NET on a private internal net with only 1 endpoint.
         skip — labelling won't merge anything.

  2. **Intent override** — is the "broken" state actually intentional?
       - net name contains RSVD / OPT / FUTURE / TBD / TEST → tag
         `future_option_rail`, skip.
       - root kind `intentional_floating` → tag `explicit_nc`, keep
         the ADD_NC_FLAG (it's already the right op for that intent).
       - I2C bus with only 1 device (the master) and no other ICs
         touching the bus → likely early-dev pin-out, soften.

Verdicts: **kept** / **skipped** / **softened** (confidence reduced).
Output preserves the original ops for audit but the
`accepted_ops` subset is what the execution layer should act on."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class FeasibilityCheck:
    """One op's verdict + reason. `confidence_adjust` is added to the
    op's existing confidence (negative for soften). `intent_tag` is
    non-None only when the rejection was due to intent override."""
    op_index:          int
    op_type:           str
    verdict:           str          # "kept" | "skipped" | "softened"
    reason:            str
    confidence_adjust: float = 0.0
    intent_tag:        Optional[str] = None


# Net-name patterns that signal "intentional optional / reserved rail".
_INTENT_RAIL_RE = re.compile(
    r"(RSVD|RESERVED|OPT|OPTIONAL|FUTURE|TBD|TODO|UNUSED|TEST|NC)",
    re.IGNORECASE,
)


# ─────────────── per-op feasibility checkers ─────────────────


def _check_insert_regulator(op_idx: int, op: Dict[str, Any], ctx
                              ) -> FeasibilityCheck:
    target = op.get("target") or {}
    out_net = target.get("output_net") or ""
    # Already-driven check: P15.1 found a regulator with this output?
    for reg in (ctx.tree.get("regulators") or ()):
        if reg.get("output_net") == out_net:
            return FeasibilityCheck(
                op_index=op_idx, op_type=op["op_type"],
                verdict="skipped",
                reason=f"regulator {reg['ref']} already drives '{out_net}'",
            )
    # Intent override: name looks like an optional / reserved rail.
    if _INTENT_RAIL_RE.search(out_net):
        return FeasibilityCheck(
            op_index=op_idx, op_type=op["op_type"],
            verdict="skipped",
            reason=f"net name '{out_net}' suggests intentional unpopulated option",
            intent_tag="future_option_rail",
        )
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept", reason="no existing regulator drives target net",
    )


def _check_insert_crystal(op_idx: int, op: Dict[str, Any], ctx
                           ) -> FeasibilityCheck:
    target = op.get("target") or {}
    mcu_ref = target.get("mcu_ref") or ""
    pins = target.get("pins") or []
    # Walk every MCU OSC pin's net — does any ref starting with Y
    # already sit on it?
    for pin in pins:
        net = ctx.pin_net(mcu_ref, str(pin))
        if not net:
            continue
        for ref in ctx.refs_on_net.get(net, ()):
            if ref.startswith("Y"):
                return FeasibilityCheck(
                    op_index=op_idx, op_type=op["op_type"],
                    verdict="skipped",
                    reason=f"crystal {ref} already on {mcu_ref}'s OSC net '{net}'",
                )
            lib = (ctx.lib_id_by_ref.get(ref) or "").lower()
            if "crystal" in lib or "oscillator" in lib:
                return FeasibilityCheck(
                    op_index=op_idx, op_type=op["op_type"],
                    verdict="skipped",
                    reason=f"oscillator-class component {ref} already on '{net}'",
                )
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept", reason="no crystal on MCU OSC pins",
    )


def _check_insert_pullup(op_idx: int, op: Dict[str, Any], ctx
                          ) -> FeasibilityCheck:
    target = op.get("target") or {}
    bus_kind = target.get("bus") or "i2c"
    # Walk all known I2C nets; for each, check if an R reaches a
    # power-domain rail (the pullup pattern).
    for net, bus in ctx.bus_by_net.items():
        if (bus.get("kind") or "") != bus_kind:
            continue
        for ref in ctx.refs_on_net.get(net, ()):
            if not ref.startswith("R"):
                continue
            r_nets = {ctx.pin_net(ref, p) for p in
                       ctx.pins_by_ref.get(ref, {})}
            r_nets.discard(net)
            for other in r_nets:
                if not other:
                    continue
                domain = ctx.rail_domain(other)
                if domain in ("digital", "analog"):
                    return FeasibilityCheck(
                        op_index=op_idx, op_type=op["op_type"],
                        verdict="skipped",
                        reason=(
                            f"pullup {ref} already on '{net}' "
                            f"(other side: {other}, domain={domain})"
                        ),
                    )
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept", reason=f"no pullup detected on {bus_kind} bus",
    )


def _check_reconnect_net(op_idx: int, op: Dict[str, Any], ctx
                          ) -> FeasibilityCheck:
    target = op.get("target") or {}
    reg_ref = target.get("regulator") or ""
    out_net = target.get("output_pin_to_net") or ""
    if out_net == "unknown" or not out_net:
        return FeasibilityCheck(
            op_index=op_idx, op_type=op["op_type"],
            verdict="softened", confidence_adjust=-0.3,
            reason="target net is 'unknown' — gather more context before applying",
        )
    if reg_ref:
        existing_nets = {ctx.pin_net(reg_ref, p) for p in
                          ctx.pins_by_ref.get(reg_ref, {})}
        existing_nets.discard(None)
        if out_net in existing_nets:
            return FeasibilityCheck(
                op_index=op_idx, op_type=op["op_type"],
                verdict="skipped",
                reason=f"{reg_ref} already touches '{out_net}' — already connected",
            )
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept", reason="reconnect target is reachable + distinct",
    )


def _check_label_net(op_idx: int, op: Dict[str, Any], ctx
                       ) -> FeasibilityCheck:
    target = op.get("target") or {}
    net = target.get("net") or ""
    # Single-endpoint auto-net: labelling won't merge with anything
    # — it just renames a private pin net. Skip.
    refs = ctx.refs_on_net.get(net, set())
    if len(refs) <= 1:
        return FeasibilityCheck(
            op_index=op_idx, op_type=op["op_type"],
            verdict="skipped",
            reason=f"'{net}' has {len(refs)} endpoint(s) — labelling won't merge anything",
        )
    # If the net's pin already lies on a labelled-elsewhere rail (via
    # some heuristic we can't easily check here), the label is still
    # safe to add. Keep.
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept",
        reason=f"'{net}' has {len(refs)} endpoints — labelling could merge with named rail",
    )


def _check_add_nc_flag(op_idx: int, op: Dict[str, Any], ctx
                         ) -> FeasibilityCheck:
    # NC flags are always safe to add. Keep as-is.
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept", reason="NC flag is always safe — documents intent",
    )


def _check_connect_diff_pair(op_idx: int, op: Dict[str, Any], ctx
                               ) -> FeasibilityCheck:
    target = op.get("target") or {}
    broken_nets = target.get("broken_nets") or []
    base = (target.get("base_name") or "").upper()
    # Heuristic: scan for refs whose lib_id references this base
    # (USB connector for USB_D pair, CAN transceiver for CAN_H/L) OR
    # for nets that contain the base name.
    has_partner_lib = any(
        base in ((ctx.lib_id_by_ref.get(r) or "").upper())
        for r in ctx.lib_id_by_ref
    )
    has_partner_net = False
    for net in (ctx.refs_on_net or {}):
        if base and base in net.upper() and net not in broken_nets:
            has_partner_net = True
            break
    if not has_partner_lib and not has_partner_net:
        return FeasibilityCheck(
            op_index=op_idx, op_type=op["op_type"],
            verdict="softened", confidence_adjust=-0.2,
            reason=(
                f"no obvious partner component/net found for {base or 'diff_pair'} "
                "— may need to insert the partner side first"
            ),
        )
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept",
        reason=f"partner candidate found for {base or 'diff_pair'}",
    )


def _check_check_bus_peer(op_idx: int, op: Dict[str, Any], ctx
                            ) -> FeasibilityCheck:
    target = op.get("target") or {}
    bus_kind = target.get("bus") or ""
    # Count refs reachable on this bus's nets. If only ONE IC touches
    # the entire bus, this is probably an early-dev / future-expand
    # design — soften the warning.
    bus_refs: Set[str] = set()
    for net, bus in ctx.bus_by_net.items():
        if (bus.get("kind") or "") == bus_kind:
            for ref in ctx.refs_on_net.get(net, ()):
                if not ref.startswith(("R", "C", "L", "FB")):
                    bus_refs.add(ref)
    if len(bus_refs) <= 1:
        return FeasibilityCheck(
            op_index=op_idx, op_type=op["op_type"],
            verdict="softened", confidence_adjust=-0.3,
            reason=(
                f"{bus_kind} bus has {len(bus_refs)} non-passive endpoint(s) — "
                "possibly intentional (expansion header / single-master)"
            ),
            intent_tag="partial_bus_dev",
        )
    return FeasibilityCheck(
        op_index=op_idx, op_type=op["op_type"],
        verdict="kept",
        reason=f"{bus_kind} bus has {len(bus_refs)} active endpoints — check is meaningful",
    )


_CHECKERS = {
    "INSERT_REGULATOR":   _check_insert_regulator,
    "INSERT_CRYSTAL":     _check_insert_crystal,
    "INSERT_PULLUP":      _check_insert_pullup,
    "RECONNECT_NET":      _check_reconnect_net,
    "LABEL_NET":          _check_label_net,
    "ADD_NC_FLAG":        _check_add_nc_flag,
    "CONNECT_DIFF_PAIR":  _check_connect_diff_pair,
    "CHECK_BUS_PEER":     _check_check_bus_peer,
}


def filter_repair_plan(
    plan: Dict[str, Any],
    ctx,
) -> Dict[str, Any]:
    """Run feasibility checks over every op in `plan`. Returns:

      {
        "checks":       [{op_index, op_type, verdict, reason,
                          confidence_adjust, intent_tag}, ...],
        "accepted_ops": [op, ...],   # ops to execute (verdict in
                                       # {kept, softened})
        "rejected_ops": [op, ...],   # ops to skip (verdict skipped)
        "intent_overrides": [check, ...],  # checks with intent_tag set
        "stats": {kept, skipped, softened, intent_overrides},
      }

    Softened ops have their `confidence` reduced by `confidence_adjust`
    and remain in `accepted_ops`. Skipped ops are excluded from
    `accepted_ops` entirely (with the reason preserved in
    `rejected_ops`)."""
    ops = (plan or {}).get("ops") or []
    checks: List[Dict[str, Any]] = []
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    intent_overrides: List[Dict[str, Any]] = []
    counts = {"kept": 0, "skipped": 0, "softened": 0,
               "intent_overrides": 0}

    for idx, op in enumerate(ops):
        op_type = op.get("op_type") or ""
        checker = _CHECKERS.get(op_type)
        if not checker:
            # No-op type without a checker: keep by default, log
            # softening so the gate notices unfamiliar op types.
            chk = FeasibilityCheck(
                op_index=idx, op_type=op_type,
                verdict="kept",
                reason="no feasibility checker for this op type — keeping by default",
            )
        else:
            chk = checker(idx, op, ctx)
        chk_dict = asdict(chk)
        checks.append(chk_dict)
        counts[chk.verdict] = counts.get(chk.verdict, 0) + 1
        if chk.intent_tag:
            intent_overrides.append(chk_dict)
            counts["intent_overrides"] += 1
        new_op = dict(op)
        if chk.confidence_adjust != 0.0:
            new_op["confidence"] = max(
                0.0, min(1.0,
                          float(new_op.get("confidence", 0.5))
                          + chk.confidence_adjust),
            )
            new_op["_softened_reason"] = chk.reason
        new_op["_feasibility"] = {
            "verdict": chk.verdict,
            "reason":  chk.reason,
            "intent_tag": chk.intent_tag,
        }
        if chk.verdict == "skipped":
            rejected.append(new_op)
        else:
            accepted.append(new_op)

    return {
        "checks":             checks,
        "accepted_ops":       accepted,
        "rejected_ops":       rejected,
        "intent_overrides":   intent_overrides,
        "stats":              counts,
    }
