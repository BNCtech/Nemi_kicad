"""P15 — power intent engine.

The connectivity graph treats every power rail as just another named
net. But humans look at a schematic and reason in terms of POWER TREES:

    VIN(barrel)  →  protection  →  LDO  →  +3V3  →  AVDD (via ferrite)
                                            └── DVDD (direct)
    VBAT(battery) →  charger    →  VSYS  →  buck →  +5V
    VBUS(usb)    →  protection  →  +5V

Each ARROW is a regulator / source / boundary element. Each NODE is a
rail with a known domain (raw / digital / analog / battery / usb) and a
known driver. This module reconstructs that tree from the schematic so
downstream stages can:

  - place decoupling caps on the CORRECT rail (P15.2 — an AVDD cap goes
    next to an AVDD pin, not just any +3V3 pin),
  - recategorise ERC `power_pin_not_driven` warnings when the DAG shows
    a real driver exists (P15.5 — collapses the false-positive flood
    on schematics where the driver is on a different sheet),
  - inform future power-aware hierarchy splits (P15.6) and regulator
    chain layout (P15.4) — both deferred but built on this datum.

Output schema:

  {
    "regulators":  [{"ref","lib_id","type":"ldo"|"buck"|"switching"|"charger",
                     "input_net","output_net","gnd_net","enable_net"}],
    "sources":     [{"ref","kind":"usb"|"battery"|"barrel"|"header",
                     "output_net"}],
    "boundaries":  [{"ref","kind":"ferrite"|"inductor"|"fuse",
                     "net_a","net_b"}],
    "rail_tree":   {net_name: {"domain": "raw"|"digital"|"analog"
                                          |"battery"|"usb"|"unknown",
                                "driver_ref":  Optional[str],
                                "driver_kind": Optional[str],
                                "upstream":    Optional[str]}},
    "stats": {regulators, sources, boundaries, rails_typed},
  }

Pure analysis. No file mutation. The detector is conservative — when
ambiguous it returns `domain="unknown"` rather than guess wrong."""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    from ..schematic_extractor import SchematicExtractor
    from .. import nets as _nets
except ImportError:
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore
    from kicad_claude import nets as _nets  # type: ignore


# Pin-name patterns for regulator I/O. Order matters for OUT detection:
# "VOUT" beats "V+" inside a part that has both — V+ on a charge-pump
# is misleading. Names are matched case-insensitively against the lib-
# symbol pin name AFTER stripping surrounding `~{}` overline markers.
_REG_INPUT_PINS  = ("VIN", "VI", "V+", "IN", "INPUT", "VS",  "VDD_IN",
                    "VBUS", "VBAT", "VSYS_IN")
_REG_OUTPUT_PINS = ("VOUT", "VO", "V-", "OUT", "OUTPUT",
                    "VCC_OUT", "VDD_OUT", "SW", "LX")
_REG_GROUND_PINS = ("GND", "VSS", "AGND", "DGND", "PGND", "EGND")
_REG_ENABLE_PINS = ("EN", "ENABLE", "SHDN", "NSHDN", "DIS", "ON_OFF")

# Regulator family detection from lib_id (token-bounded, like the P12
# OLED fix — `_LDO_` not bare `LDO`).
_REG_LIB_PATTERNS = (
    (re.compile(r"Regulator_Linear", re.IGNORECASE),        "ldo"),
    (re.compile(r"_LDO(_|:|$)|:LDO_",  re.IGNORECASE),      "ldo"),
    (re.compile(r"Regulator_Switching", re.IGNORECASE),     "buck"),
    (re.compile(r"Buck|Boost|SEPIC|Flyback", re.IGNORECASE),"buck"),
    (re.compile(r"Charge[r_]|BQ\d{4}|TP4056|MCP73", re.IGNORECASE),
                                                             "charger"),
    (re.compile(r"Regulator_SwitchedCapacitor",
                 re.IGNORECASE),                             "switching"),
)

# Power-rail name patterns — used both for source detection and domain
# tagging. Domain order: explicit analog > explicit digital > generic.
_ANALOG_NET_RE = re.compile(
    r"^(AVDD|AVCC|VDDA|VCCA|AGND|VREF[+\-]?|VREFA|VANA)(_[A-Z0-9]+)?$",
    re.IGNORECASE,
)
_DIGITAL_NET_RE = re.compile(
    r"^(DVDD|DVCC|VDDIO|VCCIO|DGND|VDD|VCC)(_[A-Z0-9]+)?$",
    re.IGNORECASE,
)
_BATTERY_NET_RE = re.compile(
    r"^(VBAT|VBATT|VCELL[123]?|BAT[+\-])$", re.IGNORECASE,
)
_USB_NET_RE     = re.compile(
    r"^(VBUS(_USB)?|USB_VBUS|USB_5V)$", re.IGNORECASE,
)
_RAW_NET_RE     = re.compile(
    r"^(VIN|VI|VSYS|VRAW|V_IN|VPP)(_[A-Z0-9]+)?$", re.IGNORECASE,
)

# Source-component patterns.
_SOURCE_LIB_PATTERNS = (
    (re.compile(r"Connector(_Generic)?:.*USB|USB_[ABC]|USB_Micro|USB_Mini",
                 re.IGNORECASE),                  "usb"),
    (re.compile(r"Battery|BatteryCell",
                 re.IGNORECASE),                  "battery"),
    (re.compile(r"Conn_Barrel_Jack|Barrel|Conn_Power",
                 re.IGNORECASE),                  "barrel"),
)

# Boundary-component patterns. Two-pin elements that don't drive a new
# rail but split the routing topology (ferrite for analog isolation,
# fuse for protection). `net_a` / `net_b` are the two pin nets.
_BOUNDARY_LIB_PATTERNS = (
    (re.compile(r"FerriteBead|Ferrite_Bead|^Device:FB$",
                 re.IGNORECASE),                  "ferrite"),
    (re.compile(r":L_|Device:L$|Inductor",
                 re.IGNORECASE),                  "inductor"),
    (re.compile(r"Fuse|PolyFuse|PTC", re.IGNORECASE),
                                                  "fuse"),
)


def _strip_overline(name: str) -> str:
    """KiCad pin names may be wrapped in `~{…}` for overline. Strip
    so `~{EN}` matches the literal `EN` pattern."""
    n = (name or "").strip()
    if n.startswith("~{") and n.endswith("}"):
        return n[2:-1]
    return n


def _classify_pin_role(pin_name: str) -> Optional[str]:
    """Return one of {"input","output","gnd","enable"} based on the pin
    name. None when unrecognised — caller falls back to electrical_type."""
    up = _strip_overline(pin_name).upper().lstrip("+-")
    if not up:
        return None
    if up in _REG_GROUND_PINS:
        return "gnd"
    if any(up == p or up.startswith(p + "_") for p in _REG_INPUT_PINS):
        return "input"
    if any(up == p or up.startswith(p + "_") for p in _REG_OUTPUT_PINS):
        return "output"
    if any(up == p or up.startswith(p + "_") for p in _REG_ENABLE_PINS):
        return "enable"
    return None


def _classify_regulator_lib(lib_id: str) -> Optional[str]:
    for pat, kind in _REG_LIB_PATTERNS:
        if pat.search(lib_id or ""):
            return kind
    return None


def _classify_source_lib(lib_id: str) -> Optional[str]:
    for pat, kind in _SOURCE_LIB_PATTERNS:
        if pat.search(lib_id or ""):
            return kind
    return None


def _classify_boundary_lib(lib_id: str) -> Optional[str]:
    for pat, kind in _BOUNDARY_LIB_PATTERNS:
        if pat.search(lib_id or ""):
            return kind
    return None


def _classify_net_domain(net_name: str) -> str:
    """Heuristic net-name → domain. Used when no driver is found
    (root-of-tree case) or as a tie-breaker when DAG walking can't
    decide."""
    if not net_name:
        return "unknown"
    name = net_name.lstrip("+-")
    if _ANALOG_NET_RE.match(name):
        return "analog"
    if _USB_NET_RE.match(name):
        return "usb"
    if _BATTERY_NET_RE.match(name):
        return "battery"
    if _RAW_NET_RE.match(name):
        return "raw"
    if _DIGITAL_NET_RE.match(name):
        return "digital"
    return "unknown"


def build_power_tree(
    schematic_path,
    classified: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """End-to-end power-tree reconstruction. See module docstring for
    the output schema. `classified` is optional but lets us catch
    regulators that lack a recognisable lib_id (POWER_REGULATOR role
    is a strong hint)."""
    extractor = SchematicExtractor(schematic_path)
    net_data = _nets.build_sheet_nets(extractor)
    lib_pins_by_lib = extractor.lib_symbol_pins()

    role_by_ref: Dict[str, str] = {}
    if classified:
        for n in classified.get("nodes", []):
            ref = n.get("ref")
            if ref and n.get("role"):
                role_by_ref[ref] = n["role"]

    # Build ref → {pin_number: (pin_name, electrical_type)} once.
    pins_by_ref: Dict[str, Dict[str, Tuple[str, str]]] = defaultdict(dict)
    lib_id_by_ref: Dict[str, str] = {}
    for orig in extractor.components():
        ref = orig.get("reference") or ""
        if not ref:
            continue
        lib_id = orig.get("lib_id") or ""
        lib_id_by_ref[ref] = lib_id
        by_unit = lib_pins_by_lib.get(lib_id) or {}
        unit_no = int(orig.get("unit", 1))
        pin_defs: List[Dict[str, Any]] = list(by_unit.get(0, []))
        if unit_no != 0:
            pin_defs.extend(by_unit.get(unit_no, []))
        for pd in pin_defs:
            num = str(pd.get("number", ""))
            pins_by_ref[ref][num] = (
                pd.get("name", ""), pd.get("electrical_type", ""),
            )

    # Build (ref, pin_number) → net_name index from build_sheet_nets.
    pin_to_net: Dict[Tuple[str, str], str] = {}
    nets_by_ref: Dict[str, Set[str]] = defaultdict(set)
    for net in net_data["nets"]:
        nm = net.get("name", "")
        for m in net.get("members", []):
            if m.get("kind") != "pin":
                continue
            key = (m.get("ref", ""), str(m.get("pin_number", "")))
            pin_to_net[key] = nm
            nets_by_ref[key[0]].add(nm)

    # --- Detect REGULATORS ----------------------------------------
    regulators: List[Dict[str, Any]] = []
    for ref, lib_id in lib_id_by_ref.items():
        kind = _classify_regulator_lib(lib_id)
        if not kind and role_by_ref.get(ref) == "POWER_REGULATOR":
            # Classifier flagged it as a regulator even if the lib_id
            # patterns missed (e.g. envil_generated synthesized symbol).
            kind = "ldo"
        if not kind:
            continue
        in_net = out_net = gnd_net = en_net = None
        for pin_num, (pin_name, _etype) in pins_by_ref.get(ref, {}).items():
            net = pin_to_net.get((ref, pin_num))
            if not net:
                continue
            role = _classify_pin_role(pin_name)
            if role == "input" and not in_net:
                in_net = net
            elif role == "output" and not out_net:
                out_net = net
            elif role == "gnd" and not gnd_net:
                gnd_net = net
            elif role == "enable" and not en_net:
                en_net = net
        # Charge-pumps / boost converters often have V+ as INPUT and
        # V- as OUTPUT — but our pattern table treats both as I/O. When
        # in_net == out_net or one is missing, fall back to picking the
        # net with the highest "raw" score for input and the highest
        # rail-named score for output.
        if not in_net or not out_net or in_net == out_net:
            candidates = [(net, _classify_net_domain(net))
                           for _pin, net in
                           ((p, pin_to_net.get((ref, p)))
                              for p in pins_by_ref.get(ref, {}))
                           if net]
            raws = [n for n, d in candidates if d in ("raw", "usb",
                                                       "battery")]
            rails = [n for n, d in candidates
                      if d in ("digital", "analog")]
            if not in_net and raws:
                in_net = raws[0]
            if not out_net and rails:
                out_net = rails[0]
        regulators.append({
            "ref":          ref,
            "lib_id":       lib_id,
            "type":         kind,
            "input_net":    in_net,
            "output_net":   out_net,
            "gnd_net":      gnd_net,
            "enable_net":   en_net,
        })

    # --- Detect SOURCES (USB / Battery / Barrel jack) -------------
    sources: List[Dict[str, Any]] = []
    for ref, lib_id in lib_id_by_ref.items():
        kind = _classify_source_lib(lib_id)
        if not kind:
            continue
        # Pick the "output" net from this component: the named-rail
        # pin (VBUS for USB, V+ / VCC for battery, the non-GND pin
        # for barrel).
        out_net = None
        for pin_num, (pin_name, _etype) in pins_by_ref.get(ref, {}).items():
            net = pin_to_net.get((ref, pin_num))
            if not net:
                continue
            up = _strip_overline(pin_name).upper().lstrip("+-")
            if up in ("VBUS", "VCC", "V+", "VBAT", "VOUT", "POWER"):
                out_net = net
                break
            domain = _classify_net_domain(net)
            if domain in ("usb", "battery", "raw"):
                out_net = net
                break
        if not out_net:
            # Single non-GND net? Take it.
            non_gnd = [pin_to_net.get((ref, p)) for p in
                        pins_by_ref.get(ref, {})
                        if pin_to_net.get((ref, p))
                        and pin_to_net.get((ref, p)).upper() not in
                            ("GND", "VSS", "AGND", "DGND")]
            if len(set(non_gnd)) == 1:
                out_net = non_gnd[0]
        if out_net:
            sources.append({
                "ref":         ref,
                "kind":        kind,
                "output_net":  out_net,
            })

    # --- Detect BOUNDARIES (ferrite / fuse / inductor on power) ---
    boundaries: List[Dict[str, Any]] = []
    for ref, lib_id in lib_id_by_ref.items():
        kind = _classify_boundary_lib(lib_id)
        if not kind:
            continue
        nets = sorted(set(nets_by_ref.get(ref, ())))
        if len(nets) != 2:
            continue
        a, b = nets
        # Only count as a power boundary if BOTH nets resolve to a
        # power domain — otherwise it's a signal-line ferrite / fuse,
        # not a rail boundary.
        da = _classify_net_domain(a)
        db = _classify_net_domain(b)
        if "unknown" in (da, db):
            # Last-chance check: net is referenced from a regulator
            # output OR carries a +N pattern → still a power net.
            reg_outputs = {r["output_net"] for r in regulators
                            if r["output_net"]}
            if a in reg_outputs or b in reg_outputs:
                pass
            elif a.lstrip("+-").upper().startswith(("V",)) and \
                 b.lstrip("+-").upper().startswith(("V",)):
                pass
            else:
                continue
        boundaries.append({
            "ref":   ref,
            "kind":  kind,
            "net_a": a,
            "net_b": b,
        })

    # --- Build RAIL TREE -------------------------------------------
    # Start with every net we know about; tag whatever has obvious
    # domain from its name, then walk forward from sources/regulators.
    rail_tree: Dict[str, Dict[str, Any]] = {}
    for net in net_data["nets"]:
        nm = net.get("name", "")
        if not nm:
            continue
        rail_tree[nm] = {
            "domain":      _classify_net_domain(nm),
            "driver_ref":  None,
            "driver_kind": None,
            "upstream":    None,
        }

    # Sources define their output rail's driver.
    for s in sources:
        nm = s["output_net"]
        if nm in rail_tree:
            rail_tree[nm]["driver_ref"]  = s["ref"]
            rail_tree[nm]["driver_kind"] = s["kind"]
            # Source-domain assignment overrules name-based guess when
            # the source kind is unambiguous (USB / battery / barrel).
            if s["kind"] == "usb":
                rail_tree[nm]["domain"] = "usb"
            elif s["kind"] == "battery":
                rail_tree[nm]["domain"] = "battery"
            elif s["kind"] == "barrel":
                rail_tree[nm]["domain"] = "raw"

    # Regulators define edges input_net → output_net. The output's
    # driver = the regulator; its `upstream` = the input net.
    for r in regulators:
        out_n = r["output_net"]
        in_n  = r["input_net"]
        if out_n and out_n in rail_tree:
            rail_tree[out_n]["driver_ref"]  = r["ref"]
            rail_tree[out_n]["driver_kind"] = r["type"]
            if in_n:
                rail_tree[out_n]["upstream"] = in_n
            # Propagate domain: a regulator output inherits "digital"
            # by default unless the net name says analog / its input
            # came from a battery / etc.
            if rail_tree[out_n]["domain"] == "unknown":
                # Look at the output net name first.
                rail_tree[out_n]["domain"] = "digital"

    # Boundaries: propagate domain across them so an LDO output going
    # through a ferrite into AVDD tags both sides correctly. We don't
    # set driver_ref on the downstream — the FERRITE isn't a driver,
    # it's a filter.
    for b in boundaries:
        a_dom = rail_tree.get(b["net_a"], {}).get("domain", "unknown")
        b_dom = rail_tree.get(b["net_b"], {}).get("domain", "unknown")
        # Ferrite typically separates digital (upstream) from analog
        # (downstream). Name-based detection should already tag the
        # analog side; if not, infer from the partner.
        if a_dom == "unknown" and b_dom != "unknown":
            rail_tree[b["net_a"]]["domain"] = b_dom
        if b_dom == "unknown" and a_dom != "unknown":
            rail_tree[b["net_b"]]["domain"] = a_dom

    rails_typed = sum(1 for r in rail_tree.values()
                      if r["domain"] != "unknown")

    return {
        "regulators":  regulators,
        "sources":     sources,
        "boundaries":  boundaries,
        "rail_tree":   rail_tree,
        "stats": {
            "regulators": len(regulators),
            "sources":    len(sources),
            "boundaries": len(boundaries),
            "rails_typed": rails_typed,
            "rails_total": len(rail_tree),
        },
    }


def driven_power_nets(tree: Dict[str, Any]) -> Set[str]:
    """Return the set of net names that have a known driver (regulator
    output or source). P15.5 uses this to suppress
    `power_pin_not_driven` ERC warnings on rails the engine knows are
    driven (and KiCad's ERC just couldn't see across sheets / through
    a power-port-less synthesized rail)."""
    out: Set[str] = set()
    for nm, info in (tree.get("rail_tree") or {}).items():
        if info.get("driver_ref"):
            out.add(nm)
    return out


def rail_domain(tree: Dict[str, Any], net_name: str) -> str:
    """Convenience lookup. Returns `"unknown"` when net unknown."""
    return (tree.get("rail_tree") or {}).get(net_name, {}).get(
        "domain", "unknown",
    )


def cap_rail_map(
    tree: Dict[str, Any],
    schematic_path,
) -> Dict[str, Dict[str, Any]]:
    """P15.2 helper — for every 2-pin capacitor, return the power rail
    it decouples (the NON-GND net) and that rail's domain.

    Output: `{C_ref: {"rail": net_name, "domain": "analog"|"digital"|
                       "battery"|"usb"|"raw"|"unknown",
                       "rail_has_driver": bool}}`

    Lets downstream placers prefer same-domain anchors (an AVDD cap
    should stick to an AVDD pin on the IC, not a DVDD pin even though
    both are "power_in" type)."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        extractor = SchematicExtractor(schematic_path)
        net_data = _nets.build_sheet_nets(extractor)
    except Exception:
        return out
    rail_tree = tree.get("rail_tree") or {}

    nets_by_ref: Dict[str, Set[str]] = defaultdict(set)
    for net in net_data["nets"]:
        nm = net.get("name", "")
        for m in net.get("members", []):
            if m.get("kind") == "pin":
                r = m.get("ref", "")
                if r:
                    nets_by_ref[r].add(nm)

    for ref, nets in nets_by_ref.items():
        if not ref.startswith("C"):
            continue
        if len(nets) != 2:
            continue
        # Pick the non-GND net as the rail.
        gnd_like = {n for n in nets
                    if n.upper().lstrip("+-").startswith(
                        ("GND", "VSS", "AGND", "DGND", "PGND", "EGND"))}
        rail_candidates = nets - gnd_like
        if len(rail_candidates) != 1:
            continue
        rail = next(iter(rail_candidates))
        info = rail_tree.get(rail, {})
        out[ref] = {
            "rail":           rail,
            "domain":         info.get("domain", "unknown"),
            "rail_has_driver": bool(info.get("driver_ref")),
        }
    return out


def _parse_pin_ref(refs_str: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract (ref, pin_number) from an ERC issue's `refs` text.

    KiCad emits forms like:
      "Pin 1 of U2"            → ("U2", "1")
      "Symbol U1 Pin 5"        → ("U1", "5")
      "U1"                     → ("U1", None)

    We return whatever we can recover; caller handles None gracefully."""
    if not refs_str:
        return (None, None)
    # Strict patterns first.
    m = re.search(r"\bPin\s+(\w+)\s+of\s+(\w+)", refs_str)
    if m:
        return (m.group(2), m.group(1))
    m = re.search(r"\bSymbol\s+(\w+)\s+Pin\s+(\w+)", refs_str)
    if m:
        return (m.group(1), m.group(2))
    # Fallback: first refdes-shaped token.
    for tok in refs_str.replace(",", " ").split():
        tok = tok.strip()
        if tok in ("Symbol", "Pin", "of"):
            continue
        if tok and tok[0].isalpha() and any(c.isdigit() for c in tok):
            return (tok, None)
    return (None, None)


def tag_component_domains(
    tree: Dict[str, Any],
    schematic_path,
) -> Dict[str, str]:
    """P15.3 — for every component, return the dominant power-rail
    domain it touches: `"analog"`, `"digital"`, `"mixed"`, `"battery"`,
    `"usb"`, `"raw"`, or `"unknown"`.

    Algorithm:
      1. Build ref → set(net) from `build_sheet_nets`.
      2. For each ref, collect the rail_tree domains of every net it
         touches (excluding the unknown-domain GND/floating nets).
      3. If the set is a single non-unknown value → that domain.
         If it contains both `analog` and `digital` → `mixed` (the
         ref bridges the two domains, e.g. an op-amp with digital
         supply).
         Otherwise → the most-specific tag that DOES appear
         (analog > usb > battery > raw > digital > unknown)."""
    out: Dict[str, str] = {}
    rail_tree = tree.get("rail_tree") or {}
    try:
        extractor = SchematicExtractor(schematic_path)
        net_data = _nets.build_sheet_nets(extractor)
    except Exception:
        return out

    specificity = {
        "analog": 6, "usb": 5, "battery": 4, "raw": 3, "digital": 2,
        "unknown": 0,
    }

    nets_by_ref: Dict[str, Set[str]] = defaultdict(set)
    for net in net_data["nets"]:
        nm = net.get("name", "")
        for m in net.get("members", []):
            if m.get("kind") == "pin":
                r = m.get("ref", "")
                if r:
                    nets_by_ref[r].add(nm)

    for ref, nets in nets_by_ref.items():
        domains: Set[str] = set()
        for nm in nets:
            d = rail_tree.get(nm, {}).get("domain", "unknown")
            if d != "unknown":
                domains.add(d)
        if not domains:
            out[ref] = "unknown"
            continue
        if "analog" in domains and "digital" in domains:
            out[ref] = "mixed"
            continue
        out[ref] = max(domains, key=lambda d: specificity.get(d, 0))
    return out


def domain_intra_edges(
    domain_by_ref: Dict[str, str],
    *,
    same_domain_weight: float = 2.0,
) -> List[Tuple[str, str, float, str]]:
    """P15.3 — build a same-domain edge list for injection into the
    community partition graph. Mirrors `functional_blocks.intra_block_edges`.

    Only analog↔analog pairs get edges (digital is the implicit
    default — boosting digital would just thicken the existing graph
    everywhere). `mixed` refs are NOT bridged: they're the actual
    isolation points and should stay neutral so Louvain places them at
    a community boundary.

    Returns `[(ref_a, ref_b, weight, "domain:analog"), ...]`. Quadratic
    in analog-set size; typical analog sub-circuits are 4-15 components
    so the count stays small."""
    analog_refs = sorted(r for r, d in domain_by_ref.items() if d == "analog")
    out: List[Tuple[str, str, float, str]] = []
    for i, a in enumerate(analog_refs):
        for b in analog_refs[i + 1:]:
            out.append((a, b, float(same_domain_weight), "domain:analog"))
    return out


def regulator_chains(
    tree: Dict[str, Any],
    schematic_path,
) -> List[Dict[str, Any]]:
    """P15.4 — for each regulator, identify the canonical chain of
    surrounding passives:

      input_source  →  [input_protection]  →  REGULATOR  →
      [output_bulk_cap]  →  [ferrite]  →  load

    Returns `[{"regulator": ref, "input_bulk_caps": [...],
                "output_bulk_caps": [...], "ferrite_refs": [...],
                "input_protection": [...], "axis_hint": "x" | "y"}]`.

    Pure detection (no placement mutation). The chain-layout post-pass
    uses this to snap chain components along the regulator's I/O axis."""
    chains: List[Dict[str, Any]] = []
    if not tree.get("regulators"):
        return chains

    try:
        extractor = SchematicExtractor(schematic_path)
        net_data = _nets.build_sheet_nets(extractor)
    except Exception:
        return chains

    # Build (net → set(refs)) and (ref → set(nets)) once.
    refs_on_net: Dict[str, Set[str]] = defaultdict(set)
    nets_by_ref: Dict[str, Set[str]] = defaultdict(set)
    for net in net_data["nets"]:
        nm = net.get("name", "")
        for m in net.get("members", []):
            if m.get("kind") == "pin":
                r = m.get("ref", "")
                if r:
                    refs_on_net[nm].add(r)
                    nets_by_ref[r].add(nm)

    boundaries_by_net: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for b in tree.get("boundaries") or ():
        boundaries_by_net[b["net_a"]].append(b)
        boundaries_by_net[b["net_b"]].append(b)

    def _gnd_like(net: str) -> bool:
        return net.upper().lstrip("+-").startswith(
            ("GND", "VSS", "AGND", "DGND", "PGND", "EGND"))

    def _two_pin_caps_on(net: str) -> List[str]:
        caps: List[str] = []
        for ref in refs_on_net.get(net, ()):
            if not ref.startswith("C"):
                continue
            r_nets = nets_by_ref.get(ref, set())
            if len(r_nets) != 2:
                continue
            other = next(iter(r_nets - {net}), None)
            if other and _gnd_like(other):
                caps.append(ref)
        return caps

    def _protection_on(net: str) -> List[str]:
        # Fuses / TVS / polyfuses sitting on this net per P15.1 detector.
        out: List[str] = []
        for b in boundaries_by_net.get(net, ()):
            if b["kind"] in ("fuse",):
                out.append(b["ref"])
        return out

    def _ferrite_on(net: str) -> List[str]:
        return [b["ref"] for b in boundaries_by_net.get(net, ())
                if b["kind"] in ("ferrite", "inductor")]

    for r in tree["regulators"]:
        in_net  = r.get("input_net")
        out_net = r.get("output_net")
        if not in_net or not out_net:
            continue
        chains.append({
            "regulator":        r["ref"],
            "regulator_type":   r["type"],
            "input_net":        in_net,
            "output_net":       out_net,
            "input_bulk_caps":  _two_pin_caps_on(in_net),
            "output_bulk_caps": _two_pin_caps_on(out_net),
            "input_protection": _protection_on(in_net),
            "ferrite_refs":     _ferrite_on(out_net),
            "axis_hint":        "x",  # default left-to-right
        })
    return chains


def refine_regulator_chains(
    placement: Dict[str, Any],
    tree: Dict[str, Any],
    schematic_path,
    *,
    chain_spacing_mm: float = 7.62,
    max_chain_move_mm: float = 25.4,
    min_clearance_mm: float = 1.27,
) -> Dict[str, Any]:
    """P15.4 — post-placement pass that snaps every regulator's chain
    components onto a straight axis with consistent spacing.

    Chain target layout (axis = X):

      ┌─protection─┬─in_caps─┬─REGULATOR─┬─out_caps─┬─ferrite─┐
      │   x = R-3s  │ x=R-2s  │   x=R    │ x=R+2s   │ x=R+3s  │
      └────────────┴─────────┴──────────┴─────────┴─────────┘

    where `s = chain_spacing_mm`. The regulator's Y stays fixed; each
    chain component moves to the column and shares the regulator's Y.
    Conservative: per-component move > `max_chain_move_mm` cancels the
    snap for THAT component (regulator + remaining chain still align).
    Collision check prevents stacking on a non-chain component."""
    chains = regulator_chains(tree, schematic_path)
    placed_by_ref = {c["ref"]: c for c in placement.get("components", [])
                      if c.get("ref")}
    if not chains or not placed_by_ref:
        return {"regulator_chains": 0, "chain_components_moved": 0,
                "chain_skipped_too_far": 0,
                "chain_skipped_collision": 0}

    static_bboxes: List[Tuple[float, float, float, float]] = []
    bbox_by_ref: Dict[str, Tuple[float, float, float, float]] = {}
    for ref, comp in placed_by_ref.items():
        cx, cy = float(comp.get("x_mm", 0)), float(comp.get("y_mm", 0))
        w, h = 2.54, 5.08
        bbox = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        static_bboxes.append(bbox)
        bbox_by_ref[ref] = bbox

    def _collides(target_x: float, target_y: float,
                  exclude_ref: str) -> bool:
        w, h = 2.54, 5.08
        x1, y1 = target_x - w / 2, target_y - h / 2
        x2, y2 = target_x + w / 2, target_y + h / 2
        own = bbox_by_ref.get(exclude_ref)
        for bb in static_bboxes:
            if own and abs(bb[0] - own[0]) < 0.01 and abs(bb[1] - own[1]) < 0.01:
                continue
            ox1, oy1, ox2, oy2 = bb
            if x2 < ox1 - min_clearance_mm or x1 > ox2 + min_clearance_mm:
                continue
            if y2 < oy1 - min_clearance_mm or y1 > oy2 + min_clearance_mm:
                continue
            return True
        return False

    moved = 0
    too_far = 0
    collisions = 0
    chains_processed = 0

    for ch in chains:
        reg_ref = ch["regulator"]
        reg = placed_by_ref.get(reg_ref)
        if not reg:
            continue
        rx, ry = float(reg["x_mm"]), float(reg["y_mm"])
        # Build the ordered chain (left → right of regulator):
        # [protection..., in_caps..., REG, out_caps..., ferrite...]
        left  = list(ch["input_protection"]) + list(ch["input_bulk_caps"])
        right = list(ch["output_bulk_caps"])  + list(ch["ferrite_refs"])
        if not left and not right:
            continue
        chains_processed += 1

        # Apply moves. Left chain: offsets are negative (regulator − N*s).
        for i, lref in enumerate(reversed(left), start=1):
            tgt_x = rx - i * chain_spacing_mm
            tgt_y = ry
            comp = placed_by_ref.get(lref)
            if not comp:
                continue
            cx, cy = float(comp["x_mm"]), float(comp["y_mm"])
            dist = ((tgt_x - cx) ** 2 + (tgt_y - cy) ** 2) ** 0.5
            if dist > max_chain_move_mm:
                too_far += 1
                continue
            if _collides(tgt_x, tgt_y, lref):
                collisions += 1
                continue
            comp["x_mm"] = float(tgt_x)
            comp["y_mm"] = float(tgt_y)
            # Update bbox so subsequent chain components see the new
            # position.
            old = bbox_by_ref[lref]
            new = (tgt_x - 1.27, tgt_y - 2.54, tgt_x + 1.27, tgt_y + 2.54)
            bbox_by_ref[lref] = new
            for k, bb in enumerate(static_bboxes):
                if abs(bb[0] - old[0]) < 0.01 and abs(bb[1] - old[1]) < 0.01:
                    static_bboxes[k] = new
                    break
            moved += 1

        for i, rref in enumerate(right, start=1):
            tgt_x = rx + i * chain_spacing_mm
            tgt_y = ry
            comp = placed_by_ref.get(rref)
            if not comp:
                continue
            cx, cy = float(comp["x_mm"]), float(comp["y_mm"])
            dist = ((tgt_x - cx) ** 2 + (tgt_y - cy) ** 2) ** 0.5
            if dist > max_chain_move_mm:
                too_far += 1
                continue
            if _collides(tgt_x, tgt_y, rref):
                collisions += 1
                continue
            comp["x_mm"] = float(tgt_x)
            comp["y_mm"] = float(tgt_y)
            old = bbox_by_ref[rref]
            new = (tgt_x - 1.27, tgt_y - 2.54, tgt_x + 1.27, tgt_y + 2.54)
            bbox_by_ref[rref] = new
            for k, bb in enumerate(static_bboxes):
                if abs(bb[0] - old[0]) < 0.01 and abs(bb[1] - old[1]) < 0.01:
                    static_bboxes[k] = new
                    break
            moved += 1

    return {
        "regulator_chains":        chains_processed,
        "chain_components_moved":  moved,
        "chain_skipped_too_far":   too_far,
        "chain_skipped_collision": collisions,
    }


def recategorize_power_issues(
    issues: List[Dict[str, Any]],
    tree: Dict[str, Any],
    schematic_path,
) -> Dict[str, Any]:
    """P15.5 — recategorise `power_pin_not_driven` ERC warnings using
    the reconstructed power DAG.

    Logic per issue:
      1. Pull (ref, pin_number) out of `issue["refs"]`.
      2. Look up that pin's net via `build_sheet_nets`.
      3. If the net is in `driven_power_nets(tree)` (a regulator or
         source drives it), tag the issue with `category=
         power_covered` — KiCad couldn't see the driver because of
         hierarchy / power-port absence / synthesized symbol, but the
         engine can prove it exists.
      4. Otherwise leave the issue alone (probably a genuine missing
         driver — keep the structural flag).

    Returns `{"issues": [...], "stats": {"covered": N, "uncovered": M}}`.
    Caller folds `covered` into a `power_covered` bucket separate from
    structural so the test gate counts only genuine driver failures."""
    driven = driven_power_nets(tree)
    if not driven:
        return {"issues": issues, "stats": {"covered": 0,
                                              "uncovered": 0}}
    try:
        extractor = SchematicExtractor(schematic_path)
        net_data = _nets.build_sheet_nets(extractor)
    except Exception:
        return {"issues": issues, "stats": {"covered": 0,
                                              "uncovered": 0}}
    pin_to_net: Dict[Tuple[str, str], str] = {}
    ref_nets: Dict[str, Set[str]] = defaultdict(set)
    for net in net_data["nets"]:
        nm = net.get("name", "")
        for m in net.get("members", []):
            if m.get("kind") == "pin":
                k = (m.get("ref", ""), str(m.get("pin_number", "")))
                pin_to_net[k] = nm
                ref_nets[k[0]].add(nm)

    covered = 0
    uncovered = 0
    out_issues: List[Dict[str, Any]] = []
    for it in issues:
        if it.get("check") != "power_pin_not_driven":
            out_issues.append(it)
            continue
        ref, pin_num = _parse_pin_ref(it.get("refs", ""))
        is_covered = False
        if ref and pin_num:
            net = pin_to_net.get((ref, pin_num))
            if net and net in driven:
                is_covered = True
        elif ref:
            # Pin number unknown — fall back to "any net on this ref
            # is driven" check. Less precise but catches the case
            # where ERC's refs string omitted the pin number.
            if any(n in driven for n in ref_nets.get(ref, ())):
                is_covered = True
        new_it = dict(it)
        if is_covered:
            new_it["_power_covered"] = True
            covered += 1
        else:
            uncovered += 1
        out_issues.append(new_it)

    return {
        "issues": out_issues,
        "stats":  {"covered": covered, "uncovered": uncovered},
    }
