"""P10 — pin-role aware placement post-pass.

The grid-based placer is graph-aware (it knows which components share a
role bucket via the classifier's `_colocate_passives`) but NOT pin-aware
— a decoupling cap colocated to the MCU's role bucket gets dropped into
whatever grid cell the bucket assigns, often nowhere near the MCU's VCC
pin. A human EE always anchors a decap to the actual power pin (visual
rule: "the cap that decouples pin 14 sits within 5 mm of pin 14, not
across the IC body").

This module fixes that for satellite 2-pin passives. For each satellite:

  1. Identify its anchor IC and which anchor PIN it electrically connects
     to (the pin both passive nets touch, modulo GND / power rails).
  2. Compute a target position near that pin — `offset_from_pin_mm`
     outward in the pin's anti-pointing direction.
  3. Move the passive there if the move is short enough (≤
     `max_passive_move_mm`) AND no body collision occurs.

Conservative: any failure / collision / ambiguity falls back to the
placer's original coord. Rollback-safe — the worst case is a no-op.

Pure algorithm. No part-number rules. Works for any IC family.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from ai_backend.kicad_claude.schematic_extractor import SchematicExtractor
    from ai_backend.kicad_claude import nets as _nets
except ImportError:  # repo-on-sys.path variant
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore
    from kicad_claude import nets as _nets  # type: ignore


_ANCHOR_ROLES = frozenset({
    "MAIN_CONTROLLER", "WIRELESS", "MEMORY", "DISPLAY",
    "POWER_REGULATOR", "SENSOR", "MOTOR", "ANALOG", "RF",
})
# P10.1 — generic anchor fallback. Component CLASSES ordered by priority.
# Used when no _ANCHOR_ROLES component is on a satellite's net; we still
# want the passive to attach to the most "anchor-shaped" candidate (an
# unrecognised 8-pin op-amp beats a connector beats a 2-pin diode).
_CLASS_PRIORITY = {"IC": 100, "CONNECTOR": 50, "REGULATOR": 80, "PASSIVE": 1}
_PASSIVE_REF_PREFIXES = ("R", "C", "L", "D", "FB", "Y")
_POWER_NET_TOKENS = ("GND", "VSS", "VCC", "VDD", "VEE", "VBUS", "VBAT",
                     "VSYS", "AGND", "DGND", "PGND", "SGND", "EGND",
                     "AVCC", "AVDD")
# P10.3 — decoupling-cap power-pin name preference. Closer-to-front =
# higher preference. Lets a decap on +3V3 prefer a pin literally named
# "AVDD" / "VDD_3V3" over a generic "power_in" pin on the same net.
_POWER_PIN_NAME_PREFERENCE = (
    "AVDD", "AVCC", "VDD", "VCC", "VDDA", "VCCA",
    "VDD_", "VCC_", "VBAT", "VBUS", "VIN", "VOUT", "VEE", "VSS_",
)
# P10.2 — crystal-anchor pin names (must match a single MCU's pin pair
# on the same two crystal nets).
_CRYSTAL_PIN_NAMES = ("OSC_IN", "OSC_OUT", "XIN", "XOUT", "OSCI", "OSCO",
                      "HSE_IN", "HSE_OUT", "XTAL1", "XTAL2",
                      "XTAL_IN", "XTAL_OUT")
# P10.4 — differential-pair suffix detection. (suffix_a, suffix_b)
# pairs; matching is case-insensitive and applied to the END of the net
# name (e.g. "USB_DP" / "USB_DM" → ("DP","DM"); "CAN_H" / "CAN_L" →
# ("H","L"); "LVDS_P" / "LVDS_N" → ("P","N")).
_DIFF_PAIR_SUFFIXES = (
    ("_P",  "_N"),
    ("_DP", "_DM"),
    ("_D+", "_D-"),
    ("_H",  "_L"),
    ("_+",  "_-"),
    ("DP",  "DM"),   # bare DP/DM tail (USB_DP / USB_DM after stripping)
)


def _is_power_net_name(name: str) -> bool:
    up = (name or "").upper()
    return (up in _POWER_NET_TOKENS
            or up.startswith(("+", "-")))


def _classify_component_class(
    ref: str, lib_id: str, role: str, pin_count: int,
) -> str:
    """P10.1 — bucket a component into IC / CONNECTOR / REGULATOR / PASSIVE
    so we can score anchor candidates when no _ANCHOR_ROLES component is
    on the satellite's net. Order of evaluation matters: REGULATOR before
    IC (regulator counts as IC at the role layer but uses its own
    placement convention), CONNECTOR before PASSIVE (J/P with 2 pins is
    still a connector, not a passive)."""
    ref_up = (ref or "").upper()
    lib_up = (lib_id or "").lower()
    if role == "POWER_REGULATOR" or "regulator_" in lib_up:
        return "REGULATOR"
    if role in _ANCHOR_ROLES or pin_count >= 4:
        return "IC"
    if ref_up.startswith(("J", "P")) and not ref_up.startswith("PWR"):
        return "CONNECTOR"
    if "connector" in lib_up or "conn_" in lib_up:
        return "CONNECTOR"
    return "PASSIVE"


def _is_crystal_ref(ref: str, lib_id: str, role: str) -> bool:
    """P10.2 — crystal detection. Y* refdes is the EE convention; lib_id
    "Crystal"/"Oscillator" covers the SMD 4-pin case where refdes might
    differ. role==CRYSTAL is the classifier vote."""
    ref_up = (ref or "").upper()
    lib_low = (lib_id or "").lower()
    if ref_up.startswith(("Y", "XTAL", "OSC")):
        return True
    if "crystal" in lib_low or "oscillator" in lib_low:
        return True
    return role == "CRYSTAL"


def _power_pin_name_score(name: str, net_name: str) -> int:
    """P10.3 — higher = better match for being the actual decoupling
    target. A pin literally named "AVDD" on the +3V3 net is a better
    decap target than an "unspecified"-typed pin on the same net.
    Returns 0 for generic / unmatched names."""
    up = (name or "").upper()
    if not up:
        return 0
    score = 0
    for i, token in enumerate(_POWER_PIN_NAME_PREFERENCE):
        # Front-of-list = highest score (256 for the first entry,
        # decreasing). Substring match is enough because pin names like
        # "VDD_3V3" / "AVDD_1V8" carry the family token at the front.
        if up.startswith(token):
            score = max(score, 256 - i * 8)
    # Bonus when the pin name literally references the net name token
    # (e.g. pin "VDD_3V3" + net "+3V3" → strong correlation).
    nu = (net_name or "").upper().lstrip("+-")
    if nu and (nu in up or up in nu):
        score += 16
    return score


def _diff_pair_partner_net(net_name: str) -> Optional[str]:
    """P10.4 — return the canonical partner net name for a diff pair.
    `USB_DP` → `USB_DM`, `CANH` → `CANL`, `LVDS_TX_P` → `LVDS_TX_N`.
    None when the name isn't recognisably one half of a pair."""
    up = (net_name or "").upper()
    if not up:
        return None
    # Try exact suffix match first (longest-first to avoid mis-matching
    # "_P" inside "_DP").
    ordered = sorted(_DIFF_PAIR_SUFFIXES,
                     key=lambda p: -max(len(p[0]), len(p[1])))
    for sa, sb in ordered:
        if up.endswith(sa):
            return up[:-len(sa)] + sb
        if up.endswith(sb):
            return up[:-len(sb)] + sa
    # Bare H/L tail without underscore: CANH/CANL, LVDSH/LVDSL — only
    # accept when the prefix ends with a letter (avoids confusing
    # "GPIOH" type GPIO names with diff pairs).
    if up.endswith("H") and len(up) > 1 and up[-2].isalpha():
        return up[:-1] + "L"
    if up.endswith("L") and len(up) > 1 and up[-2].isalpha():
        return up[:-1] + "H"
    return None


def _build_anchor_pin_index(
    extractor: SchematicExtractor,
    placement: Dict[str, Any],
    anchor_refs: Set[str],
) -> Dict[str, Dict[str, Any]]:
    """For every anchor candidate, build:
      {ref: {pin_number: {x, y, angle, ref, name, electrical_type}}}
    Pin coords are at the PLACED position (rotation/mirror applied).
    Used by `_find_anchor_pin_for_satellite` to discover which anchor
    pin a 2-pin passive's net touches.

    P10.1 — `anchor_refs` no longer restricts the index to known
    anchor roles; we index EVERY non-passive (multi-pin) component so the
    scoring layer can pick the best candidate per satellite at run-time.
    The cost is one extra pin endpoint computation per medium-pin-count
    IC — cheap compared to the routing step."""
    lib_pins = extractor.lib_symbol_pins()
    placed_by_ref = {c["ref"]: c for c in placement.get("components", [])
                      if c.get("ref") in anchor_refs}
    out: Dict[str, Dict[str, Any]] = {}
    for orig in extractor.components():
        ref = orig.get("reference") or ""
        if ref not in anchor_refs:
            continue
        placed = placed_by_ref.get(ref)
        if not placed:
            continue
        lib_id = orig.get("lib_id") or ""
        by_unit = lib_pins.get(lib_id) or {}
        unit_no = int(orig.get("unit", 1))
        pin_defs: List[Dict[str, Any]] = list(by_unit.get(0, []))
        if unit_no != 0:
            pin_defs.extend(by_unit.get(unit_no, []))
        if not pin_defs:
            continue
        synth = {
            "reference": ref,
            "lib_id": lib_id,
            "at": (float(placed["x_mm"]), float(placed["y_mm"]),
                   float(placed.get("rotation", 0.0))),
            "unit": unit_no,
        }
        endpoints = _nets.placed_pin_endpoints(synth, pin_defs)
        out.setdefault(ref, {})
        for ep in endpoints:
            out[ref][str(ep.get("number", ""))] = {
                "x": ep["x"], "y": ep["y"],
                "name": ep.get("name", ""),
                "electrical_type": ep.get("electrical_type", ""),
            }
    return out


def _outward_offset_direction(
    anchor_x: float, anchor_y: float,
    pin_x: float, pin_y: float,
) -> Tuple[float, float]:
    """Unit vector pointing AWAY from the anchor body, toward the pin's
    outward direction. Used to place a satellite outside the body in the
    pin's natural extension axis."""
    dx = pin_x - anchor_x
    dy = pin_y - anchor_y
    mag = math.hypot(dx, dy)
    if mag < 0.01:
        return (1.0, 0.0)
    # Snap to dominant axis — schematic wires are orthogonal, so the
    # extension direction should be too. Avoids diagonal satellite
    # placement that the router would later struggle with.
    if abs(dx) > abs(dy):
        return (1.0 if dx > 0 else -1.0, 0.0)
    return (0.0, 1.0 if dy > 0 else -1.0)


def _bbox_intersects(
    bx: float, by: float, bw: float, bh: float,
    others: List[Tuple[float, float, float, float]],
    pad: float,
) -> bool:
    """True iff (bx, by, bw, bh) overlaps any padded bbox in `others`.
    `others` is a list of (xmin, ymin, xmax, ymax). pad mm of slack
    is added all around to enforce minimum clearance."""
    x1 = bx - bw / 2
    x2 = bx + bw / 2
    y1 = by - bh / 2
    y2 = by + bh / 2
    for ox1, oy1, ox2, oy2 in others:
        if x2 < ox1 - pad or x1 > ox2 + pad:
            continue
        if y2 < oy1 - pad or y1 > oy2 + pad:
            continue
        return True
    return False


def refine_placement_by_pin_role(
    placement: Dict[str, Any],
    classified: Dict[str, Any],
    schematic_path,
    offset_from_pin_mm: float = 5.08,
    max_passive_move_mm: float = 50.8,
    min_clearance_mm: float = 1.27,
) -> Dict[str, Any]:
    """Mutate `placement['components']` in place: move each 2-pin satellite
    passive owned by an anchor IC to sit next to the anchor pin it
    electrically connects to. Returns stats.

    P10.1 — anchor lookup is now scored (class + pin-count + same-net
    weight) instead of role-gated, so satellites attach to unrecognised
    multi-pin parts (op-amps, opto-isolators, custom modules).
    P10.2 — crystals (Y* / XTAL* / OSC*) snap CENTRED between an MCU's
    OSC_IN / OSC_OUT pin pair, with load caps inheriting that anchor.
    P10.3 — decoupling caps prefer rail-named pins (AVDD > VDD > generic
    power_in) over distance when scoring power anchors.
    P10.4 — differential-pair satellites (USB_DP/USB_DM, CANH/CANL,
    LVDS_P/LVDS_N) are co-aligned: same axis & equal offset from their
    anchor pins so the rendered pair runs visually parallel."""
    role_by_ref: Dict[str, str] = {}
    lib_id_by_ref: Dict[str, str] = {}
    for n in classified.get("nodes", []):
        ref = n.get("ref")
        role = n.get("role")
        if ref and role:
            role_by_ref[ref] = role
            lib_id_by_ref[ref] = n.get("lib_id") or ""

    extractor = SchematicExtractor(schematic_path)
    # P10.1 — index pin counts per ref BEFORE filtering, so we can score
    # candidates by pin count later. Multi-unit ICs sum across units.
    pin_count_by_ref: Dict[str, int] = defaultdict(int)
    lib_pins_by_lib = extractor.lib_symbol_pins()
    for orig in extractor.components():
        ref = orig.get("reference") or ""
        if not ref:
            continue
        lib_id = orig.get("lib_id") or ""
        by_unit = lib_pins_by_lib.get(lib_id) or {}
        unit_no = int(orig.get("unit", 1))
        n = len(by_unit.get(0, []))
        if unit_no != 0:
            n += len(by_unit.get(unit_no, []))
        pin_count_by_ref[ref] += n
        lib_id_by_ref.setdefault(ref, lib_id)

    # P10.1 — anchor CANDIDATE set: everything that classifies as IC,
    # CONNECTOR, or REGULATOR (i.e. NOT PASSIVE). This is a strict
    # superset of _ANCHOR_ROLES; the scoring below decides which one
    # wins per satellite.
    class_by_ref: Dict[str, str] = {}
    anchor_refs: Set[str] = set()
    for ref, role in role_by_ref.items():
        klass = _classify_component_class(
            ref, lib_id_by_ref.get(ref, ""), role,
            pin_count_by_ref.get(ref, 0),
        )
        class_by_ref[ref] = klass
        if klass != "PASSIVE":
            anchor_refs.add(ref)
    if not anchor_refs:
        return {"satellites_moved": 0, "satellites_skipped_no_anchor": 0,
                "satellites_skipped_too_far": 0,
                "satellites_skipped_collision": 0,
                "crystals_centered": 0, "diff_pairs_aligned": 0}

    anchor_pin_idx = _build_anchor_pin_index(extractor, placement, anchor_refs)
    net_data = _nets.build_sheet_nets(extractor)

    # Map (anchor_ref, pin_number) → set(net_names) so we can find which
    # anchor pin a satellite's net touches.
    pin_nets: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    net_to_anchor_pins: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    # P10.1 — per-net same-net counts per anchor ref: a ref with MANY
    # of its pins on a single net (e.g. a connector with 4 GND pins on
    # the GND net) gets a higher same-net weight than a ref with just
    # one pin on that net. This biases satellites toward "the anchor
    # that really owns this net".
    same_net_count: Dict[Tuple[str, str], int] = defaultdict(int)
    # P10.4 — net → set(refs) for diff-pair partner discovery.
    refs_on_net: Dict[str, Set[str]] = defaultdict(set)
    for net in net_data["nets"]:
        net_name = net.get("name", "")
        for m in net.get("members", []):
            if m.get("kind") != "pin":
                continue
            ref = m.get("ref", "")
            refs_on_net[net_name].add(ref)
            if ref in anchor_refs:
                key = (ref, str(m.get("pin_number", "")))
                pin_nets[key].add(net_name)
                net_to_anchor_pins[net_name].append(key)
                same_net_count[(ref, net_name)] += 1

    # Existing component bboxes (rough — outline_w / outline_h from
    # classifier, fall back to placed body). Used for collision checks.
    outlines: Dict[str, Tuple[float, float]] = {}
    for n in classified.get("nodes", []):
        ref = n.get("ref")
        if not ref:
            continue
        w = float(n.get("outline_w") or n.get("body_w") or 0.0)
        h = float(n.get("outline_h") or n.get("body_h") or 0.0)
        if w > 0 and h > 0:
            outlines[ref] = (w, h)

    placed_by_ref = {c["ref"]: c for c in placement.get("components", [])
                      if c.get("ref")}

    # Build collision bbox list (every non-satellite component).
    static_bboxes: List[Tuple[float, float, float, float]] = []
    for ref, comp in placed_by_ref.items():
        if not ref:
            continue
        w, h = outlines.get(ref, (2.54, 2.54))
        cx, cy = float(comp["x_mm"]), float(comp["y_mm"])
        static_bboxes.append((cx - w / 2, cy - h / 2,
                               cx + w / 2, cy + h / 2))

    # Build per-component net memberships ONCE (avoids the O(N*M) inner
    # nested loop the old code ran per satellite). Maps ref → list of
    # net names touching that ref.
    nets_by_ref: Dict[str, List[str]] = defaultdict(list)
    for net in net_data["nets"]:
        net_name = net.get("name", "")
        seen: Set[str] = set()
        for m in net.get("members", []):
            if m.get("kind") != "pin":
                continue
            r = m.get("ref", "")
            if r and r not in seen:
                nets_by_ref[r].append(net_name)
                seen.add(r)

    moved = 0
    crystals_centered = 0
    diff_pairs_aligned = 0
    skipped_no_anchor = 0
    skipped_too_far = 0
    skipped_collision = 0
    max_move = 0.0

    # Components-to-update list — we mutate at the end so collision
    # detection sees consistent placement state.
    pending_moves: List[Tuple[Dict[str, Any], float, float, float]] = []

    # P10.2 — pre-scan: for every crystal, find an MCU whose pin-name
    # pair (OSC_IN+OSC_OUT, XIN+XOUT, ...) matches the crystal's two
    # signal nets. When found, we'll RESERVE crystal_anchor_pins[ref] so
    # the generic loop skips the crystal (and we place it centred).
    # Also map crystal_load_cap_anchor[cap_ref] → the matching anchor
    # so load caps inherit the crystal's MCU.
    crystal_anchor_pins: Dict[str, Tuple[str, str, str]] = {}
    crystal_load_caps: Dict[str, str] = {}
    for comp in placement.get("components", []):
        cref = comp.get("ref") or ""
        if not _is_crystal_ref(cref, lib_id_by_ref.get(cref, ""),
                               role_by_ref.get(cref, "")):
            continue
        # Find the two non-power nets the crystal sits on.
        crystal_nets = [n for n in nets_by_ref.get(cref, [])
                        if not _is_power_net_name(n)]
        if len(crystal_nets) != 2:
            continue
        # Find an anchor ref that has BOTH crystal_nets, with each on a
        # _CRYSTAL_PIN_NAMES pin.
        best_anchor: Optional[Tuple[str, str, str]] = None
        for net_a, net_b in (crystal_nets, list(reversed(crystal_nets))):
            for anchor_pin_a in net_to_anchor_pins.get(net_a, []):
                a_ref, a_pin = anchor_pin_a
                a_name = (anchor_pin_idx.get(a_ref, {})
                          .get(a_pin, {}).get("name") or "").upper()
                if not any(t in a_name for t in _CRYSTAL_PIN_NAMES):
                    continue
                for anchor_pin_b in net_to_anchor_pins.get(net_b, []):
                    if anchor_pin_b[0] != a_ref:
                        continue
                    b_pin = anchor_pin_b[1]
                    b_name = (anchor_pin_idx.get(a_ref, {})
                              .get(b_pin, {}).get("name") or "").upper()
                    if not any(t in b_name for t in _CRYSTAL_PIN_NAMES):
                        continue
                    best_anchor = (a_ref, a_pin, b_pin)
                    break
                if best_anchor:
                    break
            if best_anchor:
                break
        if best_anchor:
            crystal_anchor_pins[cref] = best_anchor
            # Find load caps: any 2-pin C* whose nets are exactly one of
            # the crystal_nets + a power/GND rail. These should inherit
            # the crystal's anchor pair so they sit BELOW the crystal.
            for cand in placement.get("components", []):
                cand_ref = cand.get("ref") or ""
                if not cand_ref.startswith("C"):
                    continue
                if pin_count_by_ref.get(cand_ref, 0) > 2:
                    continue
                cand_nets = set(nets_by_ref.get(cand_ref, []))
                if not cand_nets:
                    continue
                pwr_count = sum(1 for n in cand_nets if _is_power_net_name(n))
                xt_count = sum(1 for n in cand_nets if n in crystal_nets)
                if pwr_count >= 1 and xt_count >= 1:
                    crystal_load_caps[cand_ref] = cref

    # P10.4 — diff-pair scan. Map ref → (partner_satellite_ref,
    # partner_anchor_pin) so paired satellites end up co-aligned.
    diff_pair_partner: Dict[str, str] = {}
    # Build {satellite_ref: signal_net} for 2-pin passives whose one
    # signal net matches a recognised diff-pair pattern.
    sat_signal_net: Dict[str, str] = {}
    for comp in placement.get("components", []):
        sref = comp.get("ref") or ""
        if not sref or not sref.startswith(_PASSIVE_REF_PREFIXES):
            continue
        if pin_count_by_ref.get(sref, 0) > 2:
            continue
        for n in nets_by_ref.get(sref, []):
            if _is_power_net_name(n):
                continue
            if _diff_pair_partner_net(n):
                sat_signal_net[sref] = n
                break
    # For each diff-pair satellite, find a SIBLING satellite on the
    # partner net.
    for sat_ref, net_name in sat_signal_net.items():
        partner_net = _diff_pair_partner_net(net_name)
        if not partner_net:
            continue
        for other_ref, other_net in sat_signal_net.items():
            if other_ref == sat_ref:
                continue
            if other_net == partner_net:
                diff_pair_partner[sat_ref] = other_ref
                break

    # Helper: scored selection of an anchor pin from candidates. P10.1
    # (class/pin-count/same-net weighting) + P10.3 (power-pin name
    # preference) live here. Returns the best (ref, pin) or None.
    def _score_anchor_pin(
        candidates: List[Tuple[str, str]],
        cur_x: float, cur_y: float,
        net_name: str, is_pwr: bool,
    ) -> Optional[Tuple[str, str]]:
        if not candidates:
            return None
        best: Optional[Tuple[str, str]] = None
        best_score = -1.0
        for ap in candidates:
            a_ref, a_pin = ap
            pin_info = anchor_pin_idx.get(a_ref, {}).get(a_pin)
            if not pin_info:
                continue
            klass = class_by_ref.get(a_ref, "PASSIVE")
            pin_count = pin_count_by_ref.get(a_ref, 0)
            same_net = same_net_count.get((a_ref, net_name), 1)
            dist = math.hypot(pin_info["x"] - cur_x,
                              pin_info["y"] - cur_y) + 0.01
            # Combined score. Class is the dominant term (an 8-pin
            # op-amp always beats a 2-pin LED on the same signal net).
            # Then pin count (larger = more "anchor-ish"). Then same-net
            # weight (a connector that owns 4 GND pins beats one that
            # touches GND on a single pin). Then -dist so among equally
            # good candidates the closest wins.
            # Distance penalty is per-class: within an IC tier, two
            # 50mm-apart candidates should differ by ~250 points (more
            # than enough to overrule pin-count differences when the
            # closer one has fewer pins). Across classes, the 100k IC
            # ceiling still wins.
            score = (_CLASS_PRIORITY.get(klass, 1) * 1000.0
                     + pin_count * 10.0
                     + same_net * 5.0
                     - dist * 5.0)
            # P10.3 — for POWER nets specifically, boost pins whose name
            # matches the rail family. Bias is strong enough to overrule
            # raw distance: a +3V3-named VDD pin 10 mm farther beats a
            # generic power_in pin 5 mm closer.
            if is_pwr:
                pscore = _power_pin_name_score(pin_info.get("name", ""),
                                                net_name)
                score += pscore * 2.0
            if score > best_score:
                best_score = score
                best = ap
        return best

    for comp in placement.get("components", []):
        ref = comp.get("ref") or ""
        if not ref or not ref.startswith(_PASSIVE_REF_PREFIXES):
            continue

        # P10.2 — crystal special case. If this ref is a crystal with a
        # matched MCU anchor pair, place it CENTRED between the two
        # OSC pins (offset outward perpendicular to the pin axis).
        if ref in crystal_anchor_pins:
            a_ref, a_pin_a, a_pin_b = crystal_anchor_pins[ref]
            anchor_comp = placed_by_ref.get(a_ref)
            pin_a = anchor_pin_idx.get(a_ref, {}).get(a_pin_a)
            pin_b = anchor_pin_idx.get(a_ref, {}).get(a_pin_b)
            if not (anchor_comp and pin_a and pin_b):
                skipped_no_anchor += 1
                continue
            # Midpoint between the two crystal pins, offset outward
            # perpendicular to the line MCU-centre → midpoint.
            mid_x = (pin_a["x"] + pin_b["x"]) / 2.0
            mid_y = (pin_a["y"] + pin_b["y"]) / 2.0
            ax = float(anchor_comp["x_mm"])
            ay = float(anchor_comp["y_mm"])
            dx, dy = _outward_offset_direction(ax, ay, mid_x, mid_y)
            target_x = mid_x + dx * offset_from_pin_mm
            target_y = mid_y + dy * offset_from_pin_mm
            cur_x, cur_y = float(comp["x_mm"]), float(comp["y_mm"])
            move_d = math.hypot(target_x - cur_x, target_y - cur_y)
            if move_d > max_passive_move_mm * 1.5:  # crystals get slack
                skipped_too_far += 1
                continue
            w, h = outlines.get(ref, (5.08, 2.54))
            cur_bbox = (cur_x - w / 2, cur_y - h / 2,
                        cur_x + w / 2, cur_y + h / 2)
            others = [bb for bb in static_bboxes
                      if not (abs(bb[0] - cur_bbox[0]) < 0.01
                              and abs(bb[1] - cur_bbox[1]) < 0.01)]
            if _bbox_intersects(target_x, target_y, w, h, others,
                                 min_clearance_mm):
                skipped_collision += 1
                continue
            pending_moves.append((comp, target_x, target_y, move_d))
            crystals_centered += 1
            continue

        # Find candidate anchor pins per net the satellite touches.
        # signal_anchors / power_anchors are now LISTS (preserve
        # multiplicity for same-net weighting).
        signal_anchors: List[Tuple[str, str]] = []
        signal_anchor_nets: Dict[Tuple[str, str], str] = {}
        power_anchors: List[Tuple[str, str]] = []
        power_anchor_nets: Dict[Tuple[str, str], str] = {}
        for net_name in nets_by_ref.get(ref, []):
            is_pwr = _is_power_net_name(net_name)
            for anchor_pin in net_to_anchor_pins.get(net_name, []):
                if anchor_pin[0] == ref:
                    continue
                if is_pwr:
                    if anchor_pin not in power_anchor_nets:
                        power_anchors.append(anchor_pin)
                        power_anchor_nets[anchor_pin] = net_name
                else:
                    if anchor_pin not in signal_anchor_nets:
                        signal_anchors.append(anchor_pin)
                        signal_anchor_nets[anchor_pin] = net_name

        cur_x, cur_y = float(comp["x_mm"]), float(comp["y_mm"])
        anchor_target: Optional[Tuple[str, str]] = None
        anchor_net: str = ""
        if signal_anchors:
            # Use the FIRST signal-net of this sat to score, but pass
            # all signal-anchor candidates regardless of net (a
            # bus-resistor crossing two nets still scores by class).
            anchor_target = _score_anchor_pin(
                signal_anchors, cur_x, cur_y,
                next(iter(signal_anchor_nets.values())), is_pwr=False,
            )
            if anchor_target:
                anchor_net = signal_anchor_nets.get(anchor_target, "")
        if not anchor_target and power_anchors:
            anchor_target = _score_anchor_pin(
                power_anchors, cur_x, cur_y,
                next(iter(power_anchor_nets.values())), is_pwr=True,
            )
            if anchor_target:
                anchor_net = power_anchor_nets.get(anchor_target, "")
        if not anchor_target:
            skipped_no_anchor += 1
            continue
        anchor_ref, anchor_pin_no = anchor_target
        anchor_comp = placed_by_ref.get(anchor_ref)
        if not anchor_comp:
            skipped_no_anchor += 1
            continue
        anchor_pin = anchor_pin_idx.get(anchor_ref, {}).get(anchor_pin_no)
        if not anchor_pin:
            skipped_no_anchor += 1
            continue

        # Target position: offset outward from the pin tip in the
        # anchor → pin direction.
        ax = float(anchor_comp["x_mm"])
        ay = float(anchor_comp["y_mm"])
        pin_x = float(anchor_pin["x"])
        pin_y = float(anchor_pin["y"])
        dx, dy = _outward_offset_direction(ax, ay, pin_x, pin_y)
        target_x = pin_x + dx * offset_from_pin_mm
        target_y = pin_y + dy * offset_from_pin_mm

        # P10.4 — diff-pair alignment. If this satellite is paired with
        # another satellite on the partner net, and the partner is on
        # the same anchor IC, snap so the two satellites share an axis
        # (the OFFSET direction from each anchor pin must match) — this
        # makes USB_DP/DM and CANH/CANL resistors run parallel.
        partner_ref = diff_pair_partner.get(ref)
        if partner_ref:
            # Find the partner's anchor pin on the partner net.
            partner_net = _diff_pair_partner_net(anchor_net)
            if partner_net:
                for ap in net_to_anchor_pins.get(partner_net, []):
                    if ap[0] != anchor_ref:
                        continue
                    p_pin = anchor_pin_idx.get(ap[0], {}).get(ap[1])
                    if not p_pin:
                        continue
                    # Force both satellites onto the SAME perpendicular
                    # offset axis. We pick the axis from THIS pin (the
                    # one we're about to place) — the partner will then
                    # mirror via the same logic on its loop iteration.
                    if abs(dx) > abs(dy):
                        target_y = pin_y  # horizontal extension axis
                    else:
                        target_x = pin_x  # vertical extension axis
                    diff_pairs_aligned += 1
                    break

        # P10.2 — load-cap inheritance. If this is a crystal load cap,
        # bias placement BELOW the crystal (in the outward direction
        # already computed) by an additional row so the crystal sits
        # nearest the IC and the caps sit beyond.
        xt_ref = crystal_load_caps.get(ref)
        if xt_ref and xt_ref in placed_by_ref:
            target_x += dx * offset_from_pin_mm * 0.7
            target_y += dy * offset_from_pin_mm * 0.7

        # Distance check.
        move_d = math.hypot(target_x - cur_x, target_y - cur_y)
        if move_d > max_passive_move_mm:
            skipped_too_far += 1
            continue

        # Collision check — exclude the satellite's CURRENT bbox so it
        # doesn't conflict with itself.
        w, h = outlines.get(ref, (2.54, 5.08))
        # Build others-list excluding this component.
        cur_bbox = (cur_x - w / 2, cur_y - h / 2,
                    cur_x + w / 2, cur_y + h / 2)
        others = [bb for bb in static_bboxes
                  if not (abs(bb[0] - cur_bbox[0]) < 0.01
                           and abs(bb[1] - cur_bbox[1]) < 0.01)]
        if _bbox_intersects(target_x, target_y, w, h, others, min_clearance_mm):
            skipped_collision += 1
            continue

        pending_moves.append((comp, target_x, target_y, move_d))

    # Apply all pending moves at once, updating the static-bbox list
    # incrementally so later satellites in the loop see the earlier
    # moves' new positions and don't collide.
    for comp, target_x, target_y, move_d in pending_moves:
        ref = comp.get("ref", "")
        w, h = outlines.get(ref, (2.54, 5.08))
        # Re-check collision against the CURRENT static_bboxes (which
        # now include previously-applied moves' new positions).
        cur_x, cur_y = float(comp["x_mm"]), float(comp["y_mm"])
        cur_bbox = (cur_x - w / 2, cur_y - h / 2,
                    cur_x + w / 2, cur_y + h / 2)
        # Remove this component's OLD bbox and check against the rest.
        others = [bb for bb in static_bboxes
                  if not (abs(bb[0] - cur_bbox[0]) < 0.01
                           and abs(bb[1] - cur_bbox[1]) < 0.01)]
        if _bbox_intersects(target_x, target_y, w, h, others, min_clearance_mm):
            skipped_collision += 1
            continue
        # Apply the move.
        comp["x_mm"] = float(target_x)
        comp["y_mm"] = float(target_y)
        # Update static bboxes: replace OLD bbox with NEW.
        new_bbox = (target_x - w / 2, target_y - h / 2,
                    target_x + w / 2, target_y + h / 2)
        for i, bb in enumerate(static_bboxes):
            if (abs(bb[0] - cur_bbox[0]) < 0.01
                    and abs(bb[1] - cur_bbox[1]) < 0.01):
                static_bboxes[i] = new_bbox
                break
        else:
            static_bboxes.append(new_bbox)
        moved += 1
        if move_d > max_move:
            max_move = move_d

    return {
        "satellites_moved": moved,
        "satellites_skipped_no_anchor": skipped_no_anchor,
        "satellites_skipped_too_far": skipped_too_far,
        "satellites_skipped_collision": skipped_collision,
        "crystals_centered": crystals_centered,
        "diff_pairs_aligned": diff_pairs_aligned,
        "max_move_mm": round(max_move, 2),
    }
