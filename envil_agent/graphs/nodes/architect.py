"""Architect node — one Claude call returning TopologyIR JSON.

Delegates to envil_agent.tools.build_circuit._architect_call so the
prompt + retry-feedback contract stays in one place. The node bumps
`attempt` so build_graph's conditional edge can decide whether to retry
or give up.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ...tools.build_circuit import _architect_call, _extract_ir_json
from ...intent.ir import TopologyIR


def _bg_cfg(key: str, default):
    try:
        from ...intent.engine import _load_layout_config
        return (_load_layout_config().get("build_graph") or {}).get(key, default)
    except Exception:
        return default


def _try_incremental(prompt: str, render_mode: str) -> Optional[Dict[str, Any]]:
    """Forward 'draw bit by bit' for a LARGE board: PLAN the blocks, then draw
    each block in its own small call and splice into the growing board. DYNAMIC
    + part-agnostic -- the plan comes from the architect for whatever board was
    asked; nothing here is circuit-specific. Returns the node's output dict, or
    None to fall back to the one-shot path (plan failed, or the board is not
    actually large)."""
    import importlib
    import json as _json

    # NB: import the MODULE, not `from ...tools import build_circuit`. The
    # tools package __init__ re-exports the @tool-decorated `build_circuit`
    # coroutine (an SdkMcpTool object), which SHADOWS the submodule of the same
    # name -- so `from ...tools import build_circuit as _bc` binds _bc to the
    # SdkMcpTool, and `_bc._architect_plan_call` raises AttributeError, silently
    # caught below -> incremental NEVER ran (and reactive per-block repair had
    # the identical bug). importlib.import_module returns the real module from
    # sys.modules, bypassing the package-attribute shadow. Keeps the `_bc.`
    # module-attr call style so tests can still patch the module's functions.
    _bc = importlib.import_module("envil_agent.tools.build_circuit")
    from ...intent.incremental_build import build_incrementally

    try:
        plan_raw = _bc._architect_plan_call(prompt, render_mode)
        if not plan_raw:
            return None
        plan_ir = TopologyIR.from_json(_extract_ir_json(plan_raw))
    except Exception:
        return None

    n_comp = len(getattr(plan_ir, "components", []) or [])
    n_block = len(getattr(plan_ir, "blocks", []) or [])
    min_comp = int(_bg_cfg("block_repair_min_components", 40))
    if n_comp < min_comp or n_block < 2:
        return None  # not large enough (or no blocks) -> one-shot is fine/cheaper

    # Gated (build_graph.progressive_status): stream a live "drew block 3/8"
    # status to the chat as each block resolves. The sink hands off to the
    # thread-safe build_progress transport (a no-op unless the server registered
    # the turn), so it is safe from the build worker thread. on_block=None when
    # off -> build_incrementally is byte-identical.
    _on_block = None
    if bool(_bg_cfg("progressive_status", False)):
        def _on_block(name, ok, idx, total):
            from ...build_progress import emit
            if ok:
                emit(f"Drew block {idx}/{total}: {name}")
            else:
                emit(f"Block {idx}/{total} ({name}) needs attention — continuing")
    ir, drawn, skipped = build_incrementally(
        plan_ir, prompt, _bc._architect_block_call, on_block=_on_block)
    # Observability: the incremental path leaves no mark in the final graph
    # state (incremental_drawn is not a declared RouterState channel) or the
    # decision_log, so without this line the only signal it ran is the
    # LangSmith "Plan blocks" + "Fix block" spans. Print so the CLI
    # (python -m envil_agent) and the server log show it fired, for testing.
    # Matches the [architect]/[block_repair] print-diagnostic style already
    # used in tools/build_circuit.py.
    print(f"[architect] INCREMENTAL large-board path: planned {n_comp} parts / "
          f"{n_block} blocks -> drew {drawn}, skipped {skipped}", flush=True)
    try:
        ir_json = _json.dumps(ir.to_dict())
    except Exception:
        ir_json = ""
    return {"attempt": 1, "ir": ir, "ir_json": ir_json, "feedback": "",
            "incremental_drawn": drawn, "incremental_skipped": skipped}


def architect_node(state: Dict[str, Any]) -> Dict[str, Any]:
    attempt = int(state.get("attempt", 0)) + 1
    # Translate the render-mode flags decide_render_mode set into a
    # single string the architect prompt can branch on:
    #   "hierarchy"            -> emit many blocks for multi-sheet
    #   "single_sheet_blocks"  -> emit blocks for boxed single-sheet
    #   "flat"                 -> emit blocks=[] (single-IC or simple
    #                              discrete circuit, no boxes)
    # When neither flag is set, leave mode empty so the architect's
    # legacy size-based judgment runs.
    if bool(state.get("force_hierarchy")):
        render_mode = "hierarchy"
    elif bool(state.get("force_single_sheet")):
        render_mode = "single_sheet_blocks"
    else:
        render_mode = ""

    # Forward incremental ("draw bit by bit") for LARGE boards on the FIRST
    # attempt: plan the blocks, then draw each block in its own small call and
    # splice into the growing board, so a ~100-part board is never wired in one
    # shot (the BMS cross-block-tangle failure). DYNAMIC -- driven by the
    # architect's own block plan for whatever board was asked, not per-circuit.
    #
    # GENERATION mode is DECOUPLED from RENDER mode (fix 2026-06-09): the
    # authoritative "is this a large board" test is the PLAN call's REAL
    # component count (>= block_repair_min_components AND >= 2 blocks, inside
    # _try_incremental), NOT the pre-architect render-mode keyword GUESS. The
    # `incremental_trigger_render_modes` list only PRE-FILTERS which boards even
    # attempt the cheap PLAN call, so a clearly-flat tiny circuit (NE555 blinker,
    # render_mode=="") still skips the round-trip. Was hard-gated to
    # render_mode=="hierarchy", which left a ~55-part board the keyword guess
    # mis-tagged SINGLE_SHEET_BLOCKS stuck on the lossy one-shot path where the
    # LLM drops nets -- the large-board generation cliff. Now any block-decorated
    # guess (hierarchy OR single_sheet_blocks by default) attempts incremental
    # and the part-count gate decides; add "" to the list to attempt on EVERY
    # board. On RETRY we use the normal path (reactive per-block repair handles
    # localized residuals). Gated by build_graph.incremental_large_board.
    _inc_modes = _bg_cfg("incremental_trigger_render_modes",
                         ["hierarchy", "single_sheet_blocks"])
    if isinstance(_inc_modes, str):
        _inc_modes = [_inc_modes]
    if (attempt == 1 and not state.get("feedback")
            and render_mode in (_inc_modes or [])
            and bool(_bg_cfg("incremental_large_board", False))):
        inc = _try_incremental(state["prompt"], render_mode)
        if inc is not None:
            return inc

    # Pass the prior attempt's JSON so the retry prompt's pin catalog
    # gets extended with the lib_ids the failed attempt mentioned. No-
    # op on attempt 1 (state has no prior ir_json yet).
    raw = _architect_call(
        state["prompt"],
        state.get("feedback", ""),
        prior_attempt_json=state.get("ir_json", ""),
        render_mode=render_mode,
    )
    if not raw:
        return {
            "attempt": attempt,
            "error": f"architect returned empty response (attempt {attempt})",
        }
    ir_json = _extract_ir_json(raw)
    try:
        ir = TopologyIR.from_json(ir_json)
    except Exception as exc:
        # Hand the parse error back as feedback so the next loop tells
        # the architect what was wrong with its JSON.
        return {
            "attempt": attempt,
            "ir_json": ir_json,
            "ir": None,
            "feedback": (
                f"Your previous reply was not parseable as JSON: {exc}\n"
                f"Reply was (first 300 chars): {raw[:300]}\n"
                "Output ONLY a JSON object matching the TopologyIR schema. "
                "No prose, no markdown fences."
            ),
        }
    return {"attempt": attempt, "ir_json": ir_json, "ir": ir, "feedback": ""}
