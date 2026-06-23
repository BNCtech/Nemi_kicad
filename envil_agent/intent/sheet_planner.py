"""Sheet planner --- keyword-driven block-decomposition hints for the architect.

The architect prompt (intent/architect_prompt.py) ships with worked
examples but no per-prompt heuristic. For a request like
"USB-C powered STM32 with I2C sensor", we scan the prompt for known
keywords and return ["PROTECTION", "POWER", "MCU", "SENSOR"] so the
architect has structured guidance, not just pattern-matching against
examples.

Strictly advisory: the architect can ignore the hint when the PINNED
RULE applies (single-IC circuit -> blocks=[]).

Config-driven: keyword -> blocks map lives in
`envil_agent/config/sheet_planner_rules.json`. No Python literals
beyond the loader. Adding a new keyword family is a one-line JSON edit.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import List

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "sheet_planner_rules.json"
)


@lru_cache(maxsize=1)
def _load_rules() -> dict:
    """Read the rules file once and cache. Returns {} on any read or
    parse error so the caller falls back to no-hint behaviour."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _is_single_ic_prompt(lower_prompt: str, rules: dict) -> bool:
    keywords = (
        (rules.get("single_ic_keywords") or {}).get("keywords") or []
    )
    return any(kw in lower_prompt for kw in keywords)


def recommend_blocks(prompt: str) -> List[str]:
    """Return suggested block names for a circuit prompt.

    Empty list means "no hint" --- either the prompt was empty, the rules
    file is missing or unreadable, or none of the keyword families matched.

    When the prompt matches a known single-IC pattern, returns [] to
    short-circuit the multi-block hint (the architect's PINNED RULE
    handles this case explicitly).
    """
    if not prompt:
        return []
    rules = _load_rules()
    keyword_rules = rules.get("keyword_rules") or []

    lower = prompt.lower()
    if _is_single_ic_prompt(lower, rules):
        return []

    seen: List[str] = []
    for rule in keyword_rules:
        keywords = rule.get("keywords") or []
        if not any(kw in lower for kw in keywords):
            continue
        for block in rule.get("blocks") or []:
            if block not in seen:
                seen.append(block)
    return seen


def build_sheet_hint_text(prompt: str) -> str:
    """Build the XML-tagged hint block to splice into the architect
    system prompt.

    Returns "" when no blocks are recommended --- keeps the prompt-cache
    prefix byte-identical for users whose prompt doesn't trigger any
    keyword. Cache hits matter (the architect system prompt is the
    biggest chunk of every call).
    """
    blocks = recommend_blocks(prompt)
    if not blocks:
        return ""
    return (
        "<sheet_hint>\n"
        "# Keyword-derived block hint for THIS request. The keywords in\n"
        "# the user prompt suggest this circuit has the following functional\n"
        "# domains. Emit `blocks=[...]` with at least these names (and any\n"
        "# others the topology obviously needs).\n"
        "#\n"
        "# When the PINNED RULE applies (single-IC circuit such as a bare\n"
        "# NE555, LM317, or op-amp filter) you MUST still emit blocks=[]\n"
        "# regardless of this hint.\n"
        "#\n"
        "# Suggested blocks: " + ", ".join(blocks) + "\n"
        "</sheet_hint>\n\n"
    )
