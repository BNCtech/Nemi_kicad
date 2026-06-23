"""Smoke test: generate a .kicad_pcb directly from a hand-built IR and check
it parses. Run from ai_backend/:  python tests/_verify_pcb_gen.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envil_agent.intent.ir import IRComponent, IRNet, TopologyIR  # noqa: E402
from envil_agent.layout.pcb_gen import generate_pcb_from_ir       # noqa: E402


def build_ir() -> TopologyIR:
    comps = [
        IRComponent(ref="R1", lib_id="Device:R", value="10k",
                    footprint="Resistor_SMD:R_0603_1608Metric"),
        IRComponent(ref="R2", lib_id="Device:R", value="10k",
                    footprint="Resistor_SMD:R_0603_1608Metric"),
        IRComponent(ref="C1", lib_id="Device:C", value="100n",
                    footprint="Capacitor_SMD:C_0603_1608Metric"),
    ]
    nets = [
        IRNet(name="+5V", pins=["R1.1"], is_power=True),
        IRNet(name="OUT", pins=["R1.2", "R2.1", "C1.1"]),
        IRNet(name="GND", pins=["R2.2", "C1.2"], is_power=True),
    ]
    return TopologyIR(name="divider", circuit_type="GENERIC",
                      components=comps, nets=nets)


def main() -> int:
    ir = build_ir()
    out = Path(tempfile.mkdtemp(prefix="pcbgen_")) / "divider.kicad_sch"
    out.write_text("(kicad_sch)\n", encoding="utf-8")  # stub sibling
    report = generate_pcb_from_ir(ir, str(out))

    print("=== report ===")
    for k, v in report.items():
        print(f"  {k}: {v}")

    pcb = Path(report["pcb_path"])
    print(f"\n=== {pcb} ({pcb.stat().st_size} bytes) ===")
    print(pcb.read_text(encoding="utf-8"))

    ok = (report.get("footprints_placed", 0) == 3
          and report.get("pads_netted", 0) >= 5
          and not report.get("error"))
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
