"""Offline verification of the Phase 1 BMS design-checklist validators:
CAN bus termination, ALERT-reaches-MCU, current-shunt polarity.

No LLM, no render. Symbol geometry is stubbed (validate.load_symbol) so the
checks run without the real KiCad library. Run:

    python tests/_verify_design_checks.py     # exit 0 = all pass
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent, IRNet  # noqa: E402
from envil_agent.intent import validate as V                       # noqa: E402

_PHASE1_CODES = {
    "CAN_PIN_UNCONNECTED", "CAN_TERMINATION_MISSING",
    "ALERT_NET_NOT_CONNECTED",
    "SHUNT_AMP_INPUT_UNCONNECTED", "SHUNT_AMP_INPUTS_SHORTED",
    "SHUNT_POLARITY_SUSPECT",
}


class _Pin:
    def __init__(self, name, number, etype="passive"):
        self.name, self.number, self.etype = name, number, etype


class _Geom:
    def __init__(self, pins):
        self.pins = pins

    def resolve_pin(self, key):
        for p in self.pins:
            if p.name == key or p.number == key:
                return p
        return None


# lib_id -> fake symbol geometry (only the parts the checks inspect).
_LIB = {
    "Interface_CAN_LIN:TJA1051T": _Geom([
        _Pin("TXD", "1"), _Pin("GND", "2"), _Pin("VCC", "3"), _Pin("RXD", "4"),
        _Pin("VIO", "5"), _Pin("CANL", "6"), _Pin("CANH", "7"), _Pin("S", "8")]),
    # REAL INA240A1 pinout: inputs are '+'/'-', supply 'V+', OUT pin-5 unnamed.
    "Amplifier_Current:INA240A1": _Geom([
        _Pin("-", "1"), _Pin("GND", "2"), _Pin("REF2", "3"), _Pin("GND", "4"),
        _Pin("", "5"), _Pin("V+", "6"), _Pin("REF1", "7"), _Pin("+", "8")]),
    "Battery_Management:BQ76952": _Geom([
        _Pin("ALERT", "20"), _Pin("VC0", "1"), _Pin("SDA", "2"), _Pin("SCL", "3")]),
    "MCU_ST_STM32G4:STM32G474RET6": _Geom([
        _Pin("PA0", "14"), _Pin("VDD", "1"), _Pin("VSS", "2")]),
}


def _fake_load(lib_id):
    g = _LIB.get(lib_id)
    if g is None:
        raise ValueError(f"no fake symbol for {lib_id}")
    return g


def _codes(ir):
    return {i["code"] for i in V._run_design_checklist(ir)} & _PHASE1_CODES


def main() -> int:
    results = []

    def check(name, got_codes, must_have=(), must_not=()):
        ok = all(c in got_codes for c in must_have) and all(c not in got_codes for c in must_not)
        results.append((name, ok, got_codes, must_have, must_not))

    _orig = V.load_symbol
    V.load_symbol = _fake_load
    CAN = "Interface_CAN_LIN:TJA1051T"
    INA = "Amplifier_Current:INA240A1"
    BQ = "Battery_Management:BQ76952"
    MCU = "MCU_ST_STM32G4:STM32G474RET6"
    try:
        # === CAN termination ===
        # terminated (120R across CANH-CANL) -> clean
        ir = TopologyIR(name="c", circuit_type="X", components=[
            IRComponent("U1", CAN, "TJA1051"), IRComponent("R1", "Device:R", "120")],
            nets=[IRNet("CANH", ["U1.CANH", "R1.1"]), IRNet("CANL", ["U1.CANL", "R1.2"])])
        check("CAN terminated (120R) -> no warning", _codes(ir),
              must_not=("CAN_TERMINATION_MISSING", "CAN_PIN_UNCONNECTED"))

        # split termination: CANH-60-MID, CANL-60-MID, MID-cap-GND -> accepted
        ir = TopologyIR(name="c", circuit_type="X", components=[
            IRComponent("U1", CAN, "TJA1051"),
            IRComponent("R1", "Device:R", "60"), IRComponent("R2", "Device:R", "60"),
            IRComponent("C1", "Device:C", "4.7n")],
            nets=[IRNet("CANH", ["U1.CANH", "R1.1"]),
                  IRNet("CANL", ["U1.CANL", "R2.1"]),
                  IRNet("CAN_MID", ["R1.2", "R2.2", "C1.1"]),
                  IRNet("GND", ["C1.2"], is_power=True)])
        check("CAN split-term (2x60R to midpoint) accepted", _codes(ir),
              must_not=("CAN_TERMINATION_MISSING",))

        # no termination -> warning (not error)
        ir = TopologyIR(name="c", circuit_type="X", components=[
            IRComponent("U1", CAN, "TJA1051"), IRComponent("J1", "Connector:Conn", "CAN")],
            nets=[IRNet("CANH", ["U1.CANH", "J1.1"]), IRNet("CANL", ["U1.CANL", "J1.2"])])
        check("CAN no termination -> CAN_TERMINATION_MISSING", _codes(ir),
              must_have=("CAN_TERMINATION_MISSING",), must_not=("CAN_PIN_UNCONNECTED",))

        # floating CANH -> error
        ir = TopologyIR(name="c", circuit_type="X", components=[
            IRComponent("U1", CAN, "TJA1051")],
            nets=[IRNet("CANL", ["U1.CANL", "U1.GND"])])
        check("CAN floating CANH -> CAN_PIN_UNCONNECTED (error)", _codes(ir),
              must_have=("CAN_PIN_UNCONNECTED",))

        # === ALERT reaches MCU ===
        # ALERT -> MCU GPIO -> clean
        ir = TopologyIR(name="a", circuit_type="X", components=[
            IRComponent("U3", BQ, "BQ76952"), IRComponent("U4", MCU, "STM32G474")],
            nets=[IRNet("BMS_ALERT", ["U3.ALERT", "U4.PA0"])])
        check("ALERT reaches MCU -> no warning", _codes(ir),
              must_not=("ALERT_NET_NOT_CONNECTED",))

        # ALERT on a net with NO MCU pin -> warning
        ir = TopologyIR(name="a", circuit_type="X", components=[
            IRComponent("U3", BQ, "BQ76952"), IRComponent("R1", "Device:R", "10k")],
            nets=[IRNet("BMS_ALERT", ["U3.ALERT", "R1.1"])])
        check("ALERT not reaching MCU -> ALERT_NET_NOT_CONNECTED", _codes(ir),
              must_have=("ALERT_NET_NOT_CONNECTED",))

        # === current-shunt amplifier ===
        # IN+/IN- straddle a 5 mOhm shunt -> clean
        ir = TopologyIR(name="s", circuit_type="X", components=[
            IRComponent("U2", INA, "INA240"), IRComponent("R10", "Device:R", "0.005")],
            nets=[IRNet("ISENSE_P", ["U2.+", "R10.1"]), IRNet("ISENSE_N", ["U2.-", "R10.2"])])
        check("shunt straddles IN+/IN- (5mR) -> clean", _codes(ir),
              must_not=("SHUNT_POLARITY_SUSPECT", "SHUNT_AMP_INPUTS_SHORTED",
                        "SHUNT_AMP_INPUT_UNCONNECTED"))

        # IN+ and IN- on the SAME net -> error
        ir = TopologyIR(name="s", circuit_type="X", components=[
            IRComponent("U2", INA, "INA240")],
            nets=[IRNet("ISENSE", ["U2.+", "U2.-"])])
        check("shunt inputs shorted -> SHUNT_AMP_INPUTS_SHORTED", _codes(ir),
              must_have=("SHUNT_AMP_INPUTS_SHORTED",))

        # IN- floating -> error
        ir = TopologyIR(name="s", circuit_type="X", components=[
            IRComponent("U2", INA, "INA240"), IRComponent("R10", "Device:R", "0.005")],
            nets=[IRNet("ISENSE_P", ["U2.+", "R10.1"])])
        check("shunt input floating -> SHUNT_AMP_INPUT_UNCONNECTED", _codes(ir),
              must_have=("SHUNT_AMP_INPUT_UNCONNECTED",))

        # inputs across a HIGH-value resistor (not a shunt) -> polarity suspect
        ir = TopologyIR(name="s", circuit_type="X", components=[
            IRComponent("U2", INA, "INA240"), IRComponent("R10", "Device:R", "10k")],
            nets=[IRNet("ISENSE_P", ["U2.+", "R10.1"]), IRNet("ISENSE_N", ["U2.-", "R10.2"])])
        check("no low-ohm shunt across inputs -> SHUNT_POLARITY_SUSPECT", _codes(ir),
              must_have=("SHUNT_POLARITY_SUSPECT",))
    finally:
        V.load_symbol = _orig

    ok = True
    for name, passed, got, mh, mn in results:
        ok = ok and passed
        print(("PASS" if passed else "FAIL"), name)
        if not passed:
            print(f"   got={sorted(got)} must_have={mh} must_not={mn}")
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
