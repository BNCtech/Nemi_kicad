"""Tool: assess a NEW whole-circuit build request BEFORE generating.

The front of the Intake Brain. The agent calls this first on any
build_circuit request; it returns a deterministic 3-way verdict the agent
then acts on in chat:

  ASK_QUESTIONS      -- too little info; ask only the missing items, stop.
  ARCHITECTURE_FIRST -- enough to sketch; show architecture, then confirm.
  PROCEED            -- complete; go straight to the normal build preview.

The classification, completeness score and decision all live in code +
config (intent/triage.py + config/intake_rules.json) so the verdict is
reproducible and unit-testable; the LLM only phrases the questions /
summary. Safety override: a safety_critical or high/critical-risk domain
(BMS, inverter, motor, charger) is NEVER returned as PROCEED --- it is
forced to at least ARCHITECTURE_FIRST so a power board is never silently
generated, even at a high completeness score.

When the feature is disabled (intake_rules.json:enabled=false) the tool
returns PROCEED immediately so the existing build path is unchanged.
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from ..intent.triage import (
    is_enabled,
    score_completeness,
    triage,
    _load_rules,
)

# Leniency order for the "never below ARCHITECTURE_FIRST" clamp.
_ASK = "ASK_QUESTIONS"
_ARCH = "ARCHITECTURE_FIRST"
_PROCEED = "PROCEED"


def _decide(triage_res: dict, score_res: dict, rules: dict) -> tuple[str, str]:
    """Return (decision, reason). Pure -- mirrors intake_rules._decision_doc."""
    ask_thr = int(rules.get("ask_threshold_pct", 40))
    arch_thr = int(rules.get("arch_threshold_pct", 80))
    high_levels = set(rules.get("high_risk_levels") or ["high", "critical"])
    safety_force = bool(rules.get("safety_force_architecture", True))
    direct_gen = set(rules.get("direct_generate_complexity") or ["Low"])

    score = int(score_res.get("score_pct", 0))
    missing = score_res.get("missing_fields") or []
    risk = str(triage_res.get("risk_level", "low"))
    safety = bool(triage_res.get("safety_critical", False))
    complexity = str(triage_res.get("complexity", "Low"))

    # (1) Safety override -- never silently generate a critical/power board,
    # even at a high completeness score.
    if safety_force and (safety or risk in high_levels):
        if missing:
            return _ASK, (
                f"{triage_res.get('domain', 'design')} is safety-critical "
                f"(risk={risk}); {len(missing)} detail(s) missing -> ask first"
            )
        return _ARCH, (
            f"{triage_res.get('domain', 'design')} is safety-critical "
            f"(risk={risk}); review architecture before building "
            f"(score {score}%)"
        )

    # (2) Low-complexity direct generate -- honours "IF Complexity = Low
    # THEN Direct Generate". A simple low-risk board is not questioned.
    if complexity in direct_gen:
        return _PROCEED, (
            f"{complexity} complexity, low risk -> direct generate "
            f"(score {score}%)"
        )

    # (3) Completeness 3-way for medium/high-complexity non-safety designs.
    if score < ask_thr:
        base = _ASK
    elif score <= arch_thr:
        base = _ARCH
    else:
        base = _PROCEED
    reason = (
        f"completeness {score}% "
        f"(ask<{ask_thr} / arch<={arch_thr} / proceed>{arch_thr}) -> {base}"
    )
    return base, reason


@tool(
    name="assess_request",
    description=(
        "Call this FIRST on any NEW whole-circuit build request, BEFORE "
        "previewing or building. Classifies the request (project type, "
        "risk, complexity), scores whether enough info was given, and "
        "returns a `decision`: ASK_QUESTIONS (ask only the listed missing "
        "items, then stop), ARCHITECTURE_FIRST (show the architecture "
        "summary using the returned parts/blocks/estimated size, then end "
        "with 'Want me to build it?'), or PROCEED (go straight to the "
        "normal build preview). Do NOT call for edits to an existing "
        "schematic, for questions, or for the user's reply AFTER you "
        "already asked the intake questions."
    ),
    input_schema={"prompt": str},
)
async def assess_request(args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(args.get("prompt", "") or "").strip()

    if not is_enabled():
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "decision": _PROCEED,
                    "reason": "intake disabled",
                }),
            }],
        }

    rules = _load_rules()
    t = triage(prompt)
    sc = score_completeness(prompt, t)
    decision, reason = _decide(t, sc, rules)

    max_q = int(rules.get("max_questions", 4))
    result = {
        "decision": decision,
        "reason": reason,
        "project_type": t["project_type"],
        "domain": t["domain"],
        "complexity": t["complexity"],
        "estimated_size": t["estimated_size"],
        "safety_critical": t["safety_critical"],
        "power_electronics": t["power_electronics"],
        "risk_level": t["risk_level"],
        "blocks": t["blocks"],
        "named_parts": t["named_parts"],
        "score_pct": sc["score_pct"],
        "present_fields": sc["present_fields"],
        "missing_fields": sc["missing_fields"],
        "suggested_questions": (sc["suggested_questions"] or [])[:max_q],
    }
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(result, indent=2),
        }],
    }
