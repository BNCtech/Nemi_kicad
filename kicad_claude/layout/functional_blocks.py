"""P12 — functional block detection.

The community partitioner sees the connectivity graph (wires + labels +
power-port shares). That's necessary but not sufficient: a real-world
schematic groups components by FUNCTIONAL INTENT — `USBLC6 + USB-C +
22R series + 5.1k CC pulldowns + ESD` form one USB BLOCK even when
their pairwise connectivity weight is only modest. A pure-graph Louvain
will scatter them across the next-nearest power/MCU community.

This module recognises functional sub-systems by lib_id / refdes / net-
name patterns and emits two outputs the partitioner uses:

  1. `block_by_ref` — {ref: block_name} mapping. A ref may belong to
     several blocks (e.g. a decap on AVDD belongs to MCU_CORE and
     POWER_3V3); the highest-priority block wins.
  2. `intra_block_edges` — list of (ref_a, ref_b, weight, block_name)
     tuples. Injected into the partition graph BEFORE Louvain runs so
     same-block members bind tightly during modularity optimization.

Patterns are declarative — each entry has a name, priority, and one or
more trigger/expander rules. Triggers anchor a block (a single ref is
enough to "claim" the block exists); expanders pull in related refs
once the block is anchored.

No hardcoded part numbers in code — every rule lives in
`functional_blocks_config.json`, loaded via the existing layout-config
loader so it stays editable without code changes."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    from . import load_config
    from . import nets as _nets
    from ..schematic_extractor import SchematicExtractor
except ImportError:
    from kicad_claude.layout import load_config  # type: ignore
    from kicad_claude import nets as _nets  # type: ignore
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore


# Priority is lower-is-stronger. A ref claimed by both USB and POWER_5V
# resolves to USB (USB block has more specific intent).
_DEFAULT_PATTERNS: List[Dict[str, Any]] = [
    {
        "name": "USB",
        "priority": 10,
        "triggers": {
            # lib_id substring match (case-insensitive). One match
            # anchors the block.
            "lib_id_substrings": ["USB_C", "USB_B", "USB_A", "USB_Mini",
                                  "USB_Micro", "USBLC", "TPD2EUSB",
                                  "TPD4S", "ESD_USB"],
            # Net-name regex (case-insensitive). One match anchors the
            # block; refs touching the matching net are added.
            "net_regexes": [r"^USB_?D[PMN+\-]?$", r"^D[PMN]_USB$",
                             r"^VBUS(_USB)?$", r"^USB_CC[12]?$"],
        },
        "expanders": {
            # Once the block is anchored, also include any 2-pin passive
            # that sits on one of these nets (USB termination, ESD CC
            # pulldowns, VBUS bulk caps, etc.).
            "passive_net_regexes": [r"^USB_?D[PMN+\-]?$",
                                     r"^USB_CC[12]?$", r"^VBUS"],
        },
    },
    {
        "name": "CAN_BUS",
        "priority": 11,
        "triggers": {
            "lib_id_substrings": ["MCP2515", "MCP2562", "TJA1050",
                                   "TJA1051", "SN65HVD", "ADM3053"],
            "net_regexes": [r"^CAN_?H$", r"^CAN_?L$"],
        },
        "expanders": {
            "passive_net_regexes": [r"^CAN_?[HL]$"],
        },
    },
    {
        "name": "ETHERNET",
        "priority": 12,
        "triggers": {
            "lib_id_substrings": ["LAN8720", "LAN8742", "DP83848",
                                   "KSZ8081", "W5500", "RJ45"],
            "net_regexes": [r"^RMII_", r"^MII_", r"^MDIO$", r"^MDC$",
                             r"^ETH_"],
        },
        "expanders": {
            "passive_net_regexes": [r"^RMII_", r"^MII_", r"^ETH_"],
        },
    },
    {
        "name": "DEBUG",
        "priority": 13,
        "triggers": {
            "lib_id_substrings": ["Conn_ARM", "JTAG", "SWD"],
            "net_regexes": [r"^SWDIO$", r"^SWCLK$", r"^SWO$",
                             r"^JTAG_", r"^T(CK|MS|DO|DI)$"],
            "refdes_prefixes": ["TP"],   # test points cluster with debug
        },
        "expanders": {
            "passive_net_regexes": [r"^SWD", r"^JTAG_", r"^T(CK|MS|DO|DI)$"],
        },
    },
    {
        "name": "CRYSTAL_SECT",
        "priority": 14,
        "triggers": {
            "lib_id_substrings": ["Crystal", "Oscillator"],
            "refdes_prefixes": ["Y"],
        },
        "expanders": {
            # Load caps inherit via the crystal's two signal nets — the
            # partition graph already has a strong wire edge between
            # them. Listed here for completeness so a label-only routed
            # crystal still binds.
            "passive_net_regexes": [r"^X(IN|OUT|TAL[12]?)$",
                                     r"^(HSE|LSE)_(IN|OUT)$",
                                     r"^OSC[IO]_?(IN|OUT)?$"],
        },
    },
    {
        "name": "RESET_SECT",
        "priority": 15,
        "triggers": {
            "net_regexes": [r"^N?RST$", r"^N?RESET$", r"^MCLR$",
                             r"^EN$", r"^CHIP_EN$"],
        },
        "expanders": {
            "passive_net_regexes": [r"^N?RST$", r"^N?RESET$", r"^MCLR$"],
        },
    },
    {
        "name": "I2C_BUS",
        "priority": 16,
        "triggers": {
            "net_regexes": [r"^SDA(_\w+)?$", r"^SCL(_\w+)?$",
                             r"^I2C[0-9]?_(SDA|SCL)$"],
        },
        "expanders": {
            "passive_net_regexes": [r"^SDA", r"^SCL", r"^I2C"],
        },
    },
    {
        "name": "SPI_BUS",
        "priority": 17,
        "triggers": {
            "net_regexes": [r"^MOSI(_\w+)?$", r"^MISO(_\w+)?$",
                             r"^SCK(_\w+)?$", r"^SPI[0-9]?_(MOSI|MISO|SCK|CS|NSS)$"],
        },
        "expanders": {
            "passive_net_regexes": [r"^MOSI", r"^MISO", r"^SCK",
                                     r"^SPI[0-9]?_", r"^N?SS$"],
        },
    },
    {
        "name": "UART_IFACE",
        "priority": 18,
        "triggers": {
            "lib_id_substrings": ["CP210", "FT232", "CH340", "MAX3232"],
            "net_regexes": [r"^UART[0-9]?_(TX|RX|RTS|CTS|DTR)$",
                             r"^USART[0-9]?_(TX|RX)$"],
        },
        "expanders": {
            "passive_net_regexes": [r"^UART", r"^USART", r"^TXD?$", r"^RXD?$"],
        },
    },
    {
        "name": "POWER_INPUT",
        "priority": 20,
        "triggers": {
            "lib_id_substrings": ["Conn_Barrel_Jack", "Battery", "Conn_Power"],
            "refdes_prefixes": ["BT"],   # battery holder
            "net_regexes": [r"^V?IN$", r"^V_?IN$", r"^VBAT$", r"^VSYS$"],
        },
        "expanders": {
            "passive_net_regexes": [r"^V?IN$", r"^VBAT$", r"^VSYS$"],
        },
    },
    {
        "name": "PROTECTION",
        "priority": 22,
        "triggers": {
            "lib_id_substrings": ["TVS", "Varistor", "Fuse",
                                   "PolyFuse", "Diode_TVS", "PESD"],
        },
        "expanders": {},
    },
    {
        "name": "LED_INDICATOR",
        "priority": 30,
        "triggers": {
            # Token-bounded — bare "LED" substring also matches "OLED",
            # so we require the LED token to sit at a `:` boundary
            # (lib namespace separator) or be wrapped in `_LED_`.
            # Covers Device:LED, Diode:LED_*, Optoelectronic_LED_*.
            "lib_id_substrings": [":led", "_led_"],
        },
        "expanders": {
            # Restrict to RESISTORS on the LED's signal net AND require
            # the net's fanout ≤ 3 (current-limit R is on a private
            # GPIO→LED net, not a shared bus). Without the fanout cap,
            # an LED indicator on a status GPIO scoops every passive
            # also sharing that GPIO.
            "share_signal_net_with_anchor": True,
            "share_net_passive_refdes": ["R"],
            "share_net_max_fanout": 3,
        },
    },
]


def _compile_pattern(p: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "name": p["name"],
        "priority": int(p.get("priority", 100)),
        "lib_id_substrings": tuple(
            (s or "").lower() for s in
            (p.get("triggers") or {}).get("lib_id_substrings") or []
        ),
        "refdes_prefixes": tuple(
            (s or "").upper() for s in
            (p.get("triggers") or {}).get("refdes_prefixes") or []
        ),
        "trigger_net_regex": _compile_or_none(
            (p.get("triggers") or {}).get("net_regexes") or []
        ),
        "expander_net_regex": _compile_or_none(
            (p.get("expanders") or {}).get("passive_net_regexes") or []
        ),
        "expander_share_anchor": bool(
            (p.get("expanders") or {}).get("share_signal_net_with_anchor")
        ),
        "expander_anchor_prefixes": tuple(
            (s or "").upper() for s in
            (p.get("expanders") or {}).get("anchor_refdes_prefixes") or []
        ),
        # P12 LED-style tightening — restrict the "share signal net"
        # expander to specific refdes prefixes (default: any 2-pin
        # passive) and skip nets with too many refs on them (default:
        # no fanout cap).
        "share_net_passive_refdes": tuple(
            (s or "").upper() for s in
            (p.get("expanders") or {}).get("share_net_passive_refdes") or []
        ),
        "share_net_max_fanout": int(
            (p.get("expanders") or {}).get("share_net_max_fanout") or 0
        ),
    }
    return out


def _compile_or_none(regex_list: List[str]) -> Optional[re.Pattern]:
    if not regex_list:
        return None
    joined = "|".join(f"(?:{r})" for r in regex_list)
    return re.compile(joined, re.IGNORECASE)


def _passive_ref(ref: str) -> bool:
    """2-pin passive refdes prefixes — what the expander pulls in.
    Mirrors `pin_role_placement._PASSIVE_REF_PREFIXES`."""
    if not ref:
        return False
    up = ref.upper()
    return up.startswith(("R", "C", "L", "D", "FB"))


def assign_functional_blocks(
    schematic_path,
    classified: Dict[str, Any],
    config_root: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Detect functional blocks on `schematic_path`. Returns:

      {
        "block_by_ref": {ref: block_name},
        "block_members": {block_name: [refs...]},
        "intra_block_edges": [(ref_a, ref_b, weight, block_name), ...],
        "stats": {trigger_hits, expander_hits, blocks_found},
      }

    The returned `intra_block_edges` is the input the community
    partitioner uses to bind same-block members during Louvain.

    Pure detection. Does NOT mutate `classified`. Pattern set comes from
    `functional_blocks_config.json` if present, else the in-module
    defaults. Caller can override via `config_root["functional_blocks"]
    = {"patterns": [...]}` for ad-hoc tuning."""
    cfg_root = config_root
    if cfg_root is None:
        try:
            cfg_root = load_config("functional_blocks_config")
        except Exception:
            cfg_root = {}
    fb_cfg = (cfg_root.get("functional_blocks") if cfg_root else {}) or {}
    raw_patterns: List[Dict[str, Any]] = (fb_cfg.get("patterns")
                                            or _DEFAULT_PATTERNS)
    edge_weight = float(fb_cfg.get("intra_block_weight", 4.0))
    patterns = [_compile_pattern(p) for p in raw_patterns]
    # Sort by priority ascending — lower priority value wins claims.
    patterns.sort(key=lambda p: p["priority"])

    # Ref → lib_id from classified (works even when extractor can't
    # read the file).
    ref_lib_id: Dict[str, str] = {}
    for n in classified.get("nodes", []):
        ref = n.get("ref")
        lib = n.get("lib_id") or ""
        if ref:
            ref_lib_id[ref] = lib

    # Build extractor-side state: refs per net, nets per ref.
    refs_on_net: Dict[str, Set[str]] = defaultdict(set)
    nets_by_ref: Dict[str, Set[str]] = defaultdict(set)
    try:
        extractor = SchematicExtractor(schematic_path)
        net_data = _nets.build_sheet_nets(extractor)
        for net in net_data.get("nets", []):
            nm = net.get("name", "")
            for m in net.get("members", []):
                if m.get("kind") == "pin":
                    r = m.get("ref", "")
                    if r:
                        refs_on_net[nm].add(r)
                        nets_by_ref[r].add(nm)
    except Exception:
        # Extractor failures fall back to lib_id+refdes triggers only.
        pass

    # First pass: TRIGGER claims. A ref hits a pattern via lib_id OR
    # refdes-prefix OR being on a trigger-matching net.
    block_by_ref: Dict[str, str] = {}
    trigger_anchors: Dict[str, Set[str]] = defaultdict(set)
    trigger_hits = 0
    for pat in patterns:
        name = pat["name"]
        for ref, lib in ref_lib_id.items():
            if ref in block_by_ref:
                continue  # higher-priority pattern already claimed it
            up_ref = ref.upper()
            lib_low = lib.lower()
            claim = False
            if any(s and s in lib_low for s in pat["lib_id_substrings"]):
                claim = True
            elif pat["refdes_prefixes"] and any(
                up_ref.startswith(p) for p in pat["refdes_prefixes"]
            ):
                # Refdes-only triggers are only valid if the ref has at
                # least one signal net (otherwise every D* gets pulled
                # into DEBUG/test-point). Skip when ref isn't connected.
                if nets_by_ref.get(ref):
                    claim = True
            elif pat["trigger_net_regex"]:
                for nm in nets_by_ref.get(ref, ()):
                    if pat["trigger_net_regex"].search(nm):
                        claim = True
                        break
            if claim:
                block_by_ref[ref] = name
                trigger_anchors[name].add(ref)
                trigger_hits += 1

    # Second pass: EXPANDERS. For each anchored block, scoop in extra
    # refs:
    #   a) passive 2-pin parts on an expander-net-regex-matching net,
    #   b) "share signal net with anchor" mode — passives that share a
    #      non-power net with an anchor ref of the block (used for
    #      LED+resistor pairs).
    expander_hits = 0
    name_to_pat = {p["name"]: p for p in patterns}
    for name, anchors in trigger_anchors.items():
        pat = name_to_pat[name]
        if pat["expander_net_regex"]:
            # Find every matching net, then every passive on it.
            for nm, refs in refs_on_net.items():
                if not pat["expander_net_regex"].search(nm):
                    continue
                for ref in refs:
                    if ref in block_by_ref:
                        continue
                    if _passive_ref(ref):
                        block_by_ref[ref] = name
                        expander_hits += 1
        if pat["expander_share_anchor"]:
            anchor_signal_nets: Set[str] = set()
            for ar in anchors:
                for nm in nets_by_ref.get(ar, ()):
                    up = nm.upper()
                    if up in ("GND", "VSS") or up.startswith(("+", "-")):
                        continue
                    anchor_signal_nets.add(nm)
            for nm in anchor_signal_nets:
                refs_here = refs_on_net.get(nm, set())
                if (pat["share_net_max_fanout"] > 0
                        and len(refs_here) > pat["share_net_max_fanout"]):
                    # High-fanout net — likely a shared GPIO/bus. Skip
                    # so we don't drag every passive on it into the
                    # block.
                    continue
                allowed_prefixes = pat["share_net_passive_refdes"]
                for ref in refs_here:
                    if ref in block_by_ref:
                        continue
                    if not _passive_ref(ref):
                        continue
                    if allowed_prefixes and not any(
                        ref.upper().startswith(p) for p in allowed_prefixes
                    ):
                        continue
                    block_by_ref[ref] = name
                    expander_hits += 1

    # Build outputs.
    block_members: Dict[str, List[str]] = defaultdict(list)
    for ref, name in block_by_ref.items():
        block_members[name].append(ref)

    intra_block_edges: List[Tuple[str, str, float, str]] = []
    for name, members in block_members.items():
        if len(members) < 2:
            continue
        # Fully connected sub-graph within the block. Quadratic in block
        # size, but functional blocks are small (typically 3-15 refs);
        # the connectivity graph already has thousands of edges, this
        # adds at most a few hundred.
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                intra_block_edges.append((a, b, edge_weight, name))

    return {
        "block_by_ref": dict(block_by_ref),
        "block_members": {k: sorted(v) for k, v in block_members.items()},
        "intra_block_edges": intra_block_edges,
        "stats": {
            "trigger_hits": trigger_hits,
            "expander_hits": expander_hits,
            "blocks_found": len(block_members),
        },
    }
