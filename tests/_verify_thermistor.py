"""Offline verification of the DYNAMIC thermistor-input rule
(validate generic_thermistor — DETECT-ONLY). No LLM. Run:

    python tests/_verify_thermistor.py     # exit 0 = all pass

Proves, by PIN NAME on any IC (TS/THERM/NTC):
  - a thermistor input whose net holds ONLY the IC pin -> TEMP_SENSE_NO_THERMISTOR
  - a thermistor input with an external NTC on its net is NOT flagged
  - a non-thermistor pin is NOT flagged
  - the rule is DETECT-ONLY: complete_design_checklist auto-synthesises NOTHING
    for it (auto-adding a pull-up would be datasheet-harmful on an AFE)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent, IRNet  # noqa: E402
from envil_agent.intent import validate as V                       # noqa: E402


class _Pin:
    def __init__(self, name, number):
        self.name, self.number, self.etype = name, number, "input"


class _Geom:
    def __init__(self, pins):
        self.pins = pins

    def resolve_pin(self, key):
        for p in self.pins:
            if p.name == key or p.number == key:
                return p
        return None


_LIB = {
    "X:FAKEAFE": _Geom([_Pin("TS1", "1"), _Pin("TS2", "2"),
                        _Pin("VDD", "3"), _Pin("VSS", "4")]),
}


def _fake_load(lib_id):
    g = _LIB.get(lib_id)
    if g is None:
        raise ValueError("no fake symbol")
    return g


def _codes(ir):
    return {i["where"] for i in V._run_design_checklist(ir)
            if i["code"] == "TEMP_SENSE_NO_THERMISTOR"}


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    _orig = V.load_symbol
    V.load_symbol = _fake_load
    try:
        # 1. TS1 net holds only the IC pin -> flagged; TS2 has an NTC -> not flagged
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE"),
                                    IRComponent("TH2", "Device:R", "10k")],
                        nets=[IRNet("TEMP1", ["U1.TS1"]),
                              IRNet("TEMP2", ["U1.TS2", "TH2.1"]),
                              IRNet("GND", ["U1.VSS", "TH2.2"], is_power=True),
                              IRNet("+3V3", ["U1.VDD"], is_power=True)])
        c = _codes(ir)
        check("bare TS net flagged; TS net with NTC not flagged",
              "U1.TS1" in c, "U1.TS2" not in c)

        # 2. non-thermistor pin (VDD) never flagged even when bare-ish
        check("non-thermistor pin not flagged", "U1.VDD" not in c)

        # 3. DETECT-ONLY: auto-synthesis adds NOTHING for the bare TS pin
        from envil_agent.intent.checklist_repair import complete_design_checklist
        ir2 = TopologyIR(name="t", circuit_type="X",
                         components=[IRComponent("U1", "X:FAKEAFE", "AFE")],
                         nets=[IRNet("TEMP1", ["U1.TS1"]),
                               IRNet("TEMP2", ["U1.TS2"]),
                               IRNet("GND", ["U1.VSS"], is_power=True),
                               IRNet("+3V3", ["U1.VDD"], is_power=True)])
        before = len(ir2.components)
        added = complete_design_checklist(ir2)
        # nothing added for the thermistor pins (no NTC/pullup invented)
        check("DETECT-ONLY: no thermistor part auto-synthesised",
              len(ir2.components) == before, added == [],
              _codes(ir2) == {"U1.TS1", "U1.TS2"})
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
