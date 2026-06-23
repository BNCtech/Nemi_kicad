"""MCP tool: audit differential pairs in a .kicad_pcb.

Detects pairs matching `config/diff_pair_rules.json:patterns`, computes
intra-pair length skew, and reports pass/fail against the per-class
`max_skew_mm` tolerance. Read-only --- the routing version is a v2
follow-up.

Use when the user asks: 'check differential pairs', 'audit USB pair',
'measure pair length', 'diff pair skew', or 'are my high-speed pairs OK'.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from claude_agent_sdk import tool


@tool(
    name="audit_diff_pairs",
    description=(
        "Audit a .kicad_pcb for differential pair routing quality. "
        "Detects pairs from net names (USB_DP/USB_DM, *_P/*_N, ETH_*_P/N, "
        "CAN_H/CAN_L, LVDS_*_P/N) using config/diff_pair_rules.json, then "
        "computes intra-pair length skew and reports per-pair pass/fail "
        "against the class tolerance. Read-only --- does not route or "
        "modify the board.\n"
        "Args: {\"path\": \"<path to .kicad_pcb>\"}\n"
        "Output: per-pair length, skew, layer parity, and an OK/WARN flag."
    ),
    input_schema={"path": str},
)
async def audit_diff_pairs(args: Dict[str, Any]) -> Dict[str, Any]:
    raw = str(args.get("path", "")).strip()
    p = Path(raw).expanduser()
    if not p.exists():
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: not found: {p}"}],
            "is_error": True,
        }
    if p.suffix.lower() != ".kicad_pcb":
        return {
            "content": [{"type": "text",
                          "text": "ERROR: expected .kicad_pcb"}],
            "is_error": True,
        }

    # Lazy import keeps the tool importable when layout/ is partially
    # installed (e.g. config file present but module not yet shipped).
    try:
        from ..layout.diff_pair import audit_pcb
    except Exception as exc:
        return {
            "content": [{"type": "text",
                          "text": (f"ERROR: layout.diff_pair unavailable: "
                                    f"{type(exc).__name__}: {exc}")}],
            "is_error": True,
        }

    result = audit_pcb(p)

    lines = [f"Differential-pair audit: {p.name}"]
    n = result.get("pair_count", 0)
    if n == 0:
        lines.append("  no diff-pair candidates found in net list")
        if result.get("errors"):
            for e in result["errors"]:
                lines.append(f"  ERROR: {e}")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "ok": not result.get("errors"),
            "result": result,
        }

    lines.append(f"  detected {n} pair(s)")
    lines.append("")
    for pair in result["pairs"]:
        flag = "OK " if pair["ok"] else "WARN"
        layer_str = (pair["positive_layer"]
                     if pair["same_layer"]
                     else f"{pair['positive_layer']} / {pair['negative_layer']}")
        lines.append(
            f"  [{flag}] {pair['positive']:14} / {pair['negative']:14}  "
            f"class={pair['class']:14}  "
            f"len={pair['positive_length_mm']:6.2f} / {pair['negative_length_mm']:6.2f} mm  "
            f"skew={pair['skew_mm']:5.2f} (max {pair['max_skew_mm']:.2f}) mm  "
            f"layer={layer_str}"
        )
    if result.get("warnings"):
        lines.append("")
        lines.append("Warnings:")
        for w in result["warnings"]:
            lines.append(f"  - {w}")
    if result.get("errors"):
        lines.append("")
        lines.append("Errors:")
        for e in result["errors"]:
            lines.append(f"  - {e}")

    overall_ok = all(p_["ok"] for p_ in result["pairs"]) and not result.get("errors")
    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": overall_ok,
        "result": result,
    }
