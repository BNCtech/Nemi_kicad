"""Tool: auto_layout_pcb — the closed-loop PCB layout orchestrator.

The individual PCB tools (place, outline, route, ampacity, zones, thermal vias,
silk cleanup, DRC autofix, verify) each do one job; until now the user (or the
agent) had to call them one by one in the right order. This tool runs the whole
sequence in one shot and reports a single card.

The pipeline is DATA, not code: the ordered step list lives in
``layout_config.json:auto_layout_pcb.steps`` as ``[{tool, enabled, args}, ...]``.
Re-order, disable, or add a step by editing JSON — no Python change. Each step is
dispatched to the same ``@tool`` handler the agent would call directly, so there
is exactly one implementation per stage.

Behaviour:
  * runs every enabled step in order, threading the same ``pcb_path`` through;
  * ``args`` per step are merged with the tool's own config defaults;
  * a step that returns ``is_error`` is recorded; the run continues unless
    ``stop_on_error`` is set;
  * ``only`` / ``skip`` call-args restrict the run to a subset of step tools;
  * the final step (typically ``pcb_verify`` or ``drc_check``) provides the
    board's end state, surfaced as the headline.

Honest reporting: every step's pass/fail and one-line result is shown — no step
is silently dropped. Universal; never raises (a crashing step is caught and
reported). Config-driven end to end.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List


from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("auto_layout_pcb", {}) or {}
    except Exception:
        return {}


_DEFAULT_STEPS: List[Dict[str, Any]] = [
    {"tool": "auto_place_pcb", "enabled": True},
    {"tool": "auto_outline_pcb", "enabled": True},
    {"tool": "route_pcb_simple", "enabled": True},
    {"tool": "set_track_widths_pcb", "enabled": True},
    {"tool": "auto_zones_pcb", "enabled": True},
    {"tool": "auto_thermal_vias_pcb", "enabled": True},
    {"tool": "silkscreen_cleanup_pcb", "enabled": True},
    {"tool": "drc_autofix", "enabled": True, "args": {"apply": True, "max_rounds": 2}},
    {"tool": "pcb_verify", "enabled": True},
]


@tool(
    name="auto_layout_pcb",
    description=(
        "ONE-SHOT PCB layout: runs the whole post-import layout pipeline in order "
        "— place + de-collide, board outline, route easy nets, set track widths to "
        "current (ampacity), GND pour, thermal vias, silkscreen cleanup, DRC "
        "auto-fix, then verify. The step order/enable list is config-driven "
        "(layout_config.json:auto_layout_pcb.steps). Use for 'lay out the board', "
        "'auto layout', 'do the PCB', 'make the board fab-ready in one go'. Run "
        "AFTER the .kicad_pcb has footprints (F8 or pcb_gen).\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}                 # full pipeline\n'
        '  {"pcb_path": "...", "skip": ["route_pcb_simple"]}      # omit steps\n'
        '  {"pcb_path": "...", "only": ["auto_place_pcb","pcb_verify"]}\n'
        '  {"pcb_path": "...", "dry_run": true}                   # list the plan'
    ),
    input_schema={"pcb_path": str},
)
async def auto_layout_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
                "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "auto_layout_pcb disabled"}],
                "is_error": True}

    steps = cfg.get("steps") or _DEFAULT_STEPS
    stop_on_error = bool(cfg.get("stop_on_error", False))
    only = set(args.get("only") or [])
    skip = set(args.get("skip") or [])
    dry_run = bool(args.get("dry_run", False))

    # Resolve the active step list.
    active: List[Dict[str, Any]] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        name = s.get("tool", "")
        if not name or s.get("enabled") is False:
            continue
        if only and name not in only:
            continue
        if name in skip:
            continue
        active.append(s)

    if dry_run:
        plan = "\n".join(f"  {i}. {s['tool']}"
                         + (f"  {s.get('args')}" if s.get("args") else "")
                         for i, s in enumerate(active, 1))
        return {"content": [{"type": "text",
                             "text": f"auto_layout_pcb plan for {pcb_path.name}:\n{plan}"}],
                "ok": True, "plan": [s["tool"] for s in active]}

    results: List[Dict[str, Any]] = []
    for s in active:
        name = s["tool"]
        step_args = {"pcb_path": str(pcb_path), **(s.get("args") or {})}
        try:
            mod = importlib.import_module(f"envil_agent.tools.{name}")
            fn = getattr(mod, name)
            r = await fn.handler(step_args)
            ok = bool(r.get("ok", False)) and not r.get("is_error", False)
            text = (r.get("content", [{}])[0].get("text", "") or "")
            # headline = first line of the tool's own report
            head = text.strip().splitlines()[0] if text.strip() else "(no output)"
            results.append({"tool": name, "ok": ok, "head": head[:140],
                            "is_error": bool(r.get("is_error", False))})
        except ModuleNotFoundError:
            results.append({"tool": name, "ok": False,
                            "head": "tool not installed", "is_error": True})
        except Exception as exc:                          # noqa: BLE001
            results.append({"tool": name, "ok": False,
                            "head": f"{type(exc).__name__}: {exc}", "is_error": True})
        if stop_on_error and results[-1]["is_error"]:
            results.append({"tool": "(halted)", "ok": False,
                            "head": "stop_on_error — pipeline halted", "is_error": True})
            break

    n_ok = sum(1 for r in results if r["ok"])
    n_steps = len([r for r in results if r["tool"] != "(halted)"])
    lines = [f"auto_layout_pcb -> {pcb_path.name}  ({n_ok}/{n_steps} steps OK)"]
    for r in results:
        mark = "OK " if r["ok"] else ("xx " if r["is_error"] else " - ")
        lines.append(f"  {mark}{r['tool']}: {r['head']}")

    return {"content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": n_ok == n_steps, "path": str(pcb_path),
            "steps_ok": n_ok, "steps_total": n_steps, "results": results}
