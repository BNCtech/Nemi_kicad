from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata


def _to_str(token) -> str:
    if isinstance(token, sexpdata.Symbol):
        return token.value()
    return str(token)


def _walk(node, tag: str):
    """Yield every child sub-list whose head symbol == tag (depth 1 only)."""
    if not isinstance(node, list):
        return
    for child in node[1:]:
        if isinstance(child, list) and child and _to_str(child[0]) == tag:
            yield child


def _find_xy(node: list, tag: str) -> Optional[Tuple[float, float]]:
    """Find a (tag x y) child and return (x, y) as floats."""
    for sub in node[1:]:
        if (isinstance(sub, list) and len(sub) >= 3
                and _to_str(sub[0]) == tag):
            try:
                return (float(sub[1]), float(sub[2]))
            except (ValueError, TypeError):
                return None
    return None


def _find_scalar(node: list, tag: str) -> Optional[float]:
    """Find a (tag value) child and return value as float."""
    for sub in node[1:]:
        if (isinstance(sub, list) and len(sub) >= 2
                and _to_str(sub[0]) == tag):
            try:
                return float(sub[1])
            except (ValueError, TypeError):
                return None
    return None


def _property_is_hidden(prop_node: list) -> bool:
    """True iff a (property ...) node carries (hide yes) directly OR inside its
    (effects ...) block. KiCad has used both shapes across versions; treat them
    equivalently. Returns False when no hide flag is set (default = visible)."""
    for sub in prop_node[1:]:
        if not isinstance(sub, list) or not sub:
            continue
        head = _to_str(sub[0])
        if head == "hide" and len(sub) > 1 and _to_str(sub[1]).lower() == "yes":
            return True
        if head == "effects":
            for eff in sub[1:]:
                if isinstance(eff, list) and eff and _to_str(eff[0]) == "hide":
                    if len(eff) > 1 and _to_str(eff[1]).lower() == "yes":
                        return True
                if isinstance(eff, sexpdata.Symbol) and eff.value() == "hide":
                    return True  # bare (hide) atom — older KiCad
    return False


class SchematicExtractor:
    def __init__(self, path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with open(self.path, "r", encoding="utf-8") as f:
            self.tree = sexpdata.loads(f.read())

    def components(self) -> List[Dict[str, Any]]:
        out = []
        for sym in _walk(self.tree, "symbol"):
            entry: Dict[str, Any] = {"properties": {}, "property_hidden": {},
                                     "in_bom": True, "dnp": False, "unit": 1}
            for child in sym[1:]:
                if not isinstance(child, list):
                    continue
                key = _to_str(child[0])
                if key == "lib_id" and len(child) > 1:
                    entry["lib_id"] = _to_str(child[1])
                elif key == "at" and len(child) >= 3:
                    rot = float(child[3]) if len(child) > 3 else 0.0
                    entry["at"] = (float(child[1]), float(child[2]), rot)
                elif key == "unit" and len(child) > 1:
                    try:
                        entry["unit"] = int(_to_str(child[1]))
                    except ValueError:
                        pass
                elif key == "in_bom" and len(child) > 1:
                    entry["in_bom"] = _to_str(child[1]).lower() != "no"
                elif key == "dnp" and len(child) > 1:
                    entry["dnp"] = _to_str(child[1]).lower() == "yes"
                elif key == "property" and len(child) >= 3:
                    pname = _to_str(child[1])
                    pval = _to_str(child[2])
                    entry["properties"][pname] = pval
                    entry["property_hidden"][pname] = _property_is_hidden(child)
                    if pname == "Reference":
                        entry["reference"] = pval
                    elif pname == "Value":
                        entry["value"] = pval
                    elif pname == "Footprint":
                        entry["footprint"] = pval
            if "reference" in entry:
                out.append(entry)
        return out

    def labels(self) -> List[Dict[str, Any]]:
        out = []
        for tag in ("label", "global_label", "hierarchical_label"):
            for node in _walk(self.tree, tag):
                entry: Dict[str, Any] = {"kind": tag}
                if len(node) > 1 and not isinstance(node[1], list):
                    entry["name"] = _to_str(node[1])
                for child in node[1:]:
                    if (
                        isinstance(child, list)
                        and _to_str(child[0]) == "at"
                        and len(child) >= 3
                    ):
                        rot = float(child[3]) if len(child) > 3 else 0.0
                        entry["at"] = (float(child[1]), float(child[2]), rot)
                out.append(entry)
        return out

    def wires(self) -> List[List[Tuple[float, float]]]:
        out = []
        for w in _walk(self.tree, "wire"):
            for child in w[1:]:
                if isinstance(child, list) and _to_str(child[0]) == "pts":
                    pts = []
                    for pt in child[1:]:
                        if (
                            isinstance(pt, list)
                            and _to_str(pt[0]) == "xy"
                            and len(pt) >= 3
                        ):
                            pts.append((float(pt[1]), float(pt[2])))
                    if pts:
                        out.append(pts)
        return out

    def lib_symbol_bodies(self) -> Dict[str, Tuple[float, float, float, float]]:
        """Per-lib_id body bbox in symbol-local coords (min_x, min_y, max_x, max_y).

        Body = the union of all `(rectangle ...)`, `(polyline ...)`, `(circle ...)`,
        `(arc ...)` primitives in the lib_symbol — EXCLUDING pin shapes. KLC S3.5
        requires pins to lie outside the body, so a wire endpoint on the body
        boundary is a pin tip (legal); only wires passing THROUGH the body
        interior are violations.

        Returns {} if no lib_symbols block is present.
        """
        out: Dict[str, Tuple[float, float, float, float]] = {}
        for lib_root in _walk(self.tree, "lib_symbols"):
            for sym in lib_root[1:]:
                if not (isinstance(sym, list) and _to_str(sym[0]) == "symbol"):
                    continue
                if len(sym) < 2:
                    continue
                lib_id = _to_str(sym[1])
                bbox = self._collect_body_bbox(sym)
                if bbox:
                    out[lib_id] = bbox
        return out

    def _collect_body_bbox(self, sym_node: list) -> Optional[Tuple[float, float, float, float]]:
        """Walk a (symbol ...) node (lib def) and return the body bbox.

        KiCad nests sub-units as inner `(symbol "name_X_Y" ...)` blocks within
        the parent lib symbol. We recurse into those but skip every `(pin ...)`
        encountered, since pins live outside the body per KLC S3.5.
        """
        xs: List[float] = []
        ys: List[float] = []

        def visit(node):
            if not isinstance(node, list) or not node:
                return
            head = _to_str(node[0]) if isinstance(node[0], (sexpdata.Symbol, str)) else None
            if head == "pin":
                return  # KLC S3.5: pin endpoints are outside the body
            if head == "rectangle":
                start = _find_xy(node, "start")
                end = _find_xy(node, "end")
                if start and end:
                    xs.extend([start[0], end[0]])
                    ys.extend([start[1], end[1]])
            elif head == "polyline":
                for sub in node[1:]:
                    if isinstance(sub, list) and _to_str(sub[0]) == "pts":
                        for pt in sub[1:]:
                            if (isinstance(pt, list) and _to_str(pt[0]) == "xy"
                                    and len(pt) >= 3):
                                xs.append(float(pt[1]))
                                ys.append(float(pt[2]))
            elif head == "circle":
                center = _find_xy(node, "center")
                r = _find_scalar(node, "radius")
                if center and r is not None:
                    xs.extend([center[0] - r, center[0] + r])
                    ys.extend([center[1] - r, center[1] + r])
            elif head == "arc":
                # use start/mid/end as a coarse bbox proxy
                for tag in ("start", "mid", "end"):
                    p = _find_xy(node, tag)
                    if p:
                        xs.append(p[0])
                        ys.append(p[1])
            elif head == "symbol":
                # nested sub-unit — recurse into its children
                for sub in node[1:]:
                    visit(sub)
            else:
                for sub in node[1:]:
                    visit(sub)

        visit(sym_node)
        if not xs or not ys:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def junctions(self) -> List[Tuple[float, float]]:
        """Explicit junction dots placed by the user. Three+ wires meeting
        without a junction = NOT electrically connected per KLC CON_003."""
        out: List[Tuple[float, float]] = []
        for j in _walk(self.tree, "junction"):
            for child in j[1:]:
                if isinstance(child, list) and _to_str(child[0]) == "at" and len(child) >= 3:
                    out.append((float(child[1]), float(child[2])))
                    break
        return out

    def lib_symbol_pins(self) -> Dict[str, Dict[int, List[Dict[str, Any]]]]:
        """Per-lib_id, per-unit list of pin definitions in symbol-local coords.

        Returns {lib_id: {unit_no: [pin_dict, ...]}}.

        KiCad lib_symbol sub-units are named `<base>_<unit>_<bodystyle>`
        (e.g. `74LS125_0_0`, `74LS125_1_0`, ...). Unit 0 holds common pins
        (power/ground); units 1..N hold per-channel pins.

        Each pin: {electrical_type, name, number, x, y, rot, length, unit}.
        Pin TIP (wire-attach point) = (x, y) + length * direction(rot).
        """
        out: Dict[str, Dict[int, List[Dict[str, Any]]]] = {}
        for lib_root in _walk(self.tree, "lib_symbols"):
            for sym in lib_root[1:]:
                if not (isinstance(sym, list) and _to_str(sym[0]) == "symbol"):
                    continue
                if len(sym) < 2:
                    continue
                lib_id = _to_str(sym[1])
                by_unit: Dict[int, List[Dict[str, Any]]] = {}
                # The top-level symbol may have its own pins (rare); treat as unit 0.
                self._collect_pins(sym, 0, by_unit, walk_subunits=True)
                if any(by_unit.values()):
                    out[lib_id] = by_unit
        return out

    def _collect_pins(
        self,
        sym_node: list,
        current_unit: int,
        out: Dict[int, List[Dict[str, Any]]],
        walk_subunits: bool,
    ) -> None:
        """Collect pins from a lib_symbol or one of its sub-units."""
        for child in sym_node[1:]:
            if not isinstance(child, list):
                continue
            head = _to_str(child[0])
            if head == "symbol":
                if not walk_subunits:
                    continue
                # sub-unit name like "74LS125_0_0" — second-to-last token is unit no.
                sub_name = _to_str(child[1]) if len(child) > 1 else ""
                parts = sub_name.rsplit("_", 2)
                sub_unit = current_unit
                if len(parts) == 3:
                    try:
                        sub_unit = int(parts[1])
                    except ValueError:
                        pass
                self._collect_pins(child, sub_unit, out, walk_subunits=False)
                continue
            if head != "pin":
                continue
            etype = _to_str(child[1]) if len(child) > 1 else ""
            x = y = 0.0
            rot = 0.0
            length = 2.54
            name = ""
            number = ""
            for sub in child[1:]:
                if not isinstance(sub, list):
                    continue
                tag = _to_str(sub[0])
                if tag == "at" and len(sub) >= 3:
                    x = float(sub[1]); y = float(sub[2])
                    if len(sub) > 3:
                        rot = float(sub[3])
                elif tag == "length" and len(sub) > 1:
                    length = float(sub[1])
                elif tag == "name" and len(sub) > 1:
                    name = _to_str(sub[1])
                elif tag == "number" and len(sub) > 1:
                    number = _to_str(sub[1])
            out.setdefault(current_unit, []).append({
                "electrical_type": etype,
                "name": name,
                "number": number,
                "x": x, "y": y, "rot": rot, "length": length,
                "unit": current_unit,
            })

    def power_rails(self) -> List[str]:
        return sorted(
            {
                c.get("value", "")
                for c in self.components()
                if c.get("lib_id", "").startswith("power:") and c.get("value")
            }
        )

    def summary(self) -> Dict[str, Any]:
        comps = self.components()
        return {
            "path": str(self.path),
            "component_count": len(comps),
            "components": comps,
            "labels": self.labels(),
            "wire_count": len(self.wires()),
            "power_rails": self.power_rails(),
        }

    def format_for_claude(self) -> str:
        s = self.summary()
        lines = [
            f"FILE: {s['path']}",
            f"COMPONENT COUNT: {s['component_count']}",
            f"POWER RAILS: {', '.join(s['power_rails']) or '(none detected)'}",
            f"WIRE SEGMENTS: {s['wire_count']}",
            f"LABEL COUNT: {len(s['labels'])}",
            "",
            "COMPONENTS:",
        ]
        for c in s["components"]:
            lines.append(
                f"  {c.get('reference','?'):<6} value={c.get('value','?'):<14}"
                f" lib_id={c.get('lib_id','?')}"
                f" footprint={c.get('footprint','-')}"
                f" at={c.get('at','-')}"
            )
        if s["labels"]:
            lines.append("")
            lines.append("LABELS:")
            for lb in s["labels"]:
                lines.append(
                    f"  [{lb['kind']}] {lb.get('name','?')} @ {lb.get('at','-')}"
                )

        # Wire coordinates — without this, the AI knows wires exist but has no
        # idea where, so delete_wire / move_wire_endpoint ops invariably miss
        # their target. Show every wire with its endpoint pair so the AI can
        # quote them verbatim.
        wires = self.wires()
        if wires:
            lines.append("")
            lines.append("WIRES (each row = one (wire ...) sexp; use these exact endpoints in delete_wire / move_wire_endpoint):")
            for i, w in enumerate(wires):
                if len(w) < 2:
                    continue
                a = w[0]
                b = w[-1]
                mid = ""
                if len(w) > 2:
                    mid = "  via " + " ".join(f"({p[0]:.2f},{p[1]:.2f})" for p in w[1:-1])
                lines.append(
                    f"  wire#{i}: ({a[0]:.2f},{a[1]:.2f}) -> ({b[0]:.2f},{b[1]:.2f}){mid}"
                )

        # Junctions — same reason. Without this the AI can't avoid duplicate
        # add_junction ops and can't target delete_junction at existing dots.
        try:
            junctions = self.junctions()
        except Exception:
            junctions = []
        if junctions:
            lines.append("")
            lines.append("JUNCTIONS:")
            for j in junctions:
                lines.append(f"  @ ({j[0]:.2f},{j[1]:.2f})")
        return "\n".join(lines)


def format_dump_with_context(schematic_path, max_defects: int = 80) -> str:
    """Bare schematic dump + PIN ENDPOINTS + DETECTED DEFECTS.

    Single source of truth used by both server.ChatRoom and chat.ChatSession.
    Without the two appended sections the AI cannot emit wire ops that LAND on
    real pin endpoints, and has no signal that the previous turn produced
    layout/electrical defects — so it can't self-correct. Lazy imports of
    `nets` and `basic_checks` to avoid an import cycle with this module.
    """
    try:
        base = SchematicExtractor(schematic_path).format_for_claude()
    except Exception as e:
        return f"(unable to read schematic: {e})"
    parts: List[str] = [base]

    try:
        from . import nets as _nets
        ext = SchematicExtractor(schematic_path)
        lib_pins = ext.lib_symbol_pins()
        pin_lines: List[str] = []
        for c in ext.components():
            lib_id = c.get("lib_id", "")
            ref = c.get("reference", "")
            if not ref or (lib_id or "").startswith("power:"):
                continue
            by_unit = lib_pins.get(lib_id) or {}
            if not by_unit:
                continue
            inst_unit = int(c.get("unit", 1))
            pin_defs = list(by_unit.get(0, [])) + list(by_unit.get(inst_unit, []))
            if not pin_defs:
                continue
            for ep in _nets.placed_pin_endpoints(c, pin_defs):
                name = ep.get("name", "") or "~"
                num = ep.get("pin_number", "?")
                etype = ep.get("electrical_type", "")
                pin_lines.append(
                    f"  {ref}.{num}({name:>8s}) [{etype:>10s}] @ ({ep['x']:.2f}, {ep['y']:.2f})"
                )
        if pin_lines:
            parts.append("")
            parts.append("=== PIN ENDPOINTS (wire to these exact coordinates) ===")
            parts.extend(pin_lines)
    except Exception:
        pass

    try:
        from . import basic_checks
        report = basic_checks.run_all(schematic_path)
        # Sort by severity so the cap-at-`max_defects` keeps the most important
        # ones. Without this, a check that runs LATE in run_all (e.g.
        # FUNC_EXCESS_PWR_FLAG, FUNC_DECOUPLING_FAR) falls off the bottom of
        # the dump on busy schematics and the AI never sees it.
        sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        issues = sorted(
            (i for i in report.get("issues", [])
             if i.get("severity") in ("critical", "high", "medium")),
            key=lambda i: sev_order.get(i.get("severity", "low"), 9),
        )
        if issues:
            parts.append("")
            parts.append("=== DETECTED DEFECTS (fix these unless the user says otherwise) ===")
            for i in issues[:max_defects]:
                parts.append(
                    f"  [{i.get('severity','?')}/{i.get('check','?')}] "
                    f"{i.get('refs','')}: {i.get('message','')}"
                )
            if len(issues) > max_defects:
                parts.append(f"  ... +{len(issues)-max_defects} more")
    except Exception:
        pass

    return "\n".join(parts)
