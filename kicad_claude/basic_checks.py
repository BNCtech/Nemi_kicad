"""Level-1 deterministic schematic checks.

Pure-Python: runs locally, never calls Claude. Catches the structural defects
(missing/duplicate/unannotated refdes, blank values, malformed net names,
orphan labels, off-grid endpoints) so the LLM only sees schematics that
already pass syntax and gets to spend tokens on real design questions.

All universal CAD/EE conventions live in conventions.json -power-symbol
filter, refdes letter codes + pattern, missing-value sentinels, grid step.
Check-specific severities and label rules live in basic_checks_config.json.
Nothing is hardcoded in this file; add a new IC family or a new placeholder
string by editing JSON.
"""

import re
from collections import defaultdict
from typing import Any, Dict, List, Tuple

from . import hierarchy, nets as _nets
from ._config_loader import load as _load_config
from .schematic_extractor import SchematicExtractor  # kept for type / direct callers


def _bc_cfg() -> Dict[str, Any]:
    return _load_config("basic_checks_config")


def _conv_cfg() -> Dict[str, Any]:
    return _load_config("conventions")


def _is_ignored(lib_id: str, reference: str) -> bool:
    """Power-port symbols and PWR_FLAGs are not real components -universal filter
    shared with the BOM module via conventions.json."""
    cfg = _conv_cfg()["power_symbol"]
    if any((lib_id or "").startswith(p) for p in cfg["lib_id_prefixes"]):
        return True
    if any((reference or "").startswith(p) for p in cfg["reference_prefixes"]):
        return True
    return False


def _unescape_kicad(s: str) -> str:
    """Convert KiCad's {token} escapes back to their real characters.

    Without this, the label 'VPP/MCLR' (serialized as 'VPP{slash}MCLR') would
    fail format checks AND get counted as a separate net from any 'VPP/MCLR'
    instance written elsewhere — producing two false orphan reports for one net.
    Escape table lives in conventions.json:kicad_string_escapes.
    """
    table = _conv_cfg().get("kicad_string_escapes", {})
    out = s or ""
    for token, ch in table.items():
        out = out.replace(token, ch)
    return out


def _issue(severity: str, refs, message: str, check_id: str) -> Dict[str, str]:
    if isinstance(refs, list):
        refs = ", ".join(refs)
    return {"check": check_id, "severity": severity, "refs": refs, "message": message}


def check_references(components: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """References must exist, be unique, follow Letters+Digits format, and be annotated.

    Multi-unit awareness: a single physical part (e.g. 74HC125 = 4 gates) appears
    as multiple symbol instances sharing one refdes. Those share lib_id + Value, so
    they are NOT a duplicate. A refdes is only flagged when the instances disagree
    on (lib_id, value) -i.e. two genuinely different parts share one designator.
    """
    bc = _bc_cfg()["reference"]
    conv = _conv_cfg()["refdes"]
    valid_re = re.compile(conv["pattern"], re.IGNORECASE)
    unannot_re = re.compile(bc["unannotated_pattern"])
    issues: List[Dict[str, str]] = []

    by_ref: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in components:
        ref = (c.get("reference") or "").strip()
        lib_id = c.get("lib_id", "")
        if _is_ignored(lib_id, ref):
            continue

        if not ref:
            label = f"<{lib_id or 'symbol'} @ {c.get('at','?')}>"
            issues.append(_issue(bc["missing_severity"], label,
                                 "missing reference designator", "REF_MISSING"))
            continue

        if unannot_re.match(ref):
            issues.append(_issue(bc["unannotated_severity"], ref,
                                 "unannotated symbol (refdes ends in '?')", "REF_UNANNOTATED"))
            continue

        if not valid_re.match(ref):
            issues.append(_issue(bc["invalid_format_severity"], ref,
                                 f"refdes does not match pattern {conv['pattern']!r}",
                                 "REF_BAD_FORMAT"))

        by_ref[ref].append(c)

    for ref, instances in sorted(by_ref.items()):
        distinct_parts = {(c.get("lib_id", ""), (c.get("value") or "")) for c in instances}
        if len(distinct_parts) > 1:
            details = " | ".join(f"{lib}={val!r}" for lib, val in sorted(distinct_parts))
            issues.append(_issue(bc["duplicate_severity"], ref,
                                 f"duplicate refdes -different parts share this designator: {details}",
                                 "REF_DUPLICATE"))
    return issues


def check_values(components: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Every non-ignored component must declare a Value (no blanks, no placeholders)."""
    bc = _bc_cfg()["value"]
    sentinels = {s.lower() for s in _conv_cfg()["missing_value_sentinels"]["values"]}
    issues: List[Dict[str, str]] = []

    seen_refs: set = set()
    for c in components:
        ref = c.get("reference") or ""
        lib_id = c.get("lib_id", "")
        if _is_ignored(lib_id, ref):
            continue
        # One issue per physical part, not per multi-unit instance.
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        value = (c.get("value") or "").strip()
        if value.lower() in sentinels:
            issues.append(_issue(bc["missing_severity"], ref or "<unnamed>",
                                 "missing or placeholder Value", "VAL_MISSING"))
    return issues


def check_label_format(labels: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Net labels must match the configured naming pattern."""
    bc = _bc_cfg()["label"]
    pat = re.compile(bc["valid_pattern"])
    issues: List[Dict[str, str]] = []

    for lb in labels:
        raw = (lb.get("name") or "").strip()
        if not raw:
            issues.append(_issue(bc["invalid_format_severity"],
                                 f"<{lb.get('kind','label')} @ {lb.get('at','?')}>",
                                 "label has no name", "LBL_BLANK"))
            continue
        name = _unescape_kicad(raw)
        if not pat.match(name):
            issues.append(_issue(bc["invalid_format_severity"], name,
                                 f"label name does not match pattern {bc['valid_pattern']!r}",
                                 "LBL_BAD_FORMAT"))
    return issues


def check_orphan_labels(labels: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """A label that appears exactly once is dangling -no driver/receiver pair.

    Hierarchical labels can legitimately appear once per child sheet, so by
    default only flat labels (label, global_label) are checked. Override via
    config.label.orphan_kinds_to_check.
    """
    bc = _bc_cfg()["label"]
    kinds = set(bc["orphan_kinds_to_check"])
    if not kinds:
        return []
    allowed = {n.lower() for n in bc["always_allowed_singletons"]}

    counts: Dict[str, int] = defaultdict(int)
    for lb in labels:
        if lb.get("kind") not in kinds:
            continue
        raw = (lb.get("name") or "").strip()
        if not raw:
            continue
        counts[_unescape_kicad(raw)] += 1

    issues: List[Dict[str, str]] = []
    for name, n in sorted(counts.items()):
        if n == 1 and name.lower() not in allowed:
            issues.append(_issue(bc["orphan_severity"], name,
                                 "orphan label (appears exactly once -net has no second endpoint)",
                                 "LBL_ORPHAN"))
    return issues


def _on_grid(coord: float, step: float, tol: float) -> bool:
    """True iff coord is within tol of a multiple of step."""
    nearest = round(coord / step) * step
    return abs(coord - nearest) <= tol


def check_grid(
    components: List[Dict[str, Any]],
    labels: List[Dict[str, Any]],
    wires: List[List],
) -> List[Dict[str, str]]:
    """Component anchors, label anchors, and wire endpoints must lie on the
    schematic grid. Off-grid items connect silently to nothing."""
    bc = _bc_cfg()["grid"]
    grid = _conv_cfg()["grid"]
    step = float(grid["schematic_mm"])
    tol = float(grid["tolerance_mm"])
    sev = bc["off_grid_severity"]
    issues: List[Dict[str, str]] = []

    if bc.get("check_components", True):
        for c in components:
            ref = c.get("reference") or ""
            if _is_ignored(c.get("lib_id", ""), ref):
                continue
            at = c.get("at")
            if not at:
                continue
            x, y = float(at[0]), float(at[1])
            if not (_on_grid(x, step, tol) and _on_grid(y, step, tol)):
                issues.append(_issue(sev, ref or "<unnamed>",
                                     f"component anchor off grid: ({x:.4f}, {y:.4f}) -not on {step} mm",
                                     "GRID_COMPONENT"))

    if bc.get("check_labels", True):
        for lb in labels:
            at = lb.get("at")
            if not at:
                continue
            x, y = float(at[0]), float(at[1])
            if not (_on_grid(x, step, tol) and _on_grid(y, step, tol)):
                name = lb.get("name") or f"<{lb.get('kind','label')}>"
                issues.append(_issue(sev, name,
                                     f"label anchor off grid: ({x:.4f}, {y:.4f})",
                                     "GRID_LABEL"))

    if bc.get("check_wire_endpoints", True):
        for i, w in enumerate(wires):
            for x, y in w:
                if not (_on_grid(x, step, tol) and _on_grid(y, step, tol)):
                    issues.append(_issue(sev, f"wire#{i}",
                                         f"wire endpoint off grid: ({x:.4f}, {y:.4f})",
                                         "GRID_WIRE"))
                    break  # one issue per wire is enough
    return issues


def _segment_axis(p1, p2, tol: float):
    """Classify a wire segment. Returns ('h', y, x_lo, x_hi) for horizontal,
    ('v', x, y_lo, y_hi) for vertical, or None for point/diagonal."""
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    dx, dy = abs(x2 - x1), abs(y2 - y1)
    if dx <= tol and dy <= tol:
        return None  # zero-length, ignore
    if dy <= tol:
        return ("h", (y1 + y2) / 2.0, min(x1, x2), max(x1, x2))
    if dx <= tol:
        return ("v", (x1 + x2) / 2.0, min(y1, y2), max(y1, y2))
    return None  # diagonal — handled separately by check_wire_orthogonality


def check_wire_orthogonality(wires: List[List]) -> List[Dict[str, str]]:
    """Flag any wire segment that is neither horizontal nor vertical."""
    cfg = _bc_cfg()["geometry"]["wire_orthogonality"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    tol = float(_bc_cfg()["geometry"]["wire_overlap"]["tolerance_mm"])
    issues: List[Dict[str, str]] = []
    for i, w in enumerate(wires):
        for j in range(len(w) - 1):
            x1, y1 = float(w[j][0]), float(w[j][1])
            x2, y2 = float(w[j + 1][0]), float(w[j + 1][1])
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            if dx > tol and dy > tol:
                issues.append(_issue(sev, f"wire#{i}.seg{j}",
                                     f"diagonal wire segment ({x1:.2f},{y1:.2f}) -> ({x2:.2f},{y2:.2f})",
                                     "GEOM_DIAGONAL"))
    return issues


def check_wire_overlap(
    wires_with_sheet: List[Tuple[str, List[Tuple[float, float]]]],
) -> List[Dict[str, str]]:
    """Flag two wire segments lying on the same line and sharing more than a point.

    Sheet-aware: bucket-key includes the sheet hierarchy path so two wires from
    DIFFERENT sheet instances are never compared. In a complex hierarchy where
    the same child .kicad_sch is reused twice, every wire in it would otherwise
    appear as a 100% overlap with itself — false positive.
    """
    cfg = _bc_cfg()["geometry"]["wire_overlap"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    tol = float(cfg["tolerance_mm"])
    min_overlap = float(cfg["overlap_min_mm"])

    buckets: Dict[Any, List] = defaultdict(list)
    for i, (sheet, w) in enumerate(wires_with_sheet):
        for j in range(len(w) - 1):
            seg = _segment_axis(w[j], w[j + 1], tol)
            if seg is None:
                continue
            axis, perp, lo, hi = seg
            key = (sheet, axis, round(perp / max(tol, 1e-9)))
            buckets[key].append((lo, hi, i, j))

    issues: List[Dict[str, str]] = []
    for key, segs in buckets.items():
        if len(segs) < 2:
            continue
        segs.sort()
        for a in range(len(segs)):
            lo_a, hi_a, wi_a, sj_a = segs[a]
            for b in range(a + 1, len(segs)):
                lo_b, hi_b, wi_b, sj_b = segs[b]
                if lo_b >= hi_a - tol:
                    break
                overlap = min(hi_a, hi_b) - max(lo_a, lo_b)
                if overlap >= min_overlap:
                    issues.append(_issue(sev,
                                         f"wire#{wi_a}.seg{sj_a} & wire#{wi_b}.seg{sj_b}",
                                         f"collinear overlap of {overlap:.2f} mm on sheet {key[0]} "
                                         f"(>= {min_overlap} mm threshold)",
                                         "GEOM_WIRE_OVERLAP"))
    return issues


def check_label_collision(labels: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flag two labels at the same coordinate with different net names."""
    cfg = _bc_cfg()["geometry"]["label_collision"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    tol = float(cfg["coord_tolerance_mm"])
    flag_redundant = bool(cfg.get("flag_redundant", False))

    # Sheet-aware bucket key: same coordinate on two different sheets is not a
    # collision (those are separate drawing surfaces).
    by_coord: Dict[Any, List[str]] = defaultdict(list)
    for lb in labels:
        at = lb.get("at")
        if not at:
            continue
        x, y = float(at[0]), float(at[1])
        key = (lb.get("sheet", "/"),
               round(x / max(tol, 1e-9)),
               round(y / max(tol, 1e-9)))
        name = _unescape_kicad((lb.get("name") or "").strip())
        if name:
            by_coord[key].append(name)

    issues: List[Dict[str, str]] = []
    for key, names in by_coord.items():
        if len(names) < 2:
            continue
        sheet = key[0]
        unique = sorted(set(names))
        if len(unique) > 1:
            issues.append(_issue(sev, ", ".join(unique),
                                 f"{len(names)} labels collide at one point on sheet {sheet} with conflicting names",
                                 "GEOM_LABEL_CONFLICT"))
        elif flag_redundant:
            issues.append(_issue(sev, unique[0],
                                 f"{len(names)} redundant copies of '{unique[0]}' at one point on sheet {sheet}",
                                 "GEOM_LABEL_REDUNDANT"))
    return issues


def check_symbol_proximity(components: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flag two non-power components whose origin points are closer than min_separation_mm.

    Coarse proxy for full bbox-vs-bbox intersection (which needs lib_symbol
    parsing — planned). Catches the worst case: two parts placed at the same
    coordinate, or so close they certainly visually overlap.
    """
    cfg = _bc_cfg()["geometry"]["symbol_proximity"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    min_sep = float(cfg["min_separation_mm"])
    excl_power = bool(cfg.get("exclude_power", True))
    excl_dnp = bool(cfg.get("exclude_dnp", True))

    candidates: List[Dict[str, Any]] = []
    seen_per_sheet: set = set()
    for c in components:
        ref = c.get("reference") or ""
        if excl_power and _is_ignored(c.get("lib_id", ""), ref):
            continue
        if excl_dnp and c.get("dnp"):
            continue
        at = c.get("at")
        if not at:
            continue
        # Multi-unit instances share a refdes within one sheet — count once per (sheet, ref).
        key = (c.get("sheet", "/"), ref)
        if key in seen_per_sheet:
            continue
        seen_per_sheet.add(key)
        candidates.append(c)

    issues: List[Dict[str, str]] = []
    n = len(candidates)
    sep2 = min_sep * min_sep
    for i in range(n):
        ai = candidates[i]
        xi, yi = float(ai["at"][0]), float(ai["at"][1])
        si = ai.get("sheet", "/")
        for j in range(i + 1, n):
            aj = candidates[j]
            if aj.get("sheet", "/") != si:
                continue  # different sheets cannot visually overlap
            xj, yj = float(aj["at"][0]), float(aj["at"][1])
            d2 = (xi - xj) ** 2 + (yi - yj) ** 2
            if d2 < sep2:
                d = d2 ** 0.5
                issues.append(_issue(
                    sev,
                    f"{ai.get('reference','?')} & {aj.get('reference','?')}",
                    f"symbols only {d:.2f} mm apart on sheet {si} "
                    f"(< {min_sep} mm); visual overlap likely",
                    "GEOM_SYMBOL_PROXIMITY",
                ))
    return issues


# ---------------------------------------------------------------------------
# Body-aware geometry: needs lib_symbol bboxes from the schematic file.
# ---------------------------------------------------------------------------

def _rotate_local_bbox(bbox, rotation_deg: float):
    """Rotate a local-coord bbox by 0/90/180/270 deg around origin and return the new axis-aligned bbox."""
    x0, y0, x1, y1 = bbox
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    rot = (int(round(rotation_deg)) % 360) // 90
    out = []
    for x, y in corners:
        for _ in range(rot):
            x, y = -y, x
        out.append((x, y))
    xs = [p[0] for p in out]
    ys = [p[1] for p in out]
    return (min(xs), min(ys), max(xs), max(ys))


def _placed_body_bbox(component: Dict[str, Any], local_bbox):
    """World-space bbox for a placed component given its (at x y rot) and the
    lib_symbol body bbox in local coords. KiCad schematic Y axis is flipped
    relative to the symbol-editor Y axis, so we mirror Y after rotation."""
    if not local_bbox or not component.get("at"):
        return None
    at = component["at"]
    cx, cy = float(at[0]), float(at[1])
    # Read rotation from extracted data if present (extractor surfaces it as part of `at`?)
    rot = 0.0
    # at may be (x, y) or (x, y, rot) depending on extractor version
    if len(at) >= 3:
        try:
            rot = float(at[2])
        except (ValueError, TypeError):
            rot = 0.0
    rx0, ry0, rx1, ry1 = _rotate_local_bbox(local_bbox, rot)
    # KiCad schematic y-axis is inverted relative to symbol-local y-axis.
    return (cx + rx0, cy - ry1, cx + rx1, cy - ry0)


def _segment_crosses_bbox_interior(seg, bbox, margin: float) -> bool:
    """True if the axis-aligned segment passes THROUGH the bbox interior — both
    endpoints outside, segment crosses both boundaries.

    Wires that terminate inside the body (one endpoint inside the shrunken
    bbox) are legitimate interior-pin connections: diodes, transistors, and
    test points have pins on or inside the body per KLC exceptions to S3.5.
    Boundary touching alone is OK (pin tip per S3.5). Only true pass-through
    wires are real S3.5 violations.
    """
    x1, y1, x2, y2 = seg
    bx0, by0, bx1, by1 = bbox
    # Shrink bbox by margin so a wire ending exactly on the body edge isn't flagged.
    bx0 += margin; by0 += margin; bx1 -= margin; by1 -= margin
    if bx0 >= bx1 or by0 >= by1:
        return False
    # Either endpoint inside the (shrunken) interior = legitimate connection.
    if (bx0 < x1 < bx1 and by0 < y1 < by1) or (bx0 < x2 < bx1 and by0 < y2 < by1):
        return False
    if abs(y1 - y2) < 1e-9:
        # horizontal: must enter and exit BOTH x-boundaries
        y = y1
        if y <= by0 or y >= by1:
            return False
        lo, hi = min(x1, x2), max(x1, x2)
        return lo < bx0 and hi > bx1
    if abs(x1 - x2) < 1e-9:
        # vertical: must enter and exit BOTH y-boundaries
        x = x1
        if x <= bx0 or x >= bx1:
            return False
        lo, hi = min(y1, y2), max(y1, y2)
        return lo < by0 and hi > by1
    return False  # diagonal — handled by orthogonality check


def _bbox_intersection_area(a, b) -> float:
    """Axis-aligned bbox intersection area. 0 when they only touch or are disjoint."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    dx = min(ax1, bx1) - max(ax0, bx0)
    dy = min(ay1, by1) - max(ay0, by0)
    if dx <= 0 or dy <= 0:
        return 0.0
    return dx * dy


def check_symbol_bbox_overlap(schematic_path) -> List[Dict[str, str]]:
    """Real lib_symbol body-bbox intersection between placed components on the
    same sheet. Strictly more accurate than the origin-distance proxy
    (check_symbol_proximity), so this is the default while proximity is left
    available as a fallback for projects with custom libs.

    Symbols whose lib_id has no parseable body primitives fall back to a
    fallback_size_mm × fallback_size_mm square around the origin so they
    still get checked instead of silently dropping out.
    """
    cfg = _bc_cfg()["geometry"]["symbol_bbox_overlap"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    fallback = float(cfg["fallback_size_mm"])
    min_area = float(cfg["min_overlap_area_mm2"])
    excl_power = bool(cfg.get("exclude_power", True))
    excl_dnp = bool(cfg.get("exclude_dnp", True))
    half_fb = fallback / 2.0

    issues: List[Dict[str, str]] = []
    for hpath, path in hierarchy.iter_sheet_instances(schematic_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        bodies = extractor.lib_symbol_bodies()
        comps = extractor.components()

        # One bbox per (sheet, refdes) — multi-unit instances of the same part
        # share a refdes; pick the first.
        seen_ref: set = set()
        placed: List[Tuple[str, Tuple[float, float, float, float]]] = []
        for c in comps:
            ref = c.get("reference") or ""
            if not ref:
                continue
            if excl_power and _is_ignored(c.get("lib_id", ""), ref):
                continue
            if excl_dnp and c.get("dnp"):
                continue
            if ref in seen_ref:
                continue
            seen_ref.add(ref)
            at = c.get("at")
            if not at:
                continue
            local = bodies.get(c.get("lib_id", ""))
            if local:
                world = _placed_body_bbox(c, local)
                if not world:
                    continue
            else:
                # Fallback: square around origin so unparseable symbols still get checked.
                cx, cy = float(at[0]), float(at[1])
                world = (cx - half_fb, cy - half_fb, cx + half_fb, cy + half_fb)
            placed.append((ref, world))

        n = len(placed)
        for i in range(n):
            ref_a, bb_a = placed[i]
            for j in range(i + 1, n):
                ref_b, bb_b = placed[j]
                area = _bbox_intersection_area(bb_a, bb_b)
                if area > min_area:
                    issues.append(_issue(
                        sev,
                        f"{ref_a} & {ref_b}",
                        f"body bboxes overlap by {area:.2f} mm² on sheet {hpath} "
                        f"(A={bb_a[0]:.2f},{bb_a[1]:.2f}->{bb_a[2]:.2f},{bb_a[3]:.2f}; "
                        f"B={bb_b[0]:.2f},{bb_b[1]:.2f}->{bb_b[2]:.2f},{bb_b[3]:.2f})",
                        "GEOM_SYMBOL_OVERLAP",
                    ))
    return issues


# ---------------------------------------------------------------------------
# Net-aware checks: powered by the union-find walker in nets.py.
# ---------------------------------------------------------------------------

def check_net_dangling_label(schematic_path) -> List[Dict[str, str]]:
    """Real orphan-label check using the topology walker. A label is dangling
    when its net has fewer than min_endpoints electrical members (pins +
    power ports). Replaces the count-only check_orphan_labels (which is
    fundamentally noisy without net topology)."""
    cfg = _bc_cfg()["geometry"]["net_dangling_label"]
    if not cfg.get("enabled", True):
        return []
    kinds_to_check = set(cfg["kinds_to_check"])
    if not kinds_to_check:
        return []  # default-off until cross-sheet binding lands
    sev = cfg["severity"]
    min_endpoints = int(cfg["min_endpoints"])

    proj = _nets.build_project_nets(schematic_path)
    issues: List[Dict[str, str]] = []
    for hpath, sheet in proj["sheets"].items():
        for net in sheet["nets"]:
            label_members = [m for m in net["members"] if m.get("kind") in kinds_to_check]
            if not label_members:
                continue
            # Count electrical endpoints (pins + power ports) on this net.
            electrical = sum(1 for m in net["members"]
                             if m.get("kind") in ("pin", "power"))
            if electrical < min_endpoints:
                names = sorted({_unescape_kicad(m.get("name") or "") for m in label_members
                                if m.get("name")})
                if not names:
                    continue
                issues.append(_issue(sev, ", ".join(names),
                                     f"label net has only {electrical} endpoint(s) on sheet {hpath} "
                                     f"(need >= {min_endpoints})",
                                     "NET_DANGLING_LABEL"))
    return issues


def check_junction_missing(schematic_path) -> List[Dict[str, str]]:
    """Per KLC CON_003: three or more wire segments meeting at a point must
    show an explicit junction dot. Without it, KiCad treats them as visually
    crossing but NOT electrically connected — a silent net split.

    We bucket every wire-segment endpoint by snapped coordinate. Any bucket
    with >= 3 distinct (wire_idx, segment_idx) entries, where no junction
    exists at that point, is a violation."""
    cfg = _bc_cfg()["geometry"]["junction_missing"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    tol = float(cfg["coord_tolerance_mm"])

    issues: List[Dict[str, str]] = []
    for hpath, path in hierarchy.iter_sheet_instances(schematic_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        wires = extractor.wires()
        junctions = extractor.junctions()

        existing_junctions = {(round(jx / tol) * tol, round(jy / tol) * tol)
                              for jx, jy in junctions}

        # Each wire endpoint is a candidate junction site if 3+ distinct wires
        # share that exact coordinate.
        endpoint_wires: Dict[Tuple[float, float], set] = defaultdict(set)
        for wi, w in enumerate(wires):
            for x, y in w:
                key = (round(float(x) / tol) * tol, round(float(y) / tol) * tol)
                endpoint_wires[key].add(wi)

        for (kx, ky), wire_set in endpoint_wires.items():
            if len(wire_set) < 3:
                continue
            if (kx, ky) in existing_junctions:
                continue
            issues.append(_issue(sev, f"({kx:.2f}, {ky:.2f})",
                                 f"{len(wire_set)} wires meet on sheet {hpath} but no junction marker; "
                                 f"may be silently disconnected (KLC CON_003)",
                                 "JUNCTION_MISSING"))
    return issues


def check_wire_through_body(schematic_path) -> List[Dict[str, str]]:
    """Per KLC S3.5: wires must not need to cross a symbol body. Flag any wire
    segment whose interior intersects a placed component's body bbox.

    Computed per sheet: for each sheet instance, parse its lib_symbols once,
    look up each placed component's body bbox, transform to world coords,
    test every wire segment.
    """
    cfg = _bc_cfg()["geometry"]["wire_through_body"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    margin = float(cfg["interior_margin_mm"])
    min_dim = float(cfg.get("min_body_dim_mm", 0.0))
    excl_power = bool(cfg.get("exclude_power", True))

    issues: List[Dict[str, str]] = []
    for hpath, path in hierarchy.iter_sheet_instances(schematic_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        bodies = extractor.lib_symbol_bodies()
        comps = extractor.components()
        wires = extractor.wires()

        # Pre-compute world bbox per component
        placed: List[Tuple[str, Tuple[float, float, float, float]]] = []
        for c in comps:
            ref = c.get("reference") or ""
            if excl_power and _is_ignored(c.get("lib_id", ""), ref):
                continue
            local = bodies.get(c.get("lib_id", ""))
            if not local:
                continue
            # Skip tiny markers (test points, mounting holes, fiducials) where
            # the "body" is just a small symbol around a single pin — wires
            # ending at that pin would falsely look like they enter the body.
            if (local[2] - local[0]) < min_dim or (local[3] - local[1]) < min_dim:
                continue
            world = _placed_body_bbox(c, local)
            if world:
                placed.append((ref, world))

        for wi, w in enumerate(wires):
            for j in range(len(w) - 1):
                seg = (float(w[j][0]), float(w[j][1]),
                       float(w[j + 1][0]), float(w[j + 1][1]))
                for ref, bbox in placed:
                    if _segment_crosses_bbox_interior(seg, bbox, margin):
                        issues.append(_issue(sev, ref,
                                             f"wire segment ({seg[0]:.2f},{seg[1]:.2f}) -> ({seg[2]:.2f},{seg[3]:.2f}) "
                                             f"crosses body of {ref} on sheet {hpath}",
                                             "GEOM_WIRE_THRU_BODY"))
    return issues


def _label_text_bbox(label: Dict[str, Any], height: float, char_w_ratio: float, lift: float):
    """Estimate the text bbox of a label given the KLC-derived font params.

    KiCad places the anchor on the wire; the visible text sits above (rot=0)
    or to the side depending on rotation. For overlap detection we treat the
    text as a halo above the anchor of size (n_chars * char_w * height) wide
    and `height` tall, lifted by `lift` mm so the bound wire (which passes
    THROUGH the anchor) is excluded from the bbox.
    """
    name = _unescape_kicad((label.get("name") or "").strip())
    if not name or not label.get("at"):
        return None
    n = len(name)
    w = n * char_w_ratio * height
    h = height
    x, y = float(label["at"][0]), float(label["at"][1])
    rot = 0.0
    if len(label["at"]) >= 3:
        try:
            rot = float(label["at"][2])
        except (ValueError, TypeError):
            rot = 0.0
    rot_q = (int(round(rot)) % 360) // 90
    # KiCad: 0=right, 90=up, 180=left, 270=down (schematic y-down)
    if rot_q == 0:        # text extends to the right; lifted above
        return (x, y - lift - h, x + w, y - lift)
    if rot_q == 1:        # text extends upward; lifted to the right
        return (x + lift, y - w, x + lift + h, y)
    if rot_q == 2:        # text extends to the left; lifted below
        return (x - w, y + lift, x, y + lift + h)
    return (x - lift - h, y, x - lift, y + w)  # rot_q == 3


def _segment_passes_through_point(seg, px: float, py: float, tol: float = 0.05) -> bool:
    """True iff the axis-aligned segment includes the point (px, py).

    A label's BOUND wire passes through the label's anchor — that wire is
    the binding mechanism and must be excluded from label-over-wire checks."""
    x1, y1, x2, y2 = seg
    if abs(y1 - y2) < 1e-9:  # horizontal
        if abs(py - y1) > tol:
            return False
        return min(x1, x2) - tol <= px <= max(x1, x2) + tol
    if abs(x1 - x2) < 1e-9:  # vertical
        if abs(px - x1) > tol:
            return False
        return min(y1, y2) - tol <= py <= max(y1, y2) + tol
    return False


def _segment_intersects_bbox(seg, bbox, min_overlap: float) -> bool:
    """True iff an axis-aligned segment overlaps the bbox interior by more than
    min_overlap. Edge-touching counts as 0 overlap."""
    x1, y1, x2, y2 = seg
    bx0, by0, bx1, by1 = bbox
    if abs(y1 - y2) < 1e-9:
        y = y1
        if y <= by0 or y >= by1:
            return False
        lo, hi = min(x1, x2), max(x1, x2)
        ov = min(hi, bx1) - max(lo, bx0)
        return ov > min_overlap
    if abs(x1 - x2) < 1e-9:
        x = x1
        if x <= bx0 or x >= bx1:
            return False
        lo, hi = min(y1, y2), max(y1, y2)
        ov = min(hi, by1) - max(lo, by0)
        return ov > min_overlap
    return False


def check_label_over_wire(schematic_path) -> List[Dict[str, str]]:
    """Per KLC S3.2 + KiCad eeschema label_offset_ratio: a label's text must
    not visually overlap any wire it is NOT bound to. Bound wire (the one
    passing through the anchor) is always allowed. Per-sheet only."""
    cfg = _bc_cfg()["geometry"]["label_over_wire"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    height = float(cfg["text_height_mm"])
    char_w = float(cfg["char_width_ratio"])
    lift = float(cfg["lift_mm"])
    min_overlap = float(cfg["min_overlap_mm"])

    issues: List[Dict[str, str]] = []
    for hpath, path in hierarchy.iter_sheet_instances(schematic_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        labels = extractor.labels()
        wires = extractor.wires()

        for lb in labels:
            tbox = _label_text_bbox(lb, height, char_w, lift)
            if not tbox or not lb.get("at"):
                continue
            anchor_x = float(lb["at"][0])
            anchor_y = float(lb["at"][1])
            name = _unescape_kicad(lb.get("name", "")) or "<label>"
            for wi, w in enumerate(wires):
                for j in range(len(w) - 1):
                    seg = (float(w[j][0]), float(w[j][1]),
                           float(w[j + 1][0]), float(w[j + 1][1]))
                    # Skip the label's BOUND wire — the wire that runs through
                    # the anchor is the connection, not a visual collision.
                    if _segment_passes_through_point(seg, anchor_x, anchor_y):
                        continue
                    if _segment_intersects_bbox(seg, tbox, min_overlap):
                        issues.append(_issue(sev, name,
                                             f"label '{name}' text bbox overlaps unrelated wire on sheet {hpath} "
                                             f"(seg ({seg[0]:.2f},{seg[1]:.2f}) -> ({seg[2]:.2f},{seg[3]:.2f}))",
                                             "GEOM_LABEL_OVER_WIRE"))
                        break  # one issue per label is enough
                else:
                    continue
                break
    return issues


# ---------------------------------------------------------------------------
# L3 functional checks — net-aware electrical correctness
# ---------------------------------------------------------------------------

def _refdes_category(ref: str) -> str:
    prefix = re.match(r"^([A-Za-z]+)", ref or "")
    if not prefix:
        return "other"
    p = prefix.group(1).upper()
    table = _conv_cfg()["refdes"]["prefixes"]
    return (table.get(p) or {}).get("category", "other")


def check_missing_decoupling(schematic_path) -> List[Dict[str, str]]:
    """KLC POWER_001: every IC power-input pin must share a net with at least
    one decoupling capacitor. Walks the net topology built by nets.py and
    flags any power_in pin whose net contains no capacitor.
    """
    cfg = _bc_cfg()["functional"]["missing_decoupling"]
    if not cfg.get("enabled", True):
        return []
    sev = cfg["severity"]
    ic_categories = set(cfg["ic_categories"])
    cap_prefixes = {p.upper() for p in cfg["cap_prefixes"]}
    pin_types = set(cfg["pin_types"])
    skip_pin_names = {n.lower() for n in cfg["skip_pin_names"]}
    min_pins_per_ic = int(cfg["min_pins"])

    issues: List[Dict[str, str]] = []
    proj = _nets.build_project_nets(schematic_path)
    for hpath, sheet in proj["sheets"].items():
        # Pin counts per refdes — used to filter out single-pin "ICs"
        # (test points / connectors that happen to use the U prefix).
        pin_counts: Dict[str, int] = defaultdict(int)
        for net in sheet["nets"]:
            for m in net["members"]:
                if m.get("kind") == "pin":
                    pin_counts[m.get("ref", "")] += 1

        for net in sheet["nets"]:
            ic_power_pins: List[Tuple[str, str, str]] = []
            has_cap = False
            for m in net["members"]:
                if m.get("kind") != "pin":
                    continue
                ref = m.get("ref", "")
                pname = (m.get("pin_name") or "").lower()
                # Capacitor on the net?
                p = re.match(r"^([A-Za-z]+)", ref or "")
                if p and p.group(1).upper() in cap_prefixes:
                    has_cap = True
                    continue
                # IC power-input pin?
                cat = _refdes_category(ref)
                if cat not in ic_categories:
                    continue
                if m.get("electrical_type") not in pin_types:
                    continue
                if any(skip in pname for skip in skip_pin_names):
                    continue
                if pin_counts.get(ref, 0) < min_pins_per_ic:
                    continue
                ic_power_pins.append((ref, m.get("pin_number", "?"), m.get("pin_name", "")))

            if ic_power_pins and not has_cap:
                by_ref: Dict[str, List[str]] = defaultdict(list)
                for ref, pn, pname in ic_power_pins:
                    label = f"pin {pn}" + (f" ({pname})" if pname and pname != "~" else "")
                    by_ref[ref].append(label)
                for ref, pin_descs in sorted(by_ref.items()):
                    issues.append(_issue(sev, ref,
                                         f"power-input {', '.join(pin_descs)} on net '{net['name']}' "
                                         f"on sheet {hpath} has no decoupling capacitor",
                                         "FUNC_NO_DECOUPLING"))
    return issues


def run_all(schematic_path) -> Dict[str, Any]:
    """Run every L1 check across the WHOLE project (root sheet + all child sheets)."""
    components = hierarchy.aggregate_components(schematic_path)
    labels = hierarchy.aggregate_labels(schematic_path)
    wires = hierarchy.aggregate_wires(schematic_path)
    sheet_count = hierarchy.sheet_count(schematic_path)

    issues: List[Dict[str, str]] = []
    issues.extend(check_references(components))
    issues.extend(check_values(components))
    issues.extend(check_label_format(labels))
    issues.extend(check_orphan_labels(labels))
    issues.extend(check_grid(components, labels, wires))
    issues.extend(check_wire_orthogonality(wires))
    # Wire-overlap is sheet-aware: a complex hierarchy reusing the same child
    # .kicad_sch must not flag every wire as overlapping with itself. Use the
    # sheet-tagged variant from hierarchy.aggregate_wires_with_sheet.
    issues.extend(check_wire_overlap(hierarchy.aggregate_wires_with_sheet(schematic_path)))
    issues.extend(check_label_collision(labels))
    issues.extend(check_symbol_proximity(components))
    issues.extend(check_symbol_bbox_overlap(schematic_path))
    issues.extend(check_wire_through_body(schematic_path))
    issues.extend(check_label_over_wire(schematic_path))
    issues.extend(check_net_dangling_label(schematic_path))
    issues.extend(check_junction_missing(schematic_path))
    issues.extend(check_missing_decoupling(schematic_path))

    by_sev: Dict[str, int] = defaultdict(int)
    for i in issues:
        by_sev[i["severity"]] += 1

    status = "PASS" if by_sev["critical"] == 0 else "FAIL"

    physical_parts = len({
        c.get("reference") for c in components
        if not _is_ignored(c.get("lib_id", ""), c.get("reference", "")) and c.get("reference")
    })

    return {
        "path": str(schematic_path),
        "status": status,
        "summary": {
            "sheets": sheet_count,
            "physical_parts": physical_parts,
            "labels": len(labels),
            "wires": len(wires),
            "critical": by_sev["critical"],
            "high": by_sev["high"],
            "medium": by_sev["medium"],
            "low": by_sev["low"],
        },
        "issues": issues,
    }


def to_text(report: Dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"L1 BASIC CHECKS - {report['status']}",
        f"  sheets: {s.get('sheets', 1)}, parts: {s['physical_parts']}, "
        f"labels: {s['labels']}, wires: {s['wires']}",
        f"  critical: {s['critical']}, high: {s['high']}, medium: {s['medium']}, low: {s['low']}",
    ]
    if report["issues"]:
        lines.append("")
        lines.append("ISSUES:")
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        for it in sorted(report["issues"], key=lambda i: order.get(i["severity"], 9)):
            lines.append(f"  [{it['severity']:8s}] {it['check']:14s} {it['refs']}: {it['message']}")
    return "\n".join(lines)
