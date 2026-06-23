"""self_heal node --- run ERC after preview, try deterministic autofix,
escalate residuals back to the architect when they need IR-level work.

Flow:
  1. erc_check on stats.path. If 0 errors -> done.
  2. erc_autofix(apply=True) -- the existing per-fix incremental loop
     with cascade rollback.
  3. If 0 residual errors after autofix -> done.
  4. Budget gate: fix_attempt >= max_fix_attempts -> done (with errors
     surfaced in erc_summary).
  5. Classify residual violations. If any type is in
     `llm_only_violation_types` from self_heal_config.json, escalate:
     set `feedback` to a formatted summary, reset `attempt` (so the
     architect's validate-retry budget is fresh), bump `fix_attempt`.
  6. Otherwise stop -- the architect cannot help with violations the
     deterministic pass already rejected.

Non-breaking: gated by `enabled=true` in self_heal_config.json. Set
false to fall back to current behaviour (preview is the terminal node).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..error_formatter import load_self_heal_config, summarize_erc_for_llm
from ...tools.erc_autofix import _parse_erc_report


async def self_heal_node(state: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_self_heal_config()
    if not cfg.get("enabled", True):
        return {}

    stats = state.get("stats") or {}
    sch_path = stats.get("path", "")
    if not sch_path or not Path(sch_path).exists():
        return {}

    # Lazy imports -- avoid pulling kicad-cli wrappers when disabled.
    from ...tools.erc_autofix import erc_autofix
    from ...tools.erc_check import erc_check

    # Step 1: snapshot ERC. Zero errors -> nothing to do.
    pre_res = await erc_check.handler({"path": sch_path})
    if pre_res.get("is_error"):
        return {"erc_summary": {"errors": -1, "skipped": True}}
    try:
        pre = json.loads(pre_res["content"][0]["text"])
    except Exception:
        return {"erc_summary": {"errors": -1, "skipped": True}}
    pre_errors = int(pre.get("error_count", 0))
    pre_warnings = int(pre.get("warning_count", 0))
    if pre_errors == 0:
        return {
            "erc_summary": {
                "errors": 0,
                "warnings": pre_warnings,
            },
        }

    # Step 2: deterministic autofix with incremental validation.
    af_res = await erc_autofix.handler({"path": sch_path, "apply": True})
    af_applied = (af_res or {}).get("applied") or {}
    final_errors = int(af_applied.get("final_errors", pre_errors))

    if final_errors == 0:
        return {
            "erc_summary": {
                "errors": 0,
                "deterministic_fixed": True,
                "baseline_errors": int(af_applied.get("baseline_errors", pre_errors)),
            },
        }

    # Step 3: budget gate.
    fix_attempt = int(state.get("fix_attempt", 0))
    max_attempts = int(cfg.get("max_fix_attempts", 2))
    if fix_attempt >= max_attempts:
        return {
            "erc_summary": {
                "errors": final_errors,
                "exhausted": True,
                "fix_attempt": fix_attempt,
            },
        }

    # Step 4: classify residuals by re-parsing the autofix's report.
    sch = Path(sch_path)
    rpt = sch.with_suffix(".erc.txt")
    residual: list = []
    if rpt.exists():
        try:
            residual = _parse_erc_report(rpt.read_text(encoding="utf-8"))
        except OSError:
            residual = []

    llm_only = set(cfg.get("llm_only_violation_types") or [])
    needs_llm = any((v.get("type") or "") in llm_only for v in residual)
    if not needs_llm or not residual:
        # Residuals are all of types the deterministic pass already
        # tried and rolled back. Escalating won't help -- the architect
        # can't fix what the engine deemed a cascade risk.
        return {
            "erc_summary": {
                "errors": final_errors,
                "stuck": True,
                "residual_types": sorted({(v.get("type") or "") for v in residual}),
            },
        }

    # Step 5: escalate. Reset `attempt` so the architect gets a fresh
    # validate-retry window; bump `fix_attempt`.
    summary = summarize_erc_for_llm(residual, ir=state.get("ir"))
    return {
        "erc_summary": {
            "errors": final_errors,
            "escalating": True,
            "fix_attempt": fix_attempt + 1,
        },
        "fix_attempt": fix_attempt + 1,
        "fix_feedback": summary,
        "feedback": summary,
        "attempt": 0,
    }
