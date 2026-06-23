"""Offline verification of the DYNAMIC low-side current-sense RC-filter rule
(validate generic_sense_filter). No LLM. Run:

    python tests/_verify_sense_filter.py     # exit 0 = all pass

Proves, by PIN NAME on any IC (BQ76952-style SRP/SRN family):
  - a sense pair with no shunt + no diff cap -> SENSE_NO_SHUNT + SENSE_NO_DIFF_FILTER
  - a low-value shunt across the pair clears SENSE_NO_SHUNT
  - a differential cap across the pair clears SENSE_NO_DIFF_FILTER
  - a cap from a sense net to GND -> SENSE_CAP_TO_GND (datasheet anti-pattern)
  - INA-style IN+/IN- amps are NOT flagged here (CMRR risk -> architect's call)
  - auto-synthesis adds ONE diff cap across the pair, idempotent, clears the flag
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent, IRNet  # noqa: E402
from envil_agent.intent import validate as V                       # noqa: E402


class _Pin:
    def __init__(self, name, number):
        self.name, self.number, self.etype = name, number, "passive"


class _Geom:
    def __init__(self, pins):
        self.pins = pins

    def resolve_pin(self, key):
        for p in self.pins:
            if p.name == key or p.number == key:
                return p
        return None


_LIB = {
    # AFE / coulomb-counter with a low-side sense pair (BQ76952-style)
    "X:FAKEAFE": _Geom([_Pin("SRP", "1"), _Pin("SRN", "2"),
                        _Pin("VDD", "3"), _Pin("VSS", "4")]),
    # INA-style amp whose inputs are named IN+/IN- (must NOT be flagged here)
    "X:FAKEINA": _Geom([_Pin("IN+", "1"), _Pin("IN-", "2"),
                        _Pin("V+", "3"), _Pin("GND", "4"), _Pin("OUT", "5")]),
}


def _fake_load(lib_id):
    g = _LIB.get(lib_id)
    if g is None:
        raise ValueError("no fake symbol")
    return g


def _codes(ir, code):
    return {i["where"] for i in V._run_design_checklist(ir) if i["code"] == code}


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    _orig = V.load_symbol
    V.load_symbol = _fake_load
    try:
        # 1. bare sense pair, no shunt + no cap -> both under-build codes fire
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE")],
                        nets=[IRNet("SENSE_P", ["U1.SRP"]),
                              IRNet("SENSE_N", ["U1.SRN"]),
                              IRNet("+3V3", ["U1.VDD"], is_power=True),
                              IRNet("GND", ["U1.VSS"], is_power=True)])
        check("bare sense pair -> NO_SHUNT + NO_DIFF_FILTER",
              "U1.SRP" in _codes(ir, "SENSE_NO_SHUNT"),
              "SENSE_P|SENSE_N" in _codes(ir, "SENSE_NO_DIFF_FILTER"))

        # 2. add a 1m shunt across the pair -> SENSE_NO_SHUNT clears, cap still flagged
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE"),
                                    IRComponent("R1", "Device:R", "1m")],
                        nets=[IRNet("SENSE_P", ["U1.SRP", "R1.1"]),
                              IRNet("SENSE_N", ["U1.SRN", "R1.2"]),
                              IRNet("+3V3", ["U1.VDD"], is_power=True),
                              IRNet("GND", ["U1.VSS"], is_power=True)])
        check("shunt across pair clears NO_SHUNT (diff cap still flagged)",
              not _codes(ir, "SENSE_NO_SHUNT"),
              "SENSE_P|SENSE_N" in _codes(ir, "SENSE_NO_DIFF_FILTER"))

        # 3. add a differential cap across the pair -> SENSE_NO_DIFF_FILTER clears
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE"),
                                    IRComponent("R1", "Device:R", "1m"),
                                    IRComponent("C1", "Device:C", "100n")],
                        nets=[IRNet("SENSE_P", ["U1.SRP", "R1.1", "C1.1"]),
                              IRNet("SENSE_N", ["U1.SRN", "R1.2", "C1.2"]),
                              IRNet("+3V3", ["U1.VDD"], is_power=True),
                              IRNet("GND", ["U1.VSS"], is_power=True)])
        check("diff cap across pair clears NO_DIFF_FILTER + NO_SHUNT",
              not _codes(ir, "SENSE_NO_DIFF_FILTER"),
              not _codes(ir, "SENSE_NO_SHUNT"))

        # 4. cap from a sense net to GND -> SENSE_CAP_TO_GND (datasheet anti-pattern)
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE"),
                                    IRComponent("C9", "Device:C", "100n")],
                        nets=[IRNet("SENSE_P", ["U1.SRP", "C9.1"]),
                              IRNet("SENSE_N", ["U1.SRN"]),
                              IRNet("+3V3", ["U1.VDD"], is_power=True),
                              IRNet("GND", ["U1.VSS", "C9.2"], is_power=True)])
        check("cap from sense net to GND -> SENSE_CAP_TO_GND",
              "SENSE_P" in _codes(ir, "SENSE_CAP_TO_GND"))

        # 5. INA-style IN+/IN- amp -> NOT flagged by generic_sense_filter
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U2", "X:FAKEINA", "AMP")],
                        nets=[IRNet("SH_HI", ["U2.IN+"]),
                              IRNet("SH_LO", ["U2.IN-"]),
                              IRNet("+5V", ["U2.V+"], is_power=True),
                              IRNet("GND", ["U2.GND"], is_power=True),
                              IRNet("I_OUT", ["U2.OUT"])])
        check("INA IN+/IN- NOT flagged by sense-filter rule",
              not _codes(ir, "SENSE_NO_SHUNT"),
              not _codes(ir, "SENSE_NO_DIFF_FILTER"),
              not _codes(ir, "SENSE_CAP_TO_GND"))

        # 6. AUTO-SYNTHESIS: one diff cap added across the pair + idempotent
        from envil_agent.intent.checklist_repair import complete_design_checklist
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE"),
                                    IRComponent("R1", "Device:R", "1m")],
                        nets=[IRNet("SENSE_P", ["U1.SRP", "R1.1"]),
                              IRNet("SENSE_N", ["U1.SRN", "R1.2"]),
                              IRNet("+3V3", ["U1.VDD"], is_power=True),
                              IRNet("GND", ["U1.VSS"], is_power=True)])
        a1 = complete_design_checklist(ir)
        a2 = complete_design_checklist(ir)   # re-run -> nothing more
        # the added cap bridges SENSE_P and SENSE_N
        added_caps = [r for r in a1 if r.startswith("C")]
        bridged = False
        for cap in added_caps:
            p = {n.name for n in ir.nets if any(pp.startswith(f"{cap}.") for pp in n.pins)}
            if {"SENSE_P", "SENSE_N"} <= p:
                bridged = True
        check("auto-synth adds one diff cap across pair + idempotent + clears",
              len(added_caps) == 1, bridged, a2 == [],
              not _codes(ir, "SENSE_NO_DIFF_FILTER"))
    finally:
        V.load_symbol = _orig

    ok = True
    for name, passed, conds in results:
        ok = ok and passed
        print(("PASS" if passed else "FAIL"), name)
        if not passed:
            print("   conds:", [bool(c) for c in conds])
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
