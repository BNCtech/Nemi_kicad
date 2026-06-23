"""MCP tool: produce a routing PROPOSAL for differential pairs.

Read-only sibling of `audit_diff_pairs`. Where audit reports the
current state, this tool produces a PLAN: per pair, the recommended
width/gap/skew/layer (from `config/diff_pair_rules.json`) and an
`action` field telling the user whether the existing route is OK or
needs to be reworked for skew / layer / both.

Does NOT modify the .kicad_pcb. The plan is human-in-the-loop input:
the operator routes by hand in pcbnew. A v2 tool can execute the plan
via `route_pcb_simple` primitives.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from claude_agent_sdk import tool


@tool(
    name="plan_diff_pair_routes",
    description=(
        "Produce a routing PROPOSAL for every differential pair detected "
        "in a .kicad_pcb. Per pair: current width/skew/layer state + the "
        "recommended geometry from diff_pair_rules.json + an action flag "
        "(ok / rework_skew / rework_layer / rework_both). Read-only --- "
        "does not route. Use when the user asks: 'plan the diff pair "
        "routes', 'what should USB pair look like', 'recommend diff pair "
        "geometry'.\n"
        "Args: {\"path\": \"<path to .kicad_pcb>\"}\n"
        "Output: plan[] with one entry per detected pair."
    ),
    input_schema={"path": str},
)
async def plan_diff_pair_routes(args: Dict[str, Any]) -> Dict[str, Any]:
    raw = str(args.get("path", "")).strip()
    p = Path(raw).expanduser()
    if not p.exists():
        return {
            "content": [{"type": "text", "text": f"ERROR: not found: {p}"}],
            "is_error": True,
        }
    if p.suffix.lower() != ".kicad_pcb":
        return {
            "content": [{"type": "text", "text": "ERROR: expected .kicad_pcb"}],
            "is_error": True,
        }

    try:
        from ..layout.diff_pair import plan_routes
    except Exception as exc:
        return {
            "content": [{"type": "text",
                          "text": (f"ERROR: layout.diff_pair unavailable: "
                                    f"{type(exc).__name__}: {exc}")}],
            "is_error": True,
        }

    result = plan_routes(p)
    plan = result.get("plan") or []

    lines = [f"Diff-pair routing plan: {p.name}"]
    if not plan:
        lines.append("  no diff-pair candidates detected")
        if result.get("errors"):
            for e in result["errors"]:
                lines.append(f"  ERROR: {e}")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": not result.get("errors"),
            "result": result,
        }
    lines.append(f"  detected {len(plan)} pair(s) --- proposal below")
    lines.append("")
    for entry in plan:
        rec = entry["recommendation"]
        cur = entry["current_state"]
        lines.append(
            f"  [{entry['action']:13}] {entry['positive']:14} / "
            f"{entry['negative']:14}  class={entry['class']}"
        )
        lines.append(
            f"      current : len={cur['positive_length_mm']:6.2f} / "
            f"{cur['negative_length_mm']:6.2f} mm  "
            f"skew={cur['skew_mm']:.2f}  "
            f"layer={cur['positive_layer'] or '-'} / "
            f"{cur['negative_layer'] or '-'}"
        )
        lines.append(
            f"      propose : width={rec['preferred_width_mm']:.3f} mm  "
            f"gap={rec['preferred_gap_mm']:.3f} mm  "
            f"max_skew={rec['max_skew_mm']:.2f} mm  "
            f"layer={rec['preferred_layer']}"
        )
        lines.append(f"      reason  : {rec['rationale']}")
        lines.append("")
    overall_ok = all(p_["action"] == "ok" for p_ in plan)
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": overall_ok,
        "result": result,
    }
