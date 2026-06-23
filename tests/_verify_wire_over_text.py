"""Dynamic regression matrix for the R11_TEXT wire-over-field-text fix.

Proves the fix is GEOMETRIC + config-driven, not specialised to any one
circuit/part. Generates synthetic .kicad_sch files covering many part types,
rotations, mirrors, wire orientations, wire lengths, multi-field crossings,
hidden fields, and power ports, then asserts for every case:

  * the detector flags a real wire-over-text crossing (incl. the long-wire /
    off-midpoint case that the old 3-point sampler MISSED),
  * the repair relocates ONLY the obscured field text,
  * after the repair NO wire crosses any field text,
  * a second run is a no-op (idempotent),
  * wires / pins / junctions are unchanged (connectivity byte-safe),
  * a clean schematic is byte-identical through repair_after_render.

Run:  python -m tests._verify_wire_over_text   (cwd = ai_backend)
Exit 0 = all pass.
"""
import sys
import tempfile
from pathlib import Path

from envil_agent.lint.context import build_context
from envil_agent.lint.selectors import find_wires_over_component_text
from envil_agent.lint.repair import clear_wires_over_text, repair_after_render


# ---- s-expr builders -----------------------------------------------------

def _field(name, text, at, hidden=False):
    at_s = (f"(at {at[0]} {at[1]} {at[2]})" if len(at) == 3
            else f"(at {at[0]} {at[1]})")
    hide = " (hide yes)" if hidden else ""
    return (f'    (property "{name}" "{text}" {at_s}\n'
            f'      (effects (font (size 1.27 1.27)){hide})\n'
            f'    )\n')


def device(lib_id, ref, value, at, rot, ref_at, val_at,
           mirror=None, val_hidden=False):
    mir = f"    (mirror {mirror})\n" if mirror else ""
    return (f'  (symbol\n    (lib_id "{lib_id}")\n'
            f'    (at {at[0]} {at[1]} {rot})\n{mir}    (unit 1)\n'
            + _field("Reference", ref, ref_at)
            + _field("Value", value, val_at, hidden=val_hidden)
            + "  )\n")


def pwr(ref, rail, at, val_at):
    return (f'  (symbol\n    (lib_id "power:{rail}")\n'
            f'    (at {at[0]} {at[1]} 0)\n    (unit 1)\n'
            f'    (property "Reference" "{ref}" (at {at[0]} {at[1]} 0)'
            f' (effects (font (size 1.27 1.27)) (hide yes)))\n'
            f'    (property "Value" "{rail}" (at {val_at[0]} {val_at[1]} 0)'
            f' (effects (font (size 1.27 1.27))))\n  )\n')


def wire(p1, p2, uid):
    return (f'  (wire (pts (xy {p1[0]} {p1[1]}) (xy {p2[0]} {p2[1]}))'
            f' (stroke (width 0) (type default)) (uuid "{uid}"))\n')


def sch(body):
    return "(kicad_sch\n  (version 20231120) (generator test)\n" + body + ")\n"


# ---- harness -------------------------------------------------------------

_TMP = Path(tempfile.mkdtemp())
_results = []


def _write(name, text):
    p = _TMP / f"{name}.kicad_sch"
    p.write_text(text, encoding="utf-8")
    return p


def _counts(ctx):
    return (len(ctx["wires"]), len(ctx["pins"]), len(ctx["junctions"]))


def check(name, text, expect_moved, expect_before_min=1):
    """Standard crossing case: expect detection, a move, and a clean after."""
    p = _write(name, text)
    try:
        ctx = build_context(p)
    except Exception as exc:
        _results.append((name, False, f"build_context raised {exc!r}"))
        return
    c0 = _counts(ctx)
    before = find_wires_over_component_text(ctx["wires"], ctx["text_bboxes"])
    res = clear_wires_over_text(p)
    ctx2 = build_context(p)
    after = find_wires_over_component_text(ctx2["wires"], ctx2["text_bboxes"])
    res2 = clear_wires_over_text(p)              # idempotency
    c1 = _counts(build_context(p))

    ok = True
    why = []
    if len(before) < expect_before_min:
        ok = False; why.append(f"before={len(before)} < {expect_before_min}")
    if res.get("fields_moved") != expect_moved:
        ok = False; why.append(f"moved={res.get('fields_moved')} != {expect_moved}")
    if len(after) != 0:
        ok = False; why.append(f"after={len(after)} != 0")
    if res2.get("fields_moved") != 0:
        ok = False; why.append(f"2nd-run moved={res2.get('fields_moved')} (not idempotent)")
    if c0 != c1:
        ok = False; why.append(f"connectivity changed {c0} -> {c1}")
    _results.append((name, ok, "; ".join(why) or "ok"))


def check_ignored(name, text):
    """Crossing exists geometrically but the field must be IGNORED
    (hidden field / power-port text): detector 0, no move, after 0."""
    p = _write(name, text)
    ctx = build_context(p)
    before = find_wires_over_component_text(ctx["wires"], ctx["text_bboxes"])
    res = clear_wires_over_text(p)
    ok = (len(before) == 0 and res.get("fields_moved") == 0)
    _results.append((name, ok,
                     "ok" if ok else f"before={len(before)} moved={res.get('fields_moved')}"))


def check_clean_bytestable(name, text):
    p = _write(name, text)
    orig = p.read_text(encoding="utf-8")
    summary = repair_after_render(p, ir=None)
    after = p.read_text(encoding="utf-8")
    r11 = [r for r in summary.get("ran", []) if r["rule"] == "R11_TEXT"]
    moved = r11[0]["result"]["fields_moved"] if r11 else None
    ok = (orig == after and moved == 0)
    _results.append((name, ok,
                     "ok" if ok else f"byte_identical={orig==after} moved={moved}"))


# ---- the matrix ----------------------------------------------------------

# A horizontal wire whose value text sits anywhere along it.  Span chosen so
# the MIDPOINT is far from the small value box (the old 3-point miss).
def horiz_case(name, lib, value, rot=0, mirror=None, val_at=(107, 100, 0)):
    body = device(lib, "X1", value, (100, 100), rot,
                  ref_at=(100, 92, 0), val_at=val_at, mirror=mirror)
    body += wire((55, val_at[1]), (val_at[0] + 5, val_at[1]), "w1")
    check(name, sch(body), expect_moved=1)


# 1-7: part types + rotations + mirror, all long horizontal wires.
horiz_case("R_rot0",   "Device:R", "10k")
horiz_case("R_rot90",  "Device:R", "10k", rot=90)
horiz_case("R_rot180", "Device:R", "10k", rot=180)
horiz_case("R_rot270", "Device:R", "10k", rot=270)
horiz_case("C_rot0",   "Device:C", "22p")
horiz_case("C_small",  "Device:C_Small", "100n")
horiz_case("C_mirrorx", "Device:C", "100n", mirror="x")
horiz_case("C_mirrory", "Device:C", "100n", mirror="y")

# 8: long / multi-char value string.
horiz_case("R_longval", "Device:R", "100k_0.1pct_0402")

# 9: single-char value (width-model floor).
horiz_case("R_shortval", "Device:R", "R", val_at=(107, 100, 0))

# 10: VERTICAL wire crossing the value text -> move must be along X.
def vert_case():
    vx, vy = 105, 100
    body = device("Device:C", "X1", "22p", (100, 100), 0,
                  ref_at=(100, 92, 0), val_at=(vx, vy, 0))
    body += wire((vx, 60), (vx, vy + 5), "w1")   # long vertical, midpoint far
    check("C_vertical_wire", sch(body), expect_moved=1)
vert_case()

# 11: Reference AND Value both crossed by one wire -> 2 fields moved.
def both_fields():
    body = device("Device:R", "X1", "10k", (100, 100), 0,
                  ref_at=(100, 100, 0), val_at=(112, 100, 0))
    body += wire((85, 100), (118, 100), "w1")
    check("both_ref_and_value", sch(body), expect_moved=2)
both_fields()

# 12: short wire whose MIDPOINT is inside the box (must still work).
def short_mid():
    body = device("Device:R", "X1", "10k", (100, 100), 0,
                  ref_at=(100, 92, 0), val_at=(107, 100, 0))
    body += wire((104, 100), (110, 100), "w1")
    check("short_midpoint_inside", sch(body), expect_moved=1)
short_mid()

# 13: Value (at) with NO rotation token -> _set_field_at must rewrite cleanly.
def no_rot_token():
    body = device("Device:R", "X1", "10k", (100, 100), 0,
                  ref_at=(100, 92, 0), val_at=(107, 100))   # 2-tuple = no rot
    body += wire((55, 100), (112, 100), "w1")
    check("value_at_no_rotation_token", sch(body), expect_moved=1)
no_rot_token()

# 14: HIDDEN value text -> not drawn, must be ignored.
def hidden_val():
    body = device("Device:R", "X1", "10k", (100, 100), 0,
                  ref_at=(100, 92, 0), val_at=(107, 100, 0), val_hidden=True)
    body += wire((55, 100), (112, 100), "w1")
    check_ignored("hidden_value_ignored", sch(body))
hidden_val()

# 15: power-port (#PWR) GND text crossed -> excluded (context skips it).
def power_port():
    body = pwr("#PWR01", "GND", (100, 100), (100, 105))
    body += wire((80, 105), (105, 105), "w1")    # crosses the GND value text
    check_ignored("power_port_text_excluded", sch(body))
power_port()

# 16: CLEAN file (wire well clear of all text) -> byte-stable, 0 moves.
def clean():
    body = device("Device:R", "X1", "10k", (100, 100), 0,
                  ref_at=(100, 92, 0), val_at=(107, 100, 0))
    body += wire((100, 103.81), (100, 130), "w1")   # straight down the pin, clear
    check_clean_bytestable("clean_bytestable", sch(body))
clean()


# ---- audit-driven edge cases (E1 / E2 / E6) ------------------------------

from envil_agent.lint.context import text_aabb, DEFAULT_TEXT_SIZE_MM


def _box_overlap(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


# E2: a 90-rotated Value field. Its TRUE box is tall/narrow, so a horizontal
# wire BELOW the un-rotated (wide/short) model still crosses the real glyphs.
# Without the rotation fix this is a MISS (0 detected); with it, caught+moved.
def e2_rotated_field():
    # text_aabb("100k", 155,100, 1.27, 90) -> y in [98.22,101.78]
    body = device("Device:R", "X1", "100k", (150, 100), 90,
                  ref_at=(150, 92, 0), val_at=(155, 100, 90))
    body += wire((150, 101.5), (160, 101.5), "w1")   # inside true tall box only
    check("E2_rotated_value_field", sch(body), expect_moved=1)
e2_rotated_field()


# E1: the moved field must NOT land on a net label sitting where it would
# otherwise nudge.
def e1_avoids_label():
    body = device("Device:R", "X1", "10k", (100, 100), 0,
                  ref_at=(100, 92, 0), val_at=(107, 100, 0))
    body += wire((55, 100), (112, 100), "w1")
    body += ('  (label "RESET" (at 107 98.73 0) (effects (font (size 1.27 1.27))'
             ' (justify left bottom)) (uuid "lab1"))\n')
    p = _write("E1_avoids_label", sch(body))
    res = clear_wires_over_text(p)
    ctx = build_context(p)
    vb = [t for t in ctx["text_bboxes"] if t[0] == "X1" and t[1] == "Value"]
    after = find_wires_over_component_text(ctx["wires"], ctx["text_bboxes"])
    ok = bool(vb) and res.get("fields_moved") == 1 and len(after) == 0
    why = []
    if not ok:
        why.append(f"moved={res.get('fields_moved')} after={len(after)}")
    if vb:
        vbox = vb[0][2:]
        for (nm, lx, ly) in ctx["labels"]:
            lbox = text_aabb(nm, lx, ly, DEFAULT_TEXT_SIZE_MM)
            if _box_overlap(vbox, lbox):
                ok = False
                why.append(f"moved Value overlaps label {nm}")
    _results.append(("E1_avoids_label", ok, "; ".join(why) or "ok"))
e1_avoids_label()


# E6: two stacked fields both crossed, with the up-escape blocked, must not be
# nudged onto the same spot.
def e6_sibling_no_pileup():
    body = device("Device:R", "X1", "10k", (100, 120), 0,
                  ref_at=(100, 100, 0), val_at=(100, 101.27, 0))
    body += wire((90, 100), (110, 100), "w1")        # over Reference
    body += wire((90, 101.27), (110, 101.27), "w2")  # over Value
    body += wire((90, 98.73), (110, 98.73), "w3")    # block up-escape
    body += wire((90, 97.46), (110, 97.46), "w4")
    p = _write("E6_sibling_no_pileup", sch(body))
    res = clear_wires_over_text(p)
    ctx = build_context(p)
    rb = [t for t in ctx["text_bboxes"] if t[1] == "Reference"]
    vb = [t for t in ctx["text_bboxes"] if t[1] == "Value"]
    after = find_wires_over_component_text(ctx["wires"], ctx["text_bboxes"])
    ok = bool(rb) and bool(vb)
    why = []
    if ok:
        stacked = _box_overlap(rb[0][2:], vb[0][2:])
        if stacked:
            ok = False; why.append("fields still overlap each other")
        if len(after) != 0:
            ok = False; why.append(f"after={len(after)}")
        if res.get("fields_moved") != 2:
            ok = False; why.append(f"moved={res.get('fields_moved')} != 2")
    _results.append(("E6_sibling_no_pileup", ok, "; ".join(why) or "ok"))
e6_sibling_no_pileup()


# ---- report --------------------------------------------------------------

passed = sum(1 for _, ok, _ in _results if ok)
total = len(_results)
print(f"\n=== wire-over-text dynamic matrix: {passed}/{total} passed ===")
for name, ok, why in _results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:28s} {why}")
sys.exit(0 if passed == total else 1)
