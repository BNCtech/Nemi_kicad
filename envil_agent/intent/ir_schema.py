"""TopologyIR JSON schema loader — feeds the Anthropic strict tool_use
definition so the architect LLM literally cannot emit tokens that
violate the schema.

The schema body lives in ``config/topology_ir_schema.json`` (kept in
data, not Python). This module just loads it, caches it, and shapes it
into the dict the Anthropic SDK expects for a tool definition.

Why a JSON file and not a Pydantic class? Three reasons:
  1. The user's no-hardcode rule — the schema is data, not code.
  2. A future project extending the IR adds fields with a JSON edit,
     no Python deploy.
  3. The same schema is reusable when we eventually upgrade to
     `client.messages.parse(output_format=...)` once the SDK exposes it
     stably.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "topology_ir_schema.json"


@lru_cache(maxsize=1)
def _load_raw() -> Dict[str, Any]:
    """Read the JSON config once per process. Restart server to pick
    up edits — same convention as every other config file in the
    project."""
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Don't fail import — the architect call's try/except wraps
        # this and falls back to the legacy text-parse path.
        raise RuntimeError(
            f"topology_ir_schema.json unreadable: {exc}"
        ) from exc


def ir_tool_definition(strict: bool = True) -> Dict[str, Any]:
    """Return the Anthropic tool definition dict for the `emit_ir`
    tool. Pass this in `tools=[...]` on `client.messages.create()`
    together with `tool_choice={"type":"tool","name":"emit_ir"}` to
    force the architect to emit the IR through the tool's strict
    grammar-constrained schema."""
    cfg = _load_raw()
    name = cfg.get("tool_name", "emit_ir")
    description = cfg.get("tool_description", "Emit TopologyIR JSON.")
    input_schema = cfg.get("input_schema", {})
    tool_def: Dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": input_schema,
    }
    # `strict` is a 2025+ Anthropic SDK feature. Older SDKs ignore the
    # key silently (still validate via JSON Schema at parse time, just
    # without token-level grammar constraints). Keep it on by default
    # — pinned in requirements.txt to a version that supports it.
    if strict:
        tool_def["strict"] = True
    return tool_def


def ir_tool_choice() -> Dict[str, Any]:
    """The tool_choice dict that forces Claude to use the emit_ir tool
    on this turn. Without this, Claude may answer in prose even when
    the tool is in the toolbox."""
    return {
        "type": "tool",
        "name": _load_raw().get("tool_name", "emit_ir"),
    }
