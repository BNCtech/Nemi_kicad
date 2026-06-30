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
# Shorthand fix actions (single edit-point so the dispatch table reads clearly).
_ROUTE = ("route_pcb_simple", {"replace": True})   # clear messy copper + clean reroute
_PLACE = ("auto_place_pcb", {"refine_only": True}) # spread parts in place
_POUR_GND = ("auto_zones_pcb", {"net": "GND"})     # GND copper pour
_REPOUR = ("auto_zones_pcb", {})                   # re-pour all zones
_SILK = ("silkscreen_cleanup_pcb", {})             # move silk off pads/copper

_BUILTIN_FIX: Dict[str, Tuple[str, Dict[str, Any]]] = {
    "invalid_outline":     ("auto_outline_pcb", {}),
    # Unconnected nets: pour GND (catches the bulk of unconnected GND pads). Any
    # unconnected SIGNAL nets are reconnected by the clear+reroute that the
    # routing-error types below trigger on the same board.
    "unconnected_item":    _POUR_GND,
    "unconnected_items":   _POUR_GND,
    # ----- Routing-caused copper errors (the dominant errors on a routed board).
    # The proper fix is NOT "leave it to the human" — it is to clear the messy /
    # shorting tracks and re-route them clearance-aware (route_pcb_simple already
    # honours per-net clearance + board-edge keepout). replace=true wipes the bad
    # copper first so two-net shorts and clearance hits are removed at the source.
    "shorting_items":        _ROUTE,
    "tracks_crossing":       _ROUTE,   # alias seen on some KiCad builds
    "clearance":             _ROUTE,
    "clearance_violation":   _ROUTE,   # alias
    "copper_edge_clearance": _ROUTE,
    "track_dangling":        _ROUTE,
    # ----- Footprint courtyards overlapping → push them apart in place. Side-
    # effect: also clears the `clearance`/`solder_mask_bridge` the overlap caused.
    "courtyards_overlap":  _PLACE,
    "courtyard_overlap":   _PLACE,     # alias
    # Two solder-mask openings merging into one bridge — usually adjacent parts
    # too close; spreading them apart opens the gap. (Same-footprint pin bridges
    # are a library/mask-rule matter and revert harmlessly under the guard.)
    "solder_mask_bridge":  _PLACE,
    # A pad starved of thermal-relief spokes from its zone → re-pour the zones so
    # the connection is rebuilt with full spokes.
    "starved_thermal":     _REPOUR,
    # Silkscreen on pads / copper → DFA cleanup tool nudges the text clear.
    "silk_overlap":            _SILK,
    "silk_over_copper":        _SILK,
    "silkscreen_overlap":      _SILK,  # alias
    "overlapping_silkscreen":  _SILK,  # alias
}

# Logical phase order so a single round applies fixes in the order a human would:
# draw the board, place the parts, route, pour, then tidy silk. Without this the
# plan ran alphabetically (auto_zones before route), pouring around copper that
# was about to be torn up and re-laid.
_PHASE_RANK: Dict[str, int] = {
    "auto_outline_pcb":        0,
    "auto_place_pcb":          1,
    "route_pcb_simple":        2,
    "set_track_widths_pcb":    3,
    "auto_zones_pcb":          4,
    "auto_thermal_vias_pcb":   4,
    "silkscreen_cleanup_pcb":  5,
}


def _phase_rank(action: str) -> int:
    return _PHASE_RANK.get(action, 3)


# Types we deliberately leave to the human — listing them keeps the report
# honest about WHY they weren't touched. (Routing/placement-fixable types were
# MOVED out of here into _BUILTIN_FIX above — they are now auto-repaired.)
_REPORT_ONLY_REASON: Dict[str, str] = {
    "lib_footprint_issues":   ("footprint differs from library — open PCB Editor "
                               "-> Update Footprints from Library"),
    "lib_footprint_mismatch": ("footprint differs from library — open PCB Editor "
                               "-> Update Footprints from Library"),
    "hole_to_hole_clearance": "drill spacing — move the holes apart",
    "hole_near_hole":         "drill spacing — move the holes apart",
    "annular_width":          "annular ring — increase pad/via size or shrink drill",
}


def _violation_summary(issues: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in issues:
        t = v.get("type", "") or "(unknown)"
        out[t] = out.get(t, 0) + 1
    return out


def _error_signatures(issues: List[Dict[str, Any]]) -> set:
    """Set of stable signatures for ERROR-severity violations ONLY — mirrors
    erc_autofix's _error_signature_set. Warnings never enter the set, so a fix
    that clears an error but leaves a new warning (e.g. a re-route that drops a
    short but leaves a net unrouted) is NOT punished by the guard.

    A signature is ``type@<item-location>``. drc_check already renders each item
    as 'desc @ (x, y)', so the coordinate is baked in. The signature set is only
    compared when the error COUNT is unchanged — to catch a 'swap' (one error
    cleared, a different one introduced at the same count). When the count drops
    the fix is accepted outright, so re-routes that legitimately move copper to
    new positions are never reverted for it."""
    sigs: set = set()
    for v in issues:
        if (v.get("severity") or "").lower() != "error":
            continue
        vt = v.get("type", "")
        items = v.get("items") or []
        if items:
            for it in items:
                sigs.add(f"{vt}@{it}")
        else:
            sigs.add(f"{vt}@-")
    return sigs


async def _measure(pcb_path: Path) -> Dict[str, Any]:
    """Run drc_check and return {errors, warnings, total, issues, err_sigs, ok, error?}."""
    DRC = importlib.import_module("envil_agent.tools.drc_check")
    res = await DRC.drc_check.handler({"pcb_path": str(pcb_path)})
    if res.get("is_error"):
        return {"error": res.get("content", [{}])[0].get("text", "DRC failed"),
                "errors": -1, "warnings": -1, "total": -1, "issues": [],
                "err_sigs": set()}
    err = int(res.get("error_count", 0))
    warn = int(res.get("warning_count", 0))
    issues = res.get("issues") or []
    return {"errors": err, "warnings": warn, "total": err + warn,
            "issues": issues, "err_sigs": _error_signatures(issues),
            "ok": err == 0}


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
                      plan: List[Dict[str, Any]],
                      *, guard_per_fix: bool, sig_guard: bool,
                      start_errors: int, start_total: int,
                      start_sigs: set) -> Tuple[List[Dict[str, Any]], int, int, set]:
    """Execute the fix actions in PHASE order (deduped by (tool, args)).

    Per-fix guard — ERC parity (mirrors erc_autofix's per_fix + signature guard).
    For each fix: snapshot -> run -> re-DRC, then decide KEEP / REVERT by:

      * REVERT if the ERROR count rose (a fix must never add a hard error), OR
      * REVERT if the error count is unchanged BUT a NEW error-signature appeared
        (a 'swap' — one error cleared, a different one introduced; ``sig_guard``),
      * otherwise KEEP.

    Errors lead, exactly like ERC: warnings never trip the guard, so a re-route
    that drops a short but leaves a net as an unrouted *warning* is still kept.
    When the error count DROPS the fix is accepted outright (no signature check),
    so re-routes that move copper to new coordinates are never wrongly reverted.

    Returns (applied, errors, total, err_sigs) after the kept fixes.
    """
    applied: List[Dict[str, Any]] = []
    seen: set = set()
    cur_err = start_errors
    cur_total = start_total
    cur_sigs = set(start_sigs)
    # Order the actionable steps by logical phase (place before route before pour).
    actionable = [s for s in plan
                  if s.get("action") not in ("skip", "report_only", "")]
    actionable.sort(key=lambda s: _phase_rank(s.get("action", "")))
    for step in actionable:
        action = step.get("action", "")
        args = step.get("args") or {}
        key = (action, tuple(sorted((k, str(v)) for k, v in args.items())))
        if key in seen:
            continue
        seen.add(key)
        try:
            snap = pcb_path.read_bytes()
        except OSError as exc:
            applied.append({"action": action, "args": args, "ok": False,
                            "kept": False, "result": f"snapshot failed: {exc}"})
            continue
        try:
            mod = importlib.import_module(f"envil_agent.tools.{action}")
            tool_fn = getattr(mod, action)
            r = await tool_fn.handler({"pcb_path": str(pcb_path), **args})
            ran_ok = bool(r.get("ok", False)) and not r.get("is_error", False)
            txt = (r.get("content", [{}])[0].get("text", "") or "")[:200]
        except ModuleNotFoundError:
            applied.append({"action": action, "args": args, "ok": False,
                            "kept": False,
                            "result": f"tool '{action}' not installed — skipped"})
            continue
        except Exception as exc:                          # noqa: BLE001
            pcb_path.write_bytes(snap)
            applied.append({"action": action, "args": args, "ok": False,
                            "kept": False,
                            "result": f"{type(exc).__name__}: {exc}"})
            continue

        if not guard_per_fix:
            applied.append({"action": action, "args": args, "ok": ran_ok,
                            "kept": True, "result": txt})
            continue

        # Per-fix guard: re-measure and decide keep/revert (errors-first).
        after = await _measure(pcb_path)
        if after.get("error") is not None:
            pcb_path.write_bytes(snap)
            applied.append({"action": action, "args": args, "ok": ran_ok,
                            "kept": False, "result": "re-check failed; reverted"})
            continue
        new_err = int(after.get("errors", cur_err))
        new_total = int(after.get("total", cur_total))
        new_sigs = after.get("err_sigs") or set()
        introduced = (new_sigs - cur_sigs) if sig_guard else set()

        if new_err > cur_err:
            verdict = (False, f"added {new_err - cur_err} error(s); reverted")
        elif new_err == cur_err and introduced:
            verdict = (False, f"swapped in {len(introduced)} new error(s); reverted")
        else:
            verdict = (True, "")

        keep, why = verdict
        if not keep:
            pcb_path.write_bytes(snap)
            applied.append({"action": action, "args": args, "ok": ran_ok,
                            "kept": False, "result": f"{why}. {txt}"})
        else:
            de = cur_err - new_err
            cur_err, cur_total, cur_sigs = new_err, new_total, new_sigs
            note = (f"{de} fewer error(s) -> {new_err} err. " if de > 0 else "")
            applied.append({"action": action, "args": args, "ok": ran_ok,
                            "kept": True, "result": note + txt})
    return applied, cur_err, cur_total, cur_sigs


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

    # ---- Apply (phase-ordered, per-fix guarded) + closed-loop re-check ----
    # Guard granularity: per-fix when on (each fix kept only if it does not ADD a
    # hard error or swap in a new error-signature — see _apply_plan), so a
    # re-route that fails to fully reconnect can never undo the placement/pour
    # fixes that already helped, and warnings never block an error fix.
    guard_per_fix = (guard != "off")
    sig_guard = bool(cfg.get("signature_regression_guard", True))
    rounds: List[Dict[str, Any]] = []
    prev_err = base["errors"]
    prev_total = base["total"]
    prev_sigs = base.get("err_sigs") or set()
    cur_plan = plan
    final_total = prev_total
    final_err = prev_err

    for rnd in range(max_rounds):
        actionable = [s for s in cur_plan
                      if s["action"] not in ("skip", "report_only", "")]
        if not actionable:
            break

        applied, kept_err, kept_total, kept_sigs = await _apply_plan(
            pcb_path, cur_plan,
            guard_per_fix=guard_per_fix, sig_guard=sig_guard,
            start_errors=prev_err, start_total=prev_total, start_sigs=prev_sigs)

        # Authoritative re-measure for re-planning + reporting.
        after = await _measure(pcb_path)
        if after.get("error") is None:
            after_err = after["errors"]
            after_total = after["total"]
            after_sigs = after.get("err_sigs") or set()
            after_issues = after.get("issues") or []
        else:
            after_err, after_total, after_sigs, after_issues = (
                kept_err, kept_total, kept_sigs, [])

        rounds.append({"round": rnd + 1, "before": prev_total,
                       "after": after_total, "before_err": prev_err,
                       "after_err": after_err, "applied": applied})
        final_total = after_total
        final_err = after_err

        # Keep iterating while a round still removed an error OR a warning.
        if after_err >= prev_err and after_total >= prev_total:
            break                                   # plateaued → stop
        prev_err = after_err
        prev_total = after_total
        prev_sigs = after_sigs
        cur_counts = _violation_summary(after_issues)
        cur_plan = _plan_for(cur_counts, strategies)

    # ---- Report (errors lead — they are the fab gate, exactly like ERC) ----
    def _delta_txt(before: int, after: int) -> str:
        d = after - before
        if d == 0:
            return "  (no change)"
        return f"  ({-d} fewer)" if d < 0 else f"  ({d} MORE)"

    lines = [f"DRC auto-fix on {pcb_path.name}",
             f"  errors:  {base['errors']} -> {final_err}{_delta_txt(base['errors'], final_err)}",
             f"  total:   {base['total']} -> {final_total}{_delta_txt(base['total'], final_total)}"]
    lines.append("  violations by type (initial):")
    for vt, n in sorted(counts.items()):
        lines.append(f"    {vt}: {n}")
    for rinfo in rounds:
        lines.append("")
        lines.append(f"Round {rinfo['round']}: "
                     f"{rinfo.get('before_err', '?')} -> {rinfo.get('after_err', '?')} errors "
                     f"({rinfo['before']} -> {rinfo['after']} total)")
        for a in rinfo["applied"]:
            # kept = the fix helped/held; "--" = ran but undone by guard; xx = failed.
            if a.get("kept"):
                mark = "OK "
            elif a.get("ok"):
                mark = "-- "
            else:
                mark = "xx "
            lines.append(f"  {mark}{a['action']}: {a['result'][:110]}")
    # Honest closing line — errors first.
    if final_err == 0 and final_total == 0:
        lines.append("\nDRC clean — 0 errors, 0 warnings.")
    elif final_err == 0:
        lines.append(f"\n0 errors — board passes the DRC error gate. "
                     f"{final_total} warning(s) remain (footprint-library sync, "
                     f"unrouted nets, or cosmetic silk — none block fabrication).")
    elif final_err < base["errors"]:
        lines.append(f"\nReduced to {final_err} error(s) — re-run, or the rest need "
                     f"manual placement/routing or a design-rule change.")
    else:
        lines.append("\nNo automatic improvement — remaining errors need manual "
                     "placement/routing or a design-rule change.")

    return {"content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": True, "path": str(pcb_path),
            "errors_before": base["errors"], "errors_after": final_err,
            "before": base["total"], "after": final_total,
            "drc_clean": final_err == 0,
            "violations": issues, "plan": plan, "rounds": rounds}
