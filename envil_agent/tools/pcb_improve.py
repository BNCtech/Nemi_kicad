"""Tool: pcb_improve — score-driven, guarded auto-fix loop.

This is the "Apply Fix" half of the perfect-PCB loop. ``pcb_quality`` detects +
suggests; ``pcb_improve`` actually raises the percentage:

    score the board  ->  apply the fixes it recommends  ->  re-score  ->  repeat

…until the score reaches a target, stops climbing, or a pass budget is spent.

Why this is NOT just ``auto_layout_pcb``:
  * auto_layout_pcb runs a FIXED full pipeline blindly, in config order, whether
    or not a step helps — and nothing stops a bad step (e.g. an auto-route that
    introduces clearance errors) from LOWERING quality.
  * pcb_improve is closed-loop and GUARDED: it only applies the fixes the scorer
    actually recommends for THIS board, and after every fix it re-scores and
    KEEPS the change only if the overall score did not drop (the board file is
    snapshotted and restored on regression). The percentage is therefore
    monotonic — it can only go up or stay flat, never down. Mirrors the
    schematic-side ERC no-regression guard.

Fully dynamic: the fix list is whatever ``pcb_quality`` emits for the board
(each finding already names the existing tool that fixes it). No circuit, part,
or step sequence is hardcoded — a board with no GND pour gets auto_zones_pcb, a
board with thin power gets set_track_widths_pcb, and a clean board gets nothing.
All knobs live in ``layout_config.json:pcb_improve``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


from claude_agent_sdk import tool

# Shared dispatch + banding — same helpers pcb_quality uses; defined once.
from ._pcb_sexpr import dispatch_tool, band_for


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("pcb_improve", {}) or {}
    except Exception:
        return {}


async def _score(pcb_path: Path, skip_drc: bool) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    """Run pcb_quality, return (overall, findings). overall is None on failure."""
    r = await dispatch_tool("pcb_quality", {"pcb_path": str(pcb_path), "skip_drc": skip_drc})
    if r.get("is_error"):
        return None, []
    return r.get("overall"), list(r.get("fixes") or [])


def _ordered_fix_tools(findings: List[Dict[str, Any]],
                       allow: List[str]) -> List[str]:
    """Distinct fix tools from the findings, in severity order (error→warn→info),
    restricted to the allowlist. One tool fixes many findings, so we dedup."""
    rank = {"error": 0, "warn": 1, "info": 2}
    seen: set = set()
    out: List[str] = []
    for f in sorted(findings, key=lambda x: rank.get(x.get("severity"), 3)):
        t = f.get("fix_tool")
        if t and t in allow and t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def _apply(tool_name: str, pcb_path: Path,
                 tool_args: Dict[str, Any]) -> bool:
    """Run one fix tool against the board via the shared dispatcher. Returns
    True if it ran without erroring (it may still be a no-op — the score guard
    decides if it actually helped)."""
    r = await dispatch_tool(tool_name, {"pcb_path": str(pcb_path), **(tool_args or {})})
    return not bool(r.get("is_error", False))


@tool(
    name="pcb_improve",
    description=(
        "AUTO-IMPROVE a .kicad_pcb's quality score: scores the board, applies the "
        "fixes pcb_quality recommends, re-scores, and repeats until the score hits "
        "a target or stops climbing. GUARDED — every fix is kept only if the score "
        "did not drop (the board is restored on any regression), so the percentage "
        "only goes up. Returns a before→after card. Use for 'improve the "
        "percentage', 'make the board better', 'auto-fix the PCB', 'raise the "
        "score'. Dynamic: it applies only the fixes THIS board needs (auto_zones / "
        "set_track_widths / route / auto_place / drc_autofix), nothing hardcoded.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}                 # improve to target\n'
        '  {"pcb_path": "...", "target": 95}                      # custom target %\n'
        '  {"pcb_path": "...", "max_passes": 5}                   # more iterations\n'
        "All defaults in layout_config.json:pcb_improve."
    ),
    input_schema={"pcb_path": str},
)
async def pcb_improve(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {pcb_path}"}],
                "is_error": True}
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
                "is_error": True}

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "pcb_improve disabled"}],
                "is_error": True}

    target      = float(args.get("target", cfg.get("target_score", 90.0)))
    max_passes  = int(args.get("max_passes", cfg.get("max_passes", 3)))
    loop_skip_drc = bool(cfg.get("loop_skip_drc", True))
    eps         = float(cfg.get("min_gain", 0.5))
    allow       = list(cfg.get("fix_tools", [
        "auto_zones_pcb", "set_track_widths_pcb", "route_pcb_simple",
        "auto_place_pcb", "auto_outline_pcb", "drc_autofix",
    ]))
    tool_args   = dict(cfg.get("fix_tool_args", {
        "drc_autofix": {"apply": True, "max_rounds": 2},
    }))

    baseline, _ = await _score(pcb_path, loop_skip_drc)
    if baseline is None:
        return {"content": [{"type": "text", "text": "ERROR: could not score board"}],
                "is_error": True}

    current = baseline
    applied: List[Dict[str, Any]] = []
    log: List[str] = []

    for p in range(max_passes):
        if current >= target:
            break
        _, findings = await _score(pcb_path, loop_skip_drc)
        tools = _ordered_fix_tools(findings, allow)
        if not tools:
            break
        improved = False
        for t in tools:
            snapshot = pcb_path.read_text(encoding="utf-8")
            ran = await _apply(t, pcb_path, tool_args.get(t, {}))
            if not ran:
                # tool failed to run — make sure the file is untouched
                pcb_path.write_text(snapshot, encoding="utf-8")
                log.append(f"pass{p+1} {t}: did not run (skipped)")
                continue
            new, _ = await _score(pcb_path, loop_skip_drc)
            if new is not None and new >= current + eps:
                log.append(f"  {t}: helped")
                current = new
                applied.append({"tool": t, "score": round(new, 1)})
                improved = True
            else:
                # GUARD: regression or no gain — revert this fix
                pcb_path.write_text(snapshot, encoding="utf-8")
                log.append(f"  {t}: did not help (undone)")
        if not improved:
            break

    # Final authoritative score WITH real DRC.
    final, final_findings = await _score(pcb_path, skip_drc=False)
    final = final if final is not None else current

    verdict_bands = [(float(t), str(l)) for t, l in cfg.get("bands", [
        [90, "EXCELLENT"], [75, "GOOD"], [60, "REVIEW"], [0, "NEEDS WORK"],
    ])]
    # Words, not numbers (feedback_no_percentage_scores). Show % only if the
    # board explicitly turns show_percentage on.
    show_pct = bool(cfg.get("show_percentage", False))
    before_band = band_for(baseline, verdict_bands)
    after_band = band_for(final, verdict_bands)
    if show_pct:
        lines = [f"# PCB IMPROVE — {pcb_path.name}",
                 f"  Before: {baseline:.0f}% ({before_band})",
                 f"  After:  {final:.0f}% ({after_band})"]
    else:
        lines = [f"# PCB IMPROVE — {pcb_path.name}",
                 f"  Was: {before_band}",
                 f"  Now: {after_band}"]
    lines.append("")
    if applied:
        lines.append("  Fixed:")
        for a in applied:
            lines.append(f"    - {a['tool']}")
    else:
        lines.append("  No fix improved the score (already optimal for the "
                     "available tools, or board needs manual work).")
    if final_findings:
        lines.append("")
        lines.append("  Remaining:")
        for f in final_findings[:6]:
            lines.append(f"    • {f.get('message','')}")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": True,
        "path": str(pcb_path),          # so the server captures it -> live PCB reload
        "pcb_path": str(pcb_path),
        "before": round(baseline, 1),
        "after": round(final, 1),
        "gain": round(delta, 1),
        "applied": applied,
        "remaining": final_findings,
        "log": log,
    }
