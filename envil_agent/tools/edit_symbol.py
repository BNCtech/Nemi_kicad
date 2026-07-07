"""Tool: edit a KiCad symbol via natural-language ops.

The agent translates the user's prompt (e.g. "rename pin 3 to PGND in
Timer:NE555") into a structured ``ops`` list and calls this tool.  The
tool applies each op to the .kicad_sym file on disk and returns per-op
results so the agent can report exactly what changed.

Edit targets, in order:
  1. a CUSTOM .kicad_sym library file (created by create_symbol or the
     user's own library) — the edit persists for every future schematic;
  2. the definition embedded in the schematic's ``(lib_symbols ...)``
     block — the ONLY copy that exists for a symbol the user added
     manually in eeschema, and the copy edited for stock KiCad symbols
     (whose library files are read-only).  Requires ``schematic_path``.

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
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from ..kicad.edit_symbol import (
    apply_symbol_ops,
    _locate_symbol,
    _locate_embedded_symbol,
    _schematic_files,
)
from ..kicad.symbol_geom import inject_project_sym_roots


def _is_kicad_std_root(root: Path) -> bool:
    """True when `root` is a KiCad-shipped standard library directory."""
    norm = str(root).replace("\\", "/").lower()
    return ("program files/kicad" in norm or
            "/documents/kicad/" in norm)


def _classify_symbol(lib_id: str) -> tuple[str, str]:
    """Where does `lib_id` resolve to on disk?

    Returns (kind, info):
      ("custom",   path)  — in a user-writable library file: edit it there.
      ("standard", path)  — in a KiCad-shipped stock library: file is
                             read-only; only a schematic-embedded copy may
                             be edited.
      ("missing",  error) — in NO library file at all: the symbol may still
                             exist embedded in the schematic's
                             (lib_symbols ...) block — the case for a symbol
                             the user added manually in eeschema.
    """
    try:
        path, _, _ = _locate_symbol(lib_id)
    except ValueError as exc:
        return "missing", str(exc)
    for parent in path.parents:
        if _is_kicad_std_root(parent):
            return "standard", str(path)
    return "custom", str(path)


@tool(
    name="edit_symbol",
    description=(
        "Edit a KiCad symbol — in the local .kicad_sym library file, or in "
        "the schematic's embedded (lib_symbols ...) definition when the "
        "symbol has no library file. Symbols in CUSTOM libraries (created by "
        "create_symbol or placed in the user's own library) are edited in "
        "the library file. Symbols the user added MANUALLY in eeschema often "
        "exist ONLY inside the open .kicad_sch — pass schematic_path so the "
        "tool can find and edit that embedded definition. KiCad standard "
        "library files (Device:R, Timer:NE555, etc.) are never modified: if "
        "such a symbol is placed in the schematic, its embedded copy is "
        "edited instead (project-local change). "
        "Use this when the user wants to change a pin name, pin number, pin type, "
        "pin position, symbol property (MPN / Datasheet / Description / Manufacturer), "
        "or when they want to add or remove a pin. "
        "Provide lib_id in 'LibNick:PartName' form (e.g. 'Custom:RT9013') "
        "and a list of ops — each op is a JSON object with an 'op' key plus "
        "op-specific fields. "
        "Valid op values: rename_pin, rename_pin_number, change_pin_etype, "
        "move_pin, change_pin_length, set_property, add_pin, remove_pin. "
        "The tool writes the change to disk and clears the symbol cache "
        "so subsequent schematic builds see the updated symbol. "
        "ALWAYS pass schematic_path (path of the open .kicad_sch) when a "
        "schematic is open — it enables editing manually-added symbols and "
        "keeps the schematic's embedded copy in sync after a library edit. "
        "Optional: pass project_path (path to the .kicad_pro file or project "
        "folder) so the tool can discover symbols in the project's local "
        "sym-lib-table — required when the symbol lives in a project-specific "
        "library that was added via KiCad's 'Project Specific Libraries' tab."
    ),
    input_schema={
        "lib_id": str,
        "ops": list,
        "project_path": str,
        "schematic_path": str,
    },
)
async def edit_symbol(args: dict[str, Any]) -> dict[str, Any]:
    lib_id = str(args.get("lib_id", "")).strip()
    ops = args.get("ops")
    project_path = str(args.get("project_path") or "").strip()
    schematic_path = str(args.get("schematic_path") or "").strip()
    if project_path:
        inject_project_sym_roots(project_path)
    # A project path still lets us find the project's .kicad_sch files for
    # the embedded-symbol fallback when no explicit schematic was given.
    sch_hint = schematic_path or project_path

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

    kind, info = _classify_symbol(lib_id)
    target = "auto"
    if kind == "standard":
        # The stock .kicad_sym file must never be modified. If the symbol is
        # placed in the schematic, eeschema embedded a copy of its definition
        # there — editing THAT is a safe, project-local change.
        try:
            embedded = _locate_embedded_symbol(lib_id, _schematic_files(sch_hint))
        except ValueError as exc:
            return {
                "content": [{"type": "text", "text": f"ERROR: {exc}"}],
                "is_error": True,
            }
        if embedded is None:
            return {
                "content": [{"type": "text", "text": (
                    f"ERROR: {lib_id!r} is a KiCad standard library symbol "
                    f"({info}). Editing the stock library file is not allowed, "
                    f"and no copy of it was found embedded in the schematic. "
                    f"Pass schematic_path (the open .kicad_sch that uses this "
                    f"symbol) to edit its embedded copy, or use create_symbol "
                    f"to make a custom copy in your own library first."
                )}],
                "is_error": True,
            }
        target = "schematic"
    # kind == "missing": leave target="auto" — apply_symbol_ops falls back to
    # the schematic-embedded definition (a manually-added symbol) and raises
    # a combined library+schematic error when neither exists.

    try:
        results = apply_symbol_ops(lib_id, ops,
                                   schematic_path=sch_hint, target=target)
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
