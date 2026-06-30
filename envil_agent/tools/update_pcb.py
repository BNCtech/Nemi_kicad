"""Tool: update_pcb — the native KiCad "Update PCB from Schematic" (the real F8).

This is DIFFERENT from `generate_pcb`, and the difference is the whole point:

  - generate_pcb  -> REGENERATES the board from the IR and AUTO-PLACES every
                     footprint from scratch + re-runs the finish (outline, GND
                     pour, vias, …). Correct only for an EMPTY / brand-new board.
                     On an existing or hand-laid-out board it DISCARDS the user's
                     placement, routing and zones — which is why "AI update PCB"
                     and "manual F8" produced different boards.

  - update_pcb    -> triggers KiCad's OWN incremental ECO sync
                     (BOARD_NETLIST_UPDATER via eeschema's OnUpdatePCB), exactly
                     like pressing F8: it ADDS new footprints, REMOVES deleted
                     ones, and UPDATES nets / values / footprints on existing
                     footprints while PRESERVING their placement, routing and
                     zones. Byte-for-byte what the user gets pressing F8.

So "update the PCB" / "push to PCB" / "sync the board" on an existing board must
use THIS tool, not generate_pcb.

The sync itself runs inside eeschema (it owns the schematic netlist + the
per-symbol footprint assignments); this tool returns a signal the chat server
forwards as the `update_pcb_from_schematic` IPC action. It therefore needs the
project open in the running app — there is no headless equivalent (kicad-cli
has no "update pcb from schematic" subcommand). When the app is NOT running,
fall back to generate_pcb (initial population) instead.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool


@tool(
    name="update_pcb",
    description=(
        "Update PCB from Schematic — KiCad's native F8 sync. Pushes the "
        "schematic netlist to the board INCREMENTALLY: adds/removes/updates "
        "footprints + nets while PRESERVING existing placement, routing and "
        "zones (identical to pressing F8 in KiCad). USE THIS whenever the user "
        "asks to 'update the PCB', 'push to PCB', 'sync the board', or after "
        "editing an existing / hand-drawn board. Do NOT use generate_pcb for "
        "this — generate_pcb regenerates + auto-places from scratch and discards "
        "manual placement; use it only for an EMPTY board's first population. "
        "Needs the project open in the running app.\n"
        'Args: {"sch_path": "C:/.../proj.kicad_sch"}  (a .kicad_pcb path also works)'
    ),
    input_schema={"sch_path": str},
)
async def update_pcb(args: dict[str, Any]) -> dict[str, Any]:
    raw = str(args.get("sch_path") or args.get("pcb_path")
              or args.get("path") or "").strip()
    if not raw:
        return {"content": [{"type": "text",
                             "text": "ERROR: provide sch_path (the .kicad_sch)."}],
                "is_error": True}

    p = Path(raw).expanduser()
    sch = p if p.suffix.lower() == ".kicad_sch" else p.with_suffix(".kicad_sch")
    if not sch.exists():
        return {"content": [{"type": "text", "text": (
            f"ERROR: schematic not found ({sch.name}). Save the schematic "
            "first, then update the PCB.")}],
            "is_error": True}

    # Signal: the chat server sees action == "update_pcb_native" in this result
    # and broadcasts the `update_pcb_from_schematic` IPC. eeschema's OnUpdatePCB
    # then runs the native, placement-preserving sync inside the app.
    result = {
        "ok": True,
        "action": "update_pcb_native",
        "sch_path": str(sch),
        "note": ("Updating the PCB from the schematic the native way "
                 "(F8 incremental sync). Existing footprint placement, routing "
                 "and zones are preserved; only the netlist diff is applied — "
                 "same as pressing F8 in KiCad."),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "ok": True, "action": "update_pcb_native", "sch_path": str(sch)}
