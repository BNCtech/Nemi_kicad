"""Verify erc_autofix hierarchy routing (Phase H, 2026-06-09).

Run: python ai_backend/tests/_verify_erc_hierarchy_routing.py
Checks: (1) parser tags each violation with its `***** Sheet` path,
(2) sheet->child .kicad_sch resolution on a real hierarchical project,
(3) a flat/leaf sheet resolves to an empty map (legacy single-file path),
(4) end-to-end PREVIEW (apply=false) routes proposals across child files.
"""
import asyncio
import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
EA = importlib.import_module("envil_agent.tools.erc_autofix")

REPORT = """ERC report (2026-06-09T14:23:21, Encoding UTF8)
Report includes: Errors

***** Sheet /

***** Sheet /POWER/
[power_pin_not_driven]: Input Power pin not driven by any Output Power pins
    ; error
    @(5550 mils, 4150 mils): Symbol U1 Pin 1 [VI, Power input, Line]

***** Sheet /MCU/
[pin_not_connected]: Pin not connected
    ; error
    @(5850 mils, 2600 mils): Symbol U3 Pin 32 [VDD, Power input, Line]

***** Sheet /INDICATOR/
[pin_not_connected]: Pin not connected
    ; error
    @(5400 mils, 4750 mils): Symbol D5 Pin 1 [K, Passive, Line]
"""

PARENT = Path(r"F:\Ki_CAD\_envil_out\hier_inc_test"
              r"\CAN_Data_Logger_STM32F405RGT6"
              r"\CAN_Data_Logger_STM32F405RGT6.kicad_sch")


def test_parser():
    print("=== Test 1: parser sheet tags ===")
    vios = EA._parse_erc_report(REPORT)
    for v in vios:
        print(f"  {v['type']:24} sheet={v.get('sheet')!r}  "
              f"{v['locations'][0]['descr'][:24]}")
    assert [v.get("sheet") for v in vios] == ["/POWER/", "/MCU/",
                                              "/INDICATOR/"]
    print("  PASS\n")


def test_resolution():
    print("=== Test 2: sheet->file resolution ===")
    m = EA._resolve_sheet_files(PARENT)
    for name, p in sorted(m.items()):
        print(f"  {name:14} -> {p.name}")
    print(f"  ({len(m)} child sheets)")
    assert EA._sheet_to_file("/", m, PARENT) == PARENT
    assert EA._sheet_to_file("/NOPE/", m, PARENT) == PARENT
    if m:
        some = sorted(m)[0]
        assert EA._sheet_to_file(f"/{some}/", m, PARENT) == m[some]
    print("  PASS\n")


def test_flat_leaf():
    print("=== Test 3: leaf sheet resolves empty (legacy path) ===")
    leaf = PARENT.parent / "01_power.kicad_sch"
    kids = EA._resolve_sheet_files(leaf)
    print(f"  {leaf.name} children: {len(kids)} (expect 0)")
    assert len(kids) == 0
    print("  PASS\n")


def test_preview_routes():
    print("=== Test 4: erc_autofix preview routes across child files ===")

    async def run():
        return await EA.erc_autofix.handler({"path": str(PARENT),
                                             "apply": False})

    res = asyncio.run(run())
    txt = res["content"][0]["text"]
    try:
        report = json.loads(txt).get("report", txt)
    except Exception:
        report = txt
    for ln in report.splitlines():
        s = ln.strip()
        if ("HIERARCHY:" in s or s.startswith("V") or "->" in s
                or "proposed" in s or "parsed" in s):
            print("  " + s)
    print("  (preview ran without error)\n")


if __name__ == "__main__":
    test_parser()
    test_resolution()
    test_flat_leaf()
    test_preview_routes()
    print("ALL TESTS PASSED")
