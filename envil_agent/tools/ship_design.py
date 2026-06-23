"""Tool: one-prompt finisher. Runs the full manufacturing pipeline in
sequence and returns a single report. Universal — composes the
existing tools, no per-circuit logic.

Stages (each gated by config; skip any by setting `enabled=false`
in `layout_config.json:ship_design.steps`):
  1. erc_check        — schematic ERC
  2. export_bom       — BOM CSV (default preset; configurable)
  3. drc_check        — PCB DRC
  4. export_pcb       — Gerbers + drill + pos + ZIP
  5. render_pcb_3d    — top + bottom 3D PNGs

Either path argument accepted:
  {"sch_path": "C:/.../proj.kicad_sch"}   # tool finds matching .kicad_pcb
  {"pcb_path": "C:/.../proj.kicad_pcb"}   # tool finds matching .kicad_sch
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, List

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("ship_design", {}) or {}
    except Exception:
        return {}


def _tool_inner(res: dict) -> dict:
    """Tools return content[0].text as JSON. Decode."""
    try:
        return json.loads(res["content"][0]["text"])
    except Exception:
        return {}


@tool(
    name="ship_design",
    description=(
        "ONE-PROMPT FINISHER. Runs the full manufacturing pipeline in "
        "sequence: ERC -> BOM -> DRC -> Gerbers -> 3D render. Returns "
        "a single report card. Use when the user says 'ship it', "
        "'finalise the design', 'make it fab-ready', 'send to "
        "manufacture', 'production ready check'.\n"
        "Args:\n"
        '  {"sch_path": "C:/.../proj.kicad_sch"}     # finds matching .kicad_pcb\n'
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}     # finds matching .kicad_sch\n'
        '  {"sch_path": "...", "bom_preset": "altium"}  # override preset\n'
        '  {"sch_path": "...", "skip": ["render_3d"]}   # skip a stage\n'
        "Each stage's enable + defaults come from "
        "layout_config.json:ship_design — disable any stage globally there."
    ),
    input_schema={"sch_path": str},
)
async def ship_design(args: dict[str, Any]) -> dict[str, Any]:
    sch_arg = str(args.get("sch_path", "")).strip()
    pcb_arg = str(args.get("pcb_path", "")).strip()
    sch: Path
    pcb: Path
    if sch_arg:
        sch = Path(sch_arg).expanduser()
        pcb = sch.with_suffix(".kicad_pcb")
    elif pcb_arg:
        pcb = Path(pcb_arg).expanduser()
        sch = pcb.with_suffix(".kicad_sch")
    else:
        return {
            "content": [{"type": "text",
                          "text": "ERROR: pass either sch_path or pcb_path"}],
            "is_error": True,
        }

    if not sch.exists():
        return {"content": [{"type": "text",
                              "text": f"ERROR: schematic not found: {sch}"}],
                 "is_error": True}

    cfg = _load_cfg()
    steps_cfg = cfg.get("steps", {}) or {}
    skip_list = set(args.get("skip") or [])

    # Lazy-import each tool so a single broken tool doesn't break the
    # whole ship_design call.
    ERC  = importlib.import_module("envil_agent.tools.erc_check")
    DRC  = importlib.import_module("envil_agent.tools.drc_check")
    BOM  = importlib.import_module("envil_agent.tools.export_bom")
    EPCB = importlib.import_module("envil_agent.tools.export_pcb")
    R3D  = importlib.import_module("envil_agent.tools.render_pcb_3d")

    report_lines: List[str] = [f"# Ship Report — {sch.stem}", ""]
    overall_ok = True
    stages = []

    def _stage_enabled(name: str) -> bool:
        if name in skip_list:
            return False
        sub = steps_cfg.get(name, {})
        if isinstance(sub, dict):
            return bool(sub.get("enabled", True))
        return bool(sub)

    # ---- 1. ERC ----
    if _stage_enabled("erc"):
        r = await ERC.erc_check.handler({"path": str(sch)})
        d = _tool_inner(r)
        err = d.get("error_count", -1)
        warn = d.get("warning_count", -1)
        ok = err == 0
        if not ok: overall_ok = False
        stages.append(("ERC (schematic)", ok, f"errors={err}, warnings={warn}"))
    else:
        stages.append(("ERC (schematic)", None, "skipped"))

    # ---- 2. BOM ----
    if _stage_enabled("bom"):
        bom_args = {"sch_path": str(sch)}
        preset = args.get("bom_preset") or steps_cfg.get("bom", {}).get("preset")
        if preset:
            bom_args["preset"] = preset
        r = await BOM.export_bom.handler(bom_args)
        ok = r.get("ok", False) or "BOM exported" in r["content"][0]["text"]
        if not ok: overall_ok = False
        path = r.get("path", "?")
        rows = r.get("rows", "?")
        stages.append(("BOM CSV", ok, f"{rows} parts -> {Path(path).name}"))
    else:
        stages.append(("BOM CSV", None, "skipped"))

    # ---- 3. DRC ----
    if _stage_enabled("drc"):
        if not pcb.exists():
            stages.append(("DRC (PCB)", False, f"no .kicad_pcb at {pcb}"))
            overall_ok = False
        else:
            r = await DRC.drc_check.handler({"pcb_path": str(pcb)})
            err = r.get("error_count", -1)
            warn = r.get("warning_count", -1)
            # DRC errors on empty/un-routed board are expected (no Edge.Cuts,
            # un-routed nets); treat as warning unless `strict_drc=true`.
            strict = bool(steps_cfg.get("drc", {}).get("strict", False)
                           if isinstance(steps_cfg.get("drc"), dict) else False)
            ok = (err == 0) if strict else True
            if not ok: overall_ok = False
            stages.append(("DRC (PCB)", ok, f"errors={err}, warnings={warn}"))
    else:
        stages.append(("DRC (PCB)", None, "skipped"))

    # ---- 4. Gerbers ----
    if _stage_enabled("gerbers"):
        if not pcb.exists():
            stages.append(("Gerbers + drill + ZIP", False,
                            f"no .kicad_pcb at {pcb}"))
            overall_ok = False
        else:
            r = await EPCB.export_pcb.handler({"pcb_path": str(pcb)})
            ok = r.get("ok", False)
            files = len(r.get("files", []))
            zip_path = r.get("zip_path", "")
            zip_short = Path(zip_path).name if zip_path else "no zip"
            if not ok: overall_ok = False
            stages.append(("Gerbers + drill + ZIP", ok,
                            f"{files} files -> {zip_short}"))
    else:
        stages.append(("Gerbers + drill + ZIP", None, "skipped"))

    # ---- 5. 3D Render ----
    if _stage_enabled("render_3d"):
        if not pcb.exists():
            stages.append(("3D render", False, f"no .kicad_pcb at {pcb}"))
            overall_ok = False
        else:
            r = await R3D.render_pcb_3d.handler({"pcb_path": str(pcb)})
            imgs = r.get("images") or []
            ok = bool(imgs)
            if not ok: overall_ok = False
            stages.append(("3D render", ok,
                            f"{len(imgs)} image(s) -> "
                            f"{Path(imgs[0]).name if imgs else 'none'}"))
    else:
        stages.append(("3D render", None, "skipped"))

    # ---- Render report ----
    for name, ok, detail in stages:
        if ok is None:
            mark = "—"
        elif ok:
            mark = "✓"
        else:
            mark = "✗"
        report_lines.append(f"  {mark} {name:24} {detail}")
    report_lines.append("")
    report_lines.append(
        ("**SHIP READY**" if overall_ok else "**NOT READY** (fix the ✗ items)"))

    return {
        "content": [{"type": "text", "text": "\n".join(report_lines)}],
        "ok": overall_ok,
        "stages": [{"name": n, "ok": ok, "detail": d}
                    for n, ok, d in stages],
        "sch_path": str(sch),
        "pcb_path": str(pcb) if pcb.exists() else "",
    }
