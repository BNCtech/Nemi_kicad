"""decide_render_mode -- pre-architect node that picks the render mode
when the agent didn't pass an explicit force_hierarchy /
force_single_sheet flag.

Decision precedence (highest priority first):
  1. Explicit tool args from build_circuit (force_hierarchy /
     force_single_sheet=True) -- always respected; this node is a no-op.
  2. Explicit phrases in the user prompt -- "single sheet" / "block
     diagram" -> single sheet; "multi-sheet" / "hierarchy" -> multi.
  3. sheet_planner.recommend_blocks(prompt) count -- many distinct
     functional blocks => hierarchy.
  4. Sum of keyword-weight signals across the prompt -- dense circuits
     promote up a tier even with few distinct block names.
  5. Fall through: leave both flags False so the engine's size-based
     auto-decision (sheet_decision.small_max_components etc.) runs.

All phrases, weights and thresholds live in
`envil_agent/config/render_mode_rules.json`. Adding a new keyword or
phrase = one JSON edit, zero code.

The node also records `state["render_mode_decision"]` (human-readable
reason) so the chat reply can explain WHY a mode was chosen.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from ...intent.sheet_planner import recommend_blocks


_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config" / "render_mode_rules.json"
)


@lru_cache(maxsize=1)
def _load_rules() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _explicit_phrase_match(prompt_lower: str, phrases) -> str:
    for ph in phrases or []:
        if ph and ph.lower() in prompt_lower:
            return ph
    return ""


def _estimate_complexity(prompt_lower: str, signals: dict) -> int:
    """Sum keyword weights present in the prompt. Each keyword counts
    AT MOST once even if it appears multiple times --- prevents a
    repeated word from dominating the score."""
    total = 0
    for kw, weight in (signals or {}).items():
        if kw and kw.lower() in prompt_lower:
            try:
                total += int(weight)
            except (TypeError, ValueError):
                continue
    return total


def decide_render_mode_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Set state["force_hierarchy"] and/or state["force_single_sheet"]
    based on prompt analysis. No-op when the agent already passed an
    explicit flag. Returns a partial state dict per LangGraph
    convention --- only the keys we wrote, so unset keys stay unset."""

    # Respect any explicit flag the build_circuit tool passed.
    agent_force_hier = bool(state.get("force_hierarchy", False))
    agent_force_single = bool(state.get("force_single_sheet", False))
    if agent_force_hier or agent_force_single:
        return {
            "render_mode_decision": (
                "explicit tool arg from agent: "
                f"force_hierarchy={agent_force_hier}, "
                f"force_single_sheet={agent_force_single}"
            ),
        }

    rules = _load_rules()
    if not rules.get("enabled", True):
        return {"render_mode_decision": "render_mode_rules disabled; engine size-auto"}

    prompt = str(state.get("prompt") or "").strip()
    if not prompt:
        return {"render_mode_decision": "empty prompt; engine size-auto"}
    p_lower = prompt.lower()

    # (2) explicit prompt phrases
    multi_phrase = _explicit_phrase_match(
        p_lower, rules.get("explicit_multi_sheet_phrases"))
    if multi_phrase:
        return {
            "force_hierarchy": True,
            "force_single_sheet": False,
            "render_mode_decision": (
                f"prompt contains explicit multi-sheet phrase "
                f"{multi_phrase!r} -> force_hierarchy=True"
            ),
        }
    single_phrase = _explicit_phrase_match(
        p_lower, rules.get("explicit_single_sheet_phrases"))
    if single_phrase:
        return {
            "force_hierarchy": False,
            "force_single_sheet": True,
            "render_mode_decision": (
                f"prompt contains explicit single-sheet phrase "
                f"{single_phrase!r} -> force_single_sheet=True"
            ),
        }

    # (3) distinct-block-count signal via sheet_planner
    block_thr = rules.get("block_count_thresholds") or {}
    hier_blk_min = int(block_thr.get("hierarchy_when_blocks_at_least", 6))
    single_blk_min = int(block_thr.get(
        "single_sheet_blocks_when_blocks_at_least", 3))
    blocks_hint = recommend_blocks(prompt)
    n_blocks = len(blocks_hint or [])

    # (4) keyword-weight signal
    cx = _estimate_complexity(
        p_lower, rules.get("component_estimate_signals") or {})
    cx_thr = rules.get("complexity_thresholds") or {}
    hier_cx_min = int(cx_thr.get("hierarchy_when_complexity_at_least", 40))
    single_cx_min = int(cx_thr.get(
        "single_sheet_blocks_when_complexity_at_least", 15))

    # HIERARCHY fires on EITHER strong signal --- a dense circuit
    # benefits from multi-sheet even if the sheet planner missed some
    # blocks, and a circuit with many distinct functional groups
    # benefits even if keyword density is light.
    if n_blocks >= hier_blk_min or cx >= hier_cx_min:
        return {
            "force_hierarchy": True,
            "force_single_sheet": False,
            "render_mode_decision": (
                f"auto -> HIERARCHY: blocks={n_blocks} "
                f"(>= {hier_blk_min}) or complexity={cx} "
                f"(>= {hier_cx_min})"
            ),
        }
    # SINGLE_SHEET_BLOCKS needs BOTH signals to agree. Block-count
    # alone over-promotes simple discrete circuits (USB lamp with
    # PROTECTION + POWER + INDICATOR + BMS hints but only 9 actual
    # parts); complexity alone over-promotes single-IC dense circuits
    # that don't need boxes (one MCU + a pile of passives). Requiring
    # both keeps the SINGLE_SHEET_BLOCKS tier reserved for medium
    # circuits with genuine functional grouping.
    if n_blocks >= single_blk_min and cx >= single_cx_min:
        return {
            "force_hierarchy": False,
            "force_single_sheet": True,
            "render_mode_decision": (
                f"auto -> SINGLE_SHEET_BLOCKS: blocks={n_blocks} "
                f"(>= {single_blk_min}) AND complexity={cx} "
                f"(>= {single_cx_min})"
            ),
        }

    # Fall through: simple flat schematic, no block decoration. The
    # engine's render_flat path handles this with no rectangles ---
    # the USB-lamp / NE555-blinker / single-LDO style.
    return {
        "render_mode_decision": (
            f"auto -> FLAT (no block decoration): "
            f"blocks={n_blocks}, complexity={cx} "
            f"(below SINGLE_SHEET_BLOCKS threshold of "
            f"{single_blk_min} blocks AND {single_cx_min} complexity)"
        ),
    }
