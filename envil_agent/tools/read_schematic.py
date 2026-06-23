"""Tool: read a .kicad_sch and return its contents to the agent.

The first end-to-end tool in the rebuild. Validates the wiring of:
  ClaudeSDKClient -> @tool -> envil_agent.kicad.document -> sexpdata
If this round-trips a real schematic through the agent loop, the SDK
plumbing is good and subsequent tools (apply_ops, run_layout) bolt on
without architectural changes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

from ..kicad import read_summary


@tool(
    name="read_schematic",
    description=(
        "Open a KiCad .kicad_sch file and return a JSON summary: total "
        "component / wire / label counts plus a per-component list with "
        "reference, value, lib_id and (x, y, rotation). Use this before "
        "answering any question about an existing schematic — never guess "
        "from filename."
    ),
    input_schema={"path": str},
)
async def read_schematic(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("path", "")
    path = Path(raw).expanduser()
    if not path.exists():
        return {
            "content": [{
                "type": "text",
                "text": f"ERROR: schematic not found at {path}",
            }],
            "is_error": True,
        }
    summary = read_summary(path)
    return {
        "content": [{
            "type": "text",
            "text": json.dumps(summary.to_dict(), indent=2),
        }],
    }
