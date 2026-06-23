"""Step 5 of the universal layout engine: KiCad .kicad_sch emitter.

Takes placement.json + routed.json + the source .kicad_sch and writes a
new .kicad_sch the user can open directly in eeschema. The source file is
the lib_symbols donor — we copy it as the base, then surgically replace
its layout (positions, wires, labels, power-ports) with our generated
content. The lib_symbol artwork cache is preserved, so eeschema renders
every symbol correctly.

Pipeline:
  1. shutil.copy source -> out (so the new file inherits version,
     generator, uuid, paper, embedded files, AND lib_symbols).
  2. Strip layout content: drop every wire / label / junction / no_connect,
     drop existing power-port symbols (refdes starts with #PWR / #FLG),
     drop multi-unit extras (keep first unit per ref — placer collapsed
     them anyway).
  3. Move every placed real component to its new (x_mm, y_mm, rotation).
  4. Inject one (label ...) per net_label entry at the pin's outward angle.
  5. Inject one power-port symbol per power_port entry, with the rail
     name as Value and a generated #PWR refdes.
  6. doc.save(out) — relies on schematic_modifier's _format_sexpr.

The emitter reuses kicad_claude.schematic_modifier for the heavy lifting
(SchematicDocument tree management, add_component's lib-symbol caching).
Labels are built inline with private helpers because add_label hardcodes
angle=0 — bypassing it lets us honor the router's pin angles without
modifying the repair side.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import load_config

try:
    from ai_backend.kicad_claude.schematic_modifier import (
        SchematicDocument, _head, _to_str, _get_property,
        _sym, _make_at, _gen_uuid_node, _make_property,
    )
    from ai_backend.kicad_claude._lib_symbol_cache import ensure_lib_symbols_for_doc
except ImportError:  # supports both `f:/Ki_CAD` and `ai_backend/` on sys.path
    from kicad_claude.schematic_modifier import (  # type: ignore
        SchematicDocument, _head, _to_str, _get_property,
        _sym, _make_at, _gen_uuid_node, _make_property,
    )
    from kicad_claude._lib_symbol_cache import ensure_lib_symbols_for_doc  # type: ignore


_DROP_HEADS = {
    "wire", "label", "global_label", "hierarchical_label",
    "junction", "no_connect", "bus", "bus_entry", "polyline", "rectangle",
    "text", "text_box", "netclass_flag",
}

# Fields stripped from every kept (symbol ...) so the new placement is in a
# canonical, no-mirror, no-rotation state. The placer + router assume the
# unmodified pin geometry — a leftover `(mirror y)` flips every pin's X
# offset relative to the component anchor, leaving labels and power-ports
# 2 * pin_x away from their actual KiCad-rendered pin endpoints.
_STRIP_FROM_SYMBOL = {"mirror"}

# Properties whose text labels are noise on a generated schematic — Footprint
# strings like 'Package_SON:Winbond_USON-8-1EP_3x2mm_P0.5mm_EP0.2x1.6mm' or
# Description blurbs clutter every component visually. Reference and Value
# stay visible; everything else gets force-hidden during the property shift.
_FORCE_HIDE_PROPERTIES = {"Footprint", "Datasheet", "Description",
                          "ki_keywords", "ki_fp_filters", "ki_locked"}


def _is_ignored_ref(ref: str, prefixes: tuple) -> bool:
    return bool(ref) and ref.startswith(prefixes)


def _strip_layout(doc: SchematicDocument, ignored_prefixes: tuple) -> Dict[str, int]:
    """Remove every layout element from doc.tree, leaving header + lib_symbols +
    every (symbol ...) per real component (ALL units kept — multi-unit fan-out
    happens downstream in Pass C: each unit symbol is moved to its own
    placement entry). Returns counters."""
    kept_symbols = 0
    dropped_layout = 0
    dropped_power = 0
    new_children = [doc.tree[0]]

    for child in doc.tree[1:]:
        if not isinstance(child, list):
            new_children.append(child)
            continue
        head = _head(child)
        if head in _DROP_HEADS:
            dropped_layout += 1
            continue
        if head != "symbol":
            new_children.append(child)
            continue
        ref_prop = _get_property(child, "Reference")
        ref = _to_str(ref_prop[2]) if ref_prop else ""
        if _is_ignored_ref(ref, ignored_prefixes):
            dropped_power += 1
            continue
        kept_symbols += 1
        child[:] = [s for s in child
                    if not (isinstance(s, list) and _head(s) in _STRIP_FROM_SYMBOL)]
        new_children.append(child)

    doc.tree[:] = new_children
    return {
        "kept_symbols": kept_symbols,
        "dropped_layout": dropped_layout,
        "dropped_power_ports": dropped_power,
        "dropped_multiunit_extras": 0,
    }


def _hide_pin_numbers_for_small_symbols(
    tree: list, max_num_pins: int, max_name_pins: int,
    force_show_min_pins: int = 0,
) -> int:
    """Walk the (lib_symbols ...) block and force `(pin_numbers (hide yes))`
    + `(pin_names (hide yes))` on every cached symbol whose pin count is at
    or below the thresholds. The flags live on the LIB SYMBOL (not the
    instance), so changing them once in the cached definition affects every
    instance that uses that lib_id.

    When `force_show_min_pins > 0`, symbols at or above that pin count get
    `(pin_numbers (hide no))` forced regardless of any `(hide yes)` baked
    into the source lib_symbol. Necessary because some upstream libraries
    (Espressif, Nordic) ship MCU symbols with pin_numbers hidden — without
    this override, our column-aligned net labels float in space with no
    pin-number anchor next to the body, hurting readability.

    Returns count of symbols modified."""
    lib_symbols_block = next(
        (c for c in tree[1:] if isinstance(c, list) and _head(c) == "lib_symbols"),
        None,
    )
    if not lib_symbols_block:
        return 0

    modified = 0
    for sym in lib_symbols_block[1:]:
        if not (isinstance(sym, list) and _head(sym) == "symbol"):
            continue
        # Count pins. KiCad nests sub-units as (symbol "name_X_Y" ...) — walk
        # one level deep and count (pin ...) entries in each sub-unit too.
        pin_count = 0
        for child in sym[1:]:
            if not isinstance(child, list):
                continue
            head = _head(child)
            if head == "pin":
                pin_count += 1
            elif head == "symbol":  # sub-unit
                for sub in child[1:]:
                    if isinstance(sub, list) and _head(sub) == "pin":
                        pin_count += 1

        hide_numbers = pin_count > 0 and pin_count <= max_num_pins
        hide_names = pin_count > 0 and pin_count <= max_name_pins
        show_numbers = (force_show_min_pins > 0
                        and pin_count >= force_show_min_pins)
        if not hide_numbers and not hide_names and not show_numbers:
            continue

        def _force_flag(tag: str, hide: bool) -> bool:
            flag = _sym("yes" if hide else "no")
            for i, child in enumerate(sym[1:], start=1):
                if isinstance(child, list) and _head(child) == tag:
                    sym[i] = [_sym(tag), [_sym("hide"), flag]]
                    return True
            sym.insert(2, [_sym(tag), [_sym("hide"), flag]])
            return True

        changed = False
        if hide_numbers:
            _force_flag("pin_numbers", True); changed = True
        elif show_numbers:
            _force_flag("pin_numbers", False); changed = True
        if hide_names:
            _force_flag("pin_names", True); changed = True
        if changed:
            modified += 1
    return modified


def _set_paper(tree: list, size_name: str) -> bool:
    """Update the top-level (paper ...) node to match the placer's chosen
    sheet size. The placer uses 'A4_landscape' / 'A3_landscape' / etc — KiCad
    uses just the paper name ('A4') with an optional 'portrait' suffix
    (landscape is the eeschema default for A-series, no suffix needed)."""
    base, _, orient = size_name.partition("_")
    new_paper: list = [_sym("paper"), base]
    if orient == "portrait":
        new_paper.append(_sym("portrait"))
    for i, child in enumerate(tree[1:], start=1):
        if isinstance(child, list) and _head(child) == "paper":
            tree[i] = new_paper
            return True
    # No paper field present — insert after the header atoms (version,
    # generator, uuid) so KiCad parses it as a sheet-level property.
    tree.insert(2, new_paper)
    return False


def _normalize_property_positions(
    node: list, sym_x: float, sym_y: float,
    body_bbox: Optional[Tuple[float, float, float, float]],
    cfg: Dict[str, Any],
    sym_rot: float = 0.0,
) -> None:
    """Override Reference and Value property positions to KiCad's canonical
    layout. Property text rotation is FORCED TO 0 in every branch, so all
    labels stay horizontal regardless of the parent component's rotation —
    a 90°-rotated resistor still reads 'R5 / 10k' left-to-right, not bottom-
    to-top.

    Layout per rotation:
      rot 0/180   (vertical body):  Reference above body, Value below
      rot 90/270  (horizontal body): Reference left of body, Value right of

    body_bbox is in lib-symbol-local Y-UP coords:
      (x_min, y_min, x_max, y_max). The world rotation is applied here so
      passives placed by the auto-router with rot=90 get their refdes/value
      on the LEFT/RIGHT sides where they actually fit, not floating above
      the (now horizontal) body where they'd overlap adjacent rows."""
    if not cfg.get("enabled", True) or not body_bbox:
        return
    ref_margin = float(cfg.get("refdes_margin_mm", 1.27))
    val_margin = float(cfg.get("value_margin_mm", 1.27))
    rot_q = int(round(sym_rot / 90.0)) % 4  # quadrant: 0,1,2,3
    if rot_q in (1, 3):
        # Horizontal body — lib Y-extent becomes world X-extent.
        world_left  = sym_x - body_bbox[3]
        world_right = sym_x - body_bbox[1]
        targets = {
            "Reference": (world_left  - ref_margin, sym_y),
            "Value":     (world_right + val_margin, sym_y),
        }
    else:
        body_top_world = sym_y - body_bbox[3]
        body_bot_world = sym_y - body_bbox[1]
        targets = {
            "Reference": (sym_x, body_top_world - ref_margin),
            "Value":     (sym_x, body_bot_world + val_margin),
        }
    for sub in node[1:]:
        if not (isinstance(sub, list) and _head(sub) == "property"):
            continue
        if len(sub) < 2:
            continue
        prop_name = _to_str(sub[1])
        target = targets.get(prop_name)
        if not target:
            continue
        for j, ps in enumerate(sub[1:], start=1):
            if isinstance(ps, list) and _head(ps) == "at" and len(ps) >= 3:
                sub[j] = _make_at(target[0], target[1], 0)
                break


def _shift_component(node: list, new_x: float, new_y: float, new_rot: float) -> bool:
    """Move a (symbol ...) node to (new_x, new_y, new_rot) AND shift every
    nested (property ...) (at ...) by the same delta. Without this last step
    the property text labels (Reference, Value, Footprint) stay at their
    ORIGINAL source-schematic coordinates and appear as floating orphans
    100 mm away from the symbol body — exactly the bug we see in eeschema.

    SchematicDocument.move_component only updates the symbol-level (at ...);
    we bypass it here so the property children move in lockstep."""
    cur_at: Optional[Tuple[float, float]] = None
    for sub in node[1:]:
        if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
            try:
                cur_at = (float(sub[1]), float(sub[2]))
            except (TypeError, ValueError):
                pass
            break
    if cur_at is None:
        return False
    dx = new_x - cur_at[0]
    dy = new_y - cur_at[1]

    for i, sub in enumerate(node[1:], start=1):
        if not isinstance(sub, list):
            continue
        head = _head(sub)
        if head == "at" and len(sub) >= 3:
            node[i] = _make_at(new_x, new_y, new_rot)
            continue
        if head == "property":
            prop_name = _to_str(sub[1]) if len(sub) > 1 else ""
            force_hide = prop_name in _FORCE_HIDE_PROPERTIES
            for j, ps in enumerate(sub[1:], start=1):
                if not isinstance(ps, list):
                    continue
                ps_head = _head(ps)
                if ps_head == "at" and len(ps) >= 3:
                    try:
                        px = float(ps[1]) + dx
                        py = float(ps[2]) + dy
                        prot = float(ps[3]) if len(ps) > 3 else 0.0
                    except (TypeError, ValueError):
                        continue
                    sub[j] = _make_at(px, py, prot)
                elif ps_head == "effects" and force_hide:
                    # Set the (hide yes) flag inside (effects ...) so eeschema
                    # doesn't render the property text. Add if missing, update
                    # if present.
                    found_hide = False
                    for k, e in enumerate(ps[1:], start=1):
                        if isinstance(e, list) and _head(e) == "hide":
                            ps[k] = [_sym("hide"), _sym("yes")]
                            found_hide = True
                            break
                    if not found_hide:
                        ps.append([_sym("hide"), _sym("yes")])
    return True


def _build_zone_border_node(x: float, y: float, w: float, h: float,
                             stroke_mm: float, style: str) -> list:
    """A 5-point closed polyline tracing the block bbox. Dashed by default
    so it reads as 'overlay' not 'real schematic graphic'."""
    return [
        _sym("polyline"),
        [
            _sym("pts"),
            [_sym("xy"), float(x), float(y)],
            [_sym("xy"), float(x + w), float(y)],
            [_sym("xy"), float(x + w), float(y + h)],
            [_sym("xy"), float(x), float(y + h)],
            [_sym("xy"), float(x), float(y)],
        ],
        [
            _sym("stroke"),
            [_sym("width"), float(stroke_mm)],
            [_sym("type"), _sym(style)],
        ],
        _gen_uuid_node(),
    ]


def _build_zone_title_node(
    text: str, x: float, y: float, size_mm: float,
    color_rgba: Optional[Tuple[int, int, int, float]] = None,
    bold: bool = False,
    justify: str = "center",
) -> list:
    """Free-floating text annotation above the block.

    KiCad's `(justify ...)` field accepts only `left`, `right`, `top`,
    `bottom`, `mirror` — `center` is NOT a valid token. Centering is the
    DEFAULT when no justify field is emitted, so for justify="center" we
    OMIT the field entirely; for "left" we emit `(justify left bottom)`.
    Emitting `(justify center bottom)` produces a parse error in eeschema
    ("Expecting left, right, top, bottom, or mirror. Got 'center'") — that
    bug corrupted test_1.kicad_sch on 2026-05-18 PM.

    color_rgba (R,G,B,A 0-255 / 0-1) styles the title — passed as a (color)
    sub-sexp inside (effects). Bold gives the visual weight the reference
    image uses for section headers."""
    font_node: List[Any] = [_sym("font"),
                             [_sym("size"), float(size_mm), float(size_mm)]]
    if bold:
        font_node.append([_sym("bold"), _sym("yes")])
    if color_rgba is not None:
        r, g, b, a = color_rgba
        font_node.append([_sym("color"), int(r), int(g), int(b), float(a)])
    effects = [_sym("effects"), font_node]
    # Only emit justify when we explicitly want non-center alignment.
    if justify == "left":
        effects.append([_sym("justify"), _sym("left"), _sym("bottom")])
    elif justify == "right":
        effects.append([_sym("justify"), _sym("right"), _sym("bottom")])
    # justify == "center" (default) → no justify field, KiCad centres it.
    return [
        _sym("text"),
        text,
        [_sym("exclude_from_sim"), _sym("no")],
        _make_at(float(x), float(y), 0),
        effects,
        _gen_uuid_node(),
    ]


_FREQ_RE = __import__("re").compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(GHz|MHz|kHz|Hz)\s*$", __import__("re").IGNORECASE,
)
_CAP_RE  = __import__("re").compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(uF|nF|pF|mF|F)\s*$", __import__("re").IGNORECASE,
)


_IC_PREFIXES = ("U", "X")  # KiCad's RefDes prefix for ICs


def _block_display_name(
    block: Dict[str, Any],
    components: List[Dict[str, Any]],
    routed_labels: List[Dict[str, Any]],
) -> str:
    """Generate a human-readable title for a block from the CIRCUIT itself
    — no static role→name table, no hardcoded vocabulary. Rules layered
    cheapest-to-richest; first hit wins.

      R1. base = role with underscores → spaces, uppercased
      R2. SINGLE-IC PREFIX — if exactly one block member is an IC (U* /
          X*) and has a Value, prefix the IC value (e.g. 'LM317 POWER
          REGULATOR', 'STM32F103 MAIN CONTROLLER', 'W25Q32 MEMORY').
      R3. FREQUENCY PREFIX — one block member has a frequency value
          (e.g. Y1@32MHz) → '32MHz CRYSTAL'.
      R4. CAPACITANCE PREFIX — every member shares the same capacitance
          value → '0.1uF DECOUPLING'.
      R5. PARENT-IC SUFFIX (for GENERIC buckets) — if every member's
          signal labels point at exactly one IC elsewhere on the sheet,
          tag the block with that IC's value: 'LM317 SUPPORT'.
      R6. BUS / RAIL SUFFIX — '(SPI)', '(SWD)', '(VDD)' if labels share
          a known bus token or a single rail.

    Failure mode: return the base role. New roles added in
    classifier_config.json work automatically — no per-role branch
    anywhere."""
    role = str(block.get("role", "GENERIC"))
    base = role.replace("_", " ").strip().upper() or "MISC"

    members = set(block.get("members") or [])
    if not members:
        return base
    comps = [c for c in components if c.get("ref") in members]
    if not comps:
        return base

    # R2: single-IC prefix.
    ics = [c for c in comps if str(c.get("ref", "")).startswith(_IC_PREFIXES)
                                and (c.get("value") or "").strip()]
    if len(ics) == 1:
        ic_val = str(ics[0]["value"]).strip()
        # Avoid noisy 'LM317T-3.3 POWER REGULATOR'-style if value has a
        # package code; keep what's before any package marker.
        ic_short = ic_val.split("-")[0].split("_")[0].strip()
        if ic_short:
            return f"{ic_short} {base}"

    # R3: frequency prefix.
    freq_vals = [str(c.get("value") or "").strip()
                  for c in comps if _FREQ_RE.match(str(c.get("value") or ""))]
    if len(freq_vals) == 1:
        return f"{freq_vals[0]} {base}"

    # R4: shared-capacitance prefix.
    cap_vals = [str(c.get("value") or "").strip()
                 for c in comps if _CAP_RE.match(str(c.get("value") or ""))]
    if cap_vals and len(cap_vals) == len(comps) and len(set(cap_vals)) == 1:
        # GENERIC role + every member is a same-value cap = decoupling
        # cluster. Use the semantic word "DECOUPLING" instead of "GENERIC".
        suffix = "DECOUPLING" if base == "GENERIC" else base
        return f"{cap_vals[0]} {suffix}"

    # R5: parent-IC inference for orphan blocks (typically the GENERIC
    # bucket of LM317 dividers / decoupling caps / pull-ups). If every
    # net label this block emits points at the SAME single IC elsewhere
    # on the sheet, name the block after that IC.
    if role.upper() == "GENERIC":
        refs = {c.get("ref") for c in comps}
        our_nets = {str(lb.get("text") or "")
                    for lb in routed_labels if lb.get("ref") in refs}
        # Net → set of refs touching it (across the whole sheet)
        net_to_refs: Dict[str, set] = {}
        for lb in routed_labels:
            net = str(lb.get("text") or "")
            net_to_refs.setdefault(net, set()).add(lb.get("ref", ""))
        # For each of OUR nets, the OTHER refs on it (excluding our own block).
        external_ics: Dict[str, str] = {}  # ref -> value
        for net in our_nets:
            for r in net_to_refs.get(net, set()):
                if r in refs or not r:
                    continue
                if not r.startswith(_IC_PREFIXES):
                    continue
                comp = next((c for c in components if c.get("ref") == r), None)
                if comp and (comp.get("value") or ""):
                    external_ics[r] = str(comp["value"]).strip()
        if len(external_ics) == 1:
            ic_val = next(iter(external_ics.values()))
            ic_short = ic_val.split("-")[0].split("_")[0].strip()
            if ic_short:
                return f"{ic_short} SUPPORT"

    # R6: shared-token qualifier — applies ONLY to non-GENERIC roles.
    # For GENERIC clusters, "GENERIC (VCC)" reads as meaningless; we
    # want "VCC DECOUPLING" (R8) or a topology name (R7) instead.
    refs = {c.get("ref") for c in comps}
    nets = [str(lb.get("text") or "")
            for lb in routed_labels if lb.get("ref") in refs]
    if role.upper() != "GENERIC":
        qualifier = _common_net_qualifier(nets)
        if qualifier:
            return f"{base} ({qualifier})"

    # R7: topology-derived title for orphan GENERIC blocks where no other
    # rule matched AND no IC sits in the block. The IC guard is critical
    # — a block holding an MCU + decoupling caps must NEVER read as
    # "RC NETWORK" just because R+C are present; the IC's identity wins.
    has_ic = any(_is_ic_in_block(c) for c in comps)
    if role.upper() == "GENERIC" and not has_ic:
        # R8 FIRST (more specific than topology): if every member sits on
        # a common power rail, name the cluster after that rail.
        refs_in_block = {c.get("ref") for c in comps}
        nets_in_block = [
            str(lb.get("text") or "").strip()
            for lb in routed_labels
            if lb.get("ref") in refs_in_block
        ]
        rail = _common_power_rail(nets_in_block)
        if rail:
            if all((c.get("ref") or "").startswith("C") for c in comps):
                return f"{rail} DECOUPLING"
            if all((c.get("ref") or "").startswith("R") for c in comps):
                return f"{rail} BIAS"
            return f"{rail} FILTER"
        # R7 (fallback): pure-topology label when no shared rail exists.
        topo = _topology_summary(comps)
        if topo:
            return topo
        # Nothing meaningful — return empty so the title is SKIPPED
        # instead of showing the bare word "GENERIC". The dashed box
        # may still draw per bbox_policy, but no clutter label above it.
        return ""
    return base


def _common_power_rail(nets: List[str]) -> Optional[str]:
    """If every non-empty net in the list is the SAME power-rail token,
    return it. Used to name passive clusters by the rail they decouple /
    bias. Examples that match: ['VCC', 'VCC', 'VCC'] → 'VCC';
    ['+3V3', '+3V3'] → '+3V3'. Mixed nets (e.g. ['VCC', 'SDA']) → None
    because the cluster isn't purely rail-attached.

    Pure name pattern — no part-number knowledge."""
    if not nets:
        return None
    clean = [n.upper() for n in nets if n]
    if not clean:
        return None
    if len(set(clean)) != 1:
        return None
    cand = clean[0]
    POWER_TOKENS = {"VCC", "VDD", "VEE", "VSS",
                    "VBUS", "VBAT", "VSYS",
                    "GND", "AGND", "DGND", "PGND", "SGND", "EGND"}
    if cand in POWER_TOKENS:
        return cand
    # Voltage-named rails: +3V3, +5V, +12V, -12V, etc.
    if (cand.startswith(("+", "-"))
            and any(ch.isdigit() for ch in cand)):
        return cand
    return None


def _is_ic_in_block(c: Dict[str, Any]) -> bool:
    """An IC for title-suppression purposes: U/X-prefix with at least 4
    pins. Stricter than _IC_PREFIXES alone — keeps 3-pin transistors
    (Q prefix never hits here anyway) and 2-pin parts out."""
    ref = c.get("ref") or ""
    return ref.startswith(_IC_PREFIXES) and int(c.get("pin_count", 0)) >= 4


def _topology_summary(comps: List[Dict[str, Any]]) -> Optional[str]:
    """Inspect a GENERIC block's refdes-prefix mix and emit a short
    function-class name. Pure data — categorises by KiCad's standard
    prefixes (R=resistor, C=cap, L=inductor, D=diode, Q=transistor)."""
    if not comps:
        return None
    fam = {(c.get("ref") or "")[:1] for c in comps if c.get("ref")}
    n = len(comps)
    is_cap_only = fam == {"C"}
    is_res_only = fam == {"R"}
    has_rc = "R" in fam and "C" in fam
    has_rd = "R" in fam and "D" in fam
    if is_cap_only:
        return "FILTER NETWORK"
    if is_res_only:
        if n <= 4:
            return "VOLTAGE DIVIDER"
        return "RESISTOR NETWORK"
    if has_rc:
        n_r = sum(1 for c in comps if (c.get("ref") or "").startswith("R"))
        n_c = sum(1 for c in comps if (c.get("ref") or "").startswith("C"))
        if n_r >= 2 and n_c >= 2:
            return "RC NETWORK"
    if has_rd:
        return "DISCRETE NETWORK"
    return None


def _common_net_qualifier(nets: List[str]) -> Optional[str]:
    """Return a short tag (SPI / I2C / UART / SWD / VDD / ...) only when
    ≥ 2 of the block's nets share that prefix or are listed in the bus
    table. Returns None when no strong shared signal exists, so a generic
    block stays untagged instead of being mislabelled."""
    if not nets:
        return None
    BUS_TAGS = {
        "SPI": ("MOSI", "MISO", "SCLK", "SCK", "CS", "NSS", "CLK", "DI", "DO", "HOLD", "WP"),
        "I2C": ("SDA", "SCL"),
        "UART": ("TXD", "RXD", "TX", "RX", "DTR", "RTS", "CTS"),
        "USB": ("DP", "DM", "DN", "VBUS"),
        "CAN": ("CANH", "CANL", "CAN_H", "CAN_L", "CAN_TX", "CAN_RX"),
        "SWD": ("SWDIO", "SWCLK", "SWO"),
        "JTAG": ("TCK", "TMS", "TDO", "TDI", "TRST"),
    }
    nets_u = [n.upper() for n in nets if n]
    for tag, members in BUS_TAGS.items():
        hits = sum(1 for n in nets_u if any(
            n == m or n.endswith("_" + m) or n.startswith(m + "_") or m in n.split("_")
            for m in members
        ))
        if hits >= 2:
            return tag
    # Power-rail qualifier: every signal label is the same rail-like token.
    if len(nets_u) >= 2 and len(set(nets_u)) == 1 and nets_u[0]:
        cand = nets_u[0]
        if cand.startswith("+") or cand in {"VCC", "VDD", "VBUS", "VBAT", "VSYS",
                                              "GND", "AGND", "DGND"}:
            return cand
    return None


_LABEL_FONT_SIZE_MM_DEFAULT = 1.0  # fallback when label_format.fixed_font_mm
                                    # is absent. Live size comes from config —
                                    # see emit() where it threads through.


def _build_label_node(text: str, x: float, y: float, angle: int,
                       font_mm: float = _LABEL_FONT_SIZE_MM_DEFAULT) -> list:
    """Construct a (label ...) sexpr node directly. add_label() hardcodes
    angle=0, so we bypass it to honor the router's pin outward direction.
    font_mm is the fixed label font size (in mm) — passed in by emit()
    after reading label_format.fixed_font_mm."""
    return [
        _sym("label"),
        text,
        _make_at(float(x), float(y), int(angle) % 360),
        [_sym("fields_autoplaced")],
        [
            _sym("effects"),
            [_sym("font"), [_sym("size"), float(font_mm), float(font_mm)]],
            [_sym("justify"), _sym("left"), _sym("bottom")],
        ],
        _gen_uuid_node(),
    ]


def _build_power_port_node(lib_id: str, ref: str, value: str,
                            x: float, y: float, angle: int) -> list:
    """Construct a (symbol ...) entry for a power-port. We bypass
    SchematicDocument.add_component because its 5.08 mm proximity dedup
    silently drops legitimate close pin-pair ports (e.g. 4 GND pins on one
    45-pin MCU — they sit < 5 mm apart and the second/third/fourth would
    be lost). For our generated layout every routed.power_port is at an
    exact pin endpoint and must be emitted as-is."""
    return [
        _sym("symbol"),
        [_sym("lib_id"), lib_id],
        _make_at(float(x), float(y), int(angle) % 360),
        [_sym("unit"), 1],
        [_sym("exclude_from_sim"), _sym("no")],
        [_sym("in_bom"), _sym("no")],
        [_sym("on_board"), _sym("yes")],
        [_sym("dnp"), _sym("no")],
        _gen_uuid_node(),
        _make_property("Reference", ref, x + 2.54, y - 1.27, hide=True),
        _make_property("Value", value, x + 2.54, y + 1.27, hide=False),
        _make_property("Footprint", "", x, y, hide=True),
        _make_property("Datasheet", "~", x, y, hide=True),
    ]


def _rail_to_lib_id(rail: str) -> str:
    """Map a rail name to its KiCad standard power-port lib_id.

    Common rails get explicit mapping. For voltage-named rails like '+3V3',
    '+5V', '-12V' we try `power:<exact>` since KiCad's stdlib carries those
    exact symbol names. Otherwise fall back to `power:+VDC` which is the
    generic value-driven port (the Value field renders as the visible label,
    so even rare rails still look right)."""
    rail_clean = (rail or "").strip()
    rail_up = rail_clean.upper()
    DIRECT = {
        "GND": "power:GND",
        "GNDA": "power:GNDA",
        "GNDD": "power:GNDD",
        "AGND": "power:GNDA",
        "DGND": "power:GND",
        "PGND": "power:GNDPWR",
        "EGND": "power:Earth",
        "SGND": "power:GND",
        "VCC": "power:VCC",
        "VDD": "power:VDD",
        "VEE": "power:VEE",
        "VSS": "power:VSS",
        "VBUS": "power:VBUS",
        "VBAT": "power:VBATT",
    }
    if rail_up in DIRECT:
        return DIRECT[rail_up]
    if rail_clean.startswith(("+", "-")):
        return f"power:{rail_clean}"
    return "power:+VDC"


def emit(
    placement: Dict[str, Any],
    routed: Dict[str, Any],
    source_path,
    out_path,
) -> Dict[str, Any]:
    """Build a new .kicad_sch at out_path from placement + routed JSONs."""
    cfg = load_config("layout_config")
    ignored_prefixes = tuple(cfg["ignored_refdes_prefixes"]["prefixes"])

    out_p = Path(out_path)
    source_p = Path(source_path)
    if out_p.resolve() == source_p.resolve():
        raise ValueError(f"refusing to overwrite source schematic: {source_p}")
    out_p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source_p, out_p)

    doc = SchematicDocument(out_p)
    strip_stats = _strip_layout(doc, ignored_prefixes)

    # CRITICAL: prune symbols NOT in placement.components. Without this,
    # hierarchical child sheets (which receive a subset placement per role)
    # end up with the full source-schematic component list visible — every
    # child sheet shows the same flat design. _strip_layout drops wires /
    # labels / power-ports but keeps every (symbol ...). For child sheets
    # we additionally need to drop symbols whose Reference is outside the
    # placement's roster.
    #
    # For the FLAT case (single-sheet output), placement.components contains
    # every refdes anyway, so this filter is a no-op.
    placement_refs = {c.get("ref") for c in placement.get("components", []) or []
                       if c.get("ref")}
    if placement_refs:
        pruned = 0
        kept_children = [doc.tree[0]]
        for child in doc.tree[1:]:
            if not (isinstance(child, list) and _head(child) == "symbol"):
                kept_children.append(child)
                continue
            ref_prop = _get_property(child, "Reference")
            ref = _to_str(ref_prop[2]) if ref_prop else ""
            # Always keep power-port / annotation symbols (#PWR/#FLG/etc.)
            # — _strip_layout already removed the placement-irrelevant ones;
            # surviving #PWR are intentional and will be re-emitted later.
            if _is_ignored_ref(ref, ignored_prefixes):
                kept_children.append(child)
                continue
            if ref in placement_refs:
                kept_children.append(child)
            else:
                pruned += 1
        doc.tree[:] = kept_children
        strip_stats["pruned_off_sheet_symbols"] = pruned

    # Sync paper size with what the placer decided — otherwise a placer that
    # promoted to A3 still emits over an inherited A4 paper field and the
    # bottom/right of the sheet ends up off-page in eeschema.
    sheet_size = placement.get("sheet", {}).get("size") or "A4_landscape"
    _set_paper(doc.tree, sheet_size)

    # Hide pin numbers/names on small symbols (passives, diodes). 2-pin
    # symbols with visible '1' / '2' next to a 2 mm body are pure clutter.
    hp_cfg = cfg.get("hide_pin_numbers") or {}
    if hp_cfg.get("enabled", True):
        _hide_pin_numbers_for_small_symbols(
            doc.tree,
            int(hp_cfg.get("max_pins_to_hide_numbers", 4)),
            int(hp_cfg.get("max_pins_to_hide_names", 2)),
            force_show_min_pins=int(hp_cfg.get("force_show_min_pins", 0)),
        )

    # Look up body bboxes once for property normalization. The source-side
    # extractor still has the lib_symbols cache loaded, even after our strip.
    # Try the absolute import first (sys.path includes ai_backend/), fall
    # back to the relative-from-ai_backend form for the f:/Ki_CAD cwd —
    # mirrors the dual-path pattern used at the module top.
    try:
        from ai_backend.kicad_claude.schematic_extractor import SchematicExtractor as _SE
    except ImportError:
        from kicad_claude.schematic_extractor import SchematicExtractor as _SE  # type: ignore
    src_bodies = _SE(source_p).lib_symbol_bodies()
    ref_to_lib_id: Dict[str, str] = {}
    for child in doc.tree[1:]:
        if isinstance(child, list) and _head(child) == "symbol":
            lib_id = next((_to_str(s[1]) for s in child[1:]
                           if isinstance(s, list) and _head(s) == "lib_id" and len(s) > 1), "")
            ref_prop = _get_property(child, "Reference")
            if ref_prop and lib_id:
                ref_to_lib_id[_to_str(ref_prop[2])] = lib_id

    prop_cfg = cfg.get("property_normalize") or {}

    # Build (ref, unit) -> symbol_node lookup. Multi-unit ICs have multiple
    # (symbol ...) entries with the same Reference; we need to dispatch each
    # placement entry to the right unit.
    sym_by_ref_unit: Dict[Tuple[str, int], list] = {}
    for child in doc.tree[1:]:
        if not (isinstance(child, list) and _head(child) == "symbol"):
            continue
        ref_prop = _get_property(child, "Reference")
        if not ref_prop:
            continue
        ref = _to_str(ref_prop[2])
        unit = 1
        for sub in child[1:]:
            if isinstance(sub, list) and _head(sub) == "unit" and len(sub) > 1:
                try:
                    unit = int(_to_str(sub[1]))
                except ValueError:
                    pass
                break
        sym_by_ref_unit[(ref, unit)] = child

    moved = 0
    missing_refs: List[str] = []
    for comp in placement.get("components", []):
        ref = comp["ref"]
        unit = int(comp.get("unit", 1))
        target = sym_by_ref_unit.get((ref, unit)) or sym_by_ref_unit.get((ref, 1))
        if target is None:
            missing_refs.append(f"{ref}.{unit}")
            continue
        new_x = float(comp["x_mm"])
        new_y = float(comp["y_mm"])
        ok = _shift_component(
            target, new_x, new_y,
            float(comp.get("rotation", 0.0)),
        )
        if ok:
            moved += 1
            lib_id = ref_to_lib_id.get(ref)
            body = src_bodies.get(lib_id) if lib_id else None
            _normalize_property_positions(
                target, new_x, new_y, body, prop_cfg,
                sym_rot=float(comp.get("rotation", 0.0)),
            )
        else:
            missing_refs.append(f"{ref}.{unit}")

    label_fmt_cfg = cfg.get("label_format") or {}
    label_font_mm = float(label_fmt_cfg.get(
        "fixed_font_mm", _LABEL_FONT_SIZE_MM_DEFAULT))

    labels_added = 0
    for lb in routed.get("net_labels", []):
        doc.tree.append(_build_label_node(
            lb["text"], float(lb["x_mm"]), float(lb["y_mm"]),
            int(lb.get("angle", 0)),
            font_mm=label_font_mm,
        ))
        labels_added += 1

    # Emit stubs as-is. label_placer's net-aware merge (group by net + axis +
    # axis_value, take min/max extent) already handles same-net collinear
    # dedup. A geometry-only dedup here would collapse different-net wires
    # that happen to share start+direction, disconnecting their pins.
    stubs_added = 0
    for w in list(routed.get("stub_wires", []) or []):
        r = doc.add_wire([(float(w["x1"]), float(w["y1"])),
                          (float(w["x2"]), float(w["y2"]))])
        if r.get("ok"):
            stubs_added += 1

    # Functional-zone overlay: dashed border + title text per block. NOT
    # drawn unconditionally — circuit-shape gates apply so small/simple
    # designs stay clean and only multi-block layouts get the boxes.
    debug_cfg = cfg.get("debug_overlay") or {}
    zones_drawn = 0
    if debug_cfg.get("draw_zone_borders") or debug_cfg.get("draw_zone_titles"):
        blocks = placement.get("blocks", []) or []
        components_list = placement.get("components", []) or []
        total_comps = len(components_list)
        total_blocks = len(blocks)

        # Sheet-level gates: only draw boxes when they ADD clarity to a
        # navigable layout. Single-block / tiny designs skip the overlay
        # entirely — dashed boxes on a 3-component circuit are visual
        # clutter, not navigation aid.
        min_blocks = int(debug_cfg.get("min_blocks_for_borders", 2))
        min_comps = int(debug_cfg.get("min_components_for_borders", 20))
        min_subsys = int(debug_cfg.get("min_distinct_subsystems", 2))
        skip_singletons = bool(debug_cfg.get("skip_single_component_blocks", True))
        # Distinct base roles (strip _N sub-block suffix). A 13-part LM317 with
        # POWER_REGULATOR + PROTECTION + CONNECTOR + POWER_1/_2 counts as 4
        # distinct sub-systems on paper but visually it's 1 datasheet figure —
        # the role-count alone is the wrong gate. Combined with the parts
        # threshold (≥20) it correctly trips only on real multi-sub-system
        # boards (MCU + USB + memory + sensor + power + ...).
        import re as _re_subsys
        distinct_roles = {
            _re_subsys.sub(r"_\d+$", "", str(b.get("role", "GENERIC")).upper())
            for b in blocks
        }
        if (total_blocks < min_blocks
                or total_comps < min_comps
                or len(distinct_roles) < min_subsys):
            zones_drawn = 0  # bail entirely — clean look on small circuits
        else:
            border_off = float(debug_cfg.get("border_offset_mm", 0.0))
            stroke_mm = float(debug_cfg.get("border_stroke_mm", 0.15))
            style = str(debug_cfg.get("border_style", "dash"))
            title_size = float(debug_cfg.get("title_font_size_mm", 1.6))
            title_margin = float(debug_cfg.get("title_margin_mm", 1.0))
            title_color = debug_cfg.get("title_color_rgba")
            title_bold = bool(debug_cfg.get("title_bold", True))
            title_justify = str(debug_cfg.get("title_justify", "center"))
            if title_color and isinstance(title_color, list) and len(title_color) == 4:
                color_tuple = (int(title_color[0]), int(title_color[1]),
                                int(title_color[2]), float(title_color[3]))
            else:
                color_tuple = None
            routed_labels_list = routed.get("net_labels", []) or []
            # Per-block bbox policy — Arduino/Nucleo convention. Loaded once
            # outside the loop so per-block decisions stay cheap.
            policy = debug_cfg.get("bbox_policy") or {}
            always_box = {r.upper() for r in (policy.get("always_box_roles") or [])}
            never_box  = {r.upper() for r in (policy.get("never_box_roles") or [])}
            min_to_box = int(policy.get("min_components_to_box", 3))
            default_min = int(policy.get("default_min_members", 2))
            role_min = {k.upper(): int(v)
                        for k, v in (policy.get("role_min_members") or {}).items()}
            skip_titles = {t.upper() for t in (policy.get("skip_topology_titles") or [])}
            for block in blocks:
                # Strip the _N suffix that _split_oversized_blocks appends
                # ("GENERIC_1", "GENERIC_2" etc.) so the policy decisions
                # apply to sub-blocks the same way they apply to the parent
                # role. Without this strip, every GENERIC_N sub-block gets
                # treated as a brand-new role and falls through to the
                # default "box if ≥3 members" path, defeating the policy.
                raw_role = str(block.get("role", "GENERIC")).upper()
                import re as _re_role
                role = _re_role.sub(r"_\d+$", "", raw_role)
                member_count = len(block.get("members") or [])
                # Compute the title once — both for the decision (topology-title
                # skip check) and for the actual draw.
                title_text = _block_display_name(
                    block, components_list, routed_labels_list,
                )
                # Bbox policy decision tree:
                #  1. never_box_roles → no box (MCU stays focal, CRYSTAL sits next to MCU)
                #  2. skip_singletons + 1-member block → no box
                #  3. always_box_roles → box ONLY if member_count >= role_min[role]
                #     (or default_min_members). Prevents 1-member POWER_REGULATOR,
                #     PROTECTION, CONNECTOR boxes on small datasheet-style figures.
                #  4. title in skip_topology_titles → no box (VOLTAGE DIVIDER etc.)
                #  5. member_count >= min_to_box → box
                #  6. else → no box (clean look)
                if role in never_box:
                    continue
                if skip_singletons and member_count < 2:
                    continue
                if role in always_box:
                    draw_box = member_count >= role_min.get(role, default_min)
                elif title_text.upper() in skip_titles:
                    draw_box = False
                elif member_count >= min_to_box:
                    draw_box = True
                else:
                    draw_box = False
                if not draw_box:
                    continue
                bx = float(block["x_mm"]) - border_off
                by = float(block["y_mm"]) - border_off
                bw = float(block["width_mm"]) + 2 * border_off
                bh = float(block["height_mm"]) + 2 * border_off
                if debug_cfg.get("draw_zone_borders"):
                    doc.tree.append(_build_zone_border_node(bx, by, bw, bh, stroke_mm, style))
                # Skip the title when the display name resolved to empty
                # (R8 returns "" for purely-meaningless GENERIC clusters
                # — the box can still draw if the policy allows, but we
                # never label it with the word "GENERIC").
                if debug_cfg.get("draw_zone_titles") and title_text.strip():
                    if title_justify == "center":
                        tx = bx + bw / 2.0
                    else:
                        tx = bx
                    ty = by - title_margin
                    doc.tree.append(_build_zone_title_node(
                        title_text, tx, ty, title_size,
                        color_rgba=color_tuple, bold=title_bold,
                        justify=title_justify,
                    ))
                zones_drawn += 1

    # Resolve + cache every power-port lib_id into lib_symbols once. Without
    # this each emitted power-port renders as a blank "?" placeholder because
    # eeschema has no artwork for it.
    rail_to_lib = {pp["rail"]: _rail_to_lib_id(pp["rail"])
                   for pp in routed.get("power_ports", [])}
    if rail_to_lib:
        ensure_lib_symbols_for_doc(
            doc.tree, list(set(rail_to_lib.values())), project_dir=out_p.parent,
        )

    # Pattern #1 from the reference image: anchor power-port symbols to
    # the BLOCK edges instead of the pin tip itself — positive rails at
    # the TOP edge, ground rails at the BOTTOM edge. A wire from the
    # original pin tip up/down to the block edge connects them. This
    # mirrors the professional convention (POWER block, DECOUPLING
    # block, RESET CIRCUIT, etc. in the reference image): single
    # rail-port at top/bottom, components in the middle.
    # Universal: relies only on rail-name polarity + block bbox; no
    # part-number knowledge.
    block_by_ref: Dict[str, Dict[str, Any]] = {}
    for block in placement.get("blocks", []) or []:
        for member_ref in (block.get("members") or []):
            if isinstance(member_ref, str):
                block_by_ref[member_ref] = block
            elif isinstance(member_ref, dict) and member_ref.get("ref"):
                block_by_ref[member_ref["ref"]] = block

    pp_edge_cfg = cfg.get("power_port_edge_anchor") or {}
    edge_offset = float(pp_edge_cfg.get("offset_mm", 2.54))
    edge_enabled = bool(pp_edge_cfg.get("enabled", True))
    edge_wires: List[Dict[str, Any]] = []

    if edge_enabled:
        for pp in routed.get("power_ports", []) or []:
            owner_block = block_by_ref.get(pp.get("ref", ""))
            if not owner_block:
                continue
            rail_up = (pp.get("rail", "") or "").upper().lstrip()
            is_positive = (rail_up.startswith("+")
                           or rail_up in {"VCC", "VDD", "VEE",
                                          "VBUS", "VBAT", "VSYS"})
            is_ground = rail_up in {"GND", "VSS", "DGND", "AGND",
                                     "PGND", "SGND", "EGND"}
            if not (is_positive or is_ground):
                continue
            block_top = float(owner_block.get("y_mm", 0.0))
            block_h = float(owner_block.get("height_mm", 0.0))
            pin_x = float(pp["x_mm"])
            pin_y = float(pp["y_mm"])
            if is_positive:
                target_y = block_top - edge_offset
                # Only move UP — if the pin tip is already above the block
                # top (rare, but possible for an IC whose VCC pin extends
                # above its own block bbox), the port is fine in place.
                if target_y >= pin_y - 0.1:
                    continue
            else:  # ground
                target_y = block_top + block_h + edge_offset
                if target_y <= pin_y + 0.1:
                    continue
            # Snap to grid so the connecting wire lands exactly on the
            # port's connection pin (off-by-fraction = parser warning).
            grid = float(cfg.get("grid_mm", 1.27))
            target_y = round(target_y / grid) * grid
            pp["y_mm"] = target_y
            edge_wires.append({"x1": pin_x, "y1": pin_y,
                                "x2": pin_x, "y2": target_y})

    ports_added = 0
    ports_failed: List[Dict[str, Any]] = []
    pwr_idx = 1
    for pp in routed.get("power_ports", []):
        lib_id = rail_to_lib[pp["rail"]]
        ref = f"#PWR{pwr_idx:04d}"
        pwr_idx += 1
        # Power-port orientation: emit at the SYMBOL's canonical orientation
        # (rotation 0) regardless of pin angle. KiCad's stock power library
        # already has each rail symbol drawn the right way — positive rails
        # (VCC / +3V3 / VDD) point UP, ground rails (GND / VSS / AGND) point
        # DOWN. Rotating them by the pin's outward angle just upside-downs
        # the symbol; the standard practice (and the reference image) is
        # arrow-up for VCC and arrow-down for GND no matter where the pin
        # sits. Universal — works for every rail / every IC.
        doc.tree.append(_build_power_port_node(
            lib_id=lib_id,
            ref=ref,
            value=pp["rail"],
            x=float(pp["x_mm"]),
            y=float(pp["y_mm"]),
            angle=0,
        ))
        ports_added += 1

    # Emit the pin→edge connection wires for each migrated power-port.
    for w in edge_wires:
        r = doc.add_wire([(float(w["x1"]), float(w["y1"])),
                          (float(w["x2"]), float(w["y2"]))])
        if r.get("ok"):
            stubs_added += 1

    # Final-pass collinear-wire dedup: drop fully-contained segments. Safe
    # against cross-net shorts (containment-only — partial overlaps left
    # alone). The label_placer's same-net merge handles most cases upstream;
    # this catches stub_wires colliding with router-emitted segments that
    # the upstream pass couldn't see.
    wires_deduped = 0
    if hasattr(doc, "dedup_collinear_wires"):
        wires_deduped = doc.dedup_collinear_wires()

    doc.save(out_p, backup=False)

    return {
        "out_path": str(out_p),
        "strip": strip_stats,
        "components_moved": moved,
        "missing_refs": missing_refs,
        "labels_added": labels_added,
        "stub_wires_added": stubs_added,
        "wires_deduped": wires_deduped,
        "power_ports_added": ports_added,
        "power_ports_failed": ports_failed,
        "zones_drawn": zones_drawn,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.emitter")
    ap.add_argument("placement", help="placement.json from Step 3")
    ap.add_argument("routed", help="routed.json from Step 4")
    ap.add_argument("source", help="source .kicad_sch (lib_symbols donor)")
    ap.add_argument("--out", required=True, help="path to write the new .kicad_sch")
    args = ap.parse_args(argv)

    placement = json.loads(Path(args.placement).read_text(encoding="utf-8"))
    routed = json.loads(Path(args.routed).read_text(encoding="utf-8"))

    result = emit(placement, routed, args.source, args.out)
    s = result
    print(
        f"wrote {s['out_path']}  "
        f"moved={s['components_moved']}  missing={len(s['missing_refs'])}  "
        f"labels={s['labels_added']}  stubs={s.get('stub_wires_added', 0)}  "
        f"ports={s['power_ports_added']}/"
        f"{s['power_ports_added'] + len(s['power_ports_failed'])}  "
        f"zones={s.get('zones_drawn', 0)}  "
        f"stripped(layout={s['strip']['dropped_layout']}, "
        f"power={s['strip']['dropped_power_ports']}, "
        f"multi-unit-extras={s['strip']['dropped_multiunit_extras']})"
    )
    if s["missing_refs"]:
        sys.stderr.write(f"missing refs (not in source): {s['missing_refs']}\n")
    if s["power_ports_failed"]:
        sys.stderr.write(f"power-port failures: {len(s['power_ports_failed'])}\n")
        for f in s["power_ports_failed"][:5]:
            sys.stderr.write(f"  {f}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
