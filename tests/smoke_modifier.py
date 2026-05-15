import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kicad_claude.schematic_extractor import SchematicExtractor
from kicad_claude.schematic_modifier import SchematicDocument, apply_operation


SRC = Path(r"C:/Users/Admin/Downloads/tps562219_regulator.kicad_sch")
OUT = Path(r"C:/Users/Admin/Downloads/tps562219_regulator.smoke.kicad_sch")


def main():
    if not SRC.exists():
        print(f"FATAL: source schematic missing: {SRC}")
        sys.exit(1)
    shutil.copy2(SRC, OUT)

    doc = SchematicDocument(OUT)
    before = SchematicExtractor(OUT).summary()
    print(f"BEFORE: {before['component_count']} components, {before['wire_count']} wires, {len(before['labels'])} labels")

    ops = [
        {"op": "add_component", "lib_id": "Device:C", "reference": "C99",
         "value": "100n", "x": 60.0, "y": 50.0, "footprint": "Capacitor_SMD:C_0603"},
        {"op": "edit_value", "reference": "R2", "new_value": "47k"},
        {"op": "add_label", "name": "TEST_NET", "x": 80.0, "y": 50.0, "kind": "label"},
        {"op": "add_wire", "points": [(60.0, 50.0), (60.0, 60.0)]},
    ]
    for op in ops:
        r = apply_operation(doc, op)
        tag = "OK" if r["ok"] else "FAIL"
        print(f"  [{tag}] {op['op']}: {r['message']}")

    doc.save()

    after = SchematicExtractor(OUT).summary()
    print(f"AFTER : {after['component_count']} components, {after['wire_count']} wires, {len(after['labels'])} labels")

    new_c99 = next((c for c in after["components"] if c.get("reference") == "C99"), None)
    new_r2  = next((c for c in after["components"] if c.get("reference") == "R2"), None)
    new_lab = next((l for l in after["labels"]    if l.get("name") == "TEST_NET"), None)

    assert new_c99, "C99 not present after save+reload"
    assert new_c99.get("value") == "100n", f"C99 value wrong: {new_c99}"
    assert new_r2 and new_r2.get("value") == "47k", f"R2 not edited: {new_r2}"
    assert new_lab, "TEST_NET label missing"

    bak = OUT.with_suffix(OUT.suffix + ".bak")
    print(f"  backup written: {bak.exists()} ({bak})")
    print("PASS")


if __name__ == "__main__":
    main()
