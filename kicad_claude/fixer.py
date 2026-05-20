"""Validate -> fix loop.

Pipeline per iteration:
  1. Run L1 deterministic checks (basic_checks).
  2. If L1 critical fails, ask Claude to repair with ops; apply; loop.
  3. Once L1 passes, run L2 LLM judge (validator).
  4. If L2 returns critical/high issues, ask Claude to repair; apply; loop.
  5. Stop on success, on iteration cap, or on no-progress (issue count
     plateaued two iterations in a row).

All loop bounds, severity gates, model choice, and safety switches live in
fixer_config.json. Nothing in this module is hardcoded.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import basic_checks, erc as erc_mod, hierarchy, rules, validator
from ._config_loader import load as _load_config, load_prompt as _load_prompt
from .claude_client import ClaudeClient
from .schematic_extractor import SchematicExtractor
from .schematic_modifier import SchematicDocument, apply_operation


def _cfg() -> Dict[str, Any]:
    return _load_config("fixer_config")


def _render_fix_prompt() -> str:
    """Render the fixer system prompt from prompts/fixer_system.md, interpolating
    the grid step from conventions.json and the layout-guidance distances from
    fixer_config.json. Resolved at call time so edits take effect after
    _config_loader.reload_all() without restart."""
    tpl = _load_prompt("fixer_system")
    grid_mm = float(_load_config("conventions")["grid"]["schematic_mm"])
    g = _cfg()["layout_guidance"]
    return (
        tpl
        .replace("{{GRID_MM}}",            f"{grid_mm:g}")
        .replace("{{DECOUPLING_MAX_MM}}",  f"{float(g['decoupling_max_mm']):g}")
        .replace("{{STRAP_MAX_MM}}",       f"{float(g['strap_max_mm']):g}")
        .replace("{{RAIL_PORT_MM}}",       f"{float(g['rail_port_mm']):g}")
        .replace("{{MIN_BODY_GAP_MM}}",    f"{float(g['min_body_gap_mm']):g}")
        .replace("{{EDGE_MARGIN_MM}}",     f"{float(g['edge_margin_mm']):g}")
    )


from .json_utils import extract_json as _extract_json


def _gather_issues(schematic_path: str, run_l2: bool = True) -> Dict[str, Any]:
    """Run L1 (always), then ERC (local + free, runs when kicad-cli is available),
    then L2 if L1 passes and run_l2 is true (LLM call, costs tokens).

    Set run_l2=False for cheap local-only inspection. ERC always runs when
    available — it's deterministic and free, same tier as L1.
    """
    l1 = basic_checks.run_all(schematic_path)
    issues = [
        {"layer": "L1", "severity": i["severity"], "check": i["check"],
         "refs": i["refs"], "message": i["message"]}
        for i in l1["issues"]
    ]

    erc_report = erc_mod.run(schematic_path)
    if erc_report["status"] == "OK":
        issues.extend(erc_report["issues"])

    l2 = None
    if run_l2 and l1["status"] == "PASS":
        dump = hierarchy.format_for_claude(schematic_path)
        l2 = validator.validate(dump)
        for tier in ("critical", "high", "medium"):
            for raw in l2.get(tier) or []:
                issues.append({"layer": "L2", "severity": tier,
                               "check": "RULE", "refs": "", "message": raw})

    return {"l1": l1, "erc": erc_report, "l2": l2, "issues": issues}


def _filter_for_fix(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sev = set(_cfg()["gates"]["fix_severities"])
    return [i for i in issues if i["severity"] in sev]


def _is_blocking(issues: List[Dict[str, Any]]) -> bool:
    sev = set(_cfg()["gates"]["success_blocking_severities"])
    return any(i["severity"] in sev for i in issues)


def _format_issues_for_prompt(issues: List[Dict[str, Any]]) -> str:
    lines = []
    for i in issues:
        ref = f" {i['refs']}" if i["refs"] else ""
        lines.append(f"- [{i['layer']}/{i['severity']}] {i['check']}:{ref} {i['message']}")
    return "\n".join(lines)


def _ask_claude_for_ops(
    schematic_path: str, issues: List[Dict[str, Any]], client: ClaudeClient
) -> Dict[str, Any]:
    safety = _cfg()["safety"]
    max_ops = int(safety["max_ops_per_iteration"])
    allow_delete = bool(safety["allow_delete_component"])

    dump = hierarchy.format_for_claude(schematic_path)
    user_msg = (
        f"=== CURRENT SCHEMATIC ===\n{dump}\n\n"
        f"=== ISSUES TO FIX ===\n{_format_issues_for_prompt(issues)}\n\n"
        f"=== CONSTRAINTS ===\n"
        f"- Emit at most {max_ops} ops in this batch (most-impactful first).\n"
        f"- delete_component is {'ALLOWED' if allow_delete else 'FORBIDDEN'}.\n"
    )
    raw = client.ask(system=_render_fix_prompt(), user=user_msg)
    parsed = _extract_json(raw) or {"message": raw, "ops": []}
    ops = parsed.get("ops") or []
    if not allow_delete:
        ops = [op for op in ops if (op.get("op") or op.get("type")) != "delete_component"]
    if len(ops) > max_ops:
        ops = ops[:max_ops]
    parsed["ops"] = ops
    parsed.setdefault("message", "")
    return parsed


def _apply_ops(
    doc: SchematicDocument, ops: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    results = []
    for op in ops:
        results.append({"op": op.get("op") or op.get("type"), **apply_operation(doc, op)})
    if any(r.get("ok") for r in results):
        doc.save()
    return results


def diagnose(schematic_path, include_l2: bool = False) -> Dict[str, Any]:
    """Read-only inspection. Defaults to L1 only — pure local, no API call, no cost.

    Pass include_l2=True to additionally run the LLM validator (read-only but
    incurs token cost). The fix loop itself always runs L2 by default; use
    diagnose() to peek at L1 issues before deciding whether to spend tokens.
    """
    return _gather_issues(str(schematic_path), run_l2=include_l2)


def fix(schematic_path, model: Optional[str] = None) -> Dict[str, Any]:
    """Run the validate->fix loop. Returns a structured trace.

    The schematic file is mutated in place; a .bak copy is written before the
    first edit when fixer_config.safety.backup_before_apply is true.
    """
    cfg = _cfg()
    max_iters = int(cfg["loop"]["max_iterations"])
    stop_no_progress = bool(cfg["loop"]["stop_on_no_progress"])
    backup = bool(cfg["safety"]["backup_before_apply"])
    claude_cfg = cfg["claude"]
    chosen_model = model or claude_cfg.get("model")
    client = ClaudeClient(model=chosen_model) if chosen_model else ClaudeClient()

    path = Path(schematic_path)
    if backup:
        bak = path.with_suffix(path.suffix + ".prefix.bak")
        if not bak.exists():
            shutil.copy2(path, bak)

    iterations: List[Dict[str, Any]] = []
    last_count: Optional[int] = None
    plateau_streak = 0

    # Pre-pass: run the deterministic rule applier (free, no API call). Pulls
    # the easy "every IC needs decoupling, every crystal needs load caps, every
    # I2C bus needs pull-ups" defects off the table before Claude even sees the
    # schematic — saves tokens and keeps Claude focused on judgement calls.
    pre_pass = rules.apply_all(str(path))

    for i in range(1, max_iters + 1):
        report = _gather_issues(str(path))
        all_issues = report["issues"]
        fixable = _filter_for_fix(all_issues)
        blocking = _is_blocking(all_issues)

        step: Dict[str, Any] = {
            "iteration": i,
            "issue_count": len(all_issues),
            "fixable_count": len(fixable),
            "blocking": blocking,
            "issues": all_issues,
            "ops": [],
            "apply_results": [],
            "claude_message": "",
        }
        iterations.append(step)

        if not blocking and not fixable:
            step["note"] = "no blocking issues; loop terminating"
            break

        if not fixable:
            step["note"] = "blocking issues exist but none are in fix_severities; nothing to attempt"
            break

        if last_count is not None and len(all_issues) >= last_count:
            plateau_streak += 1
            if stop_no_progress and plateau_streak >= 2:
                step["note"] = f"no progress for {plateau_streak} iterations; stopping"
                break
        else:
            plateau_streak = 0
        last_count = len(all_issues)

        proposal = _ask_claude_for_ops(str(path), fixable, client)
        step["claude_message"] = proposal.get("message", "")
        step["ops"] = proposal.get("ops") or []

        if not step["ops"]:
            step["note"] = "Claude proposed zero ops; stopping"
            break

        doc = SchematicDocument(str(path))
        step["apply_results"] = _apply_ops(doc, step["ops"])

    final = _gather_issues(str(path))
    final_blocking = _is_blocking(final["issues"])
    return {
        "path": str(path),
        "status": "PASS" if not final_blocking else "FAIL",
        "pre_pass": pre_pass,
        "iterations": iterations,
        "final": final,
    }


def to_text(result: Dict[str, Any]) -> str:
    lines = [f"FIXER {result['status']} - {result['path']}"]
    for it in result["iterations"]:
        lines.append("")
        lines.append(f"--- iteration {it['iteration']} ---")
        lines.append(f"  issues: {it['issue_count']} (fixable: {it['fixable_count']}, blocking: {it['blocking']})")
        if it.get("claude_message"):
            lines.append(f"  claude: {it['claude_message']}")
        if it["ops"]:
            for op, r in zip(it["ops"], it["apply_results"]):
                tag = "ok" if r.get("ok") else "FAIL"
                name = op.get("op") or op.get("type")
                lines.append(f"    [{tag}] {name}: {r.get('message','')}")
        if it.get("note"):
            lines.append(f"  note: {it['note']}")
    final = result["final"]
    lines.append("")
    lines.append(f"FINAL: {len(final['issues'])} issues remaining")
    for i in final["issues"][:20]:
        lines.append(f"  [{i['layer']}/{i['severity']}] {i.get('check','')} {i['refs']}: {i['message']}")
    return "\n".join(lines)
