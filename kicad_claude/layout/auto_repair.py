"""P18 — causal auto-repair planner.

P17 told us WHY each issue exists. P18 turns that into WHAT TO DO:

  CausalReport(check=power_pin_not_driven, cause_chain=[
      "pin_role = power_in",
      "rail_tree[+3V3].driver_ref = None",
  ])
        ↓
  RepairOp(
      op_type   = "INSERT_REGULATOR",
      target    = {"output_net": "+3V3", "expected_input": "+5V"},
      rationale = "+3V3 has no driver; design has +5V upstream",
      fixes     = ["power_pin_not_driven#U2.1", "power_pin_not_driven#U3.13"],
      confidence= 0.85,
      risk      = "medium",
  )

Two-stage pipeline:

  1. **Root-cause collapse** — 20 fatal/major reports on the same
     undriven rail share ONE root: "missing +3V3 source". Repairing
     the root fixes every dependent issue, so the plan is concise
     instead of 20 redundant ops.

  2. **Repair catalog match** — each root cause maps to a small set
     of canonical repair operations:

       INSERT_REGULATOR · INSERT_CRYSTAL · INSERT_PULLUP ·
       INSERT_SOURCE    · ADD_NC_FLAG    · RECONNECT_NET   ·
       INSERT_BUS_PEER  · INSERT_DECAP

Output is structured (no schematic edits — those are the chat-apply
layer's job). Auto-repair INTENT lives here; auto-repair EXECUTION
will land in the chat session ops generator that already speaks
`ops.json` to the renderer.

Pure analysis. No file mutation."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class RepairOp:
    """One concrete repair action. `target` carries op-specific
    parameters (ref/net/pin/value); `fixes` lists the issue IDs the
    op resolves. Confidence is 0..1; risk is low/medium/high."""
    op_type:    str
    target:     Dict[str, Any]
    rationale:  str
    fixes:      List[str] = field(default_factory=list)
    confidence: float = 0.5
    risk:       str = "medium"
    depends_on: List[int] = field(default_factory=list)


@dataclass
class RootCause:
    """One root cause across the whole design. `affected_issues` is
    the union of issue IDs that share this root; `proposed_ops` are
    indices into the parent plan's `ops` list."""
    kind:             str
    summary:          str
    affected_issues:  List[str] = field(default_factory=list)
    proposed_ops:     List[int] = field(default_factory=list)


@dataclass
class RepairPlan:
    """Top-level structure returned by `plan_repairs`. Serialisable
    via `asdict()`."""
    root_causes:     List[RootCause]
    ops:             List[RepairOp]
    health_estimate: int       # health score AFTER all ops applied
    summary:         str       # one-line headline for the chat card


def _issue_id(report: Dict[str, Any]) -> str:
    """Stable identifier per causal report: `check#ref.pin@net`."""
    return (
        f"{report.get('check', '?')}"
        f"#{report.get('ref') or '?'}"
        f".{report.get('pin_number') or '?'}"
        f"@{report.get('net') or '?'}"
    )


def _key_power_root(report: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Power-rail root signature. All `power_pin_not_driven` (and
    power_in floating pins) sharing the same NET coalesce into one
    root: "rail X has no driver". Returns `("missing_driver", net)`
    or None when the report isn't a power-root candidate."""
    if report.get("check") == "power_pin_not_driven":
        net = report.get("net") or "unknown"
        return ("missing_driver", net)
    cause = report.get("cause_chain") or []
    if any("power_in" in c for c in cause) and \
       any("driver_ref = None" in c for c in cause):
        net = report.get("net") or "unknown"
        return ("missing_driver", net)
    return None


def _key_clock_root(report: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Clock-pin root: all floating OSC_IN/OUT pins on the SAME ref
    share one root: "no crystal connected to MCU REF". Returns
    `("missing_crystal", ref)` or None."""
    cause = report.get("cause_chain") or []
    if not any("pin_role = clock" in c for c in cause):
        return None
    ref = report.get("ref")
    if not ref:
        return None
    return ("missing_crystal", ref)


def _key_diff_pair_root(report: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Diff-pair root: all half-connected pairs on the same canonical
    base (USB_D, CAN, LVDS_TX) coalesce. Returns
    `("broken_diff_pair", base)` or None."""
    cause = report.get("cause_chain") or []
    if not any("pin_role = diff_pair" in c
                or "diff-pair member" in c for c in cause):
        return None
    net = report.get("net") or ""
    # Strip the trailing _P/_N/_DP/_DM/_+/_- to get the base.
    base = net
    for tail in ("_DP", "_DM", "_D+", "_D-", "_P", "_N",
                  "_+", "_-", "_H", "_L"):
        if base.upper().endswith(tail):
            base = base[: -len(tail)]
            break
    return ("broken_diff_pair", base.upper())


def _key_intentional_floating(report: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Detect NC/reserved/test_point pins flagged as floating by ERC.
    Each gets a per-pin ADD_NC_FLAG op (no aggregation — KiCad needs
    a `(no_connect ...)` directive per pin). Returns
    `("intentional_floating", ref.pin)` or None."""
    cause = report.get("cause_chain") or []
    if not any("intentional" in c.lower()
                or "marks this as no-connect" in c for c in cause):
        return None
    ref = report.get("ref") or "?"
    pin = report.get("pin_number") or "?"
    return ("intentional_floating", f"{ref}.{pin}")


def _key_bus_root(report: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Bus-criticality root: I2C/SPI/UART floating member → one root
    per bus base name. Returns `("broken_bus", bus_kind)` or None."""
    cause = report.get("cause_chain") or []
    for c in cause:
        if c.startswith("pin_role = ") and c.split(" = ")[1] in (
            "i2c", "spi", "uart", "can", "usb",
        ):
            kind = c.split(" = ")[1]
            return ("broken_bus", kind)
    if any("bus broken" in c.lower() for c in cause):
        return ("broken_bus", "unknown")
    return None


# Root-classification dispatch — applied in order; first match wins.
_ROOT_CLASSIFIERS = [
    _key_power_root,
    _key_clock_root,
    _key_diff_pair_root,
    _key_intentional_floating,
    _key_bus_root,
]


def _classify_root(report: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    for cls in _ROOT_CLASSIFIERS:
        key = cls(report)
        if key is not None:
            return key
    return None


# ─────────── Repair-op generators per root kind ──────────────


def _ops_for_missing_driver(
    root_key: Tuple[str, str], reports: List[Dict[str, Any]],
    tree: Dict[str, Any],
) -> List[RepairOp]:
    """`missing_driver` root → propose a regulator insertion OR a
    reconnect when a regulator already exists in the design but
    isn't wired to this output net.

    Confidence:
      - 0.9 when net name matches a canonical rail (+3V3 / +5V / AVDD)
        AND no existing regulator output matches.
      - 0.7 when an existing regulator EXISTS but isn't wired here
        (suggest RECONNECT instead of INSERT).
      - 0.4 when the net is an auto-generated name like Net-(U1-PadN)
        (means schematic isn't labelled — fix is "label the net",
        not "insert hardware")."""
    net = root_key[1]
    fixes = [_issue_id(r) for r in reports]
    rationale_parts = [
        f"{len(reports)} pins reference rail '{net}' but no DAG driver exists",
    ]
    # Check whether a regulator exists in the design.
    regulators = tree.get("regulators") or []
    has_regulator = bool(regulators)
    if net.startswith("Net-("):
        return [RepairOp(
            op_type="LABEL_NET",
            target={"net": net},
            rationale="Auto-generated net name suggests the rail isn't labelled. "
                       "Add a power-port label so the rail merges with the named "
                       "rail in the design.",
            fixes=fixes,
            confidence=0.6,
            risk="low",
        )]
    if has_regulator:
        # Suggest reconnect — pick the regulator whose output net we
        # think this should match by name.
        candidate = regulators[0]["ref"]
        return [RepairOp(
            op_type="RECONNECT_NET",
            target={
                "regulator": candidate,
                "output_pin_to_net": net,
            },
            rationale=(
                f"Regulator {candidate} exists but isn't connected to '{net}'. "
                "Wire its output pin to this net, OR rename one of the two "
                "rails so the labels merge."
            ),
            fixes=fixes,
            confidence=0.7,
            risk="medium",
        )]
    # No regulator at all — propose inserting one.
    return [RepairOp(
        op_type="INSERT_REGULATOR",
        target={
            "output_net": net,
            "input_hint":  "VBUS or VIN (highest-voltage rail in design)",
        },
        rationale=(
            f"Rail '{net}' has no driver and the design has no regulator. "
            "Insert an LDO or buck with this output and connect its input to "
            "the upstream source."
        ),
        fixes=fixes,
        confidence=0.85 if net.lstrip("+").upper() in
                       ("3V3", "3.3V", "5V", "1V8", "1V2", "AVDD") else 0.55,
        risk="medium",
    )]


def _ops_for_missing_crystal(
    root_key: Tuple[str, str], reports: List[Dict[str, Any]],
    tree: Dict[str, Any],
) -> List[RepairOp]:
    """`missing_crystal` root → propose inserting a crystal + load
    caps next to the MCU's OSC pins."""
    mcu_ref = root_key[1]
    fixes = [_issue_id(r) for r in reports]
    return [RepairOp(
        op_type="INSERT_CRYSTAL",
        target={
            "mcu_ref": mcu_ref,
            "pins":    sorted({r.get("pin_number") for r in reports
                                if r.get("pin_number")}),
            "load_caps": True,    # caller should also drop 2× C ~18pF
        },
        rationale=(
            f"{mcu_ref} OSC pin(s) floating — no crystal/oscillator in design. "
            f"Insert a Y* crystal between the OSC pins with 2× ~18pF load "
            "capacitors to GND."
        ),
        fixes=fixes,
        confidence=0.9,   # very strong pattern when MCU + floating clock
        risk="low",
    )]


def _ops_for_broken_diff_pair(
    root_key: Tuple[str, str], reports: List[Dict[str, Any]],
    tree: Dict[str, Any],
) -> List[RepairOp]:
    """`broken_diff_pair` root → propose connecting the missing
    partner net OR adding the partner terminal on the connector."""
    base = root_key[1]
    fixes = [_issue_id(r) for r in reports]
    return [RepairOp(
        op_type="CONNECT_DIFF_PAIR",
        target={
            "base_name":  base,
            "broken_nets": sorted({r.get("net") for r in reports
                                    if r.get("net")}),
        },
        rationale=(
            f"Diff-pair {base} has half-connected member(s). Ensure both "
            "P/N sides terminate at the same component and that no series "
            "resistor / TVS sits on only one side."
        ),
        fixes=fixes,
        confidence=0.75,
        risk="medium",
    )]


def _ops_for_intentional_floating(
    root_key: Tuple[str, str], reports: List[Dict[str, Any]],
    tree: Dict[str, Any],
) -> List[RepairOp]:
    """`intentional_floating` root → suggest an explicit `(no_connect)`
    flag on each pin. One op per pin (not aggregated) because KiCad
    needs a per-pin directive."""
    ref, pin = root_key[1].split(".", 1)
    fixes = [_issue_id(r) for r in reports]
    return [RepairOp(
        op_type="ADD_NC_FLAG",
        target={"ref": ref, "pin": pin},
        rationale=(
            f"{ref}.{pin} marked NC/reserved/test_point — add a `(no_connect ...)` "
            "directive to silence ERC and document intent."
        ),
        fixes=fixes,
        confidence=0.95,
        risk="low",
    )]


def _ops_for_broken_bus(
    root_key: Tuple[str, str], reports: List[Dict[str, Any]],
    tree: Dict[str, Any],
) -> List[RepairOp]:
    """`broken_bus` root → propose connecting missing peer or
    inserting pullup resistors on I2C."""
    kind = root_key[1]
    fixes = [_issue_id(r) for r in reports]
    if kind == "i2c":
        return [
            RepairOp(
                op_type="INSERT_PULLUP",
                target={"bus": "i2c", "value": "4k7", "pull_to": "+3V3"},
                rationale=(
                    "I2C bus floating — SDA/SCL need pullup resistors to VCC "
                    "(typically 4k7 to +3V3)."
                ),
                fixes=fixes,
                confidence=0.85,
                risk="low",
            ),
            RepairOp(
                op_type="CHECK_BUS_PEER",
                target={"bus": "i2c"},
                rationale=(
                    "Verify the second I2C device endpoint exists; "
                    "single-master single-slave needs both ICs reachable."
                ),
                fixes=[],
                confidence=0.6,
                risk="low",
            ),
        ]
    if kind == "spi":
        return [RepairOp(
            op_type="CHECK_BUS_PEER",
            target={"bus": "spi"},
            rationale=(
                "SPI bus member floating — verify slave device pins "
                "(MOSI/MISO/SCK/CS) all reach a slave IC."
            ),
            fixes=fixes,
            confidence=0.7,
            risk="low",
        )]
    if kind in ("usb", "can"):
        return [RepairOp(
            op_type="CHECK_BUS_PEER",
            target={"bus": kind},
            rationale=(
                f"{kind.upper()} bus member floating — verify both "
                "differential pins terminate at the same connector / "
                "transceiver."
            ),
            fixes=fixes,
            confidence=0.7,
            risk="medium",
        )]
    return [RepairOp(
        op_type="CHECK_BUS_PEER",
        target={"bus": kind},
        rationale=f"{kind} bus completeness check.",
        fixes=fixes,
        confidence=0.5,
        risk="low",
    )]


_OPS_DISPATCH = {
    "missing_driver":        _ops_for_missing_driver,
    "missing_crystal":       _ops_for_missing_crystal,
    "broken_diff_pair":      _ops_for_broken_diff_pair,
    "intentional_floating":  _ops_for_intentional_floating,
    "broken_bus":            _ops_for_broken_bus,
}


def plan_repairs(
    causal_reports: List[Dict[str, Any]],
    tree: Dict[str, Any],
    severity_counts: Optional[Dict[str, int]] = None,
) -> RepairPlan:
    """End-to-end repair planning. Inputs:

      - `causal_reports` — output of `causal_reasoning.analyze_critical_issues`
      - `tree`           — `power_intent.build_power_tree` result
      - `severity_counts`— current `p16_severity_counts` (to estimate
                            post-fix health score)

    Returns a `RepairPlan` with root_causes + ordered ops + estimated
    health score after applying every op."""
    # Step 1 — collapse to root causes.
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    unmatched: List[Dict[str, Any]] = []
    for rep in causal_reports:
        key = _classify_root(rep)
        if key is None:
            unmatched.append(rep)
        else:
            grouped[key].append(rep)

    # Step 2 — generate ops per root.
    ops: List[RepairOp] = []
    roots: List[RootCause] = []
    for key, reports in grouped.items():
        kind, ident = key
        op_idxs: List[int] = []
        new_ops = _OPS_DISPATCH[kind](key, reports, tree)
        for op in new_ops:
            ops.append(op)
            op_idxs.append(len(ops) - 1)
        roots.append(RootCause(
            kind=kind,
            summary=_root_summary(kind, ident, len(reports)),
            affected_issues=[_issue_id(r) for r in reports],
            proposed_ops=op_idxs,
        ))

    # Step 3 — estimate health after fixes. Crude model: each op
    # eliminates its `fixes` count of issues at the issue's CURRENT
    # severity. Subtract from the live counts; recompute the health
    # score with the standard formula.
    if severity_counts:
        # Build a map issue_id → severity from the reports.
        sev_by_id: Dict[str, str] = {}
        for rep in causal_reports:
            sev_by_id[_issue_id(rep)] = rep.get("severity", "warning")
        remaining = dict(severity_counts)
        for op in ops:
            for iid in op.fixes:
                sev = sev_by_id.get(iid)
                if sev and remaining.get(sev, 0) > 0:
                    remaining[sev] -= 1
        post_health = max(0, 100
                            - remaining.get("fatal", 0) * 25
                            - remaining.get("major", 0) * 5
                            - remaining.get("warning", 0) * 1)
    else:
        post_health = 100

    headline = _plan_headline(roots, ops)
    return RepairPlan(
        root_causes=roots,
        ops=ops,
        health_estimate=int(post_health),
        summary=headline,
    )


def _root_summary(kind: str, ident: str, count: int) -> str:
    if kind == "missing_driver":
        return f"Rail '{ident}' has no driver — {count} pin(s) affected"
    if kind == "missing_crystal":
        return f"MCU {ident} has no crystal — {count} clock pin(s) floating"
    if kind == "broken_diff_pair":
        return f"Differential pair '{ident}' half-connected ({count} member(s))"
    if kind == "intentional_floating":
        return f"Intentional unconnected pin {ident} — needs explicit NC flag"
    if kind == "broken_bus":
        return f"{ident.upper()} bus incomplete — {count} member(s) floating"
    return f"{kind}: {ident} ({count} affected)"


def _plan_headline(roots: List[RootCause], ops: List[RepairOp]) -> str:
    if not roots:
        return "No actionable repairs proposed."
    high_conf = sum(1 for o in ops if o.confidence >= 0.8)
    return (
        f"{len(roots)} root cause(s) → {len(ops)} repair op(s) "
        f"({high_conf} high-confidence)"
    )
