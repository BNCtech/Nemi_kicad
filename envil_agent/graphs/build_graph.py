"""build_graph — architect → validate → render → preview → self_heal.

Replaces the hand-rolled retry loop that used to live inside
tools/build_circuit.py:build_circuit (~50 lines of for-attempt-in-range
with a feedback string). Each step is now a LangGraph node, so every
attempt shows up as a child run in LangSmith and the retry policy is
visible as a conditional edge instead of buried in a Python loop.

Edge logic:
  validate  -> retry      : has errors AND attempt < MAX_ATTEMPTS
  validate  -> fail       : has errors AND attempt >= MAX_ATTEMPTS
  validate  -> ok         : no errors -> render
  render    -> preview    : always (preview is best-effort)
  preview   -> self_heal  : always (self_heal is best-effort, gated by config)
  self_heal -> escalate   : ERC residual needs IR-level redesign
                            (self_heal reset `attempt` to 0, so the
                            architect's normal validate-retry budget is
                            fresh for the post-escalation flow)
  self_heal -> done       : 0 residual errors OR fix_attempt budget hit
  any-node  -> END        : when `error` is set on the state
"""
from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

import json as _json
from pathlib import Path as _Path

from .nodes.architect import architect_node
from .nodes.decide_render_mode import decide_render_mode_node
from .nodes.render import preview_node, render_node
from .nodes.self_heal import self_heal_node
from .nodes.validate import validate_node
from .state import BuildState


def _load_max_attempts() -> int:
    """Read MAX_ATTEMPTS from layout_config.json:build_graph.max_attempts
    so projects can tune the architect-retry budget without code edits.
    Defaults to 4 --- enough for typical builds where the first IR has
    2-3 errors (missing decoupling cap + wrong pin name + minor net
    issue) which the architect resolves one per attempt. Previous
    default (2) was too tight: errors compound and the agent gives up
    after the second failure instead of retrying."""
    try:
        cfg_path = _Path(__file__).resolve().parent.parent / "config" / "layout_config.json"
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        return int((cfg.get("build_graph") or {}).get("max_attempts", 4))
    except (OSError, ValueError, TypeError):
        return 4


MAX_ATTEMPTS = _load_max_attempts()


def _keep_best_attempt() -> bool:
    """Read build_graph.keep_best_attempt (default true). When on, the
    retry loop STOPS as soon as an attempt has MORE errors than the best
    seen (the architect is diverging) and the terminal handler reports the
    fewest-error attempt instead of the last. Set false to revert to the
    old 'retry to the budget, return the last attempt' behaviour."""
    try:
        cfg_path = _Path(__file__).resolve().parent.parent / "config" / "layout_config.json"
        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
        return bool((cfg.get("build_graph") or {}).get("keep_best_attempt", True))
    except (OSError, ValueError, TypeError):
        return True


def _route_after_validate(state: BuildState) -> str:
    if state.get("error"):
        return "fail"
    issues = state.get("issues") or []
    has_err = any(i.get("severity") == "error" for i in issues)
    if not has_err:
        return "ok"
    if int(state.get("attempt", 0)) >= MAX_ATTEMPTS:
        return "fail"
    # Stop-on-divergence: if this attempt has MORE errors than the best
    # seen so far, the architect is regressing (the 5 -> 8 -> 27 spiral that
    # ends in a 21-power-pin-floating wreck). Cut the loop now and let the
    # terminal handler keep the best attempt instead of burning the budget
    # making it worse. best_error_count is set by validate's _remember_best.
    if _keep_best_attempt():
        best = state.get("best_error_count")
        cur = sum(1 for i in issues if i.get("severity") == "error")
        if best is not None and cur > best:
            return "fail"
    return "retry"


def _route_after_render(state: BuildState) -> str:
    return "fail" if state.get("error") else "ok"


def _route_after_self_heal(state: BuildState) -> str:
    """Re-enter the architect when self_heal escalated residual ERC
    violations as `fix_feedback`. Otherwise terminate. Errors raised
    inside the node already set state['error'], handled by `done`."""
    if state.get("error"):
        return "done"
    if state.get("fix_feedback"):
        return "escalate"
    return "done"


@lru_cache(maxsize=1)
def build_graph():
    """Compile and cache the build graph. Cached because the graph is
    pure (no per-request state); the BuildState dict carries everything."""
    g = StateGraph(BuildState)
    g.add_node("decide_render_mode", decide_render_mode_node)
    g.add_node("architect", architect_node)
    g.add_node("validate", validate_node)
    g.add_node("render", render_node)
    g.add_node("preview", preview_node)
    g.add_node("self_heal", self_heal_node)

    # decide_render_mode is a pre-pass: when the agent's tool args did
    # NOT include an explicit force_hierarchy / force_single_sheet, this
    # node scans the prompt and sets the appropriate flag so the
    # architect prompt + render dispatcher know which mode to target.
    # When explicit flags WERE supplied, the node is a no-op.
    g.add_edge(START, "decide_render_mode")
    g.add_edge("decide_render_mode", "architect")
    g.add_edge("architect", "validate")
    g.add_conditional_edges(
        "validate", _route_after_validate,
        {"retry": "architect", "ok": "render", "fail": END},
    )
    g.add_conditional_edges(
        "render", _route_after_render,
        {"ok": "preview", "fail": END},
    )
    g.add_edge("preview", "self_heal")
    g.add_conditional_edges(
        "self_heal", _route_after_self_heal,
        {"escalate": "architect", "done": END},
    )
    return g.compile()
