"""End-to-end hierarchy-routing proof WITHOUT kicad-cli.

erc_autofix accepts a `report_text` arg (pasted/uploaded report) which
bypasses the kicad-cli ERC run entirely. We feed a synthetic MULTI-SHEET
report against the real hier_inc_test parent and assert each proposed fix
is routed (`_file`) to the CHILD .kicad_sch named by its `***** Sheet`
header — the exact thing that was broken (everything went to the parent).
apply=false => preview only, no files written.
"""
import asyncio
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
EA = importlib.import_module("envil_agent.tools.erc_autofix")

PARENT = Path(r"F:\Ki_CAD\_envil_out\hier_inc_test"
              r"\CAN_Data_Logger_STM32F405RGT6"
              r"\CAN_Data_Logger_STM32F405RGT6.kicad_sch")

REPORT = """ERC report (2026-06-09T00:00:00, Encoding UTF8)
Report includes: Errors

***** Sheet /

***** Sheet /POWER/
[pin_not_connected]: Pin not connected
    ; error
    @(140.97 mm, 105.41 mm): Symbol C1 Pin 1 [Passive, Line]

***** Sheet /MCU/
[pin_not_connected]: Pin not connected
    ; error
    @(148.59 mm, 66.04 mm): Symbol R5 Pin 1 [Passive, Line]

***** Sheet /STORAGE/
[pin_not_connected]: Pin not connected
    ; error
    @(120.00 mm, 100.00 mm): Symbol J4 Pin 9 [Passive, Line]
"""

EXPECT = {
    "/POWER/": "01_power.kicad_sch",
    "/MCU/": "02_mcu.kicad_sch",
    "/STORAGE/": "06_storage.kicad_sch",
}


async def run():
    return await EA.erc_autofix.handler({
        "path": str(PARENT),
        "report_text": REPORT,
        "apply": False,
    })


def main():
    res = asyncio.run(run())
    assert not res.get("is_error"), res["content"][0]["text"]
    vios = res.get("violations") or []
    props = res.get("proposals") or []
    print(f"parsed {len(vios)} violations, {len(props)} proposals\n")

    print("=== violation -> routed file ===")
    ok = True
    for v in vios:
        sheet = v.get("sheet")
        got = Path(v.get("_file", "")).name
        want = EXPECT.get(sheet, "?")
        flag = "OK" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  {sheet:12} -> {got:22} (want {want})  [{flag}]")

    print("\n=== proposal -> target file (preview ops) ===")
    for p in props:
        f = Path(p.get("_file", "")).name
        n = len(p.get("ops") or [])
        print(f"  {p.get('violation_type'):20} file={f:22} ops={n}")
        # every proposal must carry a child _file, never the parent
        assert p.get("_file") and Path(p["_file"]).name != PARENT.name, \
            f"proposal not routed to a child: {p.get('_file')}"

    # the report should announce hierarchy routing
    txt = res["content"][0]["text"]
    report = json.loads(txt).get("report", txt) if txt.startswith("{") else txt
    assert "HIERARCHY: routed" in report, "missing hierarchy routing banner"
    print("\nrouting banner present:",
          [l.strip() for l in report.splitlines() if "HIERARCHY:" in l][0])

    assert ok, "a violation routed to the wrong file"
    print("\nALL ROUTING ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
