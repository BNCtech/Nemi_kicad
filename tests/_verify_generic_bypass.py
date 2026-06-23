"""Offline verification of the DYNAMIC, part-agnostic pin-bypass rule
(validate generic_pin_bypass / IC_PIN_NO_BYPASS). No LLM. Run:

    python tests/_verify_generic_bypass.py     # exit 0 = all pass

Proves: ANY IC pin named like a regulator/reference/charge-pump output is
required to have a local bypass cap to GND, by PIN NAME alone (no per-part
template); a cap on the pin's net clears it; 2-pin passives are skipped.
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


# A generic AFE-like IC: has REGIN / BREG / CP1 (need bypass) + ordinary pins.
_LIB = {
    "X:FAKEAFE": _Geom([_Pin("REGIN", "1"), _Pin("BREG", "2"), _Pin("CP1", "3"),
                        _Pin("SDA", "4"), _Pin("GND", "5"), _Pin("VC0", "6")]),
    # a 2-pin part that happens to expose a 'REGIN'-named pin -> must be skipped
    "X:TWOPIN": _Geom([_Pin("REGIN", "1"), _Pin("GND", "2")]),
}


def _fake_load(lib_id):
    g = _LIB.get(lib_id)
    if g is None:
        raise ValueError("no fake symbol")
    return g


def _codes(ir):
    return {(i["code"], i["where"]) for i in V._run_design_checklist(ir)
            if i["code"] == "IC_PIN_NO_BYPASS"}


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    _orig = V.load_symbol
    V.load_symbol = _fake_load
    try:
        # no caps -> REGIN / BREG / CP1 all flagged (by pin name, no part rule)
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE")],
                        nets=[IRNet("N_REGIN", ["U1.REGIN"]),
                              IRNet("N_BREG", ["U1.BREG"]),
                              IRNet("N_CP1", ["U1.CP1"]),
                              IRNet("GND", ["U1.GND"], is_power=True)])
        c = _codes(ir)
        check("bypass pins with no cap -> all flagged",
              ("IC_PIN_NO_BYPASS", "U1.REGIN") in c,
              ("IC_PIN_NO_BYPASS", "U1.BREG") in c,
              ("IC_PIN_NO_BYPASS", "U1.CP1") in c)

        # add a cap from REGIN to GND -> REGIN clears, others still flagged
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE"),
                                    IRComponent("C1", "Device:C", "1u")],
                        nets=[IRNet("N_REGIN", ["U1.REGIN", "C1.1"]),
                              IRNet("N_BREG", ["U1.BREG"]),
                              IRNet("N_CP1", ["U1.CP1"]),
                              IRNet("GND", ["U1.GND", "C1.2"], is_power=True)])
        c = _codes(ir)
        check("cap on REGIN->GND clears REGIN (others still flagged)",
              ("IC_PIN_NO_BYPASS", "U1.REGIN") not in c,
              ("IC_PIN_NO_BYPASS", "U1.BREG") in c)

        # SDA / VC0 (not bypass pins) are NEVER flagged
        check("non-bypass pins never flagged",
              not any(w in ("U1.SDA", "U1.VC0") for (_, w) in _codes(ir)))

        # a 2-pin part with a REGIN pin is skipped (passives, not an IC)
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("D1", "X:TWOPIN", "TVS")],
                        nets=[IRNet("N", ["D1.REGIN"]), IRNet("GND", ["D1.GND"], is_power=True)])
        check("2-pin part with REGIN pin -> skipped (not flagged)",
              _codes(ir) == set())

        # AUTO-SYNTHESIS: complete_design_checklist adds the missing bypass caps,
        # and re-running adds nothing (self-limiting via the detector).
        from envil_agent.intent.checklist_repair import complete_design_checklist
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEAFE", "AFE")],
                        nets=[IRNet("N_REGIN", ["U1.REGIN"]),
                              IRNet("N_BREG", ["U1.BREG"]),
                              IRNet("N_CP1", ["U1.CP1"]),
                              IRNet("GND", ["U1.GND"], is_power=True)])
        added1 = complete_design_checklist(ir)
        added2 = complete_design_checklist(ir)   # re-run -> nothing more
        check("auto-synthesis adds bypass caps + idempotent + clears the flag",
              len([r for r in added1 if r.startswith("C")]) >= 3,
              added2 == [],
              _codes(ir) == set())
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
