import json
import re
from typing import Any, Dict, Optional

from ._config_loader import load as _load_config, load_prompt as _load_prompt
from .claude_client import ClaudeClient
from .rules import METHODOLOGY, render_for_prompt


def _render_system_prompt() -> str:
    """Render the L2 system prompt from prompts/validator_system.md.

    Resolved at call time so edits to the .md template (or to rules.py)
    take effect after _config_loader.reload_all() without restart.
    """
    tpl = _load_prompt("validator_system")
    return (
        tpl
        .replace("{{METHODOLOGY}}", METHODOLOGY)
        .replace("{{RULES_CATALOG}}", render_for_prompt())
    )


from .json_utils import extract_json as _extract_json


def validate(text: str, model: Optional[str] = None) -> Dict[str, Any]:
    cfg = _load_config("validator_config")["claude"]
    chosen_model = model or cfg.get("model")
    client = ClaudeClient(model=chosen_model) if chosen_model else ClaudeClient()
    raw = client.ask(
        system=_render_system_prompt(),
        user=text,
        max_tokens=int(cfg["max_tokens"]),
    )
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
