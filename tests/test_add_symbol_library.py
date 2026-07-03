"""Manual test suite for the add_symbol_library tool.

Run directly:  python tests/test_add_symbol_library.py
"""
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envil_agent.tools.add_symbol_library import add_symbol_library, _find_sym_lib_tables

PASS = "[PASS]"
FAIL = "[FAIL]"
results = []


def parse(result):
    text = result["content"][0]["text"]
    is_error = result.get("is_error", False)
    return text, is_error


def check(ok, label, detail=""):
    results.append((ok, label, detail))
    icon = PASS if ok else FAIL
    print(f"{icon}  {label}")
    if detail:
        print(f"       {detail}")


# The @tool decorator wraps the function in SdkMcpTool; call via .handler
_call = add_symbol_library.handler


async def run():
    # ── T1: scope missing -> must error ──────────────────────────────────────
    r, is_err = parse(await _call({"path": "C:/fake"}))
    check(is_err and "scope" in r.lower(),
          "T1 scope missing -> error", r[:100])

    # ── T2: path missing -> must error ───────────────────────────────────────
    r, is_err = parse(await _call({"scope": "global"}))
    check(is_err and "path" in r.lower(),
          "T2 path missing -> error", r[:100])

    # ── T3: path doesn't exist -> must error ─────────────────────────────────
    r, is_err = parse(await _call({"path": "C:/does_not_exist_xyz", "scope": "global"}))
    check(is_err and "exist" in r.lower(),
          "T3 nonexistent path -> error", r[:100])

    # ── T4: scope=project without project_path -> must error ─────────────────
    r, is_err = parse(await _call({"path": "C:/fake", "scope": "project"}))
    check(is_err and "project_path" in r.lower(),
          "T4 project scope without project_path -> error", r[:100])

    # ── T5: valid .kicad_symdir + scope=global ───────────────────────────────
    tbl_path = _find_sym_lib_tables()[0]

    with tempfile.TemporaryDirectory() as td:
        symdir = Path(td) / "TestLib_EnvilAI.kicad_symdir"
        symdir.mkdir()
        (symdir / "FAKE_PART.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")

        r, is_err = parse(await _call({"path": str(symdir), "scope": "global"}))
        data = json.loads(r)
        ok = (not is_err
              and len(data.get("registered", [])) > 0
              and data.get("symbol_counts", {}).get("TestLib_EnvilAI", 0) == 1)
        check(ok, "T5 valid symdir + scope=global -> registered",
              str(data.get("registered", []))[:80])

        # T5b: entry actually written to the file
        in_table = "TestLib_EnvilAI" in tbl_path.read_text(encoding="utf-8")
        check(in_table, "T5b entry written to sym-lib-table file on disk")

        # ── T6: same path again -> already_registered ────────────────────────
        r, is_err = parse(await _call({"path": str(symdir), "scope": "global"}))
        data2 = json.loads(r)
        ok2 = (not is_err
               and len(data2.get("already_registered", [])) > 0
               and len(data2.get("registered", [])) == 0)
        check(ok2, "T6 same path again -> already_registered",
              str(data2.get("already_registered", []))[:80])

        # cleanup: remove test entry from table
        text = tbl_path.read_text(encoding="utf-8")
        clean = re.sub(r'\t\(lib \(name "TestLib_EnvilAI"\)[^\n]+\n', "", text)
        tbl_path.write_text(clean, encoding="utf-8")
        check("TestLib_EnvilAI" not in tbl_path.read_text(encoding="utf-8"),
              "T6c cleanup removed entry from sym-lib-table")

    # ── T7: .kicad_sym file path -> registers parent symdir ──────────────────
    real_symdir = Path(
        r"C:\Users\IOT-SOFT\AppData\Local\Programs\Envil CAD\share\kicad\symbols"
        r"\4xxx.kicad_symdir"
    )
    sym_file = next(real_symdir.glob("*.kicad_sym"), None)
    if sym_file:
        r, is_err = parse(await _call({"path": str(sym_file), "scope": "global"}))
        data3 = json.loads(r)
        ok3 = not is_err and (data3.get("registered") or data3.get("already_registered"))
        check(ok3, "T7 .kicad_sym file path -> handled correctly",
              str(data3.get("registered") or data3.get("already_registered"))[:80])
    else:
        check(False, "T7 skipped — no .kicad_sym file found in 4xxx.kicad_symdir")

    # ── T8: parent folder -> registers multiple symdirs ──────────────────────
    parent = Path(
        r"C:\Users\IOT-SOFT\AppData\Local\Programs\Envil CAD\share\kicad\symbols"
    )
    r, is_err = parse(await _call({"path": str(parent), "scope": "global"}))
    data4 = json.loads(r)
    total = len(data4.get("registered", [])) + len(data4.get("already_registered", []))
    check(not is_err and total > 1,
          f"T8 parent folder -> handles {total} symdirs (registered={len(data4.get('registered',[]))}, already={len(data4.get('already_registered',[]))})")

    # ── T9: custom library nickname ──────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        symdir = Path(td) / "Raw_Folder.kicad_symdir"
        symdir.mkdir()
        (symdir / "PART.kicad_sym").write_text("(kicad_symbol_lib)", encoding="utf-8")

        r, is_err = parse(await _call({
            "path": str(symdir),
            "scope": "global",
            "library": "BNC_Custom",
        }))
        data5 = json.loads(r)
        ok5 = (not is_err
               and any("BNC_Custom" in n for n in data5.get("registered", [])))
        check(ok5, "T9 custom nickname -> uses BNC_Custom in table",
              str(data5.get("registered", []))[:80])

        # cleanup
        text = tbl_path.read_text(encoding="utf-8")
        clean = re.sub(r'\t\(lib \(name "BNC_Custom"\)[^\n]+\n', "", text)
        tbl_path.write_text(clean, encoding="utf-8")

    # ── T10: END-TO-END — register path then load_symbol() resolves it ───────
    # A proper .kicad_sym file (same format create_symbol tool emits) so that
    # sexpdata can parse it and _walk_pins() finds the pins.
    MINIMAL_SYM = """\
(kicad_symbol_lib
\t(version 20251024)
\t(generator "test")
\t(generator_version "1.0")
\t(symbol "TEST_E2E_PART"
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "U"
\t\t\t(at 0 3.81 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "TEST_E2E_PART"
\t\t\t(at 0 -3.81 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t(hide yes)
\t\t)
\t\t(property "Datasheet" ""
\t\t\t(at 0 0 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t(hide yes)
\t\t)
\t\t(symbol "TEST_E2E_PART_0_1"
\t\t\t(rectangle
\t\t\t\t(start -5.08 2.54)
\t\t\t\t(end 5.08 -2.54)
\t\t\t\t(stroke (width 0.254) (type default))
\t\t\t\t(fill (type background))
\t\t\t)
\t\t)
\t\t(symbol "TEST_E2E_PART_1_1"
\t\t\t(pin power_in line
\t\t\t\t(at -7.62 1.27 0)
\t\t\t\t(length 2.54)
\t\t\t\t(name "VCC"
\t\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t\t)
\t\t\t\t(number "1"
\t\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t\t)
\t\t\t)
\t\t\t(pin power_in line
\t\t\t\t(at -7.62 -1.27 0)
\t\t\t\t(length 2.54)
\t\t\t\t(name "GND"
\t\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t\t)
\t\t\t\t(number "2"
\t\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t\t)
\t\t\t)
\t\t\t(pin output line
\t\t\t\t(at 7.62 0 180)
\t\t\t\t(length 2.54)
\t\t\t\t(name "OUT"
\t\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t\t)
\t\t\t\t(number "3"
\t\t\t\t\t(effects (font (size 1.27 1.27)))
\t\t\t\t)
\t\t\t)
\t\t)
\t\t(embedded_fonts no)
\t)
)
"""
    with tempfile.TemporaryDirectory() as td:
        symdir = Path(td) / "TestE2E_Lib.kicad_symdir"
        symdir.mkdir()
        (symdir / "TEST_E2E_PART.kicad_sym").write_text(MINIMAL_SYM, encoding="utf-8")

        # Register the library
        r_reg, is_err_reg = parse(await _call({"path": str(symdir), "scope": "global"}))
        data_reg = json.loads(r_reg)
        registered_ok = not is_err_reg and len(data_reg.get("registered", [])) > 0
        check(registered_ok, "T10a add_symbol_library registered the path",
              str(data_reg.get("registered", []))[:80])

        # Now try to load the symbol — this is the REAL end-to-end check
        from envil_agent.kicad.symbol_geom import load_symbol
        try:
            geom = load_symbol("TestE2E_Lib:TEST_E2E_PART")
            pin_names = sorted(p.name for p in geom.pins)
            check(len(geom.pins) == 3 and pin_names == ["GND", "OUT", "VCC"],
                  "T10b load_symbol resolves 3 pins correctly after registration",
                  f"pins={pin_names}")
        except Exception as exc:
            check(False, "T10b load_symbol resolves symbol after registration", str(exc))

        # Cleanup table entry
        text = tbl_path.read_text(encoding="utf-8")
        clean = re.sub(r'\t\(lib \(name "TestE2E_Lib"\)[^\n]+\n', "", text)
        tbl_path.write_text(clean, encoding="utf-8")
        # Also clear caches so the deleted-temp-dir root doesn't persist in session
        from envil_agent.kicad import symbol_geom as sg
        sg._project_sym_roots = [r for r in sg._project_sym_roots
                                  if "TestE2E_Lib" not in r and td not in r]
        for fn in ("load_symbol", "_all_symbols", "resolve_lib_id_by_value",
                   "_discover_config_roots"):
            obj = getattr(sg, fn, None)
            if obj is not None and hasattr(obj, "cache_clear"):
                obj.cache_clear()

    # ── SUMMARY ─────────────────────────────────────────────────────────────
    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    print(f"\n{'='*50}")
    print(f"Result: {passed}/{total} passed")
    if passed < total:
        print("\nFailed tests:")
        for ok, label, detail in results:
            if not ok:
                print(f"  {FAIL}  {label}")
                if detail:
                    print(f"         {detail}")


asyncio.run(run())
