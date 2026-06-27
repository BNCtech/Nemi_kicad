"""Tool: schematic_quality — the scored "is my schematic good?" reviewer.

The SCHEMATIC counterpart of ``pcb_quality``. Instead of the agent
free-forming a generic, single-circuit prose review (the failure mode the
user hit: "move R1 closer, add a 100nF, C3 = 100nF" — advice not grounded
in THIS file), this runs the two engines that already audit a ``.kicad_sch``
*directly* and folds them into ONE scored card:

    SCHEMATIC QUALITY — proj.kicad_sch
    Electrical       100%
    Wiring            88%
    ─────────────────────
    Overall: 96% — EXCELLENT

    ⚠ R3.1: net label sits on a wire (move the label off the wire)
       Fix → apply_ops

Two dynamic, no-hardcode sources (both parse the real file — nothing about
any particular circuit is baked in, so an NE555 blinker and a 200-part BMS
are reviewed identically):

  Electrical  — kicad-cli ERC (the authoritative netlist-level check:
                floating pins, missing power driver, conflicting outputs).
  Wiring      — the declarative lint engine over config/lint_rules.json
                (the 10-point AI-generation wiring checklist).

Design rules (mirrors pcb_quality):
  * READ-ONLY. Never edits the schematic. Each finding names an EXISTING
    fix tool (erc_autofix for electrical, apply_ops for wiring) so the chat
    can offer a real follow-up action.
  * Additive + config-driven. Every penalty / weight / band lives in
    ``config/schematic_quality.json``; nothing hardcoded.
  * Degrades gracefully. A source that can't run (kicad-cli missing, a
    library symbol that won't resolve) is reported "n/a" and dropped from
    the weighted average rather than scoring the schematic to zero.

Part-aware completeness rules (decoupling per VDD pin, LDO caps, LED series
resistor, …) are enforced at GENERATION time by intent/validate.py against
design_checklist.json, where the live IR exists — see the note in the JSON.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def _cfg() -> Dict[str, Any]:
    """Load config/schematic_quality.json. Same loader convention as the
    rest of the tools — read the JSON next to this package, tolerate a
    missing/broken file by returning {} (callers apply built-in defaults)."""
    try:
        p = (Path(__file__).resolve().parent.parent
             / "config" / "schematic_quality.json")
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _band_for(score: float, bands: List[Tuple[float, str]]) -> str:
    for thr, label in bands:
        if score >= thr:
            return label
    return bands[-1][1] if bands else "?"


# --------------------------------------------------------------------------- #
# Dimension runners — each returns (score|None, findings, stat_dict)
# None score == dimension could not run -> dropped from the average.
# --------------------------------------------------------------------------- #

async def _run_electrical(sch: Path, cfg: Dict[str, Any]
                          ) -> Tuple[Optional[float], List[Dict[str, Any]], Dict[str, Any]]:
    ec = dict(cfg.get("electrical", {}) or {})
    err_pen = float(ec.get("error_penalty", 12.0))
    warn_pen = float(ec.get("warning_penalty", 3.0))
    max_find = int(ec.get("max_findings", 8))
    fix_tool = str(ec.get("fix_tool", "erc_autofix"))

    ERC = importlib.import_module("envil_agent.tools.erc_check")
    r = await ERC.erc_check.handler({"path": str(sch)})
    if r.get("is_error"):
        reason = str(r.get("content", [{}])[0].get("text", "ERC unavailable")
                     ).splitlines()[0][:160]
        return None, [], {"available": False, "reason": reason}

    # erc_check returns its summary as JSON inside the content text block.
    try:
        summary = json.loads(r["content"][0]["text"])
    except Exception:
        return None, [], {"available": False, "reason": "ERC report unparseable"}

    n_err = int(summary.get("error_count", 0) or 0)
    n_warn = int(summary.get("warning_count", 0) or 0)
    score = max(0.0, 100.0 - err_pen * n_err - warn_pen * n_warn)

    findings: List[Dict[str, Any]] = []
    for it in (summary.get("issues") or [])[:max_find]:
        sev = str(it.get("severity", "warning"))
        typ = str(it.get("type", "")).strip()
        msg = str(it.get("message", "")).strip()
        label = f"{typ}: {msg}" if typ and msg else (typ or msg or "ERC violation")
        findings.append({
            "severity": "error" if sev == "error" else "warn",
            "message": label[:160],
            "reason": "kicad-cli ERC (authoritative netlist check)",
            "fix_tool": fix_tool,
        })
    return score, findings, {"available": True, "errors": n_err, "warnings": n_warn}


async def _run_wiring(sch: Path, cfg: Dict[str, Any]
                      ) -> Tuple[Optional[float], List[Dict[str, Any]], Dict[str, Any]]:
    wc = dict(cfg.get("wiring", {}) or {})
    err_pen = float(wc.get("error_penalty", 8.0))
    warn_pen = float(wc.get("warning_penalty", 2.0))
    info_pen = float(wc.get("info_penalty", 0.0))
    max_find = int(wc.get("max_findings", 8))
    fix_tool = str(wc.get("fix_tool", "apply_ops"))

    LINT = importlib.import_module("envil_agent.tools.lint_schematic")
    r = await LINT.lint_schematic.handler({"sch_path": str(sch)})
    if r.get("is_error"):
        reason = str(r.get("content", [{}])[0].get("text", "lint unavailable")
                     ).splitlines()[0][:160]
        return None, [], {"available": False, "reason": reason}

    n_err = int(r.get("errors", 0) or 0)
    n_warn = int(r.get("warnings", 0) or 0)
    n_info = int(r.get("info", 0) or 0)
    score = max(0.0, 100.0 - err_pen * n_err - warn_pen * n_warn - info_pen * n_info)

    rank = {"error": 0, "warning": 1, "info": 2}
    issues = sorted((r.get("issues") or []),
                    key=lambda x: rank.get(x.get("severity", "info"), 3))
    findings: List[Dict[str, Any]] = []
    for it in issues[:max_find]:
        sev = str(it.get("severity", "info"))
        rid = str(it.get("id", "?"))
        msg = str(it.get("message", "")).strip()
        # The lint message ALREADY carries the actionable instruction; the
        # separate fix_hint often appends engine-internal detail ("Engine
        # support: enable …") that reads as truncated noise here. Keep the
        # message only — it is the human-facing finding.
        label = f"{rid}: {msg}"
        findings.append({
            "severity": {"error": "error", "warning": "warn"}.get(sev, "info"),
            "message": label[:200],
            "reason": "wiring lint (config/lint_rules.json checklist)",
            "fix_tool": fix_tool,
        })
    return score, findings, {"available": True, "errors": n_err,
                             "warnings": n_warn, "info": n_info}


# --------------------------------------------------------------------------- #
# Card
# --------------------------------------------------------------------------- #

def _compose_card(name: str, dims: Dict[str, Optional[float]],
                  findings: List[Dict[str, Any]], cfg: Dict[str, Any]
                  ) -> Tuple[str, float]:
    weights = {k: float(v) for k, v in (cfg.get("weights", {}) or {}).items()
               if not k.startswith("_")}
    if not weights:
        weights = {"Electrical": 0.65, "Wiring": 0.35}
    bands = [(float(t), str(l)) for t, l in cfg.get("bands",
             [[90, "EXCELLENT"], [75, "GOOD"], [60, "REVIEW"], [0, "NEEDS WORK"]])]

    num = den = 0.0
    for d, sc in dims.items():
        if sc is None:
            continue
        w = float(weights.get(d, 0.0))
        num += w * sc
        den += w
    overall = (num / den) if den else 0.0

    lines = [f"# SCHEMATIC QUALITY — {name}"]
    for d in ("Electrical", "Wiring"):
        sc = dims.get(d)
        lines.append(f"  {d:<14}{'n/a' if sc is None else f'{sc:5.0f}%'}")
    lines.append("  " + "─" * 21)
    lines.append(f"  **Overall: {overall:.0f}% — {_band_for(overall, bands)}**")

    if findings:
        lines.append("")
        order = {"error": 0, "warn": 1, "info": 2}
        for f in sorted(findings, key=lambda x: order.get(x["severity"], 3)):
            mark = {"error": "✗", "warn": "⚠", "info": "•"}.get(f["severity"], "•")
            lines.append(f"  {mark} {f['message']}")
            lines.append(f"      Fix → {f['fix_tool']}")
    else:
        lines.append("")
        lines.append("  ✓ no issues found")
    return "\n".join(lines), overall


# --------------------------------------------------------------------------- #
# Tool
# --------------------------------------------------------------------------- #

@tool(
    name="schematic_quality",
    description=(
        "Review a .kicad_sch like a senior engineer and return ONE scored "
        "card: Electrical % (kicad-cli ERC) + Wiring % (the wiring-lint "
        "checklist), an overall 'is my schematic good' score + verdict, and "
        "an ordered list of findings — each naming the existing fix tool "
        "(erc_autofix / apply_ops). READ-ONLY; never edits. Fully dynamic — "
        "works on any circuit, nothing hardcoded; all penalties/weights in "
        "config/schematic_quality.json. Use for 'review this schematic', "
        "'check my design', 'is this schematic good', 'find design issues', "
        "'review design', 'design review', 'schematic score'.\n"
        "Args:\n"
        '  {"sch_path": "C:/.../proj.kicad_sch"}\n'
    ),
    input_schema={"sch_path": str},
)
async def schematic_quality(args: dict[str, Any]) -> dict[str, Any]:
    sch = Path(str(args.get("sch_path", "")).strip()).expanduser()
    if not sch.exists():
        return {"content": [{"type": "text", "text": f"ERROR: not found: {sch}"}],
                "is_error": True}
    if sch.suffix.lower() != ".kicad_sch":
        return {"content": [{"type": "text", "text": "ERROR: expected .kicad_sch"}],
                "is_error": True}

    cfg = _cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "schematic_quality disabled in config"}],
                "is_error": True}

    e_score, e_find, e_stat = await _run_electrical(sch, cfg)
    w_score, w_find, w_stat = await _run_wiring(sch, cfg)

    dims: Dict[str, Optional[float]] = {"Electrical": e_score, "Wiring": w_score}
    findings = list(e_find) + list(w_find)

    # If NEITHER source ran, that's a real failure — say so rather than
    # printing a 0% card built from nothing.
    if e_score is None and w_score is None:
        why = "; ".join(filter(None, [e_stat.get("reason"), w_stat.get("reason")]))
        return {"content": [{"type": "text",
                             "text": f"ERROR: could not review {sch.name}: {why}"}],
                "is_error": True}

    card, overall = _compose_card(sch.name, dims, findings, cfg)
    return {
        "content": [{"type": "text", "text": card}],
        "ok": True,
        "sch_path": str(sch),
        "overall": round(overall, 1),
        "dimensions": dims,
        "fixes": findings,
        "stats": {"electrical": e_stat, "wiring": w_stat},
    }
