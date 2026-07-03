"""Tool: pipeline_gate — the capstone go/no-go for the whole PCB flow.

Runs every validation gate (steps 3-8) in flow order and returns ONE verdict:
GO if all applicable gates pass, NO-GO with the first blocking stage otherwise.
This is the "every stage ends in a hard pass/fail before the flow advances"
spine from the master flow — a single call the user (or an orchestrator) makes
to know whether a project is clear to advance, and exactly where it's stuck.

Read-only: it only invokes the individual read-only gates
(footprint_audit ... power_audit), each of which resolves its own artifact
(.kicad_sch / .kicad_pro / .kicad_pcb) from the project. A gate that finds no
artifact yet (e.g. no board) is reported "not applicable", not a failure — so
a schematic-only project passes 3-4 and shows the rest as pending. Verdict is
in words.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "pipeline_gate.json"
        _CFG_CACHE = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CFG_CACHE = {}
    return _CFG_CACHE


def _gate_handlers() -> Dict[str, Any]:
    """name -> the gate tool's coroutine handler. Lazy so a broken sibling
    gate doesn't break this tool's import."""
    from .footprint_audit import footprint_audit
    from .update_audit import update_audit
    from .board_setup_audit import board_setup_audit
    from .placement_audit import placement_audit
    from .routing_audit import routing_audit
    from .power_audit import power_audit
    from .drc_audit import drc_audit
    from .dfm_audit import dfm_audit
    from .gerber_gate import gerber_gate
    return {
        "footprint_audit": footprint_audit,
        "update_audit": update_audit,
        "board_setup_audit": board_setup_audit,
        "placement_audit": placement_audit,
        "routing_audit": routing_audit,
        "power_audit": power_audit,
        "drc_audit": drc_audit,
        "dfm_audit": dfm_audit,
        "gerber_gate": gerber_gate,
    }


@tool(
    name="pipeline_gate",
    description=(
        "Run every PCB-flow validation gate (steps 3-8: footprint, sync, board "
        "setup, placement, routing, power) in order and return ONE go/no-go "
        "verdict with the first blocking stage. Read-only — it just calls the "
        "individual gates. Use it as the single 'is this project clear to "
        "advance / ship?' check. A gate whose artifact isn't built yet (no "
        "board) is reported 'not applicable', not a failure.\n"
        "Args:\n"
        '  {"path": "C:/.../projectDir"}     # project folder or any project file\n'
        '  {"path": "...", "mode": "gate"}    # stop at first FAIL (default: report all)\n'
        "Verdict in words. Policy + stage order in config/pipeline_gate.json."
    ),
    input_schema={"path": str},
)
async def pipeline_gate(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "pipeline_gate disabled in config"}],
                "is_error": True}

    raw = str(args.get("path", "")).strip()
    base = Path(raw).expanduser()
    proj = str(base if base.is_dir() else base.parent)
    mode = str(args.get("mode") or cfg.get("mode", "report"))

    handlers = _gate_handlers()
    stages = [s for s in (cfg.get("stages") or [])
              if isinstance(s, dict) and s.get("enabled", True)]
    vt = cfg.get("verdict", {}) or {}

    results: List[Dict[str, Any]] = []
    for s in stages:
        gate = str(s.get("gate", ""))
        step = s.get("step", "?")
        h = handlers.get(gate)
        if h is None:
            results.append({"step": step, "gate": gate, "state": "na",
                            "verdict": "gate not available"})
            continue
        try:
            r = await h.handler({"path": proj})
        except Exception as exc:                            # noqa: BLE001
            results.append({"step": step, "gate": gate, "state": "na",
                            "verdict": f"error: {type(exc).__name__}"})
            continue
        ok = r.get("ok")
        counts = r.get("counts") or {}
        verdict = str(r.get("verdict") or r.get("content", [{}])[0].get("text", ""))[:200]
        if r.get("is_error") or ok is None:
            state = "na"                       # artifact missing / tool unavailable
        elif ok:
            state = "pass"
        else:
            state = "fail"
        results.append({"step": step, "gate": gate, "state": state,
                        "verdict": verdict,
                        "errors": counts.get("error", 0),
                        "warnings": counts.get("warning", 0)})
        if mode == "gate" and state == "fail":
            break

    passed = [r for r in results if r["state"] == "pass"]
    failed = [r for r in results if r["state"] == "fail"]
    na = [r for r in results if r["state"] == "na"]

    if failed:
        first = failed[0]
        verdict = vt.get("nogo",
                         "NO-GO — blocked at step {step} {gate}: {detail}"
                         ).format(step=first["step"], gate=first["gate"],
                                  detail=first["verdict"])
        go = False
    elif na:
        verdict = vt.get("partial",
                         "NOT READY — {passed} pass, {na} not applicable"
                         ).format(passed=len(passed), na=len(na))
        go = None
    else:
        verdict = vt.get("go", "GO — all {passed} gate(s) pass").format(passed=len(passed))
        go = True

    icon = {"pass": "✓", "fail": "✗", "na": "—"}
    total_e = sum(r.get("errors", 0) for r in results)
    total_w = sum(r.get("warnings", 0) for r in results)
    lines = [f"# Pipeline gate — {Path(proj).name}",
             f"  mode={mode} · {len(passed)} pass · {len(failed)} fail · "
             f"{len(na)} n/a · {total_e} errors, {total_w} warnings total", ""]
    for r in results:
        w = f" ({r['errors']}E/{r['warnings']}W)" if r["state"] in ("pass", "fail") else ""
        lines.append(f"  {icon[r['state']]} step {r['step']} {r['gate']}{w}: "
                     f"{r['verdict']}")
    lines.append("")
    lines.append(f"**{verdict}**")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "go": go,
        "project": proj.replace("\\", "/"),
        "verdict": verdict,
        "passed": len(passed),
        "failed": len(failed),
        "not_applicable": len(na),
        "stages": results,
    }
