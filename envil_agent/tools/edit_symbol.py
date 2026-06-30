"""Tool: edit a KiCad symbol in the custom library via natural-language ops.

The agent translates the user's prompt (e.g. "rename pin 3 to PGND in
Timer:NE555") into a structured ``ops`` list and calls this tool.  The
tool applies each op to the .kicad_sym file on disk and returns per-op
results so the agent can report exactly what changed.

Supported operations (pass in ``ops`` list):

  rename_pin          rename a pin's display name by its number
  rename_pin_number   change a pin's number (pad mapping)
  change_pin_etype    set pin electrical type (passive, power_in, input …)
  move_pin            change a pin's (x, y, rotation) in symbol coordinates
  change_pin_length   change pin stub length
  set_property        set or create a symbol-level property (MPN, Datasheet …)
  remove_pin          delete a pin by number or name
  add_pin             insert a new pin with full attributes

Examples the agent should emit
-------------------------------
Rename GND pin:
  {"lib_id": "Timer:NE555",
   "ops": [{"op": "rename_pin", "number": "1", "new_name": "PGND"}]}

Add a NC pin:
  {"lib_id": "Device:R",
   "ops": [{"op": "add_pin", "number": "3", "name": "NC",
             "etype": "no_connect", "x": 0, "y": -7.62, "rot": 270}]}

Set MPN property:
  {"lib_id": "Timer:NE555",
   "ops": [{"op": "set_property", "key": "MPN", "value": "LM555CN"}]}
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from ..kicad.edit_symbol import apply_symbol_ops


@tool(
    name="edit_symbol",
    description=(
        "Edit a KiCad symbol directly in the local .kicad_sym library file. "
        "Use this when the user wants to change a pin name, pin number, pin type, "
        "pin position, symbol property (MPN / Datasheet / Description / Manufacturer), "
        "or when they want to add or remove a pin. "
        "Provide lib_id in 'LibNick:PartName' form (e.g. 'Timer:NE555') "
        "and a list of ops — each op is a JSON object with an 'op' key plus "
        "op-specific fields. "
        "Valid op values: rename_pin, rename_pin_number, change_pin_etype, "
        "move_pin, change_pin_length, set_property, add_pin, remove_pin. "
        "The tool writes the change to disk and clears the symbol cache "
        "so subsequent schematic builds see the updated symbol."
    ),
    input_schema={
        "lib_id": str,
        "ops": list,
    },
)
async def edit_symbol(args: dict[str, Any]) -> dict[str, Any]:
    lib_id = str(args.get("lib_id", "")).strip()
    ops = args.get("ops")

    if not lib_id:
        return {
            "content": [{"type": "text", "text": "ERROR: lib_id is required."}],
            "is_error": True,
        }
    if not ops:
        return {
            "content": [{"type": "text", "text": "ERROR: ops list is empty or missing."}],
            "is_error": True,
        }
    if not isinstance(ops, list):
        return {
            "content": [{"type": "text", "text": "ERROR: ops must be a list of operation dicts."}],
            "is_error": True,
        }

    try:
        results = apply_symbol_ops(lib_id, ops)
    except ValueError as exc:
        return {
            "content": [{"type": "text", "text": f"ERROR: {exc}"}],
            "is_error": True,
        }
    except Exception as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: {type(exc).__name__}: {exc}"}],
            "is_error": True,
        }

    any_ok = any(r["ok"] for r in results)
    summary_lines = [
        f"[{'OK' if r['ok'] else 'FAIL'}] {r['op']}: {r['msg']}"
        for r in results
    ]

    return {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "lib_id": lib_id,
                "results": results,
                "summary": "\n".join(summary_lines),
            }, indent=2),
        }],
        "is_error": not any_ok,
    }
