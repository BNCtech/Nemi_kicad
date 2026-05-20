"""P14 — bus semantics engine.

The router and label_placer treat every net as an independent string of
characters. A schematic that carries four bus lanes `DATA0..DATA3`
between an MCU and a flash chip then ends up with four scattered labels
and (worse) four cross-sheet hierarchical labels — a visual style that
no human EE would use. The convention is one BUS port `DATA[0..3]`
plus parallel-lane wires.

This module detects bus families and groups their member nets so the
downstream stages (parent-sheet pin emitter, label_placer,
local_wire_synthesis) can route them as a structured unit.

Three kinds of buses are recognised:

  - **named** (i2c / spi / uart / usb / can / ethernet) — fixed-set
    pattern, members come in canonical roles (sda/scl, mosi/miso/sck/
    cs, txd/rxd, dp/dm, canh/canl, tx_p/tx_n/rx_p/rx_n).
  - **indexed** (DATA0..DATA15 / ADDR0..ADDR23 / GPIO0..GPIO31 /
    SEG_A..SEG_H) — same prefix + numeric suffix; members come in
    contiguous or near-contiguous index ranges.
  - **diff_pair** (FOO_P + FOO_N / FOO_DP + FOO_DM / +/− / H+L) —
    reuses the P10.4 detector logic; surfaced here as a "tiny bus" so
    the same machinery (parallel-route, single-port collapse) applies.

Output: list of `Bus` dicts:

  {
    "name": "I2C_1",
    "kind": "i2c" | "spi" | "uart" | "usb" | "can" | "ethernet"
            | "indexed" | "diff_pair",
    "members": [
        {"net": "SDA",  "role": "sda", "index": None},
        {"net": "SCL",  "role": "scl", "index": None},
    ],
    "width": 2,                     # member count
    "anchor_pair": ("U1", "U2"),    # MCU + peripheral both ends (for
                                     # named buses); None for indexed
                                     # spanning many endpoints.
    "vector_label": "I2C[SDA,SCL]"  # KiCad-compatible bus label form
                                     # for indexed buses; None for
                                     # mixed-role buses.
  }

Pure detection. Pattern set extensible via `bus_semantics_config.json`
(loaded by the existing `load_config`). No file I/O here."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


# Named-bus role patterns. Each (regex, role) maps a net name to a role
# slot within the bus. Token-boundary anchored to avoid matches like
# "SDADC" → SDA or "VSCK" → SCK.
_NAMED_PATTERNS: Dict[str, List[Tuple[str, str]]] = {
    "i2c": [
        (r"(^|[_/])SDA(_[A-Z0-9]+)?$", "sda"),
        (r"(^|[_/])SCL(_[A-Z0-9]+)?$", "scl"),
        (r"^I2C[0-9]?_SDA$", "sda"),
        (r"^I2C[0-9]?_SCL$", "scl"),
    ],
    "spi": [
        (r"(^|[_/])MOSI(_[A-Z0-9]+)?$",  "mosi"),
        (r"(^|[_/])MISO(_[A-Z0-9]+)?$",  "miso"),
        (r"(^|[_/])(SCK|SCLK)(_[A-Z0-9]+)?$", "sck"),
        (r"(^|[_/])(N?SS|N?CS|CS_N)$",   "cs"),
        (r"^SPI[0-9]?_(MOSI|MISO|SCK|SCLK|N?SS|N?CS)$", None),
    ],
    "uart": [
        (r"^TXD?$",             "tx"),
        (r"^RXD?$",             "rx"),
        (r"^UART[0-9]?_TXD?$",  "tx"),
        (r"^UART[0-9]?_RXD?$",  "rx"),
        (r"^USART[0-9]?_TXD?$", "tx"),
        (r"^USART[0-9]?_RXD?$", "rx"),
    ],
    "usb": [
        (r"^USB_?DP$|^USB_?D\+$|^D\+$|^DP$", "dp"),
        (r"^USB_?DM$|^USB_?D-$|^D-$|^DM$",   "dm"),
        (r"^USB_?DN$",                       "dm"),
    ],
    "can": [
        (r"^CAN_?H$",       "canh"),
        (r"^CAN_?L$",       "canl"),
    ],
    "ethernet": [
        (r"^RMII_TXD[01]$",  "txd"),
        (r"^RMII_RXD[01]$",  "rxd"),
        (r"^RMII_CRS_DV$",   "crs"),
        (r"^RMII_REF_CLK$",  "refclk"),
        (r"^ETH_TX_?[PN]$",  "tx_pn"),
        (r"^ETH_RX_?[PN]$",  "rx_pn"),
    ],
}

# Indexed bus name patterns. The numeric suffix is captured as the bit
# position; the prefix becomes the bus base name. To avoid grabbing
# random net `GND0` we require the prefix to consist of word
# characters and end in an alphabetic letter (the [A-Z] before \d).
_INDEXED_RE = re.compile(
    r"^(?P<base>[A-Z][A-Z_]*[A-Z])(?P<bit>\d+)$",
    re.IGNORECASE,
)

# Bus families that emit a single hierarchical bus port at the sheet
# boundary (instead of N independent hierarchical labels). Use this set
# to gate the parent-sheet pin compaction logic.
_BUS_KINDS_WITH_VECTOR_PORT = {"indexed", "i2c", "spi", "usb", "can"}


def _normalize_net(name: str) -> str:
    """Strip leading +/- (KiCad power-port prefix), uppercase. Leaves
    underscore-separated tokens intact for regex matching."""
    if not name:
        return ""
    up = name.strip().upper()
    if up.startswith("+") or up.startswith("-"):
        up = up[1:]
    return up


def _match_named(net: str) -> Optional[Tuple[str, str]]:
    """Return `(family, role)` for `net` if it matches any named-bus
    role regex. None otherwise. Role may be `None` when the regex only
    confirms family membership without identifying the specific lane —
    in that case caller infers the role from the net name."""
    up = _normalize_net(net)
    if not up:
        return None
    for family, entries in _NAMED_PATTERNS.items():
        for pat, role in entries:
            if re.search(pat, up):
                return (family, role or "")
    return None


def _match_indexed(net: str) -> Optional[Tuple[str, int]]:
    """Return `(base_prefix, bit_index)` if `net` ends with a numeric
    suffix and a sensible prefix. None otherwise."""
    up = _normalize_net(net)
    if not up:
        return None
    m = _INDEXED_RE.match(up)
    if not m:
        return None
    base = m.group("base")
    # Reject too-short / too-generic prefixes (GND0, V0, etc.) — bus
    # bases are usually ≥3 chars.
    if len(base) < 3:
        return None
    # Avoid mistaking single power rails like `+3V3` or `5V` for buses.
    if base in ("VCC", "VDD", "GND", "VSS", "VBAT", "VBUS"):
        return None
    try:
        bit = int(m.group("bit"))
    except ValueError:
        return None
    return (base, bit)


def _vector_label_for_indexed(base: str, indices: List[int]) -> str:
    """Render `(base, [0,1,2,3])` as KiCad bus label `BASE[0..3]`.
    Non-contiguous indices fall back to the comma form `BASE[0,2,5]`."""
    if not indices:
        return f"{base}[]"
    sorted_idx = sorted(set(indices))
    is_contig = (sorted_idx[-1] - sorted_idx[0] + 1 == len(sorted_idx))
    if is_contig:
        return f"{base}[{sorted_idx[0]}..{sorted_idx[-1]}]"
    return f"{base}[{','.join(str(i) for i in sorted_idx)}]"


def detect_buses(
    nets: Iterable[Dict[str, Any]],
    *,
    min_indexed_width: int = 2,
    refs_on_net: Optional[Dict[str, Set[str]]] = None,
) -> List[Dict[str, Any]]:
    """Scan a flat net list and return detected bus groups.

    `nets` is the `build_sheet_nets` output's `nets` list (each entry
    has `name` + `members`). `min_indexed_width` is the smallest bus
    we'll report — 1 just collapses noise (a lone DATA0 isn't a bus),
    2 is sensible for diff-pairs.

    `refs_on_net` lets the caller pre-supply the ref→net mapping to
    save a re-scan (the partition code computes this anyway). When
    omitted we derive it from the nets list.

    Detection passes:
      1. Named buses — group every net matching i2c/spi/uart/usb/can/
         ethernet roles. Each family yields ONE bus per (anchor pair),
         distinguished by the shared MCU+peripheral refs.
      2. Indexed buses — group by `(base_prefix)` then collapse if the
         group has ≥`min_indexed_width` members.
      3. Diff-pair micro-buses — already covered by `pin_role_placement._diff_pair_partner_net`;
         we re-surface them here so the bus-port collapser can apply
         the same single-port logic."""
    if refs_on_net is None:
        refs_on_net = defaultdict(set)
        for n in nets:
            for m in n.get("members", []):
                if m.get("kind") == "pin":
                    r = m.get("ref", "")
                    if r:
                        refs_on_net[n.get("name", "")].add(r)

    nets_by_name: Dict[str, Dict[str, Any]] = {n.get("name", ""): n
                                                for n in nets}

    # --- Pass 1: NAMED buses --------------------------------------
    family_groups: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for net_name in nets_by_name:
        match = _match_named(net_name)
        if not match:
            continue
        family, role = match
        family_groups[family].append((net_name, role))

    buses: List[Dict[str, Any]] = []
    for family, entries in family_groups.items():
        # A named bus needs ≥2 distinct roles to count as a bus (a lone
        # TX without an RX is just a UART output net, not a bus).
        roles = {r for _n, r in entries if r}
        if len(roles) < 2 and family != "ethernet":
            continue
        # Anchor refs: every ref that appears on ≥1 member net.
        anchor_refs: Set[str] = set()
        for net_name, _role in entries:
            anchor_refs.update(refs_on_net.get(net_name, ()))
        buses.append({
            "name": _canonical_named_name(family, entries),
            "kind": family,
            "members": [{"net": n, "role": r, "index": None}
                          for n, r in sorted(entries)],
            "width": len(entries),
            "anchor_refs": sorted(anchor_refs),
            "vector_label": None,  # named buses keep individual labels
        })

    # --- Pass 2: INDEXED buses ------------------------------------
    indexed_groups: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for net_name in nets_by_name:
        # Skip nets already claimed by a named bus.
        if _match_named(net_name):
            continue
        m = _match_indexed(net_name)
        if not m:
            continue
        base, bit = m
        indexed_groups[base].append((net_name, bit))
    for base, group in indexed_groups.items():
        if len(group) < min_indexed_width:
            continue
        group_sorted = sorted(group, key=lambda t: t[1])
        anchor_refs: Set[str] = set()
        for net_name, _bit in group_sorted:
            anchor_refs.update(refs_on_net.get(net_name, ()))
        members = [{"net": n, "role": f"bit{b}", "index": b}
                    for n, b in group_sorted]
        buses.append({
            "name": base,
            "kind": "indexed",
            "members": members,
            "width": len(members),
            "anchor_refs": sorted(anchor_refs),
            "vector_label": _vector_label_for_indexed(
                base, [b for _n, b in group_sorted]
            ),
        })

    # --- Pass 3: DIFF-PAIR micro-buses ----------------------------
    # Re-use the pair-recognition logic locally; we don't import
    # pin_role_placement to keep this module independent. Pair tail
    # tokens, longest first to avoid sub-matching "_P" inside "_DP".
    pair_tails: List[Tuple[str, str]] = [
        ("_DP", "_DM"), ("_D+", "_D-"), ("_P", "_N"),
        ("_+", "_-"), ("_H", "_L"),
    ]
    pair_tails.sort(key=lambda p: -max(len(p[0]), len(p[1])))
    claimed: Set[str] = set()
    for b in buses:
        for m in b["members"]:
            claimed.add(_normalize_net(m["net"]))
    diff_pairs: Dict[str, Dict[str, str]] = {}
    for net_name in nets_by_name:
        up = _normalize_net(net_name)
        if not up or up in claimed:
            continue
        for sa, sb in pair_tails:
            partner = None
            if up.endswith(sa):
                base = up[:-len(sa)]
                partner = base + sb
                role_a, role_b = "p", "n"
            elif up.endswith(sb):
                base = up[:-len(sb)]
                partner = base + sa
                role_a, role_b = "n", "p"
            else:
                continue
            if partner in {_normalize_net(n) for n in nets_by_name}:
                diff_pairs.setdefault(base, {})[role_a] = net_name
                # Find partner's original-cased name.
                for orig in nets_by_name:
                    if _normalize_net(orig) == partner:
                        diff_pairs[base][role_b] = orig
                        break
            break
    for base, members in diff_pairs.items():
        if len(members) < 2:
            continue
        anchor_refs = set()
        for nm in members.values():
            anchor_refs.update(refs_on_net.get(nm, ()))
        buses.append({
            "name": base + "_PAIR",
            "kind": "diff_pair",
            "members": [{"net": members.get("p"), "role": "p", "index": None},
                         {"net": members.get("n"), "role": "n", "index": None}],
            "width": 2,
            "anchor_refs": sorted(anchor_refs),
            "vector_label": f"{base}[P,N]",
        })

    return buses


def _canonical_named_name(family: str, entries: List[Tuple[str, str]]) -> str:
    """Pick a stable display name for a named bus.

    `entries` is `[(net_name, role), ...]`. We prefer a name that
    carries the family + bus index where present (e.g. `I2C1` from
    `I2C1_SDA` + `I2C1_SCL`), falling back to family-only (`I2C`)
    when net names are bare (SDA + SCL)."""
    prefix_re = re.compile(rf"^({family.upper()}[0-9]?)_", re.IGNORECASE)
    indices: Set[str] = set()
    for net, _role in entries:
        m = prefix_re.match(_normalize_net(net))
        if m:
            indices.add(m.group(1))
    if len(indices) == 1:
        return next(iter(indices))
    return family.upper()


def bus_port_label(bus: Dict[str, Any]) -> Optional[str]:
    """Return the single hierarchical-port label to use AT THE SHEET
    BOUNDARY when this bus crosses sheets. None when the bus prefers
    individual labels (e.g. UART without an explicit suffix)."""
    if bus["kind"] in ("indexed", "diff_pair"):
        return bus.get("vector_label")
    if bus["kind"] in _BUS_KINDS_WITH_VECTOR_PORT:
        # I2C / SPI / USB / CAN: collapse to family label. KiCad's bus
        # syntax doesn't natively support mixed-role buses like
        # I2C[SDA,SCL] for routing, but the SHEET PIN can still carry
        # this name as a display label — the underlying member labels
        # remain on the child side for ERC.
        roles = sorted({m["role"] for m in bus["members"] if m["role"]})
        if not roles:
            return bus.get("name")
        return f"{bus['name']}[{','.join(r.upper() for r in roles)}]"
    return None


def buses_by_net(buses: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Reverse-index: net_name → bus dict. Lets the parent-pin emitter
    look up "does this net belong to a bus?" in O(1)."""
    out: Dict[str, Dict[str, Any]] = {}
    for b in buses:
        for m in b["members"]:
            net = m.get("net")
            if net:
                out[net] = b
    return out
