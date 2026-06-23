"""Offline verification of the DYNAMIC open-drain bus pull-up rule
(validate generic_bus_pullup / IC_BUS_NO_PULLUP). No LLM. Run:

    python tests/_verify_bus_pullup.py     # exit 0 = all pass

Proves: an I2C/SMBus net (SCL/SDA by pin name) needs a pull-up to a logic rail,
flagged PER BUS NET (one flag for a shared bus, not per IC); a pull-up clears
it; pure-SPI pins are not flagged; auto-synthesis adds one pull-up per bus.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent, IRNet  # noqa: E402
from envil_agent.intent import validate as V                       # noqa: E402


class _Pin:
    def __init__(self, name, number):
        self.name, self.number, self.etype = name, number, "bidirectional"


class _Geom:
    def __init__(self, pins):
        self.pins = pins

    def resolve_pin(self, key):
        for p in self.pins:
            if p.name == key or p.number == key:
                return p
        return None


_LIB = {
    # dual-mode I2C/SPI part (BQ76952-style pin names) + a pure-SPI pin
    "X:FAKEI2C": _Geom([_Pin("SCL/SPI_CLK", "1"), _Pin("SDA/SPI_MISO", "2"),
                        _Pin("SPI_CLK", "3"), _Pin("VDD", "4"), _Pin("GND", "5")]),
}


def _fake_load(lib_id):
    g = _LIB.get(lib_id)
    if g is None:
        raise ValueError("no fake symbol")
    return g


def _bus_codes(ir):
    return {i["where"] for i in V._run_design_checklist(ir)
            if i["code"] == "IC_BUS_NO_PULLUP"}


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    _orig = V.load_symbol
    V.load_symbol = _fake_load
    try:
        # SCL + SDA with no pull-up -> both bus nets flagged; SPI_CLK not flagged
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEI2C", "AFE")],
                        nets=[IRNet("I2C_SCL", ["U1.SCL/SPI_CLK"]),
                              IRNet("I2C_SDA", ["U1.SDA/SPI_MISO"]),
                              IRNet("SPI_CK", ["U1.SPI_CLK"]),
                              IRNet("+3V3", ["U1.VDD"], is_power=True),
                              IRNet("GND", ["U1.GND"], is_power=True)])
        c = _bus_codes(ir)
        check("SCL/SDA nets flagged, pure-SPI net NOT flagged",
              "I2C_SCL" in c, "I2C_SDA" in c, "SPI_CK" not in c)

        # add a pull-up from SCL net to +3V3 -> SCL clears, SDA still flagged
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEI2C", "AFE"),
                                    IRComponent("R1", "Device:R", "4.7k")],
                        nets=[IRNet("I2C_SCL", ["U1.SCL/SPI_CLK", "R1.1"]),
                              IRNet("I2C_SDA", ["U1.SDA/SPI_MISO"]),
                              IRNet("+3V3", ["U1.VDD", "R1.2"], is_power=True),
                              IRNet("GND", ["U1.GND"], is_power=True)])
        c = _bus_codes(ir)
        check("pull-up on SCL->+3V3 clears SCL (SDA still flagged)",
              "I2C_SCL" not in c, "I2C_SDA" in c)

        # two ICs share ONE SCL net -> flagged ONCE (per net, not per IC)
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEI2C", "A"),
                                    IRComponent("U2", "X:FAKEI2C", "B")],
                        nets=[IRNet("SCL", ["U1.SCL/SPI_CLK", "U2.SCL/SPI_CLK"]),
                              IRNet("+3V3", ["U1.VDD", "U2.VDD"], is_power=True),
                              IRNet("GND", ["U1.GND", "U2.GND"], is_power=True)])
        n = sum(1 for i in V._run_design_checklist(ir)
                if i["code"] == "IC_BUS_NO_PULLUP" and i["where"] == "SCL")
        check("shared bus net flagged exactly ONCE (per net)", n == 1)

        # AUTO-SYNTHESIS: one pull-up added per bus net + idempotent
        from envil_agent.intent.checklist_repair import complete_design_checklist
        ir = TopologyIR(name="t", circuit_type="X",
                        components=[IRComponent("U1", "X:FAKEI2C", "AFE")],
                        nets=[IRNet("I2C_SCL", ["U1.SCL/SPI_CLK"]),
                              IRNet("I2C_SDA", ["U1.SDA/SPI_MISO"]),
                              IRNet("+3V3", ["U1.VDD"], is_power=True),
                              IRNet("GND", ["U1.GND"], is_power=True)])
        a1 = complete_design_checklist(ir)
        a2 = complete_design_checklist(ir)   # re-run -> nothing more
        check("auto-synthesis adds one pull-up per bus + idempotent + clears",
              len([r for r in a1 if r.startswith("R")]) >= 2,
              added2_empty := (a2 == []),
              _bus_codes(ir) == set())
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
