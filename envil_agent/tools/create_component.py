"""Tool: create a complete KiCad component (symbol + footprint) from a datasheet.

One prompt → one tool call → full component ready in KiCad:

  1. Fetch datasheet PDF  (ONE network call)
  2. ONE Claude call extracts: every pin (name, number, etype) + package dims
  3. Write .kicad_sym  to Custom.kicad_symdir/<part>.kicad_sym
  4. Write .kicad_mod  to kicad-fp-lib/Custom.pretty/<part>.kicad_mod
  5. Link symbol Footprint + Datasheet + MPN properties
  6. Register both libraries in sym-lib-table / fp-lib-table

Agent trigger examples
----------------------
- "create symbol for TMC2209, datasheet <url>"
- "add ICM-42688-P to my library"
- "create new component BQ25798 from TI datasheet"
- "create symbol and footprint for <part>"
- "I need a KiCad symbol for <part>"
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from ..kicad.create_symbol import create_symbol as _create_symbol
from ..kicad.create_footprint import create_footprint_from_info
from ..kicad.edit_symbol import apply_symbol_ops


@tool(
    name="create_component",
    description=(
        "Create a brand-new KiCad component from scratch using its datasheet. "
        "Reads the datasheet ONCE, then: "
        "(1) generates a schematic symbol (.kicad_sym) with all pins correctly typed, "
        "(2) generates a PCB footprint (.kicad_mod) with correct pads and courtyard, "
        "(3) links Footprint, Datasheet, and MPN properties on the symbol, "
        "(4) registers both libraries in KiCad's sym-lib-table and fp-lib-table. "
        "Use this when the part does NOT exist in the KiCad standard library. "
        "lib_id format: 'LibNick:PartName' — default nick is 'Custom'. "
        "datasheet_url is required: a direct link to the PDF datasheet. "
        "After this tool returns, tell the user to reload KiCad libraries "
        "(close and reopen KiCad, or Preferences > Manage Libraries > OK) "
        "then the symbol will appear under the Custom library ready to place."
    ),
    input_schema={
        "lib_id": str,        # e.g. "Custom:TMC2209"
        "datasheet_url": str, # direct PDF link
    },
)
async def create_component(args: dict[str, Any]) -> dict[str, Any]:
    lib_id = str(args.get("lib_id", "")).strip()
    datasheet_url = str(args.get("datasheet_url", "")).strip()

    if not lib_id:
        return {"content": [{"type": "text", "text": "ERROR: lib_id is required."}],
                "is_error": True}
    if ":" not in lib_id:
        lib_id = f"Custom:{lib_id}"
    if not datasheet_url:
        return {"content": [{"type": "text",
                              "text": "ERROR: datasheet_url is required."}],
                "is_error": True}

    nick, _, part = lib_id.partition(":")

    # ── Step 1 + 2: fetch datasheet, extract pins + package in one Claude call
    sym_result = _create_symbol(lib_id, datasheet_url, lib_nick=nick)
    if not sym_result.get("ok"):
        return {"content": [{"type": "text",
                              "text": f"ERROR (symbol): {sym_result.get('error')}"}],
                "is_error": True}

    info = sym_result["info"]
    pkg  = info.get("package", {})

    # ── Step 3: generate footprint from the ALREADY-EXTRACTED package info
    fp_result = create_footprint_from_info(lib_id, pkg, fp_lib_nick=nick, fp_name=part)
    if not fp_result.get("ok"):
        return {"content": [{"type": "text",
                              "text": f"ERROR (footprint): {fp_result.get('error')}"}],
                "is_error": True}

    # ── Step 4: link all properties on the symbol
    try:
        apply_symbol_ops(lib_id, [
            {"op": "set_property", "key": "Footprint",
             "value": fp_result["fp_ref"]},
            {"op": "set_property", "key": "Datasheet",
             "value": datasheet_url},
            {"op": "set_property", "key": "MPN",
             "value": info.get("part_name", part)},
            {"op": "set_property", "key": "Manufacturer",
             "value": info.get("manufacturer", "")},
            {"op": "set_property", "key": "Description",
             "value": info.get("description", "")},
        ])
    except Exception as exc:
        print(f"[create_component] property link failed: {exc}")

    pin_count = len(info.get("pins", []))
    pkg_type  = pkg.get("type", "?")
    pitch     = pkg.get("pitch_mm", "?")
    is_smd    = "SMD" if pkg.get("is_smd") else "THT"

    summary = (
        f"Component created: {lib_id}\n"
        f"Symbol : {sym_result['path']}  ({pin_count} pins)\n"
        f"Footprint: {fp_result['path']}\n"
        f"Package  : {pkg_type} {pkg.get('pin_count','?')}-pin, "
        f"pitch={pitch}mm, {is_smd}\n"
        f"Footprint property: {fp_result['fp_ref']}\n"
        f"\nReload KiCad (close + reopen, or Preferences > Manage Libraries > OK) "
        f"to see '{lib_id}' under the Custom library."
    )

    return {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "lib_id":    lib_id,
                "sym_path":  sym_result["path"],
                "fp_ref":    fp_result["fp_ref"],
                "fp_path":   fp_result["path"],
                "pin_count": pin_count,
                "package":   pkg,
                "summary":   summary,
            }, indent=2),
        }],
        "is_error": False,
    }
