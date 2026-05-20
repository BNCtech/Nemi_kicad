"""Step 4 of the universal layout engine: net router.

Consumes placement.json (Step 3) + the original schematic, computes pin
endpoints at the NEW component positions, and emits routing primitives:
  - net labels at every signal-net pin
  - power-port symbols at every power-net pin

v1 is LABEL-DOMINANT — every connection becomes label-to-label, never long
wires. That's the canonical KiCad convention for medium/large sheets: same-
named labels are electrically equivalent to a wire, with the bonus that
they eliminate every wire crossing problem for free. Wire-based routing
for short, single-pair signals lands in v2 once the emitter (Step 5) lets
us see the result in eeschema and decide which connections actually need
visible wires.

Output (routed.json):
  {
    "net_labels":  [{text, x_mm, y_mm, angle, ref, pin_number}, ...],
    "power_ports": [{rail, x_mm, y_mm, angle, ref, pin_number}, ...],
    "stub_wires":  [],
    "junctions":   [],
    "stats":       {nets_processed, signal_nets, power_nets, ...,
                    skipped_pins: [{ref, pin_number, reason}, ...]}
  }

The router does NOT modify placement positions; it only ANNOTATES every
placed pin with the label/port that belongs there. The emitter (Step 5)
consumes both placement.json + routed.json to write a .kicad_sch.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import load_config


def _sanitize_net_name(name: str, cfg: Dict[str, Any]) -> str:
    """Strip / rewrite characters KiCad rejects in label tokens. The canonical
    offender is the auto-generated `Net-(REF-PadN)` form (parens + hyphen);
    we rewrite it to `REF_PN`. Anything else with stray invalid chars gets a
    char-for-char replacement so we never emit a label that fails to parse."""
    if not name:
        return name
    pat = cfg.get("auto_name_pattern")
    if pat:
        m = re.match(pat, name)
        if m:
            return cfg.get("auto_name_format", "{ref}_P{pad}").format(
                ref=m.group(1), pad=m.group(2)
            )
    invalid = cfg.get("invalid_chars", "()")
    replacement = cfg.get("fallback_replacement", "_")
    if any(ch in name for ch in invalid):
        for ch in invalid:
            name = name.replace(ch, replacement)
    return name


def _abbreviate(net: str, cfg: Dict[str, Any]) -> str:
    """Tier-3 abbreviation: prefix N chars + suffix M chars (e.g. 'ANALOG_A0'
    -> 'ANALO_A0', 'SPI_MOSI_PORT_B' -> 'SPI_M_T_B'). Only applied when
    per-pin space is so tight that a full name overflows into adjacent
    labels. Net names already <= abbrev_max_chars are returned unchanged."""
    max_chars = int(cfg.get("abbrev_max_chars", 8))
    if len(net) <= max_chars:
        return net
    p = int(cfg.get("abbrev_prefix_chars", 5))
    s = int(cfg.get("abbrev_suffix_chars", 3))
    return net[:p] + net[-s:]


def _apply_label_format(
    net_labels: List[Dict[str, Any]], cfg_fmt: Dict[str, Any]
) -> Dict[str, int]:
    """Three-tier text formatter, applied per (ref, unit, outward angle) group.
    Font size is FIXED — only the text content adapts to per-pin space.

      ratio = space_per_pin / (fixed_font_mm * 1.5)

      ratio >= show_pin_number_threshold : full net + pin number
      ratio >= abbrev_threshold          : full net only
      else                               : abbreviated net only

    space_per_pin is the perpendicular-axis span of the side's pins divided
    by pin count (vertical span for horizontal-outward sides, horizontal span
    for vertical-outward sides). A single-label group always lands in tier-1
    (no pitch to compute)."""
    if not cfg_fmt.get("embed_pin_number", True):
        return {"tier_1_full_plus_pin": 0, "tier_2_net_only": 0, "tier_3_abbrev": 0}

    font_mm = float(cfg_fmt.get("fixed_font_mm", 1.0))
    label_h = font_mm * 1.5
    show_thr = float(cfg_fmt.get("show_pin_number_threshold", 1.5))
    abbrev_thr = float(cfg_fmt.get("abbrev_threshold", 1.0))
    sep = str(cfg_fmt.get("separator", "  "))
    fmt_by_angle = {
        0:   cfg_fmt.get("right_format",  "{pin}{sep}{net}"),
        90:  cfg_fmt.get("bottom_format", "{pin}{sep}{net}"),
        180: cfg_fmt.get("left_format",   "{net}{sep}{pin}"),
        270: cfg_fmt.get("top_format",    "{pin}{sep}{net}"),
    }

    from collections import defaultdict as _dd
    groups: Dict[Tuple[str, int, int], List[Dict[str, Any]]] = _dd(list)
    for lb in net_labels:
        key = (lb.get("ref", ""), int(lb.get("unit", 1)), int(lb.get("angle", 0)))
        groups[key].append(lb)

    counts = {"tier_1_full_plus_pin": 0, "tier_2_net_only": 0, "tier_3_abbrev": 0}
    for (_, _, angle), labels in groups.items():
        # Per-side pitch: perp axis to the outward direction.
        horiz_outward = angle % 180 == 0
        coord_key = "y_mm" if horiz_outward else "x_mm"
        coords = sorted(float(lb[coord_key]) for lb in labels)
        if len(coords) >= 2:
            span = coords[-1] - coords[0]
            space_per_pin = span / max(len(coords) - 1, 1)
        else:
            space_per_pin = float("inf")
        ratio = space_per_pin / label_h if label_h > 0 else float("inf")

        fmt = fmt_by_angle.get(angle % 360, "{net}{sep}{pin}")
        for lb in labels:
            net = lb.get("text", "")
            pin = str(lb.get("pin_number", ""))
            if ratio >= show_thr and pin:
                lb["text"] = fmt.format(net=net, sep=sep, pin=pin)
                counts["tier_1_full_plus_pin"] += 1
            elif ratio >= abbrev_thr:
                # net-only — leave lb["text"] alone
                counts["tier_2_net_only"] += 1
            else:
                lb["text"] = _abbreviate(net, cfg_fmt)
                counts["tier_3_abbrev"] += 1
    return counts


def _shorten_net_names(names: List[str], cfg: Dict[str, Any]) -> Dict[str, str]:
    """Build a collision-safe long->short name map.

    Drop namespace prefix: `NPM1300.SCL` -> `SCL`, `ANALOG.A0` -> `A0`. Skip
    names starting with `~` (KiCad overline syntax — must preserve). If two
    different long names shrink to the same short form, ALL such conflicting
    names keep their long form so KiCad still sees distinct nets.

    `max_chars > 0` additionally truncates anything longer than the cap — only
    used as a last resort, OFF by default. Truncation is dangerous because two
    distinct nets can collapse to the same prefix; we still check collisions."""
    drop_ns = bool(cfg.get("drop_namespace_prefix", True))
    max_chars = int(cfg.get("max_chars", 0))

    short: Dict[str, str] = {}
    for n in names:
        s = n
        if drop_ns and "." in n and not n.startswith("~"):
            parts = n.rsplit(".", 1)
            if len(parts) == 2 and parts[1]:
                s = parts[1]
        if max_chars > 0 and len(s) > max_chars:
            s = s[:max_chars]
        short[n] = s

    counts: Dict[str, int] = {}
    for s in short.values():
        counts[s] = counts.get(s, 0) + 1

    return {long: (short_name if counts[short_name] == 1 else long)
            for long, short_name in short.items()}

try:
    from ai_backend.kicad_claude.schematic_extractor import SchematicExtractor
    from ai_backend.kicad_claude import nets as _nets
except ImportError:  # supports both `f:/Ki_CAD` and `ai_backend/` on sys.path
    from kicad_claude.schematic_extractor import SchematicExtractor  # type: ignore
    from kicad_claude import nets as _nets  # type: ignore


def _is_power_net(net: Dict[str, Any], power_patterns: List[str]) -> bool:
    """Same predicate the graph layer uses — power-port member wins, otherwise
    fall back to name-pattern match."""
    for m in net.get("members", []):
        if m.get("kind") == "power":
            return True
    name_up = (net.get("name") or "").upper()
    return any(name_up == p or name_up.startswith(p) for p in power_patterns)


def _power_rail_name(net: Dict[str, Any]) -> str:
    """The rail name to draw next to the power-port symbol. Prefer the explicit
    power-port `name` because it survives the union-find merge (multiple GND
    ports collapse to one net); fall back to the net name when none exists
    (rare — happens when a bare `+3V3` label has no matching power-port)."""
    for m in net.get("members", []):
        if m.get("kind") == "power" and m.get("name"):
            return m["name"]
    return net.get("name") or ""


def _is_auto_named(name: str) -> bool:
    """KiCad's default for unnamed nets is `Net-(REF-PadN)` — visually ugly
    when emitted as a label. v1 still labels them (correct electrically) but
    flags them in stats so v2's wire-replacer can target them first."""
    return name.startswith("Net-(") or name.startswith("unconnected-")


def _segment_intersects_bbox(
    x1: float, y1: float, x2: float, y2: float,
    bbox: Tuple[float, float, float, float],
) -> bool:
    """True iff axis-aligned segment passes through bbox interior. Endpoints
    on the bbox edge don't count — a pin tip sits ON the owner body's edge
    and is allowed."""
    xmin, ymin, xmax, ymax = bbox
    if abs(x1 - x2) < 1e-3:  # vertical
        if x1 <= xmin + 1e-3 or x1 >= xmax - 1e-3:
            return False
        lo, hi = min(y1, y2), max(y1, y2)
        return not (hi <= ymin + 1e-3 or lo >= ymax - 1e-3)
    if abs(y1 - y2) < 1e-3:  # horizontal
        if y1 <= ymin + 1e-3 or y1 >= ymax - 1e-3:
            return False
        lo, hi = min(x1, x2), max(x1, x2)
        return not (hi <= xmin + 1e-3 or lo >= xmax - 1e-3)
    return False


def _pick_l_corner(
    x1: float, y1: float, x2: float, y2: float,
    body_bbox_by_ref: Dict[str, Tuple[float, float, float, float]],
) -> Tuple[float, float]:
    """Choose the L-bend corner that produces two axis-aligned wire segments
    avoiding every other-component body. Endpoints touching a body (pin
    tips) are considered owners and skipped.

    Two candidates: (x2, y1) → horizontal-first; (x1, y2) → vertical-first.
    Pick whichever yields zero crossings. Tie or both-cross → default to
    (x2, y1) so behaviour is deterministic."""
    # Exclude bodies that contain either endpoint (owners).
    def _is_owner(bbox: Tuple[float, float, float, float],
                  px: float, py: float) -> bool:
        x_lo, y_lo, x_hi, y_hi = bbox
        return x_lo - 0.1 <= px <= x_hi + 0.1 and y_lo - 0.1 <= py <= y_hi + 0.1
    others = [bb for bb in body_bbox_by_ref.values()
              if not (_is_owner(bb, x1, y1) or _is_owner(bb, x2, y2))]

    def _ok(cx: float, cy: float) -> bool:
        for bb in others:
            if _segment_intersects_bbox(x1, y1, cx, cy, bb):
                return False
            if _segment_intersects_bbox(cx, cy, x2, y2, bb):
                return False
        return True

    for cx, cy in ((x2, y1), (x1, y2)):
        if _ok(cx, cy):
            return (cx, cy)
    return (x2, y1)


def _world_pin_angle(pin_def_rot: float, placed_rot: float) -> int:
    """Compute the world-space outward direction (in KiCad's math-CCW + Y-down
    convention) for a label/power-port placed at this pin's tip.

    KiCad pin `rot` is the INWARD direction expressed in the lib symbol's
    Y-UP coordinate system. Two conversions in sequence:
      1. invert (inward -> outward) in Y-up:    outward_yup = rot + 180
      2. flip Y for schematic Y-down storage:   outward_ydn = -outward_yup
    Combined: outward = (-rot - 180) % 360 = (180 - rot) % 360.

    Empirically on BM15x (body bbox x in [-17.78, 17.78], y in [-35.56, 35.56]):
      pin.rot=0   (left,  body +X):  outward=180  ✓ label extends LEFT
      pin.rot=180 (right, body -X):  outward=0    ✓ label extends RIGHT
      pin.rot=270 (top,   body -Y):  outward=270  ✓ label extends UP on screen
      pin.rot=90  (bottom,body +Y):  outward=90   ✓ label extends DOWN on screen

    The previous formula `(rot + 180) % 360` missed the Y-flip and produced
    correct angles only for horizontal pins (left/right). Vertical pins on
    every component got labels and stub wires pointing INTO the body — exactly
    the 'wire crosses through symbol body' KiCad warnings on R2/R3/R5/R6/R7."""
    return int(round(180.0 - pin_def_rot - placed_rot)) % 360


def _build_pin_lookup(
    extractor: SchematicExtractor,
    placement_components: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    """For every placed unit-instance, resolve every pin to its NEW world-coord
    tip + outward angle. Returns a (ref, pin_number) → list[entries] dict
    where each entry carries `unit` and `(x, y, angle)`. Multi-unit common
    pins (those defined in unit 0 of the lib_symbol) appear in the list
    for EACH placed unit; unit-specific pins appear once for their owning
    unit only. The router emits one label per entry — that's per-unit
    fan-out."""
    lib_pins = extractor.lib_symbol_pins()
    placed_by_ref_unit: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for c in placement_components:
        placed_by_ref_unit[(c["ref"], int(c.get("unit", 1)))] = c

    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

    for orig in extractor.components():
        ref = orig.get("reference") or ""
        if not ref:
            continue
        unit_no = int(orig.get("unit", 1))
        placed = placed_by_ref_unit.get((ref, unit_no))
        if placed is None:
            continue
        lib_id = orig.get("lib_id", "")
        by_unit = lib_pins.get(lib_id) or {}
        if not by_unit:
            continue
        pin_defs: List[Dict[str, Any]] = list(by_unit.get(0, []))
        if unit_no in by_unit and unit_no != 0:
            pin_defs.extend(by_unit[unit_no])
        if not pin_defs:
            for u_pins in by_unit.values():
                pin_defs.extend(u_pins)
        if not pin_defs:
            continue

        placed_rot = float(placed.get("rotation", 0.0))
        synth = {
            "reference": ref,
            "lib_id": lib_id,
            "at": (float(placed["x_mm"]), float(placed["y_mm"]), placed_rot),
            "unit": unit_no,
        }
        endpoints = _nets.placed_pin_endpoints(synth, pin_defs)

        def_by_num: Dict[str, Dict[str, Any]] = {}
        for p in pin_defs:
            def_by_num[str(p["number"])] = p
        for ep in endpoints:
            pd = def_by_num.get(str(ep["number"]))
            world_rot = _world_pin_angle(float(pd["rot"]) if pd else 0.0, placed_rot)
            entry = {
                "unit": unit_no,
                "x": ep["x"],
                "y": ep["y"],
                "angle": world_rot,
                "electrical_type": ep.get("electrical_type", ""),
                "pin_name": ep.get("name", ""),
            }
            out.setdefault((ep["ref"], str(ep["number"])), []).append(entry)
    return out


def route(placement: Dict[str, Any], schematic_path) -> Dict[str, Any]:
    """Build routed.json from a placement.json and the source schematic."""
    cfg = load_config("layout_config")
    power_patterns = [p.upper() for p in cfg["power_net_patterns"]["patterns"]]
    ignored_prefixes = tuple(cfg["ignored_refdes_prefixes"]["prefixes"])
    sanitize_cfg = cfg.get("net_name_sanitizer") or {}

    def _is_ignored_ref(ref: str) -> bool:
        return bool(ref) and ref.startswith(ignored_prefixes)

    extractor = SchematicExtractor(schematic_path)
    # Per-ref placed body bboxes (no padding) for auto-wire L-bend body-
    # avoidance. Built once — reused for every L-bend decision below.
    body_bbox_by_ref: Dict[str, Tuple[float, float, float, float]] = {}
    try:
        from .label_placer import _build_body_bbox_by_ref as _bbb
        body_bbox_by_ref = _bbb(placement, schematic_path)
    except Exception:
        body_bbox_by_ref = {}
    net_data = _nets.build_sheet_nets(extractor)

    pin_lookup = _build_pin_lookup(extractor, placement["components"])

    net_labels: List[Dict[str, Any]] = []
    power_ports: List[Dict[str, Any]] = []
    auto_wires: List[Dict[str, Any]] = []
    skipped_pins: List[Dict[str, Any]] = []
    auto_named_nets: List[str] = []
    signal_count = 0
    power_count = 0

    for net in net_data["nets"]:
        raw_pin_members = [m for m in net.get("members", []) if m.get("kind") == "pin"]
        # Dedup by (ref, pin_number). Multi-unit ICs have their unit-0 'common'
        # pins (VCC, GND) iterated once per unit instance in build_sheet_nets's
        # member list — 4 units => 4 member entries for the same physical pin.
        # Without dedup, the router emits 4 labels + 4 stubs at the same world
        # coord, which renders as visually overlapping wires.
        seen: set = set()
        pin_members: List[Dict[str, Any]] = []
        for m in raw_pin_members:
            key = (m.get("ref", ""), str(m.get("pin_number", "")))
            if key in seen:
                continue
            seen.add(key)
            pin_members.append(m)
        if not pin_members:
            continue

        if _is_power_net(net, power_patterns):
            rail = _sanitize_net_name(_power_rail_name(net), sanitize_cfg)
            if not rail:
                skipped_pins.append({
                    "net_id": net["id"], "reason": "power net with no rail name",
                })
                continue
            emitted_for_net: set = set()
            for m in pin_members:
                key = (m.get("ref", ""), str(m.get("pin_number", "")))
                entries = pin_lookup.get(key) or []
                if not entries:
                    if not _is_ignored_ref(m.get("ref", "")):
                        skipped_pins.append({
                            "ref": m.get("ref", ""),
                            "pin_number": m.get("pin_number", ""),
                            "reason": "pin not in placement (component unplaced)",
                        })
                    continue
                for ep in entries:
                    emit_key = (key[0], ep["unit"], key[1])
                    if emit_key in emitted_for_net:
                        continue
                    emitted_for_net.add(emit_key)
                    power_ports.append({
                        "rail": rail,
                        "x_mm": ep["x"],
                        "y_mm": ep["y"],
                        "angle": ep["angle"],
                        "ref": key[0],
                        "pin_number": key[1],
                        "unit": ep["unit"],
                    })
            power_count += 1
            continue

        raw_name = net.get("name") or ""
        if not raw_name:
            skipped_pins.append({"net_id": net["id"], "reason": "signal net with no name"})
            continue
        is_auto = _is_auto_named(raw_name)
        if is_auto:
            auto_named_nets.append(raw_name)
        name = _sanitize_net_name(raw_name, sanitize_cfg)

        # Collect every pin endpoint on this net so we can later decide
        # whether to emit labels (named nets) or a connecting wire chain
        # (auto-named nets — KiCad's Net-(REF-PadN) form). Auto-named net
        # labels are pure clutter: `C1_P1`, `R2_P2` strings tile across the
        # schematic with no semantic value. A wire between the same pins
        # is electrically equivalent and visually clean.
        emitted_for_net: set = set()
        endpoints_for_net: List[Tuple[float, float]] = []
        for m in pin_members:
            key = (m.get("ref", ""), str(m.get("pin_number", "")))
            entries = pin_lookup.get(key) or []
            if not entries:
                if not _is_ignored_ref(m.get("ref", "")):
                    skipped_pins.append({
                        "ref": m.get("ref", ""),
                        "pin_number": m.get("pin_number", ""),
                        "reason": "pin not in placement (component unplaced)",
                    })
                continue
            for ep in entries:
                emit_key = (key[0], ep["unit"], key[1])
                if emit_key in emitted_for_net:
                    continue
                emitted_for_net.add(emit_key)
                if is_auto:
                    endpoints_for_net.append((ep["x"], ep["y"]))
                else:
                    net_labels.append({
                        "text": name,
                        "x_mm": ep["x"],
                        "y_mm": ep["y"],
                        "angle": ep["angle"],
                        "ref": key[0],
                        "pin_number": key[1],
                        "unit": ep["unit"],
                    })

        # Auto-named net wire chain. >= 2 pins: connect them sequentially
        # with axis-aligned segments (KiCad's parser rejects diagonal
        # wires). Single-pin auto-named nets are dangling — leave them
        # alone, ERC will flag them honestly. For each non-collinear pair
        # of pins, pick the L-bend corner (horizontal-first OR vertical-
        # first) whose two segments avoid every OTHER component's body
        # bbox. Universal: works for any body size, any pin geometry.
        if is_auto and len(endpoints_for_net) >= 2:
            for i in range(len(endpoints_for_net) - 1):
                x1, y1 = endpoints_for_net[i]
                x2, y2 = endpoints_for_net[i + 1]
                if abs(x1 - x2) < 0.01 or abs(y1 - y2) < 0.01:
                    auto_wires.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2})
                else:
                    cx, cy = _pick_l_corner(x1, y1, x2, y2, body_bbox_by_ref)
                    auto_wires.append({"x1": x1, "y1": y1, "x2": cx, "y2": cy})
                    auto_wires.append({"x1": cx, "y1": cy, "x2": x2, "y2": y2})

        signal_count += 1

    # Drop namespace prefixes (NPM1300.SCL -> SCL) where collision-free.
    # Applied to label text only; power-port rails stay as-is (VCC/GND/+3V3
    # are already canonical short names).
    short_map = _shorten_net_names(
        sorted({lb["text"] for lb in net_labels}), sanitize_cfg,
    )
    shortened = sum(1 for k, v in short_map.items() if k != v)
    for lb in net_labels:
        lb["text"] = short_map.get(lb["text"], lb["text"])

    fmt_stats = _apply_label_format(net_labels, cfg.get("label_format") or {})

    return {
        "net_labels": net_labels,
        "power_ports": power_ports,
        "stub_wires": list(auto_wires),  # auto-named-net wires written
                                          # before label_placer adds its
                                          # pin-to-label stubs
        "junctions": [],
        "stats": {
            "nets_processed": len(net_data["nets"]),
            "signal_nets": signal_count,
            "power_nets": power_count,
            "labels_added": len(net_labels),
            "labels_shortened": shortened,
            "power_ports_added": len(power_ports),
            "auto_named_nets": sorted(set(auto_named_nets)),
            "auto_named_wires": len(auto_wires),
            "skipped_pins": skipped_pins,
            "label_format": fmt_stats,
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.router")
    ap.add_argument("placement", help="placement.json from Step 3")
    ap.add_argument("schematic", help="original .kicad_sch (pin defs + net membership)")
    ap.add_argument("--out", help="write routed JSON here (default: stdout)")
    ap.add_argument("--summary", action="store_true", help="print per-pin summary to stderr")
    args = ap.parse_args(argv)

    placement = json.loads(Path(args.placement).read_text(encoding="utf-8"))
    routed = route(placement, args.schematic)
    payload = json.dumps(routed, indent=2)

    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        s = routed["stats"]
        print(
            f"wrote {args.out}  "
            f"labels={s['labels_added']}  ports={s['power_ports_added']}  "
            f"sig_nets={s['signal_nets']}  pwr_nets={s['power_nets']}  "
            f"auto_named={len(s['auto_named_nets'])}  skipped={len(s['skipped_pins'])}"
        )
    else:
        sys.stdout.write(payload + "\n")

    if args.summary:
        for lb in routed["net_labels"][:20]:
            sys.stderr.write(
                f"  LABEL  {lb['text']:24s} @({lb['x_mm']:6.2f},{lb['y_mm']:6.2f}) "
                f"ang={lb['angle']:3d}  {lb['ref']}.{lb['pin_number']}\n"
            )
        for pp in routed["power_ports"][:20]:
            sys.stderr.write(
                f"  PORT   {pp['rail']:24s} @({pp['x_mm']:6.2f},{pp['y_mm']:6.2f}) "
                f"ang={pp['angle']:3d}  {pp['ref']}.{pp['pin_number']}\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
