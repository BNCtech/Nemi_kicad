"""Tool: run the declarative wiring-lint engine over a .kicad_sch.

This wires the previously-dormant `lint.engine.run_lint` into a reachable
surface. It parses the schematic into the canonical lint context
(`lint.context.build_context`) and runs EVERY rule in
`config/lint_rules.json` --- the existing R2/R4/R9/R11 selectors plus the
geometric-connectivity selectors that close the remaining items on the
"AI circuit generation" wiring checklist (the user's reference image).

Nothing is hardcoded here: which rules run, their severities, thresholds
(tolerances, near-miss band, etype lists) and fix hints all live in
`config/lint_rules.json`. Adding/removing a check is a JSON edit.

Read-only. Never mutates the schematic.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from claude_agent_sdk import tool

# The image's 10-point checklist, keyed by `checklist_item` in lint_rules.json
# so the report mirrors the reference card the user works from.
_CHECKLIST = {
    1: "Pin-to-wire connected properly?",
    2: "Any floating wires?",
    3: "Any wire through symbol?",
    4: "Missing junction dots?",
    5: "Crossing wires mistaken as connected?",
    6: "Net labels touching wire?",
    7: "Unused pins marked as NC?",
    8: "Wire overlapping text?",
    9: "Acute angle routing used?",
    10: "Any open wire ends?",
}

_SEV_ORDER = {"error": 0, "warning": 1, "info": 2}


def _rule_checklist_map() -> Dict[str, int]:
    """rule-id -> checklist item, read from config (no hardcoding)."""
    cfg_path = (Path(__file__).resolve().parent.parent
                / "config" / "lint_rules.json")
    out: Dict[str, int] = {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for r in cfg.get("rules") or []:
        ci = r.get("checklist_item")
        if isinstance(ci, int):
            out[str(r.get("id"))] = ci
    # R2 covers checklist #3 but predates the checklist_item field; map it.
    out.setdefault("R2", 3)
    out.setdefault("R11", 8)
    return out


def _run(sch: Path) -> Dict[str, Any]:
    from ..lint.context import build_context
    from ..lint.engine import run_lint

    ctx = build_context(sch)
    issues = run_lint(ctx)
    return {
        "ok": True,
        "counts": {
            "wires": len(ctx["wires"]),
            "pins": len(ctx["pins"]),
            "labels": len(ctx["labels"]),
            "junctions": len(ctx["junctions"]),
            "no_connects": len(ctx["no_connects"]),
            "blocks": len(ctx.get("blocks", [])),
        },
        "issues": issues,
    }


@tool(
    name="lint_schematic",
    description=(
        "Run the full wiring-lint checklist over a .kicad_sch and report "
        "every violation grouped by severity + mapped to the 10-point AI "
        "circuit-generation checklist (pin-to-wire, floating wires, wire "
        "through symbol, missing junction dots, crossing-vs-connected, net "
        "labels on wire, unused-pin NC marks, wire-over-text, acute angles, "
        "open wire ends). Rules + thresholds come from config/lint_rules.json "
        "--- nothing hardcoded. Read-only.\n"
        "Use when the user asks 'lint the schematic', 'check wire connections', "
        "'run the wiring checklist', 'find connection gaps', 'wire check "
        "pannu' (Tanglish).\n"
        "Args:\n"
        '  {"sch_path": "C:/.../proj.kicad_sch"}\n'
    ),
    input_schema={"sch_path": str},
)
async def lint_schematic(args: dict[str, Any]) -> dict[str, Any]:
    sch = Path(str(args.get("sch_path", "")).strip()).expanduser()
    if not sch.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {sch}"}],
                "is_error": True}
    if sch.suffix.lower() != ".kicad_sch":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_sch"}],
                "is_error": True}

    try:
        res = _run(sch)
    except Exception as exc:
        return {"content": [{"type": "text",
                             "text": f"ERROR: {type(exc).__name__}: {exc}"}],
                "is_error": True}

    issues: List[Dict[str, Any]] = res["issues"]
    ci_map = _rule_checklist_map()
    by_sev: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for it in issues:
        by_sev[it.get("severity", "info")].append(it)

    c = res["counts"]
    lines = [f"Wiring lint: {sch.name}"]
    lines.append(
        f"  scanned: {c['wires']} wires, {c['pins']} pins, "
        f"{c['labels']} labels, {c['junctions']} junctions, "
        f"{c['no_connects']} no-connects, {c.get('blocks', 0)} blocks"
    )
    lines.append("")

    n_err = len(by_sev.get("error", []))
    n_warn = len(by_sev.get("warning", []))
    n_info = len(by_sev.get("info", []))
    if not issues:
        lines.append("  ✓ clean --- all checklist items pass")
    else:
        lines.append(f"  {n_err} error(s), {n_warn} warning(s), {n_info} info")
        lines.append("")
        for sev in ("error", "warning", "info"):
            group = by_sev.get(sev, [])
            if not group:
                continue
            mark = {"error": "✗", "warning": "⚠", "info": "ⓘ"}[sev]
            lines.append(f"  {mark} {sev.upper()} ({len(group)}):")
            for it in group[:25]:
                rid = it.get("id", "?")
                ci = ci_map.get(rid)
                tag = f"[#{ci}] " if ci else ""
                lines.append(f"    - {tag}{rid}: {it.get('message', '')}")
                hint = it.get("fix_hint")
                if hint:
                    lines.append(f"        fix: {hint}")
            if len(group) > 25:
                lines.append(f"    ... (+{len(group) - 25} more)")
            lines.append("")

    # Per-checklist roll-up so the user sees the image's 10 items directly.
    hit = defaultdict(int)
    for it in issues:
        ci = ci_map.get(it.get("id", ""))
        if ci:
            hit[ci] += 1
    lines.append("  Checklist:")
    for n in sorted(_CHECKLIST):
        status = f"✗ {hit[n]}" if hit.get(n) else "✓"
        lines.append(f"    {status:>4}  #{n} {_CHECKLIST[n]}")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": n_err == 0,
        "errors": n_err,
        "warnings": n_warn,
        "info": n_info,
        "issues": issues,
    }
