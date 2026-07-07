"""Manual test suite for editing schematic-embedded (manually added) symbols.

A symbol the user adds manually in eeschema often exists ONLY inside the
schematic's (lib_symbols ...) block — no .kicad_sym file anywhere. These
tests verify the edit_symbol tool can find and edit that embedded copy.

Run directly:  python tests/test_edit_embedded_symbol.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envil_agent.tools.edit_symbol import edit_symbol
from envil_agent.kicad.edit_symbol import _locate_embedded_symbol, _schematic_files

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


_call = edit_symbol.handler

# A minimal schematic with one manually-added symbol embedded in
# lib_symbols and NOT present in any on-disk library. The embedded
# definition is keyed by the FULL lib_id, units by "<part>_<u>_<s>".
SCH_TEXT = """(kicad_sch
\t(version 20250114)
\t(generator "eeschema")
\t(generator_version "9.0")
\t(uuid "11111111-2222-3333-4444-555555555555")
\t(paper "A4")
\t(lib_symbols
\t\t(symbol "ManualLib_EnvilTest:FAKE_MANUAL_PART" (pin_numbers hide) (pin_names (offset 1.016)) (exclude_from_sim no) (in_bom yes) (on_board yes) (property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27)))) (property "Value" "FAKE_MANUAL_PART" (at 0 -5.08 0) (effects (font (size 1.27 1.27)))) (symbol "FAKE_MANUAL_PART_0_1" (rectangle (start -5.08 2.54) (end 5.08 -2.54) (stroke (width 0.254) (type default)) (fill (type background)))) (symbol "FAKE_MANUAL_PART_1_1" (pin passive line (at -7.62 0 0) (length 2.54) (name "A" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27))))) (pin passive line (at 7.62 0 180) (length 2.54) (name "B" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))))
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


async def run():
    with tempfile.TemporaryDirectory() as td:
        sch = Path(td) / "manual_test.kicad_sch"
        sch.write_text(SCH_TEXT, encoding="utf-8")

        # ── T1: locator finds the embedded symbol by exact lib_id ────────────
        found = _locate_embedded_symbol(
            "ManualLib_EnvilTest:FAKE_MANUAL_PART", _schematic_files(str(td)))
        check(found is not None, "T1 embedded locator: exact lib_id")

        # ── T2: locator falls back to a unique bare part-name match ──────────
        found = _locate_embedded_symbol(
            "Custom:FAKE_MANUAL_PART", [sch])
        check(found is not None, "T2 embedded locator: part-name fallback")

        # ── T3: edit (rename_pin) lands in the schematic file ────────────────
        r, is_err = parse(await _call({
            "lib_id": "ManualLib_EnvilTest:FAKE_MANUAL_PART",
            "schematic_path": str(sch),
            "ops": [{"op": "rename_pin", "number": "1", "new_name": "IN"}],
        }))
        data = json.loads(r) if not is_err else {}
        edited = sch.read_text(encoding="utf-8")
        ok = (not is_err
              and any(x["op"] == "rename_pin" and x["ok"]
                      for x in data.get("results", []))
              and any(x["op"] == "target" for x in data.get("results", []))
              and '"IN"' in edited)
        check(ok, "T3 rename_pin edits the embedded definition on disk",
              r[:160])

        # T3b: file still parses as a schematic and keeps its header ints
        check("(version 20250114" in edited and "(kicad_sch" in edited,
              "T3b schematic header survives the rewrite")

        # ── T4: set_property on the embedded symbol ──────────────────────────
        r, is_err = parse(await _call({
            "lib_id": "ManualLib_EnvilTest:FAKE_MANUAL_PART",
            "schematic_path": str(sch),
            "ops": [{"op": "set_property", "key": "MPN", "value": "XYZ-1"}],
        }))
        edited = sch.read_text(encoding="utf-8")
        check(not is_err and '"XYZ-1"' in edited,
              "T4 set_property lands in the schematic", r[:120])

        # ── T5: missing symbol + no schematic -> error mentions the hint ─────
        r, is_err = parse(await _call({
            "lib_id": "ManualLib_EnvilTest:DOES_NOT_EXIST_ANYWHERE",
            "ops": [{"op": "rename_pin", "number": "1", "new_name": "X"}],
        }))
        check(is_err and "schematic_path" in r,
              "T5 not-found without schematic -> hint to pass schematic_path",
              r[:160])

        # ── T6: missing symbol + schematic that lacks it -> combined error ───
        r, is_err = parse(await _call({
            "lib_id": "ManualLib_EnvilTest:DOES_NOT_EXIST_ANYWHERE",
            "schematic_path": str(sch),
            "ops": [{"op": "rename_pin", "number": "1", "new_name": "X"}],
        }))
        check(is_err and "lib_symbols" in r,
              "T6 not found in library nor schematic -> combined error",
              r[:160])

    n_fail = sum(1 for ok, *_ in results if not ok)
    print(f"\n{len(results) - n_fail}/{len(results)} passed")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
