import json
import re
from typing import Any, Dict, Optional

from .claude_client import ClaudeClient
from .rules import METHODOLOGY, render_for_prompt


SYSTEM_PROMPT = f"""You are a senior electronics engineer reviewing a KiCAD schematic for design correctness.

You will receive a structured dump of a schematic (component list, labels, wires, power rails)
and must check it against the rules below. Cite the exact reference designator (e.g. "U1", "R3")
whenever you raise an issue. Do not invent components that are not in the dump.

{METHODOLOGY}

DESIGN RULES (synthesized from KLC, IEEE 315, IEC 60617/60062, IPC-2612, and 35+ industry sources):

{render_for_prompt()}

RESPONSE FORMAT — reply with a single JSON object, no prose, no code fences:

{{
  "status": "PASS" | "NEEDS_FIXES",
  "score": 0-100,
  "critical": ["RULE_ID — refs — what is wrong — fix", ...],
  "high":     ["RULE_ID — refs — what is wrong — fix", ...],
  "medium":   ["RULE_ID — refs — what is wrong — fix", ...],
  "warnings": ["short note", ...],
  "recommendations": ["short note", ...],
  "checks": [
    {{"id": "POWER_001", "result": "pass|fail|na", "evidence": "...", "fix": "..."}},
    ...
  ]
}}

Rules:
- Cite refs verbatim from the dump.
- If the dump lacks information to judge a rule (no datasheet, no signal frequency,
  no current spec), mark it "na" and add one sentence to warnings.
- Each rule appears at most once in `checks`.
- An issue belongs to exactly one severity bucket; do not duplicate across critical/high/medium.
"""


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.S)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Try several extraction strategies. Returns None if every parse fails
    (e.g. response was truncated mid-string by max_tokens). Caller decides what
    to do with None instead of crashing the whole pipeline."""
    candidates = []
    m = _JSON_FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1))
    candidates.append(text.strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


# The L2 validator response carries per-rule findings for ~138 rules; the default
# 4000-token budget truncates mid-response on real schematics. Give it room.
_VALIDATE_MAX_TOKENS = 16000


def validate(text: str, model: Optional[str] = None) -> Dict[str, Any]:
    client = ClaudeClient(model=model) if model else ClaudeClient()
    raw = client.ask(system=SYSTEM_PROMPT, user=text, max_tokens=_VALIDATE_MAX_TOKENS)
    data = _extract_json(raw)
    if data is None:
        # Don't crash callers (fixer / serve / tests). Return a structured error
        # so the loop can either retry with more budget or surface it cleanly.
        return {
            "status": "ERROR",
            "score": None,
            "critical": [],
            "high": [],
            "medium": [],
            "warnings": ["validator: Claude reply was not parseable JSON (likely truncated by max_tokens)"],
            "recommendations": [],
            "checks": [],
            "_raw": raw,
            "_parse_error": True,
        }
    data.setdefault("status", "?")
    data.setdefault("score", None)
    for tier in ("critical", "high", "medium", "warnings", "recommendations"):
        data.setdefault(tier, [])
    data.setdefault("checks", [])
    data["_raw"] = raw
    return data
