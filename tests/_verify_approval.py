"""Offline verification of the Phase 5 electrical-readiness / approval card
(intent/approval.electrical_readiness). No LLM. Run:

    python tests/_verify_approval.py     # exit 0 = all pass

Proves: READY only when 0 errors; warnings don't block but show as REVIEW;
the config-driven checklist maps codes -> PASS/REVIEW/FAIL; and block provenance
splits verified-template parts from architect-generated ones.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent  # noqa: E402
from envil_agent.intent import approval                     # noqa: E402


def _ir_with_provenance():
    ir = TopologyIR(name="t", circuit_type="X", nets=[], components=[
        IRComponent("U7", "Amplifier_Current:INA240A1", "INA240A1"),
        IRComponent("U1", "MCU_ST_STM32G4:STM32G474RETx", "STM32G474"),
        IRComponent("C5", "Device:C", "100n")])         # the verified-injected part
    setattr(ir, "_verified_provenance", {"C5": "ina240_vs_decoupling"})
    return ir


def _item(rpt, label):
    return next((c["status"] for c in rpt["checklist"] if c["label"] == label), None)


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    ir = _ir_with_provenance()

    # clean -> READY, all checklist PASS, provenance split
    rpt = approval.electrical_readiness(ir, issues=[])
    check("clean schematic -> READY for PCB",
          rpt["ready_for_pcb"] is True, rpt["error_count"] == 0,
          all(c["status"] == "PASS" for c in rpt["checklist"]),
          rpt["sign_off_required"] is True)
    check("provenance splits verified vs architect parts",
          rpt["provenance"]["verified_parts"] == ["C5"],
          sorted(rpt["provenance"]["architect_parts"]) == ["U1", "U7"])

    # a blocking error -> NOT READY, the matching checklist item FAILs
    rpt = approval.electrical_readiness(ir, issues=[
        {"code": "NET_FLOATING", "severity": "error", "where": "ALERT",
         "text": "net 'ALERT' has only 1 pin"}])
    check("blocking error -> NOT READY + item FAIL + listed",
          rpt["ready_for_pcb"] is False, rpt["error_count"] == 1,
          _item(rpt, "All pins connected / no floating nets") == "FAIL",
          any("NET_FLOATING" in b for b in rpt["blocking"]))

    # a warning -> still READY (warnings don't block), item shows REVIEW
    rpt = approval.electrical_readiness(ir, issues=[
        {"code": "CAN_TERMINATION_MISSING", "severity": "warning", "where": "U2",
         "text": "no 120R across CANH-CANL"}])
    check("warning only -> READY but flagged REVIEW",
          rpt["ready_for_pcb"] is True, rpt["warning_count"] == 1,
          _item(rpt, "CAN bus termination + pins") == "REVIEW",
          any("CAN_TERMINATION_MISSING" in r for r in rpt["review"]))

    # mixed error + warning
    rpt = approval.electrical_readiness(ir, issues=[
        {"code": "REQUIREMENT_PART_MISSING", "severity": "warning", "where": "BQ76952", "text": "x"},
        {"code": "PIN_IN_MULTIPLE_NETS", "severity": "error", "where": "U1.PA0", "text": "x"}])
    check("mixed -> NOT READY, counts + statuses correct",
          rpt["ready_for_pcb"] is False, rpt["error_count"] == 1, rpt["warning_count"] == 1,
          _item(rpt, "All pins connected / no floating nets") == "FAIL",
          _item(rpt, "All requested parts present") == "REVIEW")

    # no provenance -> all architect, note reflects it
    ir2 = TopologyIR(name="t", circuit_type="X", nets=[],
                     components=[IRComponent("R1", "Device:R", "1k")])
    rpt = approval.electrical_readiness(ir2, issues=[])
    check("no verified parts -> all architect, whole-schematic note",
          rpt["provenance"]["verified_parts"] == [],
          rpt["provenance"]["architect_parts"] == ["R1"],
          "whole schematic" in rpt["provenance"]["note"])

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
