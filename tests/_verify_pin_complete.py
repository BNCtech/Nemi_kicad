"""Data-driven pin-completion engine — verification.

Proves the architectural point: ONE engine (complete_floating_pins) connects a
floating pin of ANY component, driven by DATA rules -- no function per part.

  - LED cathode floating  + led rule    -> GND
  - MCU VSS floating       + ground rule -> GND        (totally different part)
  - SAME LED, but rules=[] -> NOT fixed                 (it's the DATA, not code)
  - a NOVEL part/pin       + a NEW rule  -> fixed       (add a rule, zero code)

Run:  python tests/_verify_pin_complete.py
"""
import sys
sys.path.insert(0, "f:/Ki_CAD/ai_backend")

from envil_agent.intent.ir import TopologyIR
from envil_agent.intent.pin_complete import complete_floating_pins

R = []
def rec(n, ok, d=""):
    R.append(ok); print(("PASS" if ok else "FAIL"), "—", n, ("— " + d) if d else "")

LED_RULE = {"lib_id_contains": ["LED"], "pin_name_in": ["K", "CATHODE"],
            "two_pin_only": True, "require_other_pin_wired": True, "connect_to": "GND"}
GND_RULE = {"pin_name_contains": ["GND", "VSS"], "pin_etype_in": ["power_in"], "connect_to": "GND"}
# A NOVEL rule for a part the engine has no dedicated code for: tie a resistor's
# pin 2 to GND. Contrived, but it proves "new part = new rule, not new function".
NOVEL_RULE = {"lib_id_contains": ["DEVICE:R"], "pin_number_in": ["2"], "connect_to": "GND"}


def led_ir():
    return TopologyIR.from_dict({
        "name": "led", "circuit_type": "x",
        "components": [
            {"ref": "D1", "lib_id": "Device:LED", "value": "G", "footprint": ""},
            {"ref": "R1", "lib_id": "Device:R", "value": "330", "footprint": ""},
        ],
        "nets": [
            {"name": "+3V3", "pins": ["R1.1"], "is_power": True},
            {"name": "GND", "pins": [], "is_power": True},
            {"name": "LED_A", "pins": ["D1.A", "R1.2"], "is_power": False},
            {"name": "CAN_LED_K", "pins": ["D1.K"], "is_power": False},
        ], "blocks": [], "notes": ""})


def mcu_ir():
    return TopologyIR.from_dict({
        "name": "mcu", "circuit_type": "x",
        "components": [{"ref": "U1", "lib_id": "MCU_ST_STM32F4:STM32F405RGTx", "value": "S", "footprint": ""}],
        "nets": [{"name": "+3V3", "pins": ["U1.VDD"], "is_power": True},
                 {"name": "GND", "pins": [], "is_power": True}],
        "blocks": [], "notes": ""})


def novel_ir():
    return TopologyIR.from_dict({
        "name": "novel", "circuit_type": "x",
        "components": [{"ref": "R9", "lib_id": "Device:R", "value": "1k", "footprint": ""}],
        "nets": [{"name": "SIG", "pins": ["R9.1"], "is_power": False},
                 {"name": "GND", "pins": [], "is_power": True}],
        "blocks": [], "notes": ""})


def gnd_pins(ir, ref):
    g = next((n for n in ir.nets if n.name.upper() == "GND"), None)
    return [p for p in (g.pins if g else []) if p.split(".")[0] == ref]


# A) LED via data rule
ir = led_ir(); complete_floating_pins(ir, rules=[LED_RULE])
rec("LED cathode -> GND via led rule", len(gnd_pins(ir, "D1")) == 1, str(gnd_pins(ir, "D1")))

# B) MCU ground pin via a DIFFERENT rule, SAME engine
ir = mcu_ir(); complete_floating_pins(ir, rules=[GND_RULE])
rec("MCU VSS -> GND via ground rule (same engine)", len(gnd_pins(ir, "U1")) >= 1, str(gnd_pins(ir, "U1")))

# C) data-driven proof: no rules -> no fix
ir = led_ir(); complete_floating_pins(ir, rules=[])
rec("no rules -> nothing connected (it's the DATA)", len(gnd_pins(ir, "D1")) == 0)

# D) novel part handled by a NEW rule, zero code
ir = novel_ir(); complete_floating_pins(ir, rules=[NOVEL_RULE])
rec("novel part fixed by adding a rule (no new function)", len(gnd_pins(ir, "R9")) == 1, str(gnd_pins(ir, "R9")))

print("\nSUMMARY:", sum(R), "/", len(R))
sys.exit(0 if all(R) else 1)
