"""Tool: generate_pcb — the standalone schematic->PCB bridge (the "F8" the AI
can press itself).

`build_circuit` writes a populated `.kicad_pcb` as part of a build, but its
ERC-clean gate SKIPS that step when the schematic still has an ERC error. When
the error is then cleaned (by erc_autofix or a later edit), the board is left
EMPTY and the only recovery used to be telling the user to press F8 in KiCad.

This tool closes that gap: it reloads the full IR persisted next to the
schematic (`<basename>.envil-ir.json`, written by build_circuit) and
(re)populates the `.kicad_pcb` with footprints + nets, then runs the same
rule-driven finish pipeline (place + de-collide + rotation/rail-filter,
outline, GND pour, thermal vias, mounting holes, silkscreen cleanup, DRC
auto-fix, verify) via `auto_layout_pcb`. The agent calls it once ERC is clean
so the board is never left empty — no manual F8, no running KiCad needed.

Universal and config-driven; never raises (a failure is reported, not thrown).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from claude_agent_sdk import tool


def _resolve_paths(args: Dict[str, Any]) -> Dict[str, str]:
    """Accept either a .kicad_sch or a .kicad_pcb path; return both + the IR
    sidecar path. Empty strings when a path can't be derived."""
    raw = str(args.get("sch_path") or args.get("pcb_path")
              or args.get("path") or "").strip()
    if not raw:
        return {"sch": "", "pcb": "", "ir": ""}
    p = Path(raw).expanduser()
    if p.suffix.lower() == ".kicad_pcb":
        sch = p.with_suffix(".kicad_sch")
    else:
        sch = p if p.suffix.lower() == ".kicad_sch" else p.with_suffix(".kicad_sch")
    return {
        "sch": str(sch),
        "pcb": str(sch.with_suffix(".kicad_pcb")),
        "ir": str(sch.with_suffix(".envil-ir.json")),
    }


@tool(
    name="generate_pcb",
    description=(
        "Schematic -> PCB bridge (the F8 'Update PCB from Schematic' the AI runs "
        "itself). Populates the .kicad_pcb with footprints + nets from the "
        "schematic, then runs the full rule-driven layout finish (place + rotate "
        "+ de-collide, outline, GND pour, thermal vias, mounting holes, silk "
        "cleanup, DRC auto-fix, verify). USE THIS when the PCB is empty because "
        "build_circuit's ERC gate skipped it and ERC is now clean — instead of "
        "telling the user to press F8. Needs the <basename>.envil-ir.json sidecar "
        "build_circuit wrote next to the schematic.\n"
        'Args: {"sch_path": "C:/.../proj.kicad_sch"}   (a .kicad_pcb path also works)'
    ),
    input_schema={"sch_path": str},
)
async def generate_pcb(args: dict[str, Any]) -> dict[str, Any]:
    paths = _resolve_paths(args)
    if not paths["sch"]:
        return {"content": [{"type": "text",
                             "text": "ERROR: provide sch_path (the .kicad_sch)."}],
                "is_error": True}

    # ---- Get the IR: prefer the build_circuit sidecar, else read the
    # schematic itself via KiCad's netlister (works for ANY schematic, even
    # one open in KiCad). The sidecar is preferred only because it also carries
    # functional-block hints for nicer placement. ----
    ir = None
    ir_source = ""
    ir_path = Path(paths["ir"])
    if ir_path.exists():
        try:
            from ..intent.ir import TopologyIR
            payload = json.loads(ir_path.read_text(encoding="utf-8"))
            ir = TopologyIR.from_dict(payload.get("ir") or payload)
            ir_source = "sidecar"
        except Exception:                                   # noqa: BLE001
            ir = None
    if ir is None:
        if not Path(paths["sch"]).exists():
            return {"content": [{"type": "text", "text": (
                f"ERROR: schematic not found ({Path(paths['sch']).name}).")}],
                "is_error": True}
        try:
            from ..kicad.netlist_ir import schematic_to_ir
            ir = await schematic_to_ir(paths["sch"])
            ir_source = "schematic-netlist"
        except Exception:                                   # noqa: BLE001
            ir = None
    if ir is None or not getattr(ir, "components", None):
        return {"content": [{"type": "text", "text": (
            "ERROR: could not read the schematic's components/nets "
            "(no IR sidecar and the KiCad netlister returned nothing). "
            "Rebuild the circuit, or press F8 in KiCad to update the PCB.")}],
            "is_error": True}

    try:
        from ..layout.pcb_gen import generate_pcb_from_ir
        gen = generate_pcb_from_ir(ir, paths["sch"])
    except Exception as exc:                                # noqa: BLE001
        return {"content": [{"type": "text",
                             "text": f"ERROR: PCB generation failed: "
                                     f"{type(exc).__name__}: {exc}"}],
                "is_error": True}
    if gen.get("error"):
        return {"content": [{"type": "text",
                             "text": f"ERROR: {gen['error']}"}],
                "is_error": True}

    pcb_path = gen.get("pcb_path", paths["pcb"])
    placed = gen.get("footprints_placed", 0)
    missing = gen.get("footprints_missing", []) or []

    # ---- Run the rule-driven finish (same orchestrator build_circuit uses) ----
    finish_summary: Dict[str, Any] = {}
    try:
        from ..intent.engine import _load_layout_config as _llc
        _pcbgen_cfg = (_llc() or {}).get("pcb_gen", {}) or {}
    except Exception:
        _pcbgen_cfg = {}
    if placed and bool(_pcbgen_cfg.get("finish_board", True)):
        try:
            from .auto_layout_pcb import auto_layout_pcb as _alp
            _skip = list(_pcbgen_cfg.get("finish_skip_tools",
                                         ["route_pcb_simple"]) or [])
            _alr = await _alp.handler({"pcb_path": pcb_path, "skip": _skip})
            finish_summary = {
                "steps_ok": _alr.get("steps_ok"),
                "steps_total": _alr.get("steps_total"),
                "skipped": _skip,
            }
        except Exception as exc:                            # noqa: BLE001
            finish_summary = {"error": f"{type(exc).__name__}: {exc}"}

    # JSON result with `path` so the canvas auto-reloads (see kicad_autorefresh).
    result = {
        "ok": True,
        "action": "generate_pcb",
        "path": pcb_path,
        "footprints_placed": placed,
        "pads_netted": gen.get("pads_netted", 0),
        "nets": gen.get("nets", 0),
        "footprints_missing": missing,
        "ir_source": ir_source,
        "finish": finish_summary,
        "note": (f"PCB populated: {placed} footprints"
                 + (f", {len(missing)} missing" if missing else "")
                 + ". Board updated from the schematic."),
    }
    return {"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "ok": True, "path": pcb_path}
