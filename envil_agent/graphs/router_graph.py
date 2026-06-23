"""router_graph — single top-level LangGraph entry point.

The shape the user asked for: "one route for all". Instead of routing
in the system prompt (invisible to LangSmith), the router is a real
graph node so every classification + retry shows up in the trace tree.

  START -> classify_intent -> [build | fail] -> ... -> END
                                     |
                                     v
                              architect -> validate -> render -> preview

Today only the "build" branch is wired (build_circuit is the only verb
that needs an LLM-driven retry loop — apply_ops / erc_autofix /
read_schematic are deterministic and already work via the agent SDK).
The conditional-edge dispatcher leaves space for "fix" / "edit" /
"analyze" routes to slot in later WITHOUT moving any existing wiring.

Why this lives alongside build_graph instead of replacing it:
  - build_graph is still the "pipeline" the build branch runs.
  - router_graph wraps it with a classifier + decision log.
  - Callers that only need the pipeline (tests, future internal
    callers) can import build_graph directly; the user-facing tool
    surface goes through router_graph so the LangSmith view shows
    the routing decision.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict

from langgraph.graph import END, START, StateGraph

from .build_graph import MAX_ATTEMPTS, _route_after_render, _route_after_validate
from .nodes.architect import architect_node
from .nodes.block_repair import block_repair_node
from .nodes.decide_render_mode import decide_render_mode_node
from .nodes.render import preview_node, render_node
from .nodes.validate import normalize_node, validate_node, validate_only_node
from .state import RouterState


def _save_preview_enabled() -> bool:
    """Whether the Save-preview / export node is wired into the build graph.

    Gated by layout_config.json:build_graph.save_preview (default True).
    When False the preview node is never added and render's "ok" edge goes
    straight to END, so no preview_svgs are produced or sent and the
    'Save preview' / export span disappears from the LangSmith trace.
    Read at graph-build time (the graphs are lru_cached), so flipping the
    flag takes effect on restart — same restart-to-apply contract as
    build_graph.detailed_trace. Reverting the flag restores it with no
    code change.
    """
    try:
        from ..intent.engine import _load_layout_config
        return bool((_load_layout_config().get("build_graph") or {})
                    .get("save_preview", True))
    except Exception:
        return True


def classify_intent_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Pick a route based on the user's prompt.

    Returns {"route": <name>, "route_reason": <one-liner>,
             "decision_log": [...]} so LangSmith renders the choice as a
    discrete step. The router itself can't fail — empty / weird prompts
    still classify (as "build" by default), and the downstream validator
    will reject them with a real error message.
    """
    prompt = (state.get("prompt") or "").strip()
    log = list(state.get("decision_log") or [])

    if not prompt:
        log.append("classify_intent: empty prompt -> build (validator will reject)")
        return {
            "route": "build",
            "route_reason": "empty prompt; fall through to validator",
            "decision_log": log,
        }

    # v1 scope: only "build" is wired. Future routes (fix/edit/analyze)
    # will branch here on regex hints like '/fix', 'ERC error', refdes
    # patterns. Keeping this as one explicit decision so when we add
    # more, the diff is minimal and LangSmith trace stays readable.
    route = "build"
    reason = "build_circuit pipeline (architect -> validate -> render -> preview)"
    log.append(f"classify_intent: route={route} — {reason}")

    return {"route": route, "route_reason": reason, "decision_log": log}


def _route_after_classify(state: Dict[str, Any]) -> str:
    """Conditional-edge dispatcher. Returns the edge label, not the
    next node name. LangGraph maps the label to a target via the dict
    passed to add_conditional_edges."""
    return state.get("route", "build")


def _logged(node_name: str, inner):
    """Wrap a node fn so its return dict picks up a decision_log entry.

    LangGraph merges returned dicts into the running state. We append to
    decision_log so the audit trail survives across nodes — without the
    wrapper, each node would overwrite the previous log entry.
    """
    def _node(state: Dict[str, Any]) -> Dict[str, Any]:
        before_attempt = int(state.get("attempt", 0))
        out = inner(state) or {}
        log = list(state.get("decision_log") or [])
        # Build a one-line summary of what this node did.
        if "error" in out and out.get("error"):
            log.append(f"{node_name}: ERROR {out['error']}")
        elif node_name == "decide_render_mode":
            log.append(f"decide_render_mode: {out.get('render_mode_decision', '?')}")
        elif node_name == "architect":
            log.append(f"architect: attempt={out.get('attempt', before_attempt + 1)} "
                        f"ir_parsed={out.get('ir') is not None}")
        elif node_name == "normalize":
            log.append(f"normalize: warnings={len(out.get('norm_warnings') or [])}")
        elif node_name in ("validate", "validate_only"):
            issues = out.get("issues") or []
            err_n = sum(1 for i in issues if i.get("severity") == "error")
            warn_n = sum(1 for i in issues if i.get("severity") == "warning")
            log.append(f"validate: errors={err_n} warnings={warn_n}")
        elif node_name == "block_repair":
            log.append(f"block_repair: attempt={out.get('block_attempt', '?')} "
                        f"repaired={out.get('blocks_repaired') or []} "
                        f"skipped={out.get('blocks_skipped') or []}")
        elif node_name == "render":
            st = out.get("stats") or {}
            _lay = out.get("layout_final")
            _lay_txt = f"layout={_lay} " if _lay else ""
            log.append(f"render: {_lay_txt}path={st.get('path', '?')} "
                        f"components={st.get('components_emitted', st.get('components_total', '?'))}")
            # When the actual block count overrode the prompt-based guess, log
            # the corrected 3-way pick so the trace shows the real layout reason.
            if out.get("render_mode_decision"):
                log.append(f"layout re-decided: {out['render_mode_decision']}")
        elif node_name in ("preview", "export"):
            log.append(f"{node_name}: svgs={len(out.get('preview_svgs') or [])}")
        out["decision_log"] = log
        return out
    return _node


@lru_cache(maxsize=1)
def router_graph():
    """Compile and cache the top-level router graph.

    Cached because the graph definition is pure — the RouterState dict
    carries every per-request value, so a single compiled graph is safe
    to share across concurrent invocations.
    """
    g = StateGraph(RouterState)
    _preview_on = _save_preview_enabled()

    g.add_node("classify_intent", classify_intent_node)
    g.add_node("architect", _logged("architect", architect_node))
    g.add_node("validate", _logged("validate", validate_node))
    g.add_node("render", _logged("render", render_node))
    if _preview_on:
        g.add_node("preview", _logged("preview", preview_node))

    g.add_edge(START, "classify_intent")
    g.add_conditional_edges(
        "classify_intent", _route_after_classify,
        {
            "build": "architect",
            # Placeholders — when these routes are implemented, replace
            # END with the relevant subgraph entry node. Keeping them
            # listed here makes the future surface explicit.
            "fix": END,
            "edit": END,
            "analyze": END,
        },
    )
    g.add_edge("architect", "validate")
    g.add_conditional_edges(
        "validate", _route_after_validate,
        {"retry": "architect", "ok": "render", "fail": END},
    )
    # render -> preview (when save_preview is on) else render -> END.
    g.add_conditional_edges(
        "render", _route_after_render,
        {"ok": "preview" if _preview_on else END, "fail": END},
    )
    if _preview_on:
        g.add_edge("preview", END)
    return g.compile()


# Friendly, plain-English span names for the LIVE detailed graph. In
# LangSmith the node id IS the displayed span name, so these strings
# double as the graph's node keys — picked so the waterfall reads like
# steps a person understands instead of internal ids (normalize,
# decide_render_mode, _route_after_validate, ...).
_N_UNDERSTAND = "Understand request"
_N_LAYOUT = "Choose layout"
_N_DESIGN = "Design circuit"
_N_CLEAN = "Clean up"
_N_CHECK = "Check circuit"
_N_FIXBLOCK = "Fix block"
_N_DRAW = "Draw schematic"
_N_SAVE = "Save preview"
_N_RESET = "Retry full board"


# Route dispatchers wrapped so their LangSmith span shows a plain name
# instead of the internal function name (_route_after_validate, ...).
def _pick_route(state):
    return _route_after_classify(state)
_pick_route.__name__ = "Pick route"


def _reset_handoff_cfg():
    """(enabled, max_full_handoffs) from layout_config.json:build_graph.
    Default (True, 1). When disabled, _pass_or_fix never returns 'reset' and
    the routing is byte-identical to before."""
    try:
        from ..intent.engine import _load_layout_config
        bg = _load_layout_config().get("build_graph") or {}
        return (bool(bg.get("reset_attempt_on_full_handoff", True)),
                int(bg.get("max_full_handoffs", 1)))
    except Exception:
        return (True, 1)


def _should_reset_for_full_handoff(state) -> bool:
    """True only for the genuine dead-handoff: the architect attempt budget is
    exhausted (base == 'fail' via attempt >= MAX_ATTEMPTS) with errors still
    present, no hard `error`, and the handoff budget not yet spent. This
    EXCLUDES a divergence-fail (which fails at attempt < MAX_ATTEMPTS, line
    86-90 of build_graph) and a hard-error fail — neither should be rescued."""
    enabled, max_handoffs = _reset_handoff_cfg()
    if not enabled:
        return False
    if state.get("error"):
        return False
    if int(state.get("full_handoffs", 0)) >= max_handoffs:
        return False
    from .build_graph import MAX_ATTEMPTS
    if int(state.get("attempt", 0)) < MAX_ATTEMPTS:
        return False                          # not budget-exhaustion
    issues = state.get("issues") or []
    return any(i.get("severity") == "error" for i in issues)


def _pass_or_fix(state):
    """Block-aware retry router (router_graph_detailed only). Falls through to
    the shared _route_after_validate for ok/fail/divergence, then splits a
    'retry' into per-block repair vs a full architect regen. When
    per_block_retry is off (default) should_block_repair returns False, so this
    always returns 'full' on a retry -> identical to the old 'retry' behaviour."""
    base = _route_after_validate(state)        # "retry" | "ok" | "fail"
    if base == "fail":
        # Rescue the dead block->full handoff ONCE (gated): give a fresh
        # architect budget instead of dying at MAX_ATTEMPTS. Bounded by
        # max_full_handoffs so it can never loop.
        try:
            if _should_reset_for_full_handoff(state):
                return "reset"
        except Exception:
            pass
        return "fail"
    if base != "retry":
        return base
    try:
        from .nodes.block_repair import should_block_repair
        if should_block_repair(state):
            return "block"
    except Exception:
        pass
    return "full"
_pass_or_fix.__name__ = "Pass or fix"


def _reset_for_full_handoff(state):
    """Reset the attempt budget for one more full-board regen. Keeps the
    accumulated `feedback` (now carrying the real available-pins section) so
    the fresh architect call knows what to fix; resets block_attempt so
    per-block repair can run again; bumps full_handoffs (never reset, so the
    bound holds). Preserves best_* memory for the terminal handler."""
    return {
        "attempt": 0,
        "block_attempt": 0,
        "full_handoffs": int(state.get("full_handoffs", 0)) + 1,
        "feedback": state.get("feedback") or "",
    }
_reset_for_full_handoff.__name__ = "Retry full board"


def _draw_ok(state):
    return _route_after_render(state)
_draw_ok.__name__ = "Draw ok?"


@lru_cache(maxsize=1)
def router_graph_detailed():
    """7-node variant that surfaces more of the pipeline as discrete
    LangSmith spans:

      classify_intent -> decide_render_mode -> architect -> normalize
        -> validate -> [retry->architect | ok->render | fail->END]
        render -> [ok->export | fail->END] -> export -> END

    vs. the 5-node router_graph this:
      - adds `decide_render_mode` (promoted from build_graph) so the
        sheet-mode choice is visible BEFORE the architect runs;
      - splits the old combined `validate` into `normalize` + `validate`
        (validate_only_node) so each shows as its own span;
      - renames `preview` to `export` (same SVG export — the chat still
        reads `preview_svgs`; the node name just reflects that it is the
        artifact-export stage, not a separate preview pass).

    Behaviour is otherwise identical to router_graph: same architect
    call, same validation, same retry budget (MAX_ATTEMPTS), same render.
    Selected at the build_circuit entry via the
    `layout_config.json:build_graph.detailed_trace` flag, so flipping
    that flag reverts to the 5-node graph with zero code changes.
    """
    g = StateGraph(RouterState)
    _preview_on = _save_preview_enabled()

    g.add_node(_N_UNDERSTAND, classify_intent_node)
    g.add_node(_N_LAYOUT, _logged("decide_render_mode", decide_render_mode_node))
    g.add_node(_N_DESIGN, _logged("architect", architect_node))
    g.add_node(_N_CLEAN, _logged("normalize", normalize_node))
    g.add_node(_N_CHECK, _logged("validate_only", validate_only_node))
    g.add_node(_N_FIXBLOCK, _logged("block_repair", block_repair_node))
    g.add_node(_N_DRAW, _logged("render", render_node))
    if _preview_on:
        g.add_node(_N_SAVE, _logged("export", preview_node))
    # Full-handoff rescue node — only wired when the gate is on, so the graph
    # is byte-identical (same nodes, same edges) when disabled.
    _reset_on = _reset_handoff_cfg()[0]
    if _reset_on:
        g.add_node(_N_RESET, _logged("reset_for_full_handoff",
                                     _reset_for_full_handoff))

    g.add_edge(START, _N_UNDERSTAND)
    g.add_conditional_edges(
        _N_UNDERSTAND, _pick_route,
        {"build": _N_LAYOUT, "fix": END, "edit": END, "analyze": END},
    )
    g.add_edge(_N_LAYOUT, _N_DESIGN)
    g.add_edge(_N_DESIGN, _N_CLEAN)
    g.add_edge(_N_CLEAN, _N_CHECK)
    # Retry split (per-block #9): "block" -> regenerate only the failing
    # block(s) then re-check; "full" -> a normal whole-board architect regen.
    # When per_block_retry is off, _pass_or_fix never returns "block", so this
    # is byte-identical to the old {retry,ok,fail} routing.
    _check_routes = {"block": _N_FIXBLOCK, "full": _N_DESIGN,
                     "ok": _N_DRAW, "fail": END}
    if _reset_on:
        # Map the new 'reset' label to the rescue node, which loops back to
        # the architect with a fresh budget. Omitted when off -> _pass_or_fix
        # never emits 'reset', routing stays {block,full,ok,fail}.
        _check_routes["reset"] = _N_RESET
    g.add_conditional_edges(_N_CHECK, _pass_or_fix, _check_routes)
    g.add_edge(_N_FIXBLOCK, _N_CHECK)
    if _reset_on:
        g.add_edge(_N_RESET, _N_DESIGN)
    # Draw -> Save preview (when save_preview is on) else Draw -> END.
    g.add_conditional_edges(
        _N_DRAW, _draw_ok,
        {"ok": _N_SAVE if _preview_on else END, "fail": END},
    )
    if _preview_on:
        g.add_edge(_N_SAVE, END)
    return g.compile()


__all__ = ["router_graph", "router_graph_detailed", "MAX_ATTEMPTS"]
