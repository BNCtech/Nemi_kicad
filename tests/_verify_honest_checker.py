"""Phase 0 acceptance test — the HONEST CHECKER (no false-green).

Run:
    python tests/_verify_honest_checker.py     # exit 0 = all pass

PROVES the false-green hole in validate.PIN_IN_MULTIPLE_NETS:
the same PHYSICAL pin written two different ways (by NUMBER on one net,
by NAME on another) is a real electrical short, but the current check
keys on the raw pinref STRING, so "U1.2" and "U1.PB0" look like two
different pins and the short ships GREEN.

This test is RED until validate.py canonicalises each pinref to
(ref, pin.number) via the symbol before grouping. It must stay GREEN
after the fix. The two control cases guard against (a) the check
silently breaking and (b) a false positive on a clean board.

Test-first, exactly how Cursor uses tests as the target: write the
acceptance check, watch it fail, fix the code until it passes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # ai_backend

from envil_agent.intent.ir import TopologyIR, IRComponent, IRNet  # noqa: E402
from envil_agent.intent import validate as V                      # noqa: E402
from envil_agent.kicad import symbol_geom                         # noqa: E402

# Symbols to probe for a pin whose NUMBER differs from its NAME. First
# one that loads and has such a pin is used, so the test is robust to
# which libraries are installed.
CANDIDATE_LIBS = [
    "MCU_ST_STM32G4:STM32G474CBTx",
    "MCU_Microchip_ATtiny:ATtiny85-20PU",
    "Amplifier_Operational:LM358",
    "Timer:NE555",
    "Interface_CAN_LIN:TJA1051T",
]


def _find_distinct_pin():
    """Return (lib_id, pin_number, pin_name) for a pin where number != name."""
    for lib in CANDIDATE_LIBS:
        try:
            geom = symbol_geom.load_symbol(lib)
        except Exception:
            continue
        for p in geom.pins:
            num, name = str(p.number), str(p.name)
            if name and num.isdigit() and name not in ("~", num):
                return lib, num, name
    return None, None, None


def _codes(ir) -> set:
    return {i["code"] for i in V.validate_ir(ir)}


def main() -> int:
    lib, num, name = _find_distinct_pin()
    if lib is None:
        print("BLOCKED: no candidate symbol with a number!=name pin could be "
              "loaded — cannot exercise the short. Check sym-lib paths.")
        return 2

    print(f"using {lib}  pin {num} == name {name!r} (same physical pin)")

    results = []

    def check(label, cond):
        results.append((label, bool(cond)))

    # PRIMARY — the false-green bug. Same physical pin on two nets, written
    # by NUMBER and by NAME. This is a dead short and MUST be flagged.
    short_ir = TopologyIR(
        name="hidden-short", circuit_type="X",
        components=[IRComponent("U1", lib, "X"),
                    IRComponent("R1", "Device:R", "1k"),
                    IRComponent("R2", "Device:R", "1k")],
        nets=[IRNet("NET_A", [f"U1.{num}", "R1.1"]),
              IRNet("NET_B", [f"U1.{name}", "R2.1"])])
    check("HIDDEN SHORT (pin by number vs name) is flagged PIN_IN_MULTIPLE_NETS",
          "PIN_IN_MULTIPLE_NETS" in _codes(short_ir))

    # CONTROL 1 — same pin, SAME spelling, on two nets. The check already
    # catches this today; it must keep catching it after the fix.
    same_ir = TopologyIR(
        name="obvious-short", circuit_type="X",
        components=[IRComponent("U1", lib, "X"),
                    IRComponent("R1", "Device:R", "1k"),
                    IRComponent("R2", "Device:R", "1k")],
        nets=[IRNet("NET_A", [f"U1.{num}", "R1.1"]),
              IRNet("NET_B", [f"U1.{num}", "R2.1"])])
    check("OBVIOUS SHORT (same spelling) still flagged",
          "PIN_IN_MULTIPLE_NETS" in _codes(same_ir))

    # CONTROL 2 — clean board, no shared pin. Must NOT false-positive.
    clean_ir = TopologyIR(
        name="clean", circuit_type="X",
        components=[IRComponent("U1", lib, "X"),
                    IRComponent("R1", "Device:R", "1k"),
                    IRComponent("R2", "Device:R", "1k")],
        nets=[IRNet("NET_A", [f"U1.{num}", "R1.1"]),
              IRNet("NET_B", ["R2.1", "R2.2"])])
    check("CLEAN board not flagged (no false positive)",
          "PIN_IN_MULTIPLE_NETS" not in _codes(clean_ir))

    ok = True
    for label, passed in results:
        ok = ok and passed
        print(("PASS" if passed else "FAIL"), label)
    print("ALL PASS" if ok else "SOME FAILED (expected RED until validate.py is fixed)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
