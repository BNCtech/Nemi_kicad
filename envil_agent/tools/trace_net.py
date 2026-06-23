"""Tool: build the electrical connectivity graph of a .kicad_sch and
answer "what is on this net?" — the missing primitive for circuit-aware
ERC diagnosis.

Where `erc_autofix` pattern-matches a violation to a fix by COORDINATE,
`trace_net` actually traces the net: it unions wires, junctions, pin
tips and labels into electrical nets, reads each pin's ELECTRICAL TYPE
from the symbol library, and reports — per net — which pins sit on it,
which (if any) are DRIVERS (power_out / output), and whether the net is
floating or driver-less.

That is the data a senior engineer reconstructs by hand before touching
an ERC error: "net +3V3 has U1.VDD (power_in) but no power_out — is the
regulator output just unwired, or genuinely missing?" The deterministic
autofix can then add the missing WIRE instead of reflexively dropping a
PWR_FLAG.

Read-only. Never mutates the schematic. Universal — no per-circuit or
per-part hardcoding; driver classification comes straight from the
library symbol's pin etypes.

Connectivity model (single-sheet v1):
  - two wires sharing an endpoint  -> same net (shared coord key)
  - a junction / pin tip / label that lies ON a wire segment (endpoint
    OR mid-span) -> joined to that wire (handles T-taps, e.g. a
    decoupling cap tapping a rail mid-wire)
  - same-name labels / power ports  -> merged globally by name
    (global_label + power port semantics; local labels too, which is
    correct on a single sheet)
Limitation: cross-sheet hierarchical-label stitching is out of scope
for v1 — trace one .kicad_sch at a time.
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool


# Driver etypes. A net needs at least one of these or KiCad ERC flags it
# ("power_pin_not_driven" for power_in sinks, "input not driven" for
# input sinks). PWR_FLAG's pin is `power_out`, regular power-port symbols
# (power:+3V3, power:GND) are `power_in` — so PWR_FLAG counts as a driver
# and a bare rail port does not, exactly matching KiCad's ERC. No
# hardcoding: this falls out of the library pin types.
_DRIVER_ETYPES = {
    "power_out", "output", "bidirectional",
    "open_collector", "open_emitter", "tri_state",
}
_POWER_INPUT_ETYPES = {"power_in"}
_COORD_TOL = 0.01   # mm — KiCad pins/wires sit on a 1.27/2.54 grid


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("trace_net", {}) or {}
    except Exception:
        return {}


def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _sval(node: Any) -> str:
    """Atom -> its real string. sexpdata wraps unquoted tokens in Symbol,
    whose str() escapes specials (a label `VPP/MCLR` becomes the bogus
    `VPP{slash}MCLR`). .value() gives the true text — use this for any
    name/value we compare or merge on."""
    if isinstance(node, sexpdata.Symbol):
        return node.value()
    return str(node)


def _key(x: float, y: float) -> str:
    """Quantise a coord to a stable union-find node key. round(2) lands
    grid-aligned pins/wires on the same key; trig float noise from
    place_pin (e.g. 100.32999998) collapses to 100.33."""
    return f"{round(x, 2):.2f},{round(y, 2):.2f}"


class _UF:
    """Tiny union-find over string coord-keys. find() auto-creates."""
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, k: str) -> str:
        p = self.parent.setdefault(k, k)
        while p != self.parent[p]:
            self.parent[p] = self.parent[self.parent[p]]
            p = self.parent[p]
        return p

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _pin_tag(p: Dict[str, Any]) -> str:
    """A unique, referenceable handle for a pin: REF.NAME, falling back
    to REF.NUMBER when the symbol left the pin unnamed ("" or "~")."""
    pname = p["name"] if p["name"] not in ("", "~") else p["number"]
    return f"{p['ref']}.{pname}"


def _on_segment(px: float, py: float,
                ax: float, ay: float, bx: float, by: float,
                tol: float = _COORD_TOL) -> bool:
    """True if (px,py) lies on segment A-B (within tol). Covers the
    endpoints too. Used to join a junction / pin / label that taps a
    wire mid-span to that wire's net."""
    # Distance from point to the infinite line, then bounds check.
    dx, dy = bx - ax, by - ay
    seg_len2 = dx * dx + dy * dy
    if seg_len2 < tol * tol:
        # Degenerate (zero-length) wire — treat as a point.
        return abs(px - ax) <= tol and abs(py - ay) <= tol
    t = ((px - ax) * dx + (py - ay) * dy) / seg_len2
    if t < -tol or t > 1 + tol:
        return False
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy) <= tol


# ----------------------------------------------------------------------
# Schematic scan: symbols (+ world-placed pins with etype), wires,
# junctions, labels.
# ----------------------------------------------------------------------
def _symbol_value(sym_node: list) -> str:
    for c in sym_node[1:]:
        if (isinstance(c, list) and _head(c) == "property"
                and len(c) >= 3 and _sval(c[1]) == "Value"):
            return _sval(c[2])
    return ""


def _symbol_ref(sym_node: list) -> str:
    for c in sym_node[1:]:
        if (isinstance(c, list) and _head(c) == "property"
                and len(c) >= 3 and _sval(c[1]) == "Reference"):
            return _sval(c[2])
    return ""


def _scan(root: list) -> Dict[str, Any]:
    """Return {pins, wires, junctions, labels, power_ports}.

    pins:        [{ref, name, number, etype, x, y}]
    wires:       [(x1, y1, x2, y2)]
    junctions:   [(x, y)]
    labels:      [(name, x, y, kind)]   kind in local/global/hier
    power_ports: [(net_name, x, y)]     named power ports (NOT PWR_FLAG)
    """
    from ..kicad.symbol_geom import load_symbol, place_pin

    pins: List[Dict[str, Any]] = []
    wires: List[Tuple[float, float, float, float]] = []
    junctions: List[Tuple[float, float]] = []
    labels: List[Tuple[str, float, float, str]] = []
    power_ports: List[Tuple[str, float, float]] = []

    for child in root[1:]:
        if not isinstance(child, list):
            continue
        h = _head(child)

        if h == "symbol":
            ref = _symbol_ref(child)
            lib_id = ""
            atx = aty = atrot = 0.0
            mirror: Optional[str] = None
            unit = 1
            for c in child[1:]:
                if not isinstance(c, list):
                    continue
                ch = _head(c)
                if ch == "lib_id" and len(c) >= 2:
                    lib_id = _sval(c[1])
                elif ch == "at":
                    try:
                        atx = float(c[1]); aty = float(c[2])
                        if len(c) >= 4:
                            atrot = float(c[3])
                    except (TypeError, ValueError):
                        pass
                elif ch == "mirror" and len(c) >= 2:
                    mirror = str(_head([c[1]]) or c[1])
                    mirror = mirror if mirror in ("x", "y") else None
                elif ch == "unit" and len(c) >= 2:
                    try:
                        unit = int(c[1])
                    except (TypeError, ValueError):
                        unit = 1
            if not lib_id:
                continue
            is_pwr = lib_id.startswith("power:")
            is_flag = lib_id.lower().endswith("pwr_flag")
            try:
                g = load_symbol(lib_id)
            except Exception:
                continue
            for pin in g.pins:
                # Multi-unit: a pin tagged unit 0 is shared; otherwise it
                # only renders on the matching instance unit.
                if pin.unit not in (0, unit):
                    continue
                px, py, _r = place_pin(pin, atx, aty, atrot, mirror=mirror)
                pins.append({
                    "ref": ref or lib_id,
                    "name": pin.name,
                    "number": pin.number,
                    "etype": pin.etype,
                    "x": px, "y": py,
                })
                # A named power port (power:+3V3, power:GND) anchors a net
                # name at its pin tip. PWR_FLAG names nothing — it only
                # contributes a power_out driver via the pin above.
                if is_pwr and not is_flag:
                    net_name = _symbol_value(child) or pin.name
                    power_ports.append((net_name, px, py))

        elif h == "wire":
            pts = next((c for c in child[1:]
                        if isinstance(c, list) and _head(c) == "pts"), None)
            if not pts:
                continue
            try:
                ax, ay = float(pts[1][1]), float(pts[1][2])
                bx, by = float(pts[2][1]), float(pts[2][2])
            except (IndexError, TypeError, ValueError):
                continue
            wires.append((ax, ay, bx, by))

        elif h == "junction":
            at = next((c for c in child[1:]
                       if isinstance(c, list) and _head(c) == "at"), None)
            if at:
                try:
                    junctions.append((float(at[1]), float(at[2])))
                except (TypeError, ValueError):
                    pass

        elif h in ("label", "global_label", "hierarchical_label"):
            name = _sval(child[1]) if len(child) >= 2 else ""
            at = next((c for c in child[1:]
                       if isinstance(c, list) and _head(c) == "at"), None)
            if name and at:
                try:
                    kind = ({"label": "local",
                             "global_label": "global",
                             "hierarchical_label": "hier"}[h])
                    labels.append((name, float(at[1]), float(at[2]), kind))
                except (TypeError, ValueError):
                    pass

    return {
        "pins": pins, "wires": wires, "junctions": junctions,
        "labels": labels, "power_ports": power_ports,
    }


def _build_nets(scan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Union wires/pins/labels into nets; assign names; classify drivers."""
    uf = _UF()
    wires = scan["wires"]

    # 1. Union each wire's two endpoints.
    for (ax, ay, bx, by) in wires:
        uf.union(_key(ax, ay), _key(bx, by))

    # 2. Join every "tap point" (pin tip, junction, label anchor) to any
    #    wire it lies on — endpoint OR mid-span (T-tap). Mid-span taps are
    #    how a decoupling cap or a power port attaches to a rail wire.
    tap_points: List[Tuple[float, float]] = []
    tap_points += [(p["x"], p["y"]) for p in scan["pins"]]
    tap_points += list(scan["junctions"])
    tap_points += [(x, y) for (_n, x, y, _k) in scan["labels"]]
    tap_points += [(x, y) for (_n, x, y) in scan["power_ports"]]
    for (px, py) in tap_points:
        tk = _key(px, py)
        uf.find(tk)  # ensure singleton even if it touches no wire
        for (ax, ay, bx, by) in wires:
            if _on_segment(px, py, ax, ay, bx, by):
                uf.union(tk, _key(ax, ay))

    # 3. Collect names per root, then merge roots that share a name
    #    (global/local labels + named power ports connect by name).
    names_at_root: Dict[str, set] = defaultdict(set)
    name_to_roots: Dict[str, set] = defaultdict(set)
    for (name, x, y, _kind) in scan["labels"]:
        r = uf.find(_key(x, y))
        names_at_root[r].add(name)
        name_to_roots[name].add(r)
    for (name, x, y) in scan["power_ports"]:
        r = uf.find(_key(x, y))
        names_at_root[r].add(name)
        name_to_roots[name].add(r)
    for name, roots in name_to_roots.items():
        roots = list(roots)
        for other in roots[1:]:
            uf.union(roots[0], other)

    # 4. Bucket pins by final root.
    pins_by_root: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in scan["pins"]:
        pins_by_root[uf.find(_key(p["x"], p["y"]))].append(p)

    # Re-resolve names after the name-merge unions collapsed roots.
    final_names: Dict[str, set] = defaultdict(set)
    for r0, ns in names_at_root.items():
        final_names[uf.find(r0)] |= ns

    nets: List[Dict[str, Any]] = []
    all_roots = set(pins_by_root) | set(final_names)
    for r in all_roots:
        rpins = pins_by_root.get(r, [])
        names = sorted(final_names.get(r, set()))
        net_name = names[0] if names else ""
        pin_strs, drivers, power_ins = [], [], []
        driver_pins: List[Dict[str, Any]] = []
        for p in rpins:
            tag = _pin_tag(p)
            et = p["etype"]
            pin_strs.append(f"{tag} ({et})")
            if et in _DRIVER_ETYPES:
                drivers.append(tag)
                # Keep the driver's coord + etype so a consumer (e.g.
                # erc_autofix) can draw a wire to it instead of guessing.
                driver_pins.append({"tag": tag, "etype": et,
                                    "x": p["x"], "y": p["y"]})
            if et in _POWER_INPUT_ETYPES:
                power_ins.append(tag)
        has_driver = bool(drivers)
        # A net with a single connection point is a dangling pin / stub.
        floating = len(rpins) <= 1 and not names
        nets.append({
            "name": net_name,
            "aliases": names,
            "pin_count": len(rpins),
            "pins": sorted(pin_strs),
            "drivers": sorted(set(drivers)),
            "driver_pins": driver_pins,
            "power_inputs": sorted(set(power_ins)),
            "has_driver": has_driver,
            "undriven_power": bool(power_ins) and not has_driver,
            "floating": floating,
        })
    # Stable, useful ordering: problems first, then by name.
    nets.sort(key=lambda n: (
        not n["undriven_power"], not n["floating"],
        n["name"] or "~", -n["pin_count"]))
    return nets


def _match_net(nets: List[Dict[str, Any]], scan: Dict[str, Any],
               net: str, pin: str, x: Optional[float], y: Optional[float]
               ) -> List[Dict[str, Any]]:
    """Filter nets by the requested selector (net name / REF.PIN / coord)."""
    if net:
        nl = net.strip().lower()
        return [n for n in nets
                if nl in (a.lower() for a in n["aliases"])
                or n["name"].lower() == nl]
    if pin:
        want = pin.strip().lower().replace(" ", "")
        return [n for n in nets
                if any(ps.split(" ")[0].lower() == want for ps in n["pins"])]
    if x is not None and y is not None:
        # Find which net owns the pin/label nearest the ERC coord.
        target = None
        best = 1e9
        for p in scan["pins"]:
            d = math.hypot(p["x"] - x, p["y"] - y)
            if d < best:
                best, target = d, _pin_tag(p).lower()
        if target is None:
            return []
        return [n for n in nets
                if any(ps.split(" ")[0].lower() == target for ps in n["pins"])]
    return nets


def trace(sch_path: Path, net: str = "", pin: str = "",
          x: Optional[float] = None, y: Optional[float] = None
          ) -> Dict[str, Any]:
    """Programmatic entry point (callable from erc_autofix). Returns the
    full structured result without the chat-formatting wrapper."""
    text = sch_path.read_text(encoding="utf-8")
    root = sexpdata.loads(text)
    if not isinstance(root, list) or _head(root) != "kicad_sch":
        return {"ok": False, "error": "not a kicad_sch"}
    scan = _scan(root)
    nets = _build_nets(scan)
    matched = _match_net(nets, scan, net, pin, x, y)
    return {
        "ok": True,
        "net_count": len(nets),
        "matched": matched,
        "all_nets": nets,
        "undriven_power_nets": [n for n in nets if n["undriven_power"]],
        "floating_pins": [n for n in nets if n["floating"]],
    }


@tool(
    name="trace_net",
    description=(
        "Trace the electrical connectivity of a .kicad_sch: builds the "
        "net graph (wires + junctions + pin tips + labels) and reports, "
        "per net, which pins sit on it, which are DRIVERS (power_out / "
        "output) and whether the net is floating or driver-less. This is "
        "the circuit-aware primitive for ERC root-cause analysis — call "
        "it BEFORE proposing an ERC fix to find the actual cause (missing "
        "wire vs missing driver) instead of reflexively adding a "
        "PWR_FLAG.\n"
        "Triggers: 'what's on net +3V3', 'is this rail driven', 'trace "
        "the net for U1.VDD', 'why is this pin not driven', 'net trace "
        "pannu' (Tanglish).\n"
        "Args (path required; selector optional):\n"
        '  {"path": "...kicad_sch"}                  # summary of all nets\n'
        '  {"path": "...", "net": "+3V3"}            # one net by name\n'
        '  {"path": "...", "pin": "U1.VDD"}          # net carrying a pin\n'
        '  {"path": "...", "x": 100.3, "y": 88.9}    # net at an ERC coord\n'
        '  {"path": "...", "only_problems": true}    # undriven/floating only\n'
        "Read-only — never edits the schematic."
    ),
    input_schema={"path": str},
)
async def trace_net(args: dict[str, Any]) -> dict[str, Any]:
    sch = Path(str(args.get("path", "")).strip()).expanduser()
    if not sch.exists():
        return {"content": [{"type": "text",
                              "text": f"ERROR: not found: {sch}"}],
                "is_error": True}
    if sch.suffix.lower() != ".kicad_sch":
        return {"content": [{"type": "text",
                              "text": "ERROR: expected a .kicad_sch file"}],
                "is_error": True}

    net = str(args.get("net", "")).strip()
    pin = str(args.get("pin", "")).strip()
    only_problems = bool(args.get("only_problems", False))
    x = y = None
    try:
        if args.get("x") is not None:
            x = float(args.get("x"))
        if args.get("y") is not None:
            y = float(args.get("y"))
    except (TypeError, ValueError):
        x = y = None

    try:
        res = trace(sch, net=net, pin=pin, x=x, y=y)
    except Exception as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: {type(exc).__name__}: {exc}"}],
                "is_error": True}
    if not res.get("ok"):
        return {"content": [{"type": "text",
                              "text": f"ERROR: {res.get('error')}"}],
                "is_error": True}

    has_selector = bool(net or pin or (x is not None and y is not None))
    if has_selector:
        show = res["matched"]
        header = f"trace_net: {sch.name} — {len(show)} net(s) match selector"
    elif only_problems:
        show = res["undriven_power_nets"] + [
            n for n in res["floating_pins"]
            if not n["undriven_power"]]
        header = (f"trace_net: {sch.name} — {len(show)} problem net(s) "
                  f"of {res['net_count']} total")
    else:
        show = res["all_nets"]
        header = f"trace_net: {sch.name} — {res['net_count']} net(s)"

    lines = [header, ""]
    if not show:
        lines.append("  (no matching net)")
    for n in show[:40]:
        nm = n["name"] or "(unnamed)"
        flags = []
        if n["undriven_power"]:
            flags.append("UNDRIVEN POWER")
        elif not n["has_driver"] and n["pin_count"] > 1:
            flags.append("no driver")
        if n["floating"]:
            flags.append("floating/dangling")
        flag_str = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  • {nm}  ({n['pin_count']} pin)" + flag_str)
        if n["drivers"]:
            lines.append(f"      drivers: {', '.join(n['drivers'])}")
        elif n["power_inputs"]:
            lines.append(f"      power_in (needs driver): "
                         f"{', '.join(n['power_inputs'])}")
        for ps in n["pins"][:8]:
            lines.append(f"        - {ps}")
        if len(n["pins"]) > 8:
            lines.append(f"        - ... (+{len(n['pins']) - 8} more pins)")
    if len(show) > 40:
        lines.append(f"  ... (+{len(show) - 40} more nets)")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": True,
        "net_count": res["net_count"],
        "matched": res["matched"] if has_selector else None,
        "undriven_power_nets": res["undriven_power_nets"],
        "floating_pins": res["floating_pins"],
        "nets": res["all_nets"],
    }
