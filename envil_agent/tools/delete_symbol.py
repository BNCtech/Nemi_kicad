"""Tool: delete a custom KiCad symbol — the reverse of create_symbol.

The agent calls this when the user wants to remove a symbol they created (a
wrong pinout, a duplicate, an experiment). It deletes the .kicad_sym from disk,
removes the now-empty ``.kicad_symdir`` library folder, unregisters the library
from KiCad's global sym-lib-table, and refreshes the resolver caches so the
part stops resolving without a KiCad restart.

Safety: symbols that were NOT created by envil (stock KiCad parts / shared
multi-symbol libraries) are refused unless ``force=true`` so a stray call can't
wipe a standard library part.
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from ..kicad.edit_symbol import delete_symbol as _delete_symbol


@tool(
    name="delete_symbol",
    description=(
        "Delete a custom KiCad schematic symbol (.kicad_sym) — the reverse of "
        "create_symbol. Use this when the user wants to remove a symbol they "
        "created (wrong pinout, duplicate, no longer needed).\n\n"
        "It removes the symbol file from disk; if that empties the "
        "'<library>.kicad_symdir' folder, the folder is removed and the library "
        "is unregistered from KiCad's global symbol-library table. Resolver "
        "caches are refreshed so the part stops resolving immediately (KiCad "
        "itself must be restarted to update the GUI symbol chooser).\n\n"
        "SAFETY: a symbol that was not created by envil (a stock KiCad part, or "
        "one of many symbols in a shared library file) is refused unless "
        "force=true.\n\n"
        "Args:\n"
        "  lib_id: 'LibNick:PartName' to delete (required, e.g. 'Custom:BQ76952').\n"
        "  unregister: bool — also strip the library from KiCad's sym-lib-table "
        "when the delete empties it (default true).\n"
        "  force: bool — delete even a non-envil / stock symbol, or edit a "
        "shared multi-symbol library file (default false).\n\n"
        "Returns JSON: {ok, lib_id, removed, library_removed, unregister, note}."
    ),
    input_schema={
        "lib_id": str,
        "unregister": bool,
        "force": bool,
    },
)
async def delete_symbol(args: dict[str, Any]) -> dict[str, Any]:
    lib_id = str(args.get("lib_id", "")).strip()
    if not lib_id:
        return {
            "content": [{"type": "text", "text": "ERROR: lib_id is required."}],
            "is_error": True,
        }
    unregister = bool(args.get("unregister", True))
    force = bool(args.get("force", False))

    try:
        result = _delete_symbol(lib_id, unregister=unregister, force=force)
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

    return {
        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
        "is_error": False,
    }
