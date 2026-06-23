"""Per-block audit must merge same-named nets — the fix for the BMS where
cap-heavy blocks were held out by a FLOOD of false PIN_IN_MULTIPLE_NETS
(['+3V3','+3V3']) created by listing the block's own rail AND the boundary
rail of the same name as two separate nets.

Run:  python tests/_verify_audit_dedup.py
"""
import sys
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

from envil_agent.intent.ir import IRComponent, IRNet
from envil_agent.intent.incremental_build import _audit_block

R = []
def rec(n, ok, d=""):
    R.append(ok); print(("PASS" if ok else "FAIL"), "—", n, ("— " + d) if d else "")

def mnet(issues):
    return [i["where"] for i in issues if i.get("code") == "PIN_IN_MULTIPLE_NETS"]


# 1. block draws +3V3/GND; boundary repeats them (SAME name) -> the shared pins
#    must NOT be flagged (they're one net, not "multiple nets").
comps = [IRComponent("C1", "Device:C", "100n"), IRComponent("C2", "Device:C", "100n")]
nets = [IRNet("+3V3", ["C1.1", "C2.1"], is_power=True),
        IRNet("GND",  ["C1.2", "C2.2"], is_power=True)]
boundary = [{"name": "+3V3", "is_power": True,
             "my_pins": ["C1.1", "C2.1"], "external_pins": []},
            {"name": "GND",  "is_power": True,
             "my_pins": ["C1.2", "C2.2"], "external_pins": []}]
m = mnet(_audit_block(comps, nets, boundary))
rec("duplicate same-named rail -> NO false PIN_IN_MULTIPLE_NETS", m == [], f"got={m}")

# 2. a pin GENUINELY on two DIFFERENT nets -> still flagged (real conflict kept).
comps2 = [IRComponent("R1", "Device:R", "10k")]
nets2 = [IRNet("+3V3", ["R1.1"], is_power=True),
         IRNet("FOO",  ["R1.1"]),
         IRNet("GND",  ["R1.2"], is_power=True)]
m2 = mnet(_audit_block(comps2, nets2, []))
rec("pin on two DIFFERENT nets -> still flagged", "R1.1" in m2, f"got={m2}")

print("\nSUMMARY:", sum(R), "/", len(R))
sys.exit(0 if all(R) else 1)
