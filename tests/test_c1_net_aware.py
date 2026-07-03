"""C1 net-aware junction guard --- functional checks.

Run directly:  python tests/test_c1_net_aware.py
No pytest dependency (this repo uses a golden harness, not pytest).

Covers the reported defect: two circuits placed close together, one wire's
endpoint lands on the other's mid-span. Geometry alone would dot it and short
the nets; the net-aware guard must refuse when the two sides carry different
named nets, and still dot a genuine same-net tap.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envil_agent.lint.selectors import (  # noqa: E402
    find_cross_net_touches, find_missing_junction_dots, resolve_wire_nets,
)

_fail = 0


def check(name, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


# --- Scenario 1: accidental cross-net T-tap (the bug) ---------------------
# Net A horizontal wire y=100 from x=50..70, labelled NET_A at its left end.
# Net B vertical wire x=60 from y=100..110, labelled NET_B at its bottom.
# B's top endpoint (60,100) lands on A's mid-span -> a T-tap.
wires = [((50.0, 100.0), (70.0, 100.0)),   # net A
         ((60.0, 100.0), (60.0, 110.0))]   # net B taps A mid-span
labels = [("NET_A", 50.0, 100.0), ("NET_B", 60.0, 110.0)]

geom = find_missing_junction_dots(wires, [])
check("geometry alone flags the T-tap as needing a dot",
      any(abs(i["where"]["x"] - 60.0) < 0.1 and abs(i["where"]["y"] - 100.0) < 0.1
          for i in geom))

touches = find_cross_net_touches(wires, labels)
check("cross-net touch detected at (60,100)",
      any(t["id"] == "CROSS_NET_TOUCH" and abs(t["where"]["x"] - 60.0) < 0.1
          for t in touches))

# --- Scenario 2: legitimate same-net tap ----------------------------------
# Same geometry but BOTH wires are NET_A -> not a cross-net touch.
labels_same = [("NET_A", 50.0, 100.0), ("NET_A", 60.0, 110.0)]
touches_same = find_cross_net_touches(wires, labels_same)
check("same-net tap is NOT flagged", len(touches_same) == 0)

# --- Scenario 3: unnamed side stays conservative --------------------------
# Only net A is labelled; B has no label -> cannot prove different -> allowed.
labels_partial = [("NET_A", 50.0, 100.0)]
touches_partial = find_cross_net_touches(wires, labels_partial)
check("unnamed side is not flagged (conservative)", len(touches_partial) == 0)

# --- Scenario 4: collinear cross-net overlap ------------------------------
# Two horizontal wires on the same Y, overlapping span, different nets.
wires_ov = [((0.0, 5.0), (10.0, 5.0)), ((6.0, 5.0), (16.0, 5.0))]
labels_ov = [("RAIL_X", 0.0, 5.0), ("RAIL_Y", 16.0, 5.0)]
touches_ov = find_cross_net_touches(wires_ov, labels_ov)
check("collinear cross-net overlap detected",
      any(t["id"] == "CROSS_NET_OVERLAP" for t in touches_ov))

# --- Scenario 5: net resolution groups a real multi-wire net --------------
# L-route: two wires sharing an endpoint form one cluster; one label names it.
wires_net = [((0.0, 0.0), (10.0, 0.0)), ((10.0, 0.0), (10.0, 10.0))]
roots, names = resolve_wire_nets(wires_net, [("SIG", 0.0, 0.0)])
check("two wires sharing an endpoint are one cluster",
      roots[0] == roots[1])
check("cluster carries the label name",
      names.get(roots[0]) == frozenset({"SIG"}))

# --- Scenario 6: end-to-end through add_missing_junction_dots -------------
# Build a minimal .kicad_sch with the cross-net T-tap and confirm the guard
# withholds the dot when net_aware, and dots it when net_aware is off.
import tempfile  # noqa: E402
from envil_agent.lint.repair import add_missing_junction_dots  # noqa: E402

SCH = """(kicad_sch (version 20211123) (generator eeschema)
  (wire (pts (xy 50 100) (xy 70 100)) (uuid "w1"))
  (wire (pts (xy 60 100) (xy 60 110)) (uuid "w2"))
  (label "NET_A" (at 50 100 0) (uuid "l1"))
  (label "NET_B" (at 60 110 0) (uuid "l2"))
)
"""

with tempfile.TemporaryDirectory() as td:
    p_off = Path(td) / "off.kicad_sch"
    p_on = Path(td) / "on.kicad_sch"
    p_off.write_text(SCH, encoding="utf-8")
    p_on.write_text(SCH, encoding="utf-8")

    r_off = add_missing_junction_dots(p_off, net_aware=False)
    r_on = add_missing_junction_dots(p_on, net_aware=True)

    check("net_aware OFF dots the cross-net T-tap (legacy behaviour)",
          r_off["junctions_added"] == 1)
    check("net_aware ON withholds the cross-net dot",
          r_on["junctions_added"] == 0)
    check("net_aware ON reports the skipped cross-net touch",
          any(t["id"] == "CROSS_NET_TOUCH"
              for t in r_on.get("cross_net_skipped", [])))

print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
sys.exit(1 if _fail else 0)
