"""Offline verification of the Phase 3 requirement-conformance / drift check
(validate._check_requirement_conformance). No LLM. Run:

    python tests/_verify_requirement_conformance.py     # exit 0 = all pass

Proves: a part the user NAMED in the prompt must appear in the circuit (catches
the architect dropping/substituting it); package suffixes still match; protocol
tokens are excluded; empty prompt is a no-op (golden byte-stable).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent  # noqa: E402
from envil_agent.intent import validate as V               # noqa: E402


def _ir(*lib_value_pairs):
    return TopologyIR(name="t", circuit_type="X", nets=[], components=[
        IRComponent(f"U{i}", lib, val) for i, (lib, val) in enumerate(lib_value_pairs, 1)])


def _missing(prompt, ir):
    return {i["where"] for i in V._check_requirement_conformance(ir, prompt)}


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    P = "13S BMS with BQ76952 AFE, STM32G474 MCU and a TJA1051 CAN transceiver"

    # all named parts present (note package suffixes) -> no conformance issue
    ir = _ir(("Battery_Management:BQ7695201PFBR", "BQ7695201PFBR"),
             ("MCU_ST_STM32G4:STM32G474CBTx", "STM32G474CBTx"),
             ("Interface_CAN_LIN:TJA1051T", "TJA1051T"))
    check("all named parts present (suffixes ok) -> clean",
          _missing(P, ir) == set())

    # architect substituted the AFE -> BQ76952 flagged, others not
    ir = _ir(("Battery_Management:BQ76940", "BQ76940"),     # wrong AFE
             ("MCU_ST_STM32G4:STM32G474CBTx", "STM32G474CBTx"),
             ("Interface_CAN_LIN:TJA1051T", "TJA1051T"))
    m = _missing(P, ir)
    check("dropped/substituted BQ76952 -> flagged (others not)",
          "BQ76952" in m, "STM32G474" not in m, "TJA1051" not in m)

    # severity is warning (never an error) — IR with none of the named parts
    iss = V._check_requirement_conformance(_ir(("Device:R", "1k")), P)
    check("conformance issues are warning severity",
          all(i["severity"] == "warning" for i in iss),
          all(i["code"] == "REQUIREMENT_PART_MISSING" for i in iss))

    # protocol/bus tokens are excluded (RS485 named but not a part) -> not flagged
    ir = _ir(("Interface_CAN_LIN:TJA1051T", "TJA1051T"))
    check("protocol token RS485 excluded -> not flagged",
          "RS485" not in _missing("CAN logger over RS485 using TJA1051", ir))

    # empty prompt -> no-op (golden byte-stable)
    check("empty prompt -> no issues",
          V._check_requirement_conformance(_ir(("X:Y", "Y")), "") == [])

    # generic words / values are not treated as parts
    ir = _ir(("Device:R", "120"), ("Device:C", "100n"))
    check("non-part words/values not flagged",
          _missing("a 120 ohm resistor and 100n cap on the 5V rail", ir) == set())

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
