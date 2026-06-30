"""Tool: generate a .kicad_mod footprint from a component datasheet.

The agent calls this when the user wants to create the PCB footprint for a
component that already has a symbol in the local library.  The tool reads the
symbol's Datasheet URL, asks Claude to extract the package dimensions, then
generates a standards-compliant .kicad_mod and saves it to
  fp_lib_dir()/<fp_lib_nick>.pretty/<fp_name>.kicad_mod

It also updates the symbol's Footprint property so the schematic and the
new footprint are linked automatically.

Supported packages
------------------
DIP / SIP (through-hole), SOIC / TSSOP / SSOP (2-row SMD),
SOT-23 (3/5/6 pin), QFP / TQFP / LQFP (4-side SMD),
QFN / DFN (4-side no-lead, optional thermal pad).

Agent trigger examples
----------------------
- "create footprint for Timer:NE555"
- "make footprint for MyLib:ATmega328P"
- "generate PCB footprint for Power:LM7805"
- "draw footprint for Custom:MyIC, save as SOIC8 in MyParts library"
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from ..kicad.create_footprint import create_footprint as _create_footprint


@tool(
    name="create_footprint",
    description=(
        "Generate a KiCad PCB footprint (.kicad_mod) for a component by reading "
        "its datasheet. The tool fetches the Datasheet URL from the symbol's "
        "properties, sends it to Claude to extract package dimensions "
        "(pad size, pitch, body outline, courtyard), then writes the .kicad_mod "
        "file to the local footprint library. "
        "It also updates the symbol's Footprint property to link schematic and PCB. "
        "Supported packages: DIP, SIP, SOIC, TSSOP, SSOP, SOT-23, QFP, TQFP, QFN, DFN. "
        "lib_id format: 'LibNick:PartName' (e.g. 'Timer:NE555'). "
        "fp_lib_nick and fp_name are optional — they default to the symbol's "
        "library nick and part name."
    ),
    input_schema={
        "lib_id": str,
        "fp_lib_nick": str,
        "fp_name": str,
        "datasheet_url": str,
    },
)
async def create_footprint(args: dict[str, Any]) -> dict[str, Any]:
    lib_id = str(args.get("lib_id", "")).strip()
    if not lib_id:
        return {
            "content": [{"type": "text", "text": "ERROR: lib_id is required."}],
            "is_error": True,
        }

    fp_lib_nick = str(args.get("fp_lib_nick", "")).strip()
    fp_name = str(args.get("fp_name", "")).strip()
    datasheet_url = str(args.get("datasheet_url", "")).strip()

    try:
        result = _create_footprint(
            lib_id=lib_id,
            fp_lib_nick=fp_lib_nick,
            fp_name=fp_name,
            datasheet_url=datasheet_url,
            link_symbol=True,
        )
    except Exception as exc:
        return {
            "content": [{"type": "text",
                         "text": f"ERROR: {type(exc).__name__}: {exc}"}],
            "is_error": True,
        }

    if not result.get("ok"):
        return {
            "content": [{"type": "text", "text": f"ERROR: {result.get('error', 'unknown')}"}],
            "is_error": True,
        }

    pkg = result.get("package", {})
    summary = (
        f"Footprint created: {result['fp_ref']}\n"
        f"File: {result['path']}\n"
        f"Package: {pkg.get('package_type', '?')} "
        f"{pkg.get('pin_count', '?')}-pin, "
        f"pitch={pkg.get('pitch_mm', '?')} mm, "
        f"{'SMD' if pkg.get('is_smd') else 'THT'}\n"
        f"Symbol Footprint property updated to: {result['fp_ref']}"
    )

    return {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "lib_id": lib_id,
                "fp_ref": result["fp_ref"],
                "path": result["path"],
                "package": pkg,
                "summary": summary,
            }, indent=2),
        }],
        "is_error": False,
    }
