"""C2 exhaustive pin-completeness --- functional checks.

Run directly:  python tests/test_c2_pin_completeness.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envil_agent.lint.selectors import find_incomplete_pins  # noqa: E402

_fail = 0


def check(name, cond):
    global _fail
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        _fail += 1


wires = [((0.0, 0.0), (10.0, 0.0))]
labels = [("SIG", 20.0, 0.0)]
no_connects = [(30.0, 0.0)]
pins = [
    {"x": 0.0, "y": 0.0, "ref": "U1", "number": "1", "name": "A",
     "etype": "input"},        # on wire endpoint -> ok
    {"x": 5.0, "y": 0.0, "ref": "U1", "number": "2", "name": "B",
     "etype": "input"},        # on wire mid-span -> ok
    {"x": 20.0, "y": 0.0, "ref": "U1", "number": "3", "name": "C",
     "etype": "output"},       # on label -> ok
    {"x": 30.0, "y": 0.0, "ref": "U1", "number": "4", "name": "D",
     "etype": "input"},        # on no-connect -> ok
    {"x": 99.0, "y": 99.0, "ref": "U1", "number": "5", "name": "E",
     "etype": "input"},        # connects to NOTHING -> incomplete
    {"x": 50.0, "y": 50.0, "ref": "U1", "number": "6", "name": "NC",
     "etype": "no_connect"},   # NC etype -> exempt even though floating
]

issues = find_incomplete_pins(pins, wires, labels, no_connects)
refs = {(i["where"]["ref"], i["where"]["pin"]) for i in issues}

check("floating signal pin flagged", ("U1", "5") in refs)
check("wired / labelled / no-connect pins not flagged",
      not any(p in refs for p in
              [("U1", "1"), ("U1", "2"), ("U1", "3"), ("U1", "4")]))
check("no_connect-etype pin exempt", ("U1", "6") not in refs)
check("exactly one incomplete pin", len(issues) == 1)

# Two pins abutting directly (no wire) count as connected.
pins2 = [
    {"x": 0.0, "y": 0.0, "ref": "J1", "number": "1", "name": "P", "etype": "passive"},
    {"x": 0.0, "y": 0.0, "ref": "J2", "number": "1", "name": "Q", "etype": "passive"},
]
check("directly-abutting pins are connected",
      len(find_incomplete_pins(pins2, [], [], [])) == 0)

print()
print("ALL PASS" if _fail == 0 else f"{_fail} FAILURE(S)")
sys.exit(1 if _fail else 0)
