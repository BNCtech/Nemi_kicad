"""_verify_reroute_off_bodies.py — proof for the R2 wire-off-body reroute.

Covers the user's "wire over component" report: a wire drawn THROUGH a
component body is bent into an L/Z around it, with BOTH endpoints fixed so
the net list is byte-identical.

Hermetic — no symbol library needed. The router primitives are tested on
hand-built geometry, and the end-to-end file mutation uses an INJECTED
context (the `ctx=` param) so `build_context`/`load_symbol` are never hit.

Run from ai_backend:
    PYTHONPATH=. python tests/_verify_reroute_off_bodies.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envil_agent.lint import repair                       # noqa: E402
from envil_agent.lint.repair import (                     # noqa: E402
    reroute_wires_off_bodies,
    _route_around_box,
    _seg_pierces_box,
    _seg_overlaps_wire,
    _grid_beyond,
    _extract_wires,
)
from envil_agent.lint.selectors import (                  # noqa: E402
    find_wires_piercing_bodies,
)

_fails: list = []


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _seg(a, b):
    return (tuple(a), tuple(b))


def _wires_from_file(path: Path):
    """Re-parse the mutated file into the context wire form."""
    coords, _spans = _extract_wires(path.read_text(encoding="utf-8"))
    return [((c[0], c[1]), (c[2], c[3])) for c in coords]


# --------------------------------------------------------------------------
# 1. Router primitives (no file, no symbol lib)
# --------------------------------------------------------------------------
def test_router_primitives():
    print("[1] router primitives")
    blocker = (100.0, 100.0, 120.0, 110.0)
    # pin-aware router takes (ref,box) list + pins; endpoints below sit on no
    # body, so an empty pin list means every body is a foreign obstacle.
    boxes_ref = [("U1", 100.0, 100.0, 120.0, 110.0)]
    pins = []

    # 1a. _grid_beyond snaps strictly away from the edge, on-grid.
    check(round(_grid_beyond(98.73, -1), 2) == 97.79,
          "_grid_beyond(98.73,-1) snaps down to 97.79 (grid line above body)")
    check(round(_grid_beyond(111.27, +1), 2) == 111.76,
          "_grid_beyond(111.27,+1) snaps up to 111.76 (grid line below body)")

    # 1b. horizontal wire straight through the body -> clean over-detour.
    a, b = (90.0, 105.0), (130.0, 105.0)
    path = _route_around_box(a, b, blocker, boxes_ref, pins, [], 1.27, 0.5, 0.05)
    check(path is not None, "horizontal pierce -> a detour is found")
    check(path[0] == a and path[-1] == b, "detour preserves both endpoints exactly")
    pierces = any(_seg_pierces_box(path[i], path[i + 1], blocker, 0.5)
                  for i in range(len(path) - 1))
    check(not pierces, "no detour segment pierces the body")
    mid_ys = [p[1] for p in path[1:-1]]
    check(all(y < 100.0 for y in mid_ys),
          "clean case routes OVER the body (first side tried)")

    # 1c. an existing wire on the over-detour line forces the OTHER side.
    over_wire = _seg((80.0, 97.79), (140.0, 97.79))     # collinear w/ over-detour
    path2 = _route_around_box(a, b, blocker, boxes_ref, pins, [over_wire],
                              1.27, 0.5, 0.05)
    check(path2 is not None, "overlap on one side -> still finds a detour")
    check(all(p[1] > 110.0 for p in path2[1:-1]),
          "detour flips UNDER to avoid stacking on the existing wire (no new short)")
    new_short = any(_seg_overlaps_wire(path2[i], path2[i + 1], over_wire, 0.05)
                    for i in range(len(path2) - 1))
    check(not new_short, "chosen detour does NOT overlap the existing wire")

    # 1d. offset wire (differs on both axes) -> an L clears it.
    a3, b3 = (90.0, 100.0), (130.0, 110.0)
    path3 = _route_around_box(a3, b3, blocker, boxes_ref, pins, [], 1.27, 0.5, 0.05)
    check(path3 is not None and len(path3) == 3,
          "both-axis-offset wire routes as a 2-segment L")


# --------------------------------------------------------------------------
# 2. End-to-end file mutation with an injected context
# --------------------------------------------------------------------------
_SCH_TMPL = """(kicad_sch
{wires}
)
"""

_WIRE = ('\t(wire (pts (xy {ax} {ay}) (xy {bx} {by}))\n'
         '\t\t(stroke (width 0) (type default))\n'
         '\t\t(uuid "{uid}")\n'
         '\t)')


def _write_sch(tmp: Path, wire_specs) -> Path:
    body = "\n".join(_WIRE.format(ax=ax, ay=ay, bx=bx, by=by, uid=uid)
                     for (ax, ay, bx, by, uid) in wire_specs)
    p = tmp / "board.kicad_sch"
    p.write_text(_SCH_TMPL.format(wires=body), encoding="utf-8")
    return p


def test_end_to_end():
    print("[2] end-to-end file mutation (injected ctx)")
    # U1 body sits square in the path of a wire from R1 pin to R2 pin.
    bboxes = [("U1", 100.0, 100.0, 120.0, 110.0)]
    pins = [(90.0, 105.0, "R1"), (130.0, 105.0, "R2"),
            (105.0, 100.0, "U1"), (105.0, 110.0, "U1")]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _write_sch(tmp, [(90, 105, 130, 105, "orig-uuid-0001")])

        ctx = {"wires": [((90.0, 105.0), (130.0, 105.0))],
               "bboxes": bboxes, "pin_positions": pins}

        # Pre-condition: exactly one R2 pierce.
        pre = find_wires_piercing_bodies(ctx["wires"], bboxes, pins)
        check(len(pre) == 1, "pre: wire pierces U1 (R2 count == 1)")

        res = reroute_wires_off_bodies(path, ctx=ctx)
        check(res.get("wires_rerouted") == 1,
              f"reroute reports 1 wire fixed (got {res.get('wires_rerouted')})")

        new_wires = _wires_from_file(path)
        check(len(new_wires) == 3, f"straight wire became a 3-segment detour "
                                   f"(got {len(new_wires)} segments)")

        # Post-condition: no wire pierces U1 anymore.
        post = find_wires_piercing_bodies(new_wires, bboxes, pins)
        check(len(post) == 0, "post: no wire pierces U1 (R2 count == 0)")

        # Connectivity preserved: both original pin tips are still wire ends.
        endpoints = {pt for w in new_wires for pt in w}
        check((90.0, 105.0) in endpoints and (130.0, 105.0) in endpoints,
              "both original endpoints (R1 & R2 pin tips) preserved")
        # And the straight pin-to-pin bridge is gone.
        straight_present = any(
            {w[0], w[1]} == {(90.0, 105.0), (130.0, 105.0)} for w in new_wires)
        check(not straight_present, "the straight through-body wire is gone")

        # Idempotent: a second pass (ctx rebuilt from the fixed geometry) no-ops.
        bytes_after_first = path.read_bytes()
        ctx2 = {"wires": new_wires, "bboxes": bboxes, "pin_positions": pins}
        res2 = reroute_wires_off_bodies(path, ctx=ctx2)
        check(res2.get("wires_rerouted") == 0, "idempotent: 2nd pass fixes 0")
        check(path.read_bytes() == bytes_after_first,
              "idempotent: 2nd pass leaves the file byte-identical")


_LABEL = ('\t(label "{name}" (at {ax} {ay} 0)\n'
          '\t\t(effects (font (size 1.27 1.27)) (justify left bottom))\n'
          '\t\t(uuid "{uid}")\n'
          '\t)')


def test_clamp_overshoot():
    print("[2b] clamp a dead-end overshoot back onto the pin it crossed")
    # Wire from R1 pin overshoots PAST U1's left-edge pin, dead-ending inside.
    bboxes = [("U1", 100.0, 100.0, 120.0, 110.0)]
    pins = [(90.0, 105.0, "R1"), (100.0, 105.0, "U1"),
            (110.0, 100.0, "U1")]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        path = _write_sch(tmp, [(90, 105, 105, 105, "ov-uuid-1")])
        ctx = {"wires": [((90.0, 105.0), (105.0, 105.0))],
               "bboxes": bboxes, "pin_positions": pins,
               "labels": [], "junctions": []}
        pre = find_wires_piercing_bodies(ctx["wires"], bboxes, pins)
        check(len(pre) == 1, "pre: overshoot pierces U1 (R2 == 1)")
        res = reroute_wires_off_bodies(path, ctx=ctx)
        new_wires = _wires_from_file(path)
        post = find_wires_piercing_bodies(new_wires, bboxes, pins)
        check(len(post) == 0, "post: overshoot clamped, no pierce (R2 == 0)")
        ends = {pt for w in new_wires for pt in w}
        check((100.0, 105.0) in ends,
              "wire now lands exactly on U1's pin (100,105)")
        check((90.0, 105.0) in ends, "the live R1 endpoint is preserved")
        check(not any((142.0, 100.0) == p for p in ends), "no stray point")


def test_relocate_label():
    print("[2c] relocate a net label placed inside a body")
    bboxes = [("U1", 100.0, 100.0, 120.0, 110.0)]
    pins = [(90.0, 105.0, "R1"), (100.0, 100.0, "U1")]
    labels = [("SIG", 110.0, 105.0)]              # label buried inside U1
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        wires_txt = _WIRE.format(ax=90, ay=105, bx=110, by=105, uid="w-1")
        lbl_txt = _LABEL.format(name="SIG", ax=110, ay=105, uid="l-1")
        p = tmp / "board.kicad_sch"
        p.write_text(_SCH_TMPL.format(wires=wires_txt + "\n" + lbl_txt),
                     encoding="utf-8")
        ctx = {"wires": [((90.0, 105.0), (110.0, 105.0))],
               "bboxes": bboxes, "pin_positions": pins,
               "labels": labels, "junctions": []}
        pre = find_wires_piercing_bodies(ctx["wires"], bboxes, pins)
        check(len(pre) == 1, "pre: label-stub pierces U1 (R2 == 1)")
        res = reroute_wires_off_bodies(p, ctx=ctx)
        check(res.get("labels_moved") == 1,
              f"one label relocated (got {res.get('labels_moved')})")
        new_wires = _wires_from_file(p)
        post = find_wires_piercing_bodies(new_wires, bboxes, pins)
        check(len(post) == 0, "post: label moved out, no pierce (R2 == 0)")
        txt = p.read_text(encoding="utf-8")
        check('(label "SIG"' in txt and '(at 110 105' not in txt,
              "the SIG label anchor moved off its old in-body position")
        ends = {pt for w in new_wires for pt in w}
        check((90.0, 105.0) in ends, "the R1 pin endpoint is preserved")


# --------------------------------------------------------------------------
# 3. Byte-stable when nothing pierces
# --------------------------------------------------------------------------
def test_byte_stable_clean():
    print("[3] byte-stable on a clean schematic")
    bboxes = [("U1", 100.0, 100.0, 120.0, 110.0)]
    pins = [(80.0, 95.0, "R1"), (95.0, 95.0, "R2")]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Wire well clear of U1 (y=95, x 80..95) -> no pierce.
        path = _write_sch(tmp, [(80, 95, 95, 95, "clean-uuid-1")])
        before = path.read_bytes()
        ctx = {"wires": [((80.0, 95.0), (95.0, 95.0))],
               "bboxes": bboxes, "pin_positions": pins}
        res = reroute_wires_off_bodies(path, ctx=ctx)
        check(res.get("wires_rerouted") == 0, "clean board: 0 reroutes")
        check(path.read_bytes() == before, "clean board: file byte-identical")


if __name__ == "__main__":
    test_router_primitives()
    test_end_to_end()
    test_clamp_overshoot()
    test_relocate_label()
    test_byte_stable_clean()
    print()
    if _fails:
        print(f"RESULT: FAIL ({len(_fails)} check(s) failed)")
        for m in _fails:
            print("   - " + m)
        sys.exit(1)
    print("RESULT: PASS (all checks)")
    sys.exit(0)
