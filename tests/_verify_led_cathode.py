"""Floating LED cathode -> GND — verification (dynamic, symbol-grounded).

normalize._tie_floating_led_cathode returns any LED's floating cathode to GND
(the live CAN_LED_K / SD_LED_K NET_FLOATING failure), driven by the LED symbol's
cathode pin -- NOT by net name. A properly-wired LED is untouched; a fully
floating LED (anode also unwired) is left for the architect.

Run:  python tests/_verify_led_cathode.py
"""
import sys
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

from envil_agent.intent.ir import TopologyIR
from envil_agent.intent.normalize import normalize_ir
from envil_agent.intent.validate import validate_ir
from envil_agent.kicad.symbol_geom import load_symbol

R = []
def rec(n, ok, d=""):
    R.append(ok); print(("PASS" if ok else "FAIL"), "—", n, ("— " + d) if d else "")


def floating_cath(name_k):
    """LED with anode wired through R to +3V3, cathode on a single-pin stub."""
    return {
        "name": "status LED, cathode floating", "circuit_type": "MCU_BOARD",
        "components": [
            {"ref": "D1", "lib_id": "Device:LED", "value": "GRN", "footprint": ""},
            {"ref": "R1", "lib_id": "Device:R", "value": "330", "footprint": ""},
        ],
        "nets": [
            {"name": "+3V3", "pins": ["R1.1"], "is_power": True},
            {"name": "GND", "pins": [], "is_power": True},
            {"name": "LED_A", "pins": ["D1.A", "R1.2"], "is_power": False},
            {"name": name_k, "pins": ["D1.K"], "is_power": False},   # floating cathode
        ],
        "blocks": [], "notes": "",
    }


def proper_led():
    return {
        "name": "status LED, fully wired", "circuit_type": "MCU_BOARD",
        "components": [
            {"ref": "D1", "lib_id": "Device:LED", "value": "GRN", "footprint": ""},
            {"ref": "R1", "lib_id": "Device:R", "value": "330", "footprint": ""},
        ],
        "nets": [
            {"name": "+3V3", "pins": ["R1.1"], "is_power": True},
            {"name": "LED_A", "pins": ["D1.A", "R1.2"], "is_power": False},
            {"name": "GND", "pins": ["D1.K"], "is_power": True},     # already grounded
        ],
        "blocks": [], "notes": "",
    }


def run():
    for nm in ("CAN_LED_K", "SD_LED_K"):
        ir = TopologyIR.from_dict(floating_cath(nm))
        fl_before = {i["where"] for i in validate_ir(ir) if i["code"] == "NET_FLOATING"}
        normalize_ir(ir)
        codes_after = validate_ir(ir)
        fl_after = {i["where"] for i in codes_after if i["code"] == "NET_FLOATING"}
        gnd = next((n for n in ir.nets if n.name.upper() == "GND"), None)
        cath_on_gnd = gnd is not None and any(p.split(".")[0] == "D1" for p in gnd.pins)
        rec(f"[{nm}] floating cathode cleared",
            nm in {w for w in fl_before} and nm not in fl_after and cath_on_gnd,
            f"before={fl_before} after={fl_after} gnd={gnd.pins if gnd else None}")

    # proper LED untouched (cathode stays on GND, no spurious change)
    ir2 = TopologyIR.from_dict(proper_led())
    n_nets_before = len(ir2.nets)
    normalize_ir(ir2)
    gnd2 = next((n for n in ir2.nets if n.name.upper() == "GND"), None)
    rec("properly-wired LED untouched",
        gnd2 is not None and any(p.split(".")[0] == "D1" for p in gnd2.pins),
        f"gnd={gnd2.pins if gnd2 else None}")

    print("\nSUMMARY:", sum(R), "/", len(R))
    return 0 if all(R) else 1


if __name__ == "__main__":
    sys.exit(run())
