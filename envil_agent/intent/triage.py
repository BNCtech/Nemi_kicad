"""Intake Brain (Stages 1-3) --- deterministic intent/risk triage and
requirement-completeness scoring for a NEW whole-circuit build request.

This module is a thin orchestrator over three analyzers that ALREADY
exist; it adds no new scoring math of its own:

  * project_type / risk      <- config/intake_rules.json keyword table
  * blocks                   <- intent.sheet_planner.recommend_blocks
  * complexity / size proxy  <- graphs.nodes.decide_render_mode._estimate_complexity
                                (reusing render_mode_rules.json weights)
  * named parts              <- intent.pin_catalog.resolve_lib_ids_from_prompt

Everything tunable --- the type map, per-domain required fields, risk
flags, question wording and decision thresholds --- lives in
``config/intake_rules.json``. Adding a domain or a required field is a
one-line JSON edit, zero code.

The tool layer (tools/assess_request.py) wraps these two pure functions
with the 3-way decision (ASK_QUESTIONS / ARCHITECTURE_FIRST / PROCEED)
and the safety override. Keeping triage() and score_completeness() pure
and LLM-free makes the whole intake stage unit-testable.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from .sheet_planner import recommend_blocks
from .pin_catalog import resolve_lib_ids_from_prompt

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "intake_rules.json"
)
_RENDER_RULES_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "render_mode_rules.json"
)

# Low < Medium < High < Critical --- used to combine the per-type floor
# complexity with the keyword-density-derived tier (take the higher).
_COMPLEXITY_ORDER = ["Low", "Medium", "High", "Critical"]


@lru_cache(maxsize=1)
def _load_rules() -> dict:
    """Read intake_rules.json once and cache. Returns {} on any read or
    parse error so callers degrade to the disabled / no-op path."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def _load_render_rules() -> dict:
    try:
        return json.loads(_RENDER_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_enabled() -> bool:
    return bool(_load_rules().get("enabled", True))


def ask_assumptions_enabled() -> bool:
    """Gate for the Cursor-style 'ask before assuming' step: before any build
    preview the agent asks one round of chip-questions about the design choices
    it would otherwise assume (supply voltage, package, indicator, key values),
    even for simple/complete prompts. Reads
    intake_rules.json:always_ask_assumptions (default True). On any error -> ON."""
    return bool(_load_rules().get("always_ask_assumptions", True))


def _estimate_complexity_score(prompt_lower: str) -> int:
    """Reuse the EXACT keyword-weight loop from decide_render_mode so the
    intake estimate and the render-mode decision can never disagree. Falls
    back to a local sum only if that import is unavailable."""
    signals = _load_render_rules().get("component_estimate_signals") or {}
    try:
        from ..graphs.nodes.decide_render_mode import _estimate_complexity
        return int(_estimate_complexity(prompt_lower, signals))
    except Exception:
        total = 0
        for kw, weight in signals.items():
            if kw and not kw.startswith("_") and kw.lower() in prompt_lower:
                try:
                    total += int(weight)
                except (TypeError, ValueError):
                    continue
        return total


def _score_to_tier(score: int) -> str:
    """Map a keyword-weight sum to a complexity tier using the SAME
    thresholds decide_render_mode uses for its render decision."""
    thr = _load_render_rules().get("complexity_thresholds") or {}
    hier = int(thr.get("hierarchy_when_complexity_at_least", 65))
    single = int(thr.get("single_sheet_blocks_when_complexity_at_least", 12))
    if score >= hier:
        return "High"
    if score >= single:
        return "Medium"
    return "Low"


def _higher_tier(a: str, b: str) -> str:
    ia = _COMPLEXITY_ORDER.index(a) if a in _COMPLEXITY_ORDER else 0
    ib = _COMPLEXITY_ORDER.index(b) if b in _COMPLEXITY_ORDER else 0
    return _COMPLEXITY_ORDER[max(ia, ib)]


def _match_project_type(prompt_lower: str, rules: dict) -> dict:
    """First keyword match wins; `generic` (empty keywords) is the
    fallback and is expected to be last in the config list."""
    fallback: dict = {}
    for entry in rules.get("project_types") or []:
        kws = entry.get("keywords") or []
        if not kws:
            fallback = entry
            continue
        if any(kw.lower() in prompt_lower for kw in kws):
            return entry
    return fallback or {
        "type": "generic", "label": "Generic Circuit", "complexity": "Low",
        "risk": {"safety_critical": False, "power_electronics": False,
                 "risk_level": "low"},
        "required_fields": [],
    }


def _estimate_sheets(n_blocks: int, score: int) -> int:
    """Rough sheet-count estimate: hierarchy designs span one sheet per
    block, everything else is a single sheet. Mirrors the render-mode
    hierarchy trigger so the preview number matches what will be built."""
    rr = _load_render_rules()
    cx_thr = rr.get("complexity_thresholds") or {}
    blk_thr = rr.get("block_count_thresholds") or {}
    hier_cx = int(cx_thr.get("hierarchy_when_complexity_at_least", 65))
    hier_blk = int(blk_thr.get("hierarchy_when_blocks_at_least", 6))
    if (n_blocks >= hier_blk) or (score >= hier_cx):
        return max(1, n_blocks)
    return 1


def triage(prompt: str) -> Dict[str, Any]:
    """Classify a build prompt. Pure + deterministic.

    Returns a dict with project_type, domain label, complexity tier,
    estimated_size, the risk flags, the recommended blocks and the set of
    parts the user already named. Safe on empty / weird input: falls
    through to the generic type.
    """
    rules = _load_rules()
    p_lower = (prompt or "").lower()

    entry = _match_project_type(p_lower, rules)
    risk = entry.get("risk") or {}

    score = _estimate_complexity_score(p_lower)
    blocks = recommend_blocks(prompt or "")
    n_blocks = len(blocks or [])

    # Final complexity = max(per-type floor, keyword-density tier). The
    # type floor guarantees a BMS reads "High" and an inverter "Critical"
    # even from a terse prompt; the density tier upgrades an otherwise
    # generic prompt that is clearly dense.
    type_floor = str(entry.get("complexity") or "Low")
    complexity = _higher_tier(type_floor, _score_to_tier(score))

    named_parts = resolve_lib_ids_from_prompt(prompt or "")

    return {
        "project_type": entry.get("type", "generic"),
        "domain": entry.get("label", entry.get("type", "generic")),
        "complexity": complexity,
        "complexity_score": score,
        "estimated_size": {
            "components": score,
            "sheets": _estimate_sheets(n_blocks, score),
        },
        "safety_critical": bool(risk.get("safety_critical", False)),
        "power_electronics": bool(risk.get("power_electronics", False)),
        "risk_level": str(risk.get("risk_level", "low")),
        "blocks": list(blocks or []),
        "named_parts": list(named_parts or []),
    }


def _field_present(field: dict, prompt_lower: str, has_named_part: bool) -> bool:
    """A required field counts as satisfied if ANY signal hits: a keyword
    substring, the regex, or a named part (when the field accepts one)."""
    for kw in field.get("satisfied_keywords") or []:
        if kw and kw.lower() in prompt_lower:
            return True
    rx = field.get("satisfied_regex")
    if rx:
        try:
            if re.search(rx, prompt_lower, re.IGNORECASE):
                return True
        except re.error:
            pass
    if field.get("satisfied_by_named_part") and has_named_part:
        return True
    return False


def score_completeness(
    prompt: str, triage_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Score how much of the per-domain required info the prompt gave.

    Returns score_pct (0-100), the present/missing field ids, and the
    plain-English questions for the missing ones (wording from config).
    Pure + deterministic; the tool layer turns this into a decision.
    """
    rules = _load_rules()
    p_lower = (prompt or "").lower()
    ptype = triage_result.get("project_type", "generic")
    has_named = bool(triage_result.get("named_parts"))

    entry = next(
        (e for e in rules.get("project_types") or []
         if e.get("type") == ptype),
        {},
    )
    required = entry.get("required_fields") or []
    total = len(required)

    if total == 0:
        return {
            "project_type": ptype, "score_pct": 100,
            "present_fields": [], "missing_fields": [],
            "suggested_questions": [],
        }

    present: List[str] = []
    missing: List[Dict[str, str]] = []
    questions: List[str] = []
    for field in required:
        fid = field.get("id", "")
        if _field_present(field, p_lower, has_named):
            present.append(fid)
        else:
            missing.append({"id": fid, "label": field.get("label", fid)})
            q = field.get("question") or f"Which {field.get('label', fid)}?"
            questions.append(q)

    score_pct = round(len(present) / total * 100)
    return {
        "project_type": ptype,
        "score_pct": score_pct,
        "present_fields": present,
        "missing_fields": missing,
        "suggested_questions": questions,
    }
