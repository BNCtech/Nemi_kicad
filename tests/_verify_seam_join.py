"""Cross-block seam JOIN + VERIFY — the U1<->U2 connection check.

The doubt: in incremental per-block draw, when U1 (block 1) connects to U2
(block 2), and a block's redraw DROPS its endpoint of that cross-block net, is
the connection actually re-joined? build_incrementally snapshots the plan's
cross-block contract and re-asserts every endpoint after drawing.

Here the MCU block deliberately drops its CAN_TX endpoint (U1.PA12). The seam
re-join must restore it so CAN_TX still bridges U1<->U2.

Run:  python tests/_verify_seam_join.py
"""
import sys
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

from envil_agent.intent.ir import IRComponent, IRNet, IRBlock, TopologyIR
from envil_agent.intent.incremental_build import build_incrementally, _reassert_plan_seams

R = []
def rec(n, ok, d=""):
    R.append(ok); print(("PASS" if ok else "FAIL"), "—", n, ("— " + d) if d else "")


def plan():
    return TopologyIR(
        "x", "x",
        [IRComponent("U1", "MCU", "U1"), IRComponent("U2", "CAN", "U2")],
        [IRNet("+3V3", ["U1.1", "U2.1"], is_power=True),
         IRNet("CAN_TX", ["U1.PA12", "U2.D"])],          # the U1<->U2 cross-block net
        [IRBlock("MCU", "mcu", ["U1"]), IRBlock("CAN", "comm", ["U2"])])


def draw(bname, btype, comps, errs, boundary, prompt):
    if bname == "MCU":
        # MCU block redraw DROPS its CAN_TX endpoint (only reconnects the rail) --
        # the exact failure: a block forgets a cross-block signal.
        return ([IRComponent("U1", "MCU", "U1")],
                [IRNet("+3V3", ["U1.1"], is_power=True)])
    if bname == "CAN":
        return ([IRComponent("U2", "CAN", "U2")],
                [IRNet("+3V3", ["U2.1"], is_power=True),
                 IRNet("CAN_TX", ["U2.D"])])
    return None, None


# 1) end-to-end: build, then the cross-block net must still bridge U1<->U2
ir, drawn, skipped = build_incrementally(plan(), "x", draw, normalize=False)
cantx = next((n for n in ir.nets if n.name == "CAN_TX"), None)
pins = set(cantx.pins) if cantx else set()
rec("U1<->U2 seam re-joined after a block dropped it",
    {"U1.PA12", "U2.D"} <= pins, f"CAN_TX={sorted(pins)}")

# 2) unit: _reassert_plan_seams re-adds exactly the missing endpoint
ir2 = TopologyIR("x", "x", [], [IRNet("CAN_TX", ["U2.D"])], [])
planned = {"CAN_TX": (False, ["U1.PA12", "U2.D"])}
n = _reassert_plan_seams(ir2, planned)
seam = next((x for x in ir2.nets if x.name == "CAN_TX"), None)
rec("re-assert adds the dropped endpoint (count=1)",
    n == 1 and "U1.PA12" in seam.pins, f"rejoined={n} pins={seam.pins}")

# 3) intact seam -> nothing re-joined (no spurious adds)
ir3 = TopologyIR("x", "x", [], [IRNet("CAN_TX", ["U1.PA12", "U2.D"])], [])
n3 = _reassert_plan_seams(ir3, {"CAN_TX": (False, ["U1.PA12", "U2.D"])})
rec("intact seam -> 0 re-joins", n3 == 0, f"rejoined={n3}")

print("\nSUMMARY:", sum(R), "/", len(R))
sys.exit(0 if all(R) else 1)
