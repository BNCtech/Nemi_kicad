"""Tool: parse a DRC report, classify violations, propose / apply fixes by
composing the existing PCB-mutating tools — then RE-CHECK and guard against
regressions.

Mirrors the mature `erc_autofix` contract (2026-06-23 rewrite):

  * Closed loop — measure violations BEFORE, apply the plan, measure AFTER, and
    report ``before -> after`` honestly. No false greens: if the board is no
    cleaner the report says so.
  * Regression guard — the .kicad_pcb bytes are snapshotted before any mutation.
    When ``regression_guard != "off"`` and the total (errors+warnings) rises, the
    snapshot is restored and the round is reported as reverted. Applying a fix can
    therefore never leave the board worse than it started.
  * Iteration — with ``max_rounds > 1`` the loop repeats while each round strictly
    reduces the total, stopping as soon as it plateaus.

Dispatch table (config-overridable via ``layout_config.json:drc_autofix.strategies``):

  invalid_outline        -> auto_outline_pcb   (draw Edge.Cuts rectangle)
  unconnected_item(s)    -> auto_zones_pcb     (GND pour catches most of GND)
  courtyard_overlap      -> auto_place_pcb {refine_only:true}  (de-collide in place)
  silkscreen_overlap     -> silkscreen_cleanup_pcb  (when that tool is present)
  lib_footprint_mismatch -> report-only (needs a library re-sync in the editor)
  clearance_violation    -> report-only (move parts / relax the rule)

Universal — works on any .kicad_pcb. Preview by default; pass ``apply=true`` to
execute. Never raises on a single failed sub-tool.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("drc_autofix", {}) or {}
    except Exception:
        return {}


# Built-in fix dispatch — type -> (tool_name, default_args). A config
# `strategies.<type>.action` (+ optional `args`) overrides the tool; a config
# `strategies.<type>.enabled=false` turns the fix off (left report-only).
# NOTE: keys are kicad-cli's actual violation `type` strings (verified against
# `kicad-cli pcb drc --format json`): courtyards_overlap / clearance /
# silk_overlap / lib_footprint_issues / unconnected_items. Legacy aliases are
# kept so older KiCad report schemas still match.
_BUILTIN_FIX: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "invalid_outline":     ("auto_outline_pcb", {}),
    "unconnected_item":    ("auto_zones_pcb", {"net": "GND"}),
    "unconnected_items":   ("auto_zones_pcb", {"net": "GND"}),
    # Footprint courtyards overlapping → push them apart in place. Side-effect:
    # also clears the `clearance` errors caused by the same physical overlap.
    "courtyards_overlap":  ("auto_place_pcb", {"refine_only": True}),
    "courtyard_overlap":   ("auto_place_pcb", {"refine_only": True}),  # alias
    # Silkscreen fixes dispatch to the DFA cleanup tool when it is installed;
    # until then they fall through gracefully (ModuleNotFoundError handled) and
    # the guard keeps the board safe.
    "silk_overlap":            ("silkscreen_cleanup_pcb", {}),
    "silk_over_copper":        ("silkscreen_cleanup_pcb", {}),
    "silkscreen_overlap":      ("silkscreen_cleanup_pcb", {}),  # alias
    "overlapping_silkscreen":  ("silkscreen_cleanup_pcb", {}),  # alias
}

# Types we deliberately leave to the human — listing them keeps the report
# honest about WHY they weren't touched.
_REPORT_ONLY_REASON: Dict[str, str] = {
    "lib_footprint_issues":   ("footprint differs from library — open PCB Editor "
                               "-> Update Footprints from Library"),
    "lib_footprint_mismatch": ("footprint differs from library — open PCB Editor "
                               "-> Update Footprints from Library"),
    "clearance":              ("clearance — usually clears once courtyards_overlap "
                               "is fixed; otherwise re-route the track or relax the rule"),
    "clearance_violation":    "clearance — move parts apart or relax the net-class rule",
    "hole_to_hole_clearance": "drill spacing — move the holes apart",
    "hole_near_hole":         "drill spacing — move the holes apart",
    "annular_width":          "annular ring — increase pad/via size or shrink drill",
    "track_dangling":         "dangling track — route it to a pad or delete it",
    "shorting_items":         "two nets touch — re-route the offending track",
}


def _violation_summary(issues: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in issues:
        t = v.get("type", "") or "(unknown)"
        out[t] = out.get(t, 0) + 1
    return out


async def _measure(pcb_path: Path) -> Dict[str, Any]:
    """Run drc_check and return {errors, warnings, total, issues, ok, error?}."""
    DRC = importlib.import_module("envil_agent.tools.drc_check")
    res = await DRC.drc_check.handler({"pcb_path": str(pcb_path)})
    if res.get("is_error"):
        return {"error": res.get("content", [{}])[0].get("text", "DRC failed"),
                "errors": -1, "warnings": -1, "total": -1, "issues": []}
    err = int(res.get("error_count", 0))
    warn = int(res.get("warning_count", 0))
    return {"errors": err, "warnings": warn, "total": err + warn,
            "issues": res.get("issues") or [], "ok": err == 0}


def _plan_for(counts: Dict[str, int],
              strategies: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One plan entry per distinct violation type."""
    plan: List[Dict[str, Any]] = []
    for vtype, n in sorted(counts.items()):
        strat = strategies.get(vtype, {}) if isinstance(strategies, dict) else {}
        if isinstance(strat, dict) and strat.get("enabled") is False:
            plan.append({"violation_type": vtype, "count": n, "action": "skip",
                         "reason": "strategy disabled in config"})
            continue
        # config action wins; else built-in; else report-only.
        action = (strat.get("action") if isinstance(strat, dict) else None)
        args: Dict[str, Any] = {}
        if action:
            args = (strat.get("args") if isinstance(strat, dict) else {}) or {}
        elif vtype in _BUILTIN_FIX:
            action, args = _BUILTIN_FIX[vtype]
            args = {**args, **((strat.get("args") if isinstance(strat, dict) else {}) or {})}
        if not action:
            reason = _REPORT_ONLY_REASON.get(vtype, "no auto-fix strategy registered")
            plan.append({"violation_type": vtype, "count": n,
                         "action": "report_only", "reason": reason})
            continue
        plan.append({"violation_type": vtype, "count": n,
                     "action": action, "args": args,
                     "reason": f"dispatch to {action}"})
    return plan


async def _apply_plan(pcb_path: Path,
                      plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Execute every fix action once (deduped by (tool, args))."""
    applied: List[Dict[str, Any]] = []
    seen: set = set()
    for step in plan:
        action = step.get("action", "")
        if action in ("skip", "report_only", ""):
            continue
        args = step.get("args") or {}
        key = (action, tuple(sorted((k, str(v)) for k, v in args.items())))
        if key in seen:
            continue
        seen.add(key)
        try:
            mod = importlib.import_module(f"envil_agent.tools.{action}")
            tool_fn = getattr(mod, action)
            ta = {"pcb_path": str(pcb_path), **args}
            r = await tool_fn.handler(ta)
            applied.append({
                "action": action, "args": args,
                "ok": bool(r.get("ok", False)) and not r.get("is_error", False),
                "result": (r.get("content", [{}])[0].get("text", "") or "")[:240],
            })
        except ModuleNotFoundError:
            applied.append({"action": action, "args": args, "ok": False,
                            "result": f"tool '{action}' not installed yet — skipped"})
        except Exception as exc:                          # noqa: BLE001
            applied.append({"action": action, "args": args, "ok": False,
                            "result": f"{type(exc).__name__}: {exc}"})
    return applied


@tool(
    name="drc_autofix",
    description=(
        "INTELLIGENT DRC repair with re-check + regression guard. Runs DRC, "
        "groups violations by type, dispatches the right PCB tool per type "
        "(auto_outline_pcb for missing edge cuts, auto_zones_pcb for unconnected "
        "GND, auto_place_pcb refine for courtyard overlaps), then RE-RUNS DRC and "
        "reports before->after. If a fix would raise the violation count the board "
        "is restored from a snapshot. Use for 'fix DRC errors', 'auto-fix the "
        "PCB', 'clean up the board', 'make the PCB fab-ready'.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}              # preview only\n'
        '  {"pcb_path": "...", "apply": true}                 # execute + re-check\n'
        '  {"pcb_path": "...", "apply": true, "max_rounds": 3}# iterate to plateau\n'
        '  {"pcb_path": "...", "only_severity": "error"}      # plan errors only\n'
        "Strategy/guard config in layout_config.json:drc_autofix."
    ),
    input_schema={"pcb_path": str},
)
async def drc_autofix(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "drc_autofix disabled"}],
                "is_error": True}

    apply = bool(args.get("apply", False))
    only_sev = str(args.get("only_severity", "")).lower()
    strategies = cfg.get("strategies", {}) or {}
    guard = str(cfg.get("regression_guard", "on")).lower()
    max_rounds = max(1, int(args.get("max_rounds", cfg.get("max_rounds", 1))))

    # ---- Initial measurement ----
    base = await _measure(pcb_path)
    if base.get("error") is not None:
        return {"content": [{"type": "text",
                              "text": f"DRC could not run: {base['error']}"}],
                "is_error": True}
    issues = base["issues"]
    if only_sev:
        issues = [i for i in issues if i.get("severity") == only_sev]
    if not issues:
        return {"content": [{"type": "text", "text": "DRC clean — nothing to fix."}],
                "ok": True, "violations": [], "plan": [], "before": base["total"]}

    counts = _violation_summary(issues)
    plan = _plan_for(counts, strategies)

    # ---- Preview mode: plan only, no mutation ----
    if not apply:
        lines = [f"DRC auto-fix PREVIEW on {pcb_path.name}",
                 f"  current: {base['errors']} errors, {base['warnings']} warnings",
                 "  violations by type:"]
        for vt, n in sorted(counts.items()):
            lines.append(f"    {vt}: {n}")
        lines.append("")
        lines.append("Plan:")
        for i, s in enumerate(plan, 1):
            mark = "->" if s["action"] not in ("skip", "report_only") else " *"
            lines.append(f"  {i}. [{s['violation_type']} x{s['count']}] {mark} "
                         f"{s['action']}: {s['reason']}")
        lines.append("")
        lines.append("(preview only — pass apply=true to execute + re-check)")
        return {"content": [{"type": "text", "text": "\n".join(lines)}],
                "ok": True, "violations": issues, "plan": plan,
                "before": base["total"], "applied": None}

    # ---- Apply with snapshot + closed-loop re-check ----
    rounds: List[Dict[str, Any]] = []
    prev_total = base["total"]
    cur_counts = counts
    cur_plan = plan
    final_total = prev_total

    for rnd in range(max_rounds):
        actionable = [s for s in cur_plan
                      if s["action"] not in ("skip", "report_only", "")]
        if not actionable:
            break
        try:
            snapshot = pcb_path.read_bytes()
        except OSError as exc:
            return {"content": [{"type": "text",
                                  "text": f"ERROR: cannot snapshot board: {exc}"}],
                    "is_error": True}

        applied = await _apply_plan(pcb_path, cur_plan)
        after = await _measure(pcb_path)

        reverted = False
        if after.get("error") is not None:
            # DRC broke after the edit — restore and stop.
            pcb_path.write_bytes(snapshot)
            reverted = True
            after = {"errors": prev_total, "warnings": 0, "total": prev_total,
                     "issues": [], "note": "re-check failed; reverted"}
        elif guard != "off" and after["total"] > prev_total:
            pcb_path.write_bytes(snapshot)
            reverted = True
            after = {**after, "total": prev_total,
                     "note": "regression — reverted to snapshot"}

        rounds.append({"round": rnd + 1, "before": prev_total,
                       "after": after["total"], "reverted": reverted,
                       "applied": applied})
        final_total = after["total"]

        if reverted or after["total"] >= prev_total:
            break                                   # no progress / reverted → stop
        # Re-plan from the post-fix state for the next round.
        prev_total = after["total"]
        cur_counts = _violation_summary(after.get("issues") or [])
        cur_plan = _plan_for(cur_counts, strategies)

    # ---- Report ----
    _delta = final_total - base["total"]            # negative = fewer violations
    if _delta == 0:
        _delta_txt = "  (no change)"
    elif _delta < 0:
        _delta_txt = f"  ({-_delta} fewer)"
    else:
        _delta_txt = f"  ({_delta} MORE)"
    lines = [f"DRC auto-fix on {pcb_path.name}",
             f"  total violations: {base['total']} -> {final_total}{_delta_txt}"]
    lines.append("  violations by type (initial):")
    for vt, n in sorted(counts.items()):
        lines.append(f"    {vt}: {n}")
    for rinfo in rounds:
        lines.append("")
        tag = " [REVERTED]" if rinfo["reverted"] else ""
        lines.append(f"Round {rinfo['round']}: {rinfo['before']} -> "
                     f"{rinfo['after']}{tag}")
        for a in rinfo["applied"]:
            mark = "OK " if a["ok"] else "xx "
            lines.append(f"  {mark}{a['action']}: {a['result'][:110]}")
    # Honest closing line.
    if final_total == 0:
        lines.append("\nDRC clean.")
    elif final_total < base["total"]:
        lines.append(f"\nReduced to {final_total} remaining — re-run or fix the "
                     f"report-only items by hand.")
    else:
        lines.append("\nNo automatic improvement — remaining items need manual "
                     "placement/routing or a design-rule change.")

    return {"content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": True, "path": str(pcb_path),
            "before": base["total"], "after": final_total,
            "violations": issues, "plan": plan, "rounds": rounds}
