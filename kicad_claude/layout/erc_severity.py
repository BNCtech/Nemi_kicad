"""P16 — ERC severity intelligence.

KiCad's ERC emits a flat issue list with a single `severity` field
(error / warning) and a `check` name. That's not enough signal for an
AI-generated schematic: half the warnings are intentional (synthesised
power ports, NC pins, optional jumpers), and the other half include
genuinely fatal conditions buried among noise.

This module reclassifies each issue onto a six-level semantic ladder:

    fatal          electrically broken — schematic won't work
    major          highly suspicious, very likely a real bug
    warning        probably okay but risky / worth a glance
    cosmetic       visual / readability — doesn't affect correctness
    synthesized    AI-generated intentional artefact (already audited)
    informational  useful metadata only — test points, reserved pins

Reclassification draws on every semantic layer the engine built:
  - `rail_tree`         (P15) — knows which power nets have drivers
  - `functional_blocks` (P12) — knows which subsystem a pin belongs to
  - `buses_by_net`      (P14) — knows the bus criticality
  - lib-symbol pin names + electrical_type — knows the pin's role
  - classifier roles    — knows which refs are MCUs vs passives

Output: every issue gets `_severity`, `_severity_reason`, and the
caller gets per-level counts plus an `overall_health` score in
0..100 suitable for CI gates and regression metrics."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from . import power_intent as _pi
    from . import bus_semantics as _bs
    from .. import nets as _nets
    from ..schematic_extractor import SchematicExtractor
except ImportError:
    from kicad_claude.layout import power_intent as _pi  # type: ignore
    from kicad_claude.layout import bus_semantics as _bs  # type: ignore
    from kicad_claude import nets as _nets  # type: ignore
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore


SEVERITY_ORDER = ("fatal", "major", "warning", "cosmetic",
                  "synthesized", "informational")

# Base severity per check name. These are the DEFAULTS — the context-
# aware classifier may up- or down-grade based on pin role, bus
# criticality, or rail driver presence.
_DEFAULT_CHECK_SEVERITY: Dict[str, str] = {
    # Hard structural failures.
    "duplicate_reference":          "fatal",
    "duplicate_pin_number":         "fatal",
    "duplicate_sheet_names":        "fatal",
    "bus_definition_conflict":      "fatal",

    # Connectivity violations — escalate on critical pins via P16.2.
    "different_unit_net":           "major",
    "hierarchical_label_mismatch":  "major",
    "sheet_pin_not_connected":      "major",
    "multiple_net_names":           "major",
    "power_pin_not_driven":         "major",   # P16.3 will downgrade
    "same_local_global_label":      "warning",

    # Floating / dangling — base warning; P16.2 promotes via pin role.
    "pin_not_connected":            "warning",
    "wire_dangling":                "warning",
    "unconnected_wire_endpoint":    "warning",
    "isolated_pin_label":           "warning",
    "label_dangles":                "warning",
    "global_label_dangles":         "warning",
    "hier_label_dangles":           "warning",
    "no_connect_connected":         "major",
    "no_connect_dangling":          "informational",

    # Cosmetic / library shape issues.
    "lib_symbol_mismatch":          "cosmetic",
    "lib_symbol_issues":            "cosmetic",
    "missing_unit":                 "cosmetic",
    "extra_units":                  "cosmetic",
    "pin_to_pin":                   "cosmetic",
    "similar_labels":               "cosmetic",
    "endpoint_off_grid":            "cosmetic",
}

# Pin-role escalation table — when a `pin_not_connected` or `_dangles`
# issue lands on a pin matching one of these roles, escalate to the
# tabled severity. Roles are derived from pin-name patterns below.
_PIN_ROLE_SEVERITY: Dict[str, str] = {
    "reset":         "fatal",        # MCU reset floating = no boot
    "clock":         "fatal",        # OSC_IN/OUT floating = no clock
    "diff_pair":     "major",        # USB_DP missing partner
    "analog_input":  "major",        # ADC input floating
    "i2c":           "major",
    "spi":           "major",
    "can":           "fatal",
    "usb":           "fatal",
    "enable":        "major",        # regulator EN pin floating
    "feedback":      "fatal",        # FB pin floating = wrong vout
    "gpio":          "warning",
    "passive":       "warning",
    "power_in":      "major",        # caught here when DAG misses
    "test_point":    "informational",
    "nc":            "informational",
    "reserved":      "informational",
    "unknown":       "warning",
}

# Bus-criticality table — escalation when a floating/dangling issue is
# on a net that belongs to a known bus.
_BUS_KIND_SEVERITY: Dict[str, str] = {
    "i2c":       "major",
    "spi":       "major",
    "uart":      "warning",        # often a debug port
    "usb":       "fatal",
    "can":       "fatal",
    "ethernet":  "fatal",
    "indexed":   "major",          # broken bit in DATA[] = corrupt
    "diff_pair": "fatal",          # broken pair member = wrong signal
}

# Pin-role pattern table. Matched against the lib symbol's pin NAME
# (case-insensitive, ~{} overline stripped) — first match wins. Order
# matters: specific patterns ("OSC_IN") before generic ("IN").
_PIN_ROLE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"^(NRST|N?RESET|MCLR|RST_N?)$", re.IGNORECASE), "reset"),
    (re.compile(r"^(OSC[12_]?(IN|OUT)?|X(TAL)?[12]?(IN|OUT)?|"
                  r"HSE(IN|OUT)?|LSE(IN|OUT)?|CLK(IN|OUT)?|MCO)$",
                 re.IGNORECASE),                                       "clock"),
    (re.compile(r"^(USB_?D[PMN]|D[PMN]|CAN[HL]|DP|DM|"
                  r"\w+_(P|N|DP|DM))$", re.IGNORECASE),                "diff_pair"),
    (re.compile(r"^(VBUS|USB_VBUS|USB_5V)$", re.IGNORECASE),           "usb"),
    (re.compile(r"^(CAN_?[HL])$", re.IGNORECASE),                      "can"),
    (re.compile(r"^(SDA|SCL|I2C\d?_(SDA|SCL))$", re.IGNORECASE),       "i2c"),
    (re.compile(r"^(MOSI|MISO|SCK|SCLK|NSS|N?CS|SPI\d?_\w+)$",
                 re.IGNORECASE),                                        "spi"),
    (re.compile(r"^(ADC\d*|AIN\d*|VINP|VINN|VIN[+\-]|"
                  r"VREF[A]?[+\-]?|IN[+\-])$", re.IGNORECASE),         "analog_input"),
    (re.compile(r"^(EN|ENABLE|SHDN|N?SHDN|N?CS|DIS|ON_?OFF)$",
                 re.IGNORECASE),                                        "enable"),
    (re.compile(r"^(FB|FEEDBACK|FBK?|COMP|VFB)$", re.IGNORECASE),      "feedback"),
    (re.compile(r"^(NC|N/C|NIC|DNU|NOT_USED)$", re.IGNORECASE),        "nc"),
    (re.compile(r"^(RSVD|RESERVED|RESERVED_\d+)$", re.IGNORECASE),     "reserved"),
    (re.compile(r"^(P[A-H]\d+|GPIO\d+|IO\d+|PIN\d+)$", re.IGNORECASE), "gpio"),
]


def classify_pin_role(pin_name: str,
                       electrical_type: str = "",
                       refdes: str = "") -> str:
    """Map a pin name (+ optional electrical type, refdes) to a P16
    role tag. Returns "unknown" when no pattern matches."""
    # Refdes-level overrides — test points cluster by TP* refdes
    # regardless of pin name.
    if refdes and refdes.upper().startswith("TP"):
        return "test_point"
    name = (pin_name or "").strip()
    if name.startswith("~{") and name.endswith("}"):
        name = name[2:-1]
    # Electrical-type hints from KiCad pin def.
    et = (electrical_type or "").lower()
    if et == "no_connect":
        return "nc"
    if et == "power_in":
        # Power-in pins get caught by the power_pin_not_driven path,
        # but in case they show up under a different check, classify
        # them so escalation still happens.
        return "power_in"
    if et == "passive":
        # Pure passive — fall through to name patterns; if no match,
        # final fallback is "passive" rather than "unknown".
        for pat, role in _PIN_ROLE_PATTERNS:
            if pat.match(name):
                return role
        return "passive"
    for pat, role in _PIN_ROLE_PATTERNS:
        if pat.match(name):
            return role
    return "unknown"


def _parse_issue_ref_pin(refs_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract (ref, pin_number) from an ERC issue's `refs` text.
    Mirrors `power_intent._parse_pin_ref` but kept local to avoid
    cross-module circular load on rare import orderings."""
    if not refs_str:
        return (None, None)
    m = re.search(r"\bPin\s+(\w+)\s+of\s+(\w+)", refs_str)
    if m:
        return (m.group(2), m.group(1))
    m = re.search(r"\bSymbol\s+(\w+)\s+Pin\s+(\w+)", refs_str)
    if m:
        return (m.group(1), m.group(2))
    for tok in refs_str.replace(",", " ").split():
        tok = tok.strip()
        if tok in ("Symbol", "Pin", "of"):
            continue
        if tok and tok[0].isalpha() and any(c.isdigit() for c in tok):
            return (tok, None)
    return (None, None)


class _Context:
    """Aggregate of every semantic layer the severity classifier needs.

    Built ONCE per ERC run. All lookups are O(1) dict access; building
    the context costs one schematic re-parse plus the power-tree
    construction (already cheap).

    Carries:
      - rail_tree (P15)              → driver presence + domain
      - functional_blocks (P12)      → block role per ref
      - bus_by_net (P14)             → bus criticality
      - pin defs (lib_symbol_pins)   → pin name + electrical type
      - dnp_by_ref                   → P16.5 intent override
      - net_centrality (degree)      → P16.4 risk weighting
      - block_by_ref                 → P16.4 critical-block aggregation
    """

    def __init__(self, schematic_path, classified=None):
        self.schematic_path = schematic_path
        self.classified = classified or {}
        self.tree = _pi.build_power_tree(schematic_path,
                                          classified=classified)
        self.driven_nets: Set[str] = _pi.driven_power_nets(self.tree)

        # Ref → pin → (name, electrical_type)
        self.pins_by_ref: Dict[str, Dict[str, Tuple[str, str]]] = defaultdict(dict)
        self.lib_id_by_ref: Dict[str, str] = {}
        # P16.5 — DNP markers per ref (intentional break / not-populated).
        self.dnp_by_ref: Dict[str, bool] = {}
        try:
            extractor = SchematicExtractor(schematic_path)
            lib_pins_by_lib = extractor.lib_symbol_pins()
            for comp in extractor.components():
                ref = comp.get("reference") or ""
                if not ref:
                    continue
                lib_id = comp.get("lib_id") or ""
                self.lib_id_by_ref[ref] = lib_id
                self.dnp_by_ref[ref] = bool(comp.get("dnp"))
                by_unit = lib_pins_by_lib.get(lib_id) or {}
                unit_no = int(comp.get("unit", 1))
                pin_defs = list(by_unit.get(0, []))
                if unit_no != 0:
                    pin_defs.extend(by_unit.get(unit_no, []))
                for pd in pin_defs:
                    self.pins_by_ref[ref][str(pd.get("number", ""))] = (
                        pd.get("name", ""), pd.get("electrical_type", ""),
                    )
            # Pin → net + bus map.
            net_data = _nets.build_sheet_nets(extractor)
            self.pin_to_net: Dict[Tuple[str, str], str] = {}
            self.refs_on_net: Dict[str, Set[str]] = defaultdict(set)
            for net in net_data["nets"]:
                nm = net.get("name", "")
                for m in net.get("members", []):
                    if m.get("kind") == "pin":
                        k = (m.get("ref", ""), str(m.get("pin_number", "")))
                        self.pin_to_net[k] = nm
                        self.refs_on_net[nm].add(k[0])
            # Bus index.
            buses = _bs.detect_buses(net_data["nets"])
            self.bus_by_net = _bs.buses_by_net(buses)
        except Exception:
            self.pin_to_net = {}
            self.refs_on_net = defaultdict(set)
            self.bus_by_net = {}

        # P16.4 — net centrality: number of refs sharing the net.
        # Multiplied by rail-importance weight to produce a per-net
        # risk multiplier in `net_importance`.
        self.net_importance: Dict[str, float] = {}
        for nm, refs in self.refs_on_net.items():
            fanout = len(refs)
            # Domain weight: analog/usb/can rails are more critical than
            # generic digital; debug nets are less critical.
            domain = _pi.rail_domain(self.tree, nm)
            domain_w = {
                "analog": 1.5, "usb": 1.5, "battery": 1.3,
                "raw": 1.2, "digital": 1.0,
            }.get(domain, 1.0)
            # Bus-kind boost — broken member of an indexed/diff bus
            # impacts the whole bus.
            bus = self.bus_by_net.get(nm)
            bus_w = 1.0
            if bus:
                bus_w = {
                    "usb": 1.6, "can": 1.6, "ethernet": 1.6,
                    "diff_pair": 1.5, "i2c": 1.3, "spi": 1.3,
                    "indexed": 1.2, "uart": 1.0, "clock": 1.4,
                }.get(bus.get("kind", ""), 1.0)
            self.net_importance[nm] = fanout * domain_w * bus_w

        # P12 functional-block tags — best-effort; missing if community
        # partition path wasn't run.
        self.block_by_ref: Dict[str, str] = {}
        try:
            from . import functional_blocks as _fb
            fb_result = _fb.assign_functional_blocks(
                schematic_path, classified or {},
            )
            self.block_by_ref = fb_result.get("block_by_ref", {})
        except Exception:
            self.block_by_ref = {}

    def pin_role(self, ref: str, pin_num: str) -> str:
        name, etype = self.pins_by_ref.get(ref, {}).get(pin_num, ("", ""))
        return classify_pin_role(name, etype, ref)

    def pin_net(self, ref: str, pin_num: str) -> Optional[str]:
        return self.pin_to_net.get((ref, pin_num))

    def rail_domain(self, net: Optional[str]) -> str:
        if not net:
            return "unknown"
        return _pi.rail_domain(self.tree, net)

    def is_dnp(self, ref: str) -> bool:
        return self.dnp_by_ref.get(ref, False)

    def block(self, ref: str) -> Optional[str]:
        return self.block_by_ref.get(ref)

    def net_importance_score(self, net: Optional[str]) -> float:
        if not net:
            return 1.0
        return self.net_importance.get(net, 1.0)


def classify_issue_severity(
    issue: Dict[str, Any],
    ctx: _Context,
) -> Tuple[str, str]:
    """Return `(severity, reason)` for a single ERC issue. `reason` is
    a short tag describing WHICH rule fired — useful for debugging the
    severity ladder and for surfacing to the user."""
    check = issue.get("check", "")
    base = _DEFAULT_CHECK_SEVERITY.get(check, "warning")

    # P9 / P15.5 carry-over — issues pre-tagged by upstream stages
    # respect their tag.
    if issue.get("_power_covered"):
        return ("synthesized", "p15.5_dag_drives_net")

    ref, pin_num = _parse_issue_ref_pin(issue.get("refs", ""))

    # P16.5 — INTENT OVERRIDE. A DNP-marked component is a do-not-
    # populate placeholder; every ERC issue against it collapses to
    # `synthesized` (intentional, not a real fault). Mirrors the
    # P9 provenance-tag escape hatch but driven by the schematic's own
    # `(dnp yes)` field.
    if ref and ctx.is_dnp(ref):
        return ("synthesized", "dnp_marker")

    # P16.3 — power_pin_not_driven WITH DAG context.
    if check == "power_pin_not_driven":
        if ref and pin_num:
            net = ctx.pin_net(ref, pin_num)
            if net and net in ctx.driven_nets:
                return ("synthesized", "driver_in_dag")
            domain = ctx.rail_domain(net)
            if domain == "unknown":
                # Rail-with-no-source AND no known domain = real fatal.
                return ("fatal", "untyped_rail_no_driver")
            # Typed rail (analog/digital/usb/battery/raw) but no DAG
            # driver — likely a sheet-crossing rail; major not fatal.
            return ("major", f"typed_rail_{domain}_no_dag_driver")
        return (base, "power_pin_no_pin_context")

    # P16.6 — intentional-floating detection. NC / RSVD / test-point
    # pins downgrade `pin_not_connected` to informational. Mostly the
    # pin role classifier already produces these tags; we surface the
    # reason explicitly.
    if check in ("pin_not_connected", "isolated_pin_label"):
        if ref and pin_num:
            role = ctx.pin_role(ref, pin_num)
            if role in ("nc", "reserved", "test_point"):
                return ("informational", f"intentional_floating:{role}")
            # P16.2 escalation by pin role.
            promoted = _PIN_ROLE_SEVERITY.get(role)
            if promoted:
                return (promoted, f"pin_role:{role}")
        return (base, "pin_not_connected_no_role_context")

    # P16.4 — bus-criticality escalation on dangling labels / wires.
    if check in ("label_dangles", "global_label_dangles",
                  "hier_label_dangles", "wire_dangling",
                  "unconnected_wire_endpoint"):
        # Net name comes from the issue's refs OR from the pin lookup
        # — try the pin path first.
        net = None
        if ref and pin_num:
            net = ctx.pin_net(ref, pin_num)
        # Sometimes the issue's `refs` field is just the label text.
        # Try treating it as a net name directly.
        if not net and ref and ref in ctx.bus_by_net:
            net = ref
        if net:
            bus = ctx.bus_by_net.get(net)
            if bus:
                kind = bus.get("kind", "")
                escalated = _BUS_KIND_SEVERITY.get(kind)
                if escalated:
                    return (escalated, f"bus:{kind}")
        return (base, "no_bus_context")

    # P16.5 — hierarchy integrity. Sheet-pin / hierarchical-label
    # mismatches are major by default; we leave them there unless we
    # see a clear synthesised-port marker on the affected ref (future:
    # parse the provenance properties).
    if check in ("hierarchical_label_mismatch",
                  "sheet_pin_not_connected"):
        return (base, "hierarchy_default")

    return (base, "default")


def compute_health_score(counts: Dict[str, int]) -> int:
    """0..100 health score. Fatal weights heaviest, then major, then
    warning. Cosmetic / synthesized / informational don't move the
    needle. Floors at 0 — once you're broken, the gradient between
    50 broken vs 200 broken doesn't matter for the gate."""
    score = 100
    score -= int(counts.get("fatal",   0)) * 25
    score -= int(counts.get("major",   0)) * 5
    score -= int(counts.get("warning", 0)) * 1
    return max(0, min(100, score))


def reclassify_erc_issues(
    issues: List[Dict[str, Any]],
    schematic_path,
    classified: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """End-to-end reclassification. Returns:

      {
        "issues":          [<issue> + _severity + _severity_reason +
                              _context {ref, pin_role, net, net_domain,
                                          bus_kind, block, dnp,
                                          net_importance}, ...],
        "counts":          {fatal,major,warning,cosmetic,
                              synthesized,informational},
        "overall_health":  0..100,
        "by_check":        {check_name: {severity: count}},
        "top_risk_blocks": [(block_name, weighted_risk), ...],
        "critical_nets":   [(net_name, weighted_risk), ...],
      }

    `top_risk_blocks` and `critical_nets` collect WHERE the fatal +
    major issues land — driven by the P16.4 net-importance score —
    so the report can answer "what subsystem is most broken" instead
    of just "how many issues exist"."""
    ctx = _Context(schematic_path, classified=classified)
    out_issues: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {s: 0 for s in SEVERITY_ORDER}
    by_check: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {s: 0 for s in SEVERITY_ORDER}
    )
    # P16.4 — weighted risk accumulators. Per-severity weights mirror
    # `compute_health_score`; risk = sev_weight * net_importance.
    sev_weight = {"fatal": 25.0, "major": 5.0, "warning": 1.0}
    block_risk: Dict[str, float] = defaultdict(float)
    net_risk:   Dict[str, float] = defaultdict(float)

    for it in issues:
        sev, reason = classify_issue_severity(it, ctx)
        new_it = dict(it)
        new_it["_severity"] = sev
        new_it["_severity_reason"] = reason
        # P16.0 — attach the context tensor to every issue. Empty when
        # we couldn't parse a ref / pin out of the ERC refs string.
        ref, pin_num = _parse_issue_ref_pin(it.get("refs", ""))
        net = ctx.pin_net(ref or "", pin_num or "") if ref else None
        new_it["_context"] = {
            "ref":             ref,
            "pin_number":      pin_num,
            "pin_role":        ctx.pin_role(ref, pin_num) if (ref and pin_num) else None,
            "net":             net,
            "net_domain":      ctx.rail_domain(net) if net else "unknown",
            "bus_kind":        (ctx.bus_by_net.get(net) or {}).get("kind") if net else None,
            "block":           ctx.block(ref) if ref else None,
            "dnp":             ctx.is_dnp(ref) if ref else False,
            "net_importance":  round(ctx.net_importance_score(net), 2),
        }
        out_issues.append(new_it)
        counts[sev] = counts.get(sev, 0) + 1
        chk = it.get("check") or "unknown"
        by_check[chk][sev] += 1
        # Accumulate weighted risk for fatal/major/warning only.
        if sev in sev_weight:
            w = sev_weight[sev] * max(1.0, new_it["_context"]["net_importance"])
            blk = new_it["_context"]["block"]
            if blk:
                block_risk[blk] += w
            if net:
                net_risk[net] += w

    top_blocks = sorted(block_risk.items(), key=lambda kv: -kv[1])[:5]
    top_nets   = sorted(net_risk.items(),   key=lambda kv: -kv[1])[:10]

    # P17.2 — normalise net_importance to percentiles so cross-tier
    # comparison is meaningful. The raw `fanout × domain × bus`
    # formula has no upper bound (GND on big = 1250; NE555 GND ~ 8);
    # the 0..100 form lets the gate interpret "75 = top-quartile" the
    # same way regardless of design size.
    from . import causal_reasoning as _cr
    importance_pct = _cr.normalize_net_importance(ctx.net_importance)
    # Re-stamp _context entries with the percentile-form score.
    for it in out_issues:
        nm = (it.get("_context") or {}).get("net")
        if nm and nm in importance_pct:
            it["_context"]["net_importance_pct"] = importance_pct[nm]

    # P17.1 — causal reports for fatal+major issues. Cross-cluster
    # edges come from the caller (we don't have access to the
    # partition result here); pass empty for now — explanations still
    # carry cause_chain + expected/actual, just no downstream
    # propagation list.
    causal_reports = _cr.analyze_critical_issues(out_issues, ctx)

    # P18 — auto-repair plan. Collapses per-issue causal reports into
    # root causes and proposes a small set of repair operations
    # (INSERT_REGULATOR / INSERT_CRYSTAL / INSERT_PULLUP / ADD_NC_FLAG
    # / RECONNECT_NET / LABEL_NET / CONNECT_DIFF_PAIR / CHECK_BUS_PEER)
    # with per-op confidence and an estimated post-fix health score.
    repair_plan = None
    try:
        from . import auto_repair as _ar
        plan = _ar.plan_repairs(causal_reports, ctx.tree,
                                  severity_counts=counts)
        repair_plan = {
            "summary":         plan.summary,
            "health_estimate": plan.health_estimate,
            "root_causes":     [
                {"kind": r.kind, "summary": r.summary,
                  "affected_issues": r.affected_issues,
                  "proposed_ops": r.proposed_ops}
                for r in plan.root_causes
            ],
            "ops":             [
                {"op_type": o.op_type, "target": o.target,
                  "rationale": o.rationale, "fixes": o.fixes,
                  "confidence": o.confidence, "risk": o.risk}
                for o in plan.ops
            ],
        }
        # P18.5 — feasibility filter. Runs the raw P18 ops through
        # structural-existence + intent-override checks. Result rides
        # alongside the raw plan under `feasibility`; the chat-card
        # / execution layer should read `accepted_ops` (kept +
        # softened) rather than the raw `ops` list.
        try:
            from . import repair_feasibility as _rf
            feas = _rf.filter_repair_plan(repair_plan, ctx)
            repair_plan["feasibility"] = feas
        except Exception:
            repair_plan["feasibility"] = None
    except Exception:
        repair_plan = None

    return {
        "issues":          out_issues,
        "counts":          counts,
        "overall_health":  compute_health_score(counts),
        "by_check":        {k: dict(v) for k, v in by_check.items()},
        "top_risk_blocks": [(b, round(r, 1)) for b, r in top_blocks],
        "critical_nets":   [(n, round(r, 1)) for n, r in top_nets],
        "causal_reports":  causal_reports,
        "net_importance_pct": importance_pct,
        "repair_plan":     repair_plan,
    }
