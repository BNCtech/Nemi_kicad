"""Offline verification of the Phase 0.2 cross-block net-name collision guard.

No LLM, no render — pure IR-level checks on block_repair.splice_block (Layer B)
and validate._check_cross_block_collisions (Layer A). Run:

    python tests/_verify_cross_block_guard.py     # exit 0 = all pass

Proves the guard prevents a silent short (two distinct same-named signals
bonding via global labels) WITHOUT (a) breaking a legitimate emergent shared
signal and (b) changing the byte-stable behaviour when the gates are off.
Reflects the 2026-06-11 adversarial review: Layer B renames ONLY weak bus-index
names (a descriptive emergent signal is unioned, never split); Layer A is an
opt-in diagnostic whose pattern list excludes legitimate functional signals.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import (  # noqa: E402
    TopologyIR, IRComponent, IRNet, IRBlock,
)
from envil_agent.intent import block_repair                     # noqa: E402
from envil_agent.intent import validate as V                    # noqa: E402

# Bus-index weak patterns (mirror the tightened config default).
_WEAK = [re.compile(p) for p in
         (r"^D[0-9]+$", r"^A[0-9]+$", r"^IO[0-9]+$", r"^GPIO[0-9]+$")]


def _mk_two_block_ir(b_signal_name):
    """Block A (U1) owns net 'D5'; Block B (U2) owns net 'FOO'. Regenerating
    Block B emits its OWN net named `b_signal_name` (an incidental collision
    when it equals A's net name)."""
    return TopologyIR(
        name="t", circuit_type="X",
        components=[IRComponent("U1", "Lib:A", "A"), IRComponent("U2", "Lib:B", "B")],
        nets=[IRNet("D5", ["U1.1", "U1.2"]), IRNet("FOO", ["U2.7", "U2.8"])],
        blocks=[IRBlock("BLKA", "mcu", ["U1"]), IRBlock("BLKB", "comm", ["U2"])],
    )


def main() -> int:
    results = []

    def check(name, *conds):
        results.append((name, all(bool(c) for c in conds), conds))

    _orig_splice = block_repair._splice_rename_cfg
    _orig_vcfg = V._cross_block_guard_cfg

    # --- Layer B, gate OFF -> legacy union (the silent short) -----------------
    block_repair._splice_rename_cfg = lambda: (False, "__{block}", _WEAK)
    ir = _mk_two_block_ir("D5")
    rep = block_repair.splice_block(
        ir, "BLKB", [IRComponent("U2", "Lib:B", "B")], [IRNet("D5", ["U2.1", "U2.2"])])
    d5 = next(n for n in ir.nets if n.name == "D5")
    check("gate OFF unions (legacy behaviour = short)",
          "U2.1" in d5.pins, "U1.1" in d5.pins, rep.get("collision_renames") == [])

    # --- Layer B, gate ON, WEAK name -> rename, NO union (two-instance safe) --
    block_repair._splice_rename_cfg = lambda: (True, "__{block}", _WEAK)
    ir = _mk_two_block_ir("D5")
    rep = block_repair.splice_block(
        ir, "BLKB", [IRComponent("U2", "Lib:B", "B")], [IRNet("D5", ["U2.1", "U2.2"])])
    d5 = next(n for n in ir.nets if n.name == "D5")
    renamed = [n for n in ir.nets if n.name == "D5__BLKB"]
    check("gate ON renames WEAK 'D5' -> BLKA.D5 and BLKB.D5 stay SEPARATE",
          "U2.1" not in d5.pins, set(d5.pins) == {"U1.1", "U1.2"},
          len(renamed) == 1, set(renamed[0].pins) == {"U2.1", "U2.2"},
          rep["collision_renames"] == [{"from": "D5", "to": "D5__BLKB", "block": "BLKB"}])

    # --- Layer B, gate ON, DESCRIPTIVE name -> UNION (reviewer-1 hole fixed) --
    # A descriptive emergent shared signal the architect added off-plan must NOT
    # be split: it is the SAME signal, just not declared in the plan.
    block_repair._splice_rename_cfg = lambda: (True, "__{block}", _WEAK)
    ir = TopologyIR(
        name="t", circuit_type="X",
        components=[IRComponent("U1", "Lib:A", "A"), IRComponent("U2", "Lib:B", "B")],
        nets=[IRNet("SIGNAL_A", ["U1.1"]), IRNet("FOO", ["U2.7", "U2.8"])],
        blocks=[IRBlock("BLKA", "mcu", ["U1"]), IRBlock("BLKB", "comm", ["U2"])])
    rep = block_repair.splice_block(
        ir, "BLKB", [IRComponent("U2", "Lib:B", "B")], [IRNet("SIGNAL_A", ["U2.1"])])
    sig = next(n for n in ir.nets if n.name == "SIGNAL_A")
    check("gate ON UNIONS descriptive 'SIGNAL_A' (not weak) -> connection kept",
          "U2.1" in sig.pins, "U1.1" in sig.pins,
          not any(n.name.startswith("SIGNAL_A__") for n in ir.nets),
          rep.get("collision_renames") == [])

    # --- Layer B, power rail is NEVER renamed (is_power guard) ----------------
    block_repair._splice_rename_cfg = lambda: (True, "__{block}", _WEAK + [re.compile(r"^GND$")])
    ir = _mk_two_block_ir("D5")
    ir.nets = [IRNet("GND", ["U1.1"], is_power=True), IRNet("FOO", ["U2.7"])]
    block_repair.splice_block(
        ir, "BLKB", [IRComponent("U2", "Lib:B", "B")],
        [IRNet("GND", ["U2.2"], is_power=True)])
    gnd = next(n for n in ir.nets if n.name == "GND")
    check("power rail GND unions, never renamed (is_power guard)",
          "U2.2" in gnd.pins, "U1.1" in gnd.pins,
          not any(n.name.startswith("GND__") for n in ir.nets))

    block_repair._splice_rename_cfg = _orig_splice  # restore

    # --- Layer A (forced ON) flags WEAK bus-index, ignores functional/bus -----
    V._cross_block_guard_cfg = lambda: {
        "validate_collision_check": True, "collision_severity": "warning",
        "weak_signal_name_patterns": [p.pattern for p in _WEAK]}
    ir2 = TopologyIR(
        name="t2", circuit_type="X",
        components=[IRComponent("U1", "Lib:A", "A"), IRComponent("U2", "Lib:B", "B")],
        nets=[IRNet("D5", ["U1.3", "U2.5"]),            # weak bus-index -> flag
              IRNet("ALERT", ["U1.4", "U2.6"]),         # functional -> NOT flagged
              IRNet("MCU_BQ_ALERT", ["U1.8", "U2.9"]),  # descriptive -> NOT flagged
              IRNet("SPI_CLK", ["U1.7", "U2.8"])],      # protocol bus -> NOT flagged
        blocks=[IRBlock("BLKA", "mcu", ["U1"]), IRBlock("BLKB", "comm", ["U2"])])
    a_iss = V._check_cross_block_collisions(ir2)
    wheres = [i["where"] for i in a_iss]
    check("Layer A flags weak 'D5', ignores ALERT / descriptive / protocol bus",
          "D5" in wheres, "ALERT" not in wheres,
          "MCU_BQ_ALERT" not in wheres, "SPI_CLK" not in wheres,
          all(i["severity"] == "warning" for i in a_iss),
          all(i["code"] == "CROSS_BLOCK_NET_COLLISION" for i in a_iss))

    # --- Layer A: weak name living in ONE block is NOT flagged ----------------
    ir2b = TopologyIR(
        name="t2b", circuit_type="X",
        components=[IRComponent("U1", "Lib:A", "A"), IRComponent("R1", "Lib:R", "R")],
        nets=[IRNet("D5", ["U1.3", "R1.1"])],
        blocks=[IRBlock("BLKA", "mcu", ["U1", "R1"]), IRBlock("BLKB", "comm", ["U2"])])
    check("Layer A ignores a weak name that lives in ONE block",
          V._check_cross_block_collisions(ir2b) == [])

    V._cross_block_guard_cfg = _orig_vcfg  # restore real config

    # --- Default posture: Layer A is OPT-IN (off) -> no flag with real config -
    ir2c = TopologyIR(
        name="t2c", circuit_type="X",
        components=[IRComponent("U1", "Lib:A", "A"), IRComponent("U2", "Lib:B", "B")],
        nets=[IRNet("D5", ["U1.3", "U2.5"])],
        blocks=[IRBlock("BLKA", "mcu", ["U1"]), IRBlock("BLKB", "comm", ["U2"])])
    check("Layer A OFF by default -> no warning even on a weak cross-block net",
          V._check_cross_block_collisions(ir2c) == [])

    # --- Layer A: flat (block-less) IR -> no-op (byte-stable) -----------------
    ir3 = TopologyIR(
        name="t3", circuit_type="X",
        components=[IRComponent("U1", "Lib:A", "A")],
        nets=[IRNet("D5", ["U1.1", "U1.2"])], blocks=[])
    check("Layer A no-op on flat IR", V._check_cross_block_collisions(ir3) == [])

    # --- Layer B rename surfaced by validate (stamp -> issue), config-agnostic-
    ir4 = TopologyIR(name="t4", circuit_type="X",
                     components=[IRComponent("U1", "Lib:A", "A")], nets=[], blocks=[])
    setattr(ir4, "_cross_block_collisions",
            [{"from": "D5", "to": "D5__BLKB", "block": "BLKB"}])
    s_iss = V._check_cross_block_collisions(ir4)
    check("Layer B rename surfaced as CROSS_BLOCK_NET_COLLISION",
          len(s_iss) == 1, s_iss[0]["code"] == "CROSS_BLOCK_NET_COLLISION",
          "D5__BLKB" in s_iss[0]["text"])

    # --- tally ---------------------------------------------------------------
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
