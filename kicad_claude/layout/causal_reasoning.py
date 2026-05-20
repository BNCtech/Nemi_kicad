"""P17 — causal graph reasoning over the ERC severity engine.

P16 v2 told us WHAT is broken, WHERE it lives, and HOW MUCH it
matters. P17 asks the next question: **WHY is it broken, and WHAT
breaks as a consequence?**

Each fatal/major issue gets a `CausalReport`:

    {
      "summary":      "OSC32_IN floating because no Y2 crystal in design",
      "expected":     "Y2.OSC32_OUT -> U2.PC14_OSC32_IN",
      "actual":       "U2.PC14_OSC32_IN with no driver pin found",
      "cause_chain":  ["pin_role=clock", "no crystal Y* references OSC32_IN",
                       "rail_tree.lookup(OSC32_IN).driver_ref = None"],
      "propagation":  ["MAIN_CONTROLLER: RTC stop", "RESET_SECT: ..."],
    }

The reasoning draws on every prior layer:

  - rail_tree (P15)         -> "is there a driver in the DAG?"
  - functional_blocks (P12) -> "which block owns this ref?"
  - bus_semantics (P14)     -> "which other bus members exist?"
  - partition cross edges   -> "which blocks downstream depend on this?"
  - lib_symbol pins         -> "is there a partner pin (OSC_OUT for OSC_IN)?"

Pure analysis. The output is a list of CausalReports keyed by issue;
auto-repair (P18) consumes it to choose remediation actions."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class CausalReport:
    """One issue's causal explanation. Serialisable via `asdict()`."""
    check:       str
    severity:    str
    ref:         Optional[str]
    pin_number:  Optional[str]
    net:         Optional[str]
    summary:     str
    expected:    Optional[str] = None
    actual:      Optional[str] = None
    cause_chain: List[str] = field(default_factory=list)
    propagation: List[str] = field(default_factory=list)


def _find_partner_clock_pin(ref: str, pin_role: str,
                             pin_name: str,
                             ctx) -> Optional[str]:
    """For a clock-pin issue, return the canonical PARTNER pin name
    expected to drive this one. E.g. OSC_IN's partner is OSC_OUT;
    HSE_IN's partner is HSE_OUT. None when no partner pattern matches."""
    if pin_role != "clock":
        return None
    up = (pin_name or "").upper().lstrip("~{").rstrip("}")
    swaps = (
        ("IN", "OUT"), ("OUT", "IN"),
        ("XIN", "XOUT"), ("XOUT", "XIN"),
        ("OSCI", "OSCO"), ("OSCO", "OSCI"),
    )
    for a, b in swaps:
        if up.endswith(a):
            return up[: -len(a)] + b
    return None


def _crystal_refs_on_net(net: str, ctx) -> List[str]:
    """Return refs on `net` whose lib_id is a crystal/oscillator (Y*).
    P15's rail_tree doesn't track crystals; we re-scan here."""
    out: List[str] = []
    for ref in ctx.refs_on_net.get(net, ()):
        if ref.startswith("Y"):
            out.append(ref)
        else:
            lib = ctx.lib_id_by_ref.get(ref, "").lower()
            if "crystal" in lib or "oscillator" in lib:
                out.append(ref)
    return out


def _block_downstream_blocks(block: Optional[str],
                              cross_edges: List[Dict[str, Any]],
                              ) -> List[str]:
    """Given a functional block, return the OTHER blocks that share a
    cross-cluster edge with it. Approximate "downstream" — without
    flow direction we treat any sharing block as affected."""
    if not block or not cross_edges:
        return []
    seen: Set[str] = set()
    for e in cross_edges:
        a, b = e.get("a_block"), e.get("b_block")
        if a == block and b and b != block:
            seen.add(b)
        elif b == block and a and a != block:
            seen.add(a)
    return sorted(seen)


def _explain_power_pin(issue, ctx, cross_edges) -> CausalReport:
    context = issue.get("_context", {}) or {}
    ref = context.get("ref")
    pin_num = context.get("pin_number")
    net = context.get("net")
    domain = context.get("net_domain", "unknown")
    rail_tree = ctx.tree.get("rail_tree", {})
    info = rail_tree.get(net, {})
    sev = issue.get("_severity", "major")
    cause_chain: List[str] = []
    expected = None
    actual = None
    summary = f"{ref}.{pin_num} on {net or 'unknown net'}: power pin not driven"

    if info.get("driver_ref"):
        # P15.5 should have caught this — but if it surfaces here the
        # ERC reported it on a different pin on the same net. Either
        # way the DAG knows a driver exists.
        driver = info["driver_ref"]
        kind = info.get("driver_kind", "unknown")
        cause_chain.append(
            f"rail_tree[{net}].driver_ref = {driver} ({kind})"
        )
        expected = f"{driver} -> {net}"
        actual = f"{driver} drives {net} in DAG but ERC blind to it"
        summary = f"{net} IS driven by {driver} ({kind}); ERC false-positive"
    else:
        # Find candidate sources by domain + naming.
        cause_chain.append(
            f"rail_tree[{net}].driver_ref = None (no DAG-confirmed driver)"
        )
        if domain == "analog":
            expected = (
                f"Analog LDO or ferrite-filtered digital rail -> {net}"
            )
            actual = "No regulator with this output_net found"
            cause_chain.append(
                "no regulator.output_net matches " + str(net)
            )
            cause_chain.append(
                "domain = analog -> expected dedicated AVDD/AVCC source"
            )
        elif domain == "usb":
            expected = f"USB connector VBUS pin -> {net}"
            actual = "No USB source feeding this rail"
            cause_chain.append("domain = usb but no USB connector drives net")
        elif domain == "battery":
            expected = f"Battery / charger output -> {net}"
            actual = "No battery source feeding this rail"
        elif domain == "unknown":
            expected = "rail definition (regulator output or source pin)"
            actual = f"net {net} has no domain typing and no driver"
            cause_chain.append(
                "domain = unknown -> rail name not in canonical patterns"
            )
        else:
            expected = f"regulator output -> {net}"
            actual = "Driver missing"
        summary = f"{net} has no driver (domain={domain})"

    block = context.get("block")
    propagation = _block_downstream_blocks(block, cross_edges)
    if block and propagation:
        propagation = [f"{block} -> {b}" for b in propagation]
    return CausalReport(
        check=issue.get("check", ""),
        severity=sev, ref=ref, pin_number=pin_num, net=net,
        summary=summary, expected=expected, actual=actual,
        cause_chain=cause_chain, propagation=propagation,
    )


def _explain_floating_pin(issue, ctx, cross_edges) -> CausalReport:
    context = issue.get("_context", {}) or {}
    ref = context.get("ref")
    pin_num = context.get("pin_number")
    net = context.get("net")
    pin_role = context.get("pin_role", "unknown")
    sev = issue.get("_severity", "warning")
    pin_name, _et = ctx.pins_by_ref.get(ref, {}).get(pin_num, ("", ""))
    summary = f"{ref}.{pin_num} ({pin_name or pin_role}) floating"
    expected = None
    actual = "no connection"
    cause_chain = [f"pin_role = {pin_role}"]

    if pin_role == "reset":
        expected = "pullup resistor to VCC + optional manual reset switch"
        cause_chain.append(
            "reset pin floating = MCU stays in reset / random boot"
        )
        summary = f"RESET pin {ref}.{pin_num} floating — MCU won't boot"
    elif pin_role == "clock":
        partner = _find_partner_clock_pin(ref, pin_role, pin_name, ctx)
        crystals = _crystal_refs_on_net(net or "", ctx)
        if partner:
            expected = f"crystal Y* with {partner} -> {ref}.{pin_num}"
            cause_chain.append(f"expected partner pin: {partner}")
        else:
            expected = "external clock source or crystal"
        if not crystals:
            cause_chain.append(
                "no crystal/oscillator (Y*) found on this net"
            )
            actual = "no crystal in design"
        summary = f"clock pin {ref}.{pin_num} has no driver — clock tree broken"
    elif pin_role == "diff_pair":
        # Find partner diff member
        up = pin_name.upper().lstrip("~{").rstrip("}")
        for sa, sb in (("DP", "DM"), ("D+", "D-"), ("_P", "_N"),
                        ("H", "L")):
            if up.endswith(sa):
                expected = f"partner pin ending in '{sb}' (current: {up})"
                break
            if up.endswith(sb):
                expected = f"partner pin ending in '{sa}' (current: {up})"
                break
        cause_chain.append(
            "diff-pair member without paired connection — differential broken"
        )
        summary = f"diff-pair member {ref}.{pin_num} half-connected"
    elif pin_role in ("i2c", "spi"):
        bus = (ctx.bus_by_net.get(net) if net else None) or {}
        present_members = [m["net"] for m in bus.get("members", [])]
        expected = (
            f"{pin_role.upper()} bus completion ({', '.join(present_members)})"
            if present_members else f"{pin_role.upper()} peer device"
        )
        cause_chain.append(
            f"bus members detected: {present_members or 'none'}"
        )
    elif pin_role == "analog_input":
        expected = "signal source / sensor output or test point"
        cause_chain.append(
            "ADC/VREF input floating — readings undefined"
        )
    elif pin_role in ("nc", "reserved", "test_point"):
        actual = "intentionally unconnected"
        cause_chain.append("pin role marks this as no-connect / reserved")
        summary = f"{ref}.{pin_num} intentionally unconnected ({pin_role})"
    elif pin_role == "power_in":
        # KiCad's `power_in` electrical type on a pin that ERC sees as
        # not_connected almost always means the rail it expects isn't
        # connected to a driver. Look up the rail in the power tree.
        rail = ctx.tree.get("rail_tree", {}).get(net or "", {})
        if rail.get("driver_ref"):
            expected = f"{rail['driver_ref']} ({rail.get('driver_kind')}) -> {net}"
            actual = f"DAG knows driver; ERC blind across sheets"
        else:
            expected = "regulator output or rail source feeding " + str(net or pin_name)
            actual = "no driver in rail_tree"
            cause_chain.append(
                f"rail_tree[{net}].driver_ref = None"
            )
        summary = f"{ref}.{pin_num} ({pin_name or 'power_in'}) on undriven rail {net or '?'}"

    block = context.get("block")
    propagation = _block_downstream_blocks(block, cross_edges)
    if block:
        propagation = [f"{block} -> {b}" for b in propagation]
    return CausalReport(
        check=issue.get("check", ""),
        severity=sev, ref=ref, pin_number=pin_num, net=net,
        summary=summary, expected=expected, actual=actual,
        cause_chain=cause_chain, propagation=propagation,
    )


def _explain_dangling(issue, ctx, cross_edges) -> CausalReport:
    context = issue.get("_context", {}) or {}
    ref = context.get("ref")
    pin_num = context.get("pin_number")
    net = context.get("net")
    bus_kind = context.get("bus_kind")
    sev = issue.get("_severity", "warning")
    summary = f"dangling label/wire near {net or ref}"
    expected = None
    actual = "label/wire endpoint with no destination"
    cause_chain = []
    if bus_kind:
        cause_chain.append(f"net belongs to {bus_kind} bus")
        bus = ctx.bus_by_net.get(net) or {}
        member_nets = [m["net"] for m in bus.get("members", [])]
        # Find which members of this bus are 'dangling' (no second ref).
        broken = [m for m in member_nets
                  if len(ctx.refs_on_net.get(m, set())) < 2]
        if broken:
            cause_chain.append(
                f"bus members with <2 endpoints: {broken}"
            )
            expected = (
                f"both endpoints of {bus_kind.upper()} bus to "
                f"connect to peer device"
            )
        summary = f"{bus_kind.upper()} bus broken: members {broken or 'all'} half-connected"
    block = context.get("block")
    propagation = _block_downstream_blocks(block, cross_edges)
    if block:
        propagation = [f"{block} -> {b}" for b in propagation]
    return CausalReport(
        check=issue.get("check", ""),
        severity=sev, ref=ref, pin_number=pin_num, net=net,
        summary=summary, expected=expected, actual=actual,
        cause_chain=cause_chain, propagation=propagation,
    )


def analyze_issue(issue: Dict[str, Any], ctx,
                  cross_edges: Optional[List[Dict[str, Any]]] = None,
                  ) -> CausalReport:
    """Dispatch by `check` name to the right explainer. `ctx` is the
    P16 `_Context` (already built with rail_tree + buses + pins).
    `cross_edges` is the partition's `cross_cluster_edges` list with
    `a_block`/`b_block` enrichment — when omitted, propagation comes
    back empty (still reports cause_chain + expected/actual)."""
    check = issue.get("check", "")
    cross_edges = cross_edges or []
    if check == "power_pin_not_driven":
        return _explain_power_pin(issue, ctx, cross_edges)
    if check in ("pin_not_connected", "isolated_pin_label"):
        return _explain_floating_pin(issue, ctx, cross_edges)
    if "dangl" in check or check == "unconnected_wire_endpoint":
        return _explain_dangling(issue, ctx, cross_edges)
    # Fallback — minimal report, no causal chain.
    context = issue.get("_context", {}) or {}
    return CausalReport(
        check=check,
        severity=issue.get("_severity", "warning"),
        ref=context.get("ref"),
        pin_number=context.get("pin_number"),
        net=context.get("net"),
        summary=f"{check}: {issue.get('refs', '')[:80]}",
    )


def analyze_critical_issues(
    issues: List[Dict[str, Any]],
    ctx,
    cross_edges: Optional[List[Dict[str, Any]]] = None,
    *,
    severities: Tuple[str, ...] = ("fatal", "major"),
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Run `analyze_issue` over the fatal+major subset (the part a
    human / auto-repair actually needs to read). Returns a list of
    `CausalReport.asdict()` dicts, capped at `limit` so the report
    surface stays bounded."""
    out: List[Dict[str, Any]] = []
    for it in issues:
        sev = it.get("_severity", "warning")
        if sev not in severities:
            continue
        rep = analyze_issue(it, ctx, cross_edges=cross_edges)
        out.append(asdict(rep))
        if len(out) >= limit:
            break
    return out


def normalize_net_importance(
    raw_scores: Dict[str, float],
) -> Dict[str, float]:
    """P17.2 — normalise net_importance to a 0..100 percentile so the
    score is comparable across tiers. The raw `fanout × domain × bus`
    formula has no upper bound (GND on big = 1250; small NE555 GND
    might be 8); the 0..100 form lets the chat-card and CI gate
    interpret "75 means top-quartile importance for THIS schematic"
    consistently no matter the design size."""
    if not raw_scores:
        return {}
    sorted_scores = sorted(raw_scores.values())
    n = len(sorted_scores)
    out: Dict[str, float] = {}
    for nm, raw in raw_scores.items():
        # Percentile rank: count of nets with strictly lower score / N.
        rank = sum(1 for s in sorted_scores if s < raw)
        out[nm] = round(100.0 * rank / max(1, n - 1), 1) if n > 1 else 100.0
    return out
