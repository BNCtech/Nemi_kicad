"""Tool: build a KiCad schematic from a natural-language description.

End-to-end orchestration of Stages 2-6 of the pipeline:
  Stage 2  architect call  -> TopologyIR JSON
  Stage 3  IR validation   -> error list (or empty)
  Stage 4  lib resolution  -> SymbolGeom cache populated
  Stage 5  engine render   -> .kicad_sch file
  Stage 6  post-emit check -> (deferred to v1.1)

The agent calls this with `{"prompt": "build a 1Hz blinker with NE555"}`.
The tool calls Claude internally (the *architect* call, distinct from the
outer agent's conversation) and returns the path + stats.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, Optional


# Avast's HTTPS scanner re-signs api.anthropic.com with its own cert; the
# anthropic SDK's bundled certifi store doesn't trust that, so the call
# fails with SSL CERTIFICATE_VERIFY_FAILED. Switching SSL to the OS
# trust store (which DOES have Avast's MITM cert) fixes it.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# LangSmith: traceable decorator + anthropic-client wrapper so the
# architect Claude call shows up in the run tree with tokens + cost.
try:
    from langsmith import traceable
    from langsmith.wrappers import wrap_anthropic
    try:
        from langsmith import get_current_run_tree
    except ImportError:                       # older langsmith layout
        from langsmith.run_helpers import get_current_run_tree
    try:
        from langsmith.run_helpers import trace as _ls_trace
    except ImportError:                       # langsmith too old for `trace`
        _ls_trace = None
except ImportError:
    def traceable(*args, **kwargs):  # type: ignore
        def _wrap(fn):
            return fn
        return _wrap if not args else args[0]
    def wrap_anthropic(client, **kwargs):  # type: ignore
        return client
    def get_current_run_tree():  # type: ignore
        return None
    _ls_trace = None


import contextlib

# ---------------------------------------------------------------------------
# SINGLE BUILD TRACE — collapse the scattered "Fix block" / "Plan blocks" /
# "Ask AI" rows into ONE nested route in LangSmith.
#
# Root cause of the scatter: the Claude Agent SDK invokes in-process MCP tools
# (build_circuit) in a task that does NOT inherit run_turn's langsmith run-tree
# contextvar (that's also why "Assess Request" shows as its own top-level row).
# So inside build_circuit there is NO current parent run, and every @traceable
# call below (Plan blocks, one Fix block per block-repair pass, Ask AI, the
# engine spans) starts its OWN top-level trace — a single large board fills the
# trace list with dozens of detached "Fix block" rows.
#
# Fix: run_turn stashes the live "Chat" run here BEFORE the SDK loop starts
# (plain module global — turns are strictly sequential, capped by the per-turn
# build guard, so there is no concurrency to race). build_circuit then opens ONE
# umbrella `trace()` run around the LangGraph invocation, parented to that Chat
# run. Because the umbrella sets the langsmith run-tree contextvar, and both the
# `await ainvoke` (langchain copies context into node executor threads) and the
# `asyncio.to_thread(invoke)` offload path COPY contextvars, every inner
# @traceable nests under the umbrella → one route, nested under the conversation.
# Best-effort + flag-gated (build_graph.single_build_trace): any failure, or the
# gate off, yields a no-op and the build runs exactly as before.
# ---------------------------------------------------------------------------
_TURN_PARENT_RUN = None


def set_turn_parent_run(run) -> None:
    """Called by agent.run_turn with get_current_run_tree() so the build can
    re-root its sub-runs under the conversation run across the SDK tool boundary
    (contextvars don't cross it). `run` may be None when tracing is off."""
    global _TURN_PARENT_RUN
    _TURN_PARENT_RUN = run


@contextlib.contextmanager
def _single_build_trace(enabled: bool):
    """Open ONE umbrella langsmith run around the graph invocation so Plan
    blocks + every Fix block + the engine spans form a single nested route,
    parented to the turn's Chat run. Enters/exits best-effort so tracing can
    never break or alter the build; yields exactly once regardless."""
    if not enabled or _ls_trace is None:
        yield
        return
    cm = None
    try:
        cm = _ls_trace(name="Build schematic", run_type="chain",
                       parent=_TURN_PARENT_RUN)
        cm.__enter__()
    except Exception:
        cm = None
    try:
        yield
    finally:
        if cm is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass


def _mark_run_failed(reason: str, issue_count: int = 0, attempt: int = 0) -> None:
    """Mark the live LangSmith run as FAILED (red) on a terminal build
    failure, WITHOUT raising — the tool still returns its `is_error=True`
    dict so the agent's friendly reply is unchanged. Pure observability:
    a build that produced no schematic should not show green in the trace.

    Gated by layout_config.json:build_graph.mark_run_failed_on_validation_error
    (default true). Best-effort: any failure to reach the run tree is
    swallowed so tracing can never break a build. `RunTree.end()` only sets
    error when it is None, so a pre-set `.error` survives @traceable's
    successful close and the run patches red."""
    try:
        from ..intent.engine import _load_layout_config as _lc_t
        if not bool((_lc_t().get("build_graph") or {}).get(
                "mark_run_failed_on_validation_error", True)):
            return
        run = get_current_run_tree()
        if run is None:
            return
        run.error = reason
        try:
            run.add_metadata({"build_failed": True,
                              "issue_count": issue_count,
                              "attempt": attempt})
        except Exception:
            pass
    except Exception:
        pass

from claude_agent_sdk import tool

from ..intent.architect_prompt import architect_system
from ..intent.ir import TopologyIR  # noqa: F401  (kept for type-hint readers)
from ..intent.ir_schema import ir_tool_definition, ir_tool_choice
from ..intent.pin_catalog import extract_lib_ids_from_ir_json
from ..intent.validate import has_errors


# ---------------------------------------------------------------------------
# Hard per-turn guard against the build_circuit RE-CALL STORM.
# The agent (Haiku) ignores the system-prompt's "call build_circuit at most once
# per message" rule and re-invokes it 3-6x on a hard board -- turning ONE failed
# build into a 600s+ no-output disaster (observed live: CAN logger via the chat
# WebSocket, 6 calls / 676s / no schematic). run_turn calls reset_build_guard()
# at the start of every turn; the 2nd+ call this turn is short-circuited INSTANTLY
# (no architect loop) with a STOP message, so even if the agent keeps asking, the
# cost is capped at one real build. A prompt rule can't enforce this -- the model
# ignored it; this is the deterministic enforcement layer.
# ---------------------------------------------------------------------------
_BUILD_CALLS_THIS_TURN = 0


def reset_build_guard() -> None:
    """Reset the per-turn build counter. Called by run_turn at turn start."""
    global _BUILD_CALLS_THIS_TURN
    _BUILD_CALLS_THIS_TURN = 0


def _build_guard_tripped():
    """Increment the per-turn counter; return a STOP short-circuit result dict on
    the (max+1)th call this turn, else None. max = build_graph.max_builds_per_turn
    (default 1; set 0 to disable the guard)."""
    global _BUILD_CALLS_THIS_TURN
    _BUILD_CALLS_THIS_TURN += 1
    try:
        from ..intent.engine import _load_layout_config as _lc_g
        _maxn = int((_lc_g().get("build_graph") or {}).get("max_builds_per_turn", 1))
    except Exception:
        _maxn = 1
    if _maxn <= 0:
        return None  # guard disabled
    if _BUILD_CALLS_THIS_TURN > _maxn:
        return {"content": [{"type": "text", "text": (
            f"STOP -- build_circuit already ran this turn (call #{_BUILD_CALLS_THIS_TURN}). "
            "It runs AT MOST ONCE per user message. Do NOT call it again. Report the "
            "previous build's outcome to the user: if it failed, give the three-option "
            "recovery message (Simplify / Fix / More detail) and WAIT for their choice. "
            "Re-running will not help and wastes minutes.")}],
            "is_error": True}
    return None


@traceable(run_type="llm", name="Ask AI")
def _architect_call(prompt: str, revision_feedback: str = "",
                     prior_attempt_json: str = "",
                     render_mode: str = "") -> str:
    """Call Claude with the architect system prompt; return the raw JSON
    string. Uses the Anthropic SDK directly (not the agent SDK) because
    this is a one-shot prompt-response, not a multi-turn agent loop.

    Strategy (Phase A2):
      1. Force the LLM to use the `emit_ir` strict tool — its
         `input_schema` is grammar-constrained, so the LLM literally
         cannot emit tokens that violate TopologyIR's structure.
         The tool's `input` block is the IR dict; we serialise it
         back to JSON and return.
      2. If the tool path is unavailable (older SDK rejects `strict`,
         model refuses, etc.) — fall through to the legacy text
         path so the call still returns something usable.

    `revision_feedback`, when non-empty, is prepended to the user
    prompt so the architect knows which constraints to fix this
    attempt (lib_id misspellings, missing pins, etc.).

    `prior_attempt_json` is the raw JSON the previous failed attempt
    emitted. We mine it for lib_ids and inject pin maps for THOSE
    parts into the system prompt — so the retry sees the exact pin
    names of the parts it tried but mis-pinned. No-op on the first
    attempt.
    """
    import anthropic

    client = wrap_anthropic(anthropic.Anthropic(), chat_name="Claude")
    model = (os.environ.get("CLAUDE_MODEL_DEEP")
             or os.environ.get("ENVIL_MODEL")
             or "claude-sonnet-4-6")
    user_text = prompt
    if revision_feedback:
        user_text = (
            "Previous attempt FAILED with these issues:\n"
            + revision_feedback
            + "\n\nFix every issue above and re-emit the IR JSON.\n\n"
            + "Original request: " + prompt
        )
    # Per-retry pin-catalog extension (Phase A1).
    extra_lib_ids = extract_lib_ids_from_ir_json(prior_attempt_json) \
        if prior_attempt_json else []
    # P3 sheet-planner hint: scans `prompt` for known domain keywords
    # (USB-C, MCU families, sensor IDs, motor drivers, ...) and splices
    # a <sheet_hint> block into the system prompt so the architect sees
    # a recommended block list. Strictly advisory --- the PINNED RULE for
    # single-IC circuits still wins.
    #
    # render_mode (added 2026-06-02): tells the architect which sheet
    # layout decide_render_mode picked, so block emission aligns with
    # the engine's render path. Was the dominant integration bug ---
    # AUTO would pick SINGLE_SHEET_BLOCKS but the architect, unaware,
    # would still emit blocks=[] and the engine had no blocks to box.
    system_prompt = architect_system(
        extra_lib_ids=extra_lib_ids, prompt=prompt,
        render_mode=render_mode,
    )

    # Phase A3 — wrap the system prompt in a content-block with
    # cache_control=ephemeral so Anthropic's prompt cache picks it up.
    # On byte-identical re-use (same seed catalog, no retry extras) the
    # cached prefix costs 0.1× input tokens and shaves ~85% off TTFT.
    # When `extra_lib_ids` differs (retry path) the catalog bytes change
    # and we'd miss the cache; that's fine — first-pass calls dominate
    # and they hit. The flag stays on always: Anthropic's policy is
    # "no-op when prompt is too small to bother caching", so we can't
    # accidentally hurt anything by always passing cache_control.
    system_blocks = [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]

    # ---- Strict tool_use path (Phase A2) ----
    # Explicit timeout (added 2026-06-02): the Anthropic SDK default
    # is 600s (10 min) which is far too long --- if the API stalls, the
    # whole build_graph hangs and the chat just shows "designing..."
    # forever. Config-driven via layout_config.json:build_graph
    # .architect_timeout_seconds; default 60 is enough for Sonnet 4.6
    # with thinking on a 12 KB system prompt. If the call takes longer,
    # fail fast and let the retry edge (max_attempts in
    # layout_config.json:build_graph) do its job with a fresh request.
    try:
        from ..intent.engine import _load_layout_config as _lc_t
        _bg_cfg = _lc_t().get("build_graph") or {}
        _architect_timeout_s = float(_bg_cfg.get("architect_timeout_seconds", 60))
        # Output budget. 4000 was far too small for a large board: a full
        # ~100-part IR (components + nets) overflows it, and because the strict
        # emit_ir tool is grammar-constrained the overflow closes the JSON
        # VALIDLY with no nets[] (parses fine, board is unwired) instead of an
        # obvious truncation. 16000 fits a ~100-part netlist; small boards
        # finish well under it, so this only raises the ceiling.
        _architect_max_tokens = int(_bg_cfg.get("architect_max_tokens", 16000))
    except Exception:
        _architect_timeout_s = 60.0
        _architect_max_tokens = 16000
    try:
        tool_def = ir_tool_definition(strict=True)
        resp = client.messages.create(
            model=model,
            max_tokens=_architect_max_tokens,
            system=system_blocks,
            tools=[tool_def],
            tool_choice=ir_tool_choice(),
            messages=[{"role": "user", "content": user_text}],
            timeout=_architect_timeout_s,
        )
        for block in resp.content:
            # The SDK exposes tool_use blocks with type=='tool_use'.
            # Their `input` is the validated dict — already a Python
            # object, no JSON parsing needed.
            if getattr(block, "type", None) == "tool_use":
                ir_dict = getattr(block, "input", None) or {}
                # Serialise back to JSON so the rest of the pipeline
                # (which expects a JSON string from this function)
                # keeps working untouched. The TopologyIR.from_json
                # consumer round-trips this fine.
                return json.dumps(ir_dict)
        # Tool wasn't called — fall through to text path below
    except (TypeError, ValueError) as exc:
        # SDK rejected the strict flag or the schema → fall through.
        # Log so we notice if this is happening on every call.
        print(f"[architect] strict tool_use unavailable, falling back: "
              f"{type(exc).__name__}: {exc}", flush=True)
    except anthropic.BadRequestError as exc:
        # 400 from Anthropic API (e.g. tool definition rejected).
        print(f"[architect] BadRequest on strict tool_use, falling back: "
              f"{exc}", flush=True)
    except Exception:
        # Any other transient error — fall through to legacy path.
        # We don't want a transient API issue to leave the user with
        # no answer at all.
        pass

    # ---- Legacy text path (fallback / older SDKs) ----
    # Same 60s timeout as the strict tool_use path above --- fail fast
    # if Anthropic stalls rather than hanging the build for 10 min.
    resp = client.messages.create(
        model=model,
        max_tokens=_architect_max_tokens,
        system=system_blocks,
        messages=[{"role": "user", "content": user_text}],
        timeout=_architect_timeout_s,
    )
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "")
    return ""


def _plan_contract_cfg():
    """(enabled, max_attempts) from layout_config.json:build_graph. Default
    (True, 2). When disabled, the plan is used as-is (byte-identical)."""
    try:
        from ..intent.engine import _load_layout_config as _lc
        bg = _lc().get("build_graph") or {}
        return (bool(bg.get("plan_contract_complete", True)),
                int(bg.get("plan_contract_max_attempts", 2)))
    except Exception:
        return (True, 2)


def _incomplete_signal_nets(ir) -> list:
    """Inter-block SIGNAL nets in the plan that have fewer than two endpoints ->
    they would float (NET_FLOATING). A power rail with one pin is fine (blocks
    attach more pins as they draw), so only NON-power nets count. Returns
    [(name, [pins])]. Purely structural (pin-count + is_power) -- no net-name
    table, works for any board."""
    out = []
    for n in getattr(ir, "nets", []) or []:
        if bool(getattr(n, "is_power", False)):
            continue
        pins = list(getattr(n, "pins", []) or [])
        if len(pins) < 2:
            out.append((n.name, pins))
    return out


def _complete_plan_contract(raw: str, prompt: str, render_mode: str,
                            client, model, timeout: float, max_tokens: int) -> str:
    """THE cure for floating cross-block control nets. After the plan call,
    detect inter-block signal nets with only one endpoint (ALERT/CFETOFF/DFETOFF/
    REGIN/... declared on the source IC but never wired to the MCU) and ask the
    architect to add the missing endpoint, re-emitting the FULL plan. No
    downstream repair can invent which GPIO a control signal lands on -- only the
    architect can -- so this closes the gap at the generation layer, BEFORE any
    block is drawn (so the both-ended contract propagates to both block draws).

    Bounded (plan_contract_max_attempts) and gated (plan_contract_complete);
    accepts a re-emit only when it actually reduces the one-ended count, so it
    can never loop or regress. The completion call sees the board's real pins (pin
    catalog via the system prompt) so it picks valid pins. Returns the completed
    plan JSON, or the original on any failure / gate off. Fully dynamic."""
    enabled, max_attempts = _plan_contract_cfg()
    if not enabled:
        return raw
    try:
        from ..intent.ir import TopologyIR
    except Exception:
        return raw
    for _ in range(max(1, max_attempts)):
        try:
            ir = TopologyIR.from_json(_extract_ir_json(raw))
        except Exception:
            return raw
        incomplete = _incomplete_signal_nets(ir)
        if not incomplete:
            return raw                       # contract complete
        lib_ids = [c.lib_id for c in getattr(ir, "components", []) or []]
        try:
            system_prompt = architect_system(extra_lib_ids=lib_ids, prompt=prompt,
                                             render_mode=render_mode)
        except Exception:
            return raw
        system_blocks = [{"type": "text", "text": system_prompt,
                          "cache_control": {"type": "ephemeral"}}]
        bad_lines = "\n".join(
            f"  {nm}: currently {pins or '[]'} -- add its missing endpoint "
            f"(a REAL pin on the consuming IC: an MCU GPIO/ADC pin, a driver "
            f"input, ...)"
            for nm, pins in incomplete)
        user_text = (
            "Your PLAN has inter-block signal nets with only ONE endpoint. A "
            "one-ended signal net FLOATS and the build WILL FAIL. Add the SECOND "
            "endpoint to each -- a REAL <ref>.<pin> on the consuming IC. Use ONLY "
            "pins that exist on the target symbol; if unsure, pick a free GPIO/ADC "
            "pin on the MCU.\n\n"
            "ONE-ENDED NETS to complete:\n"
            f"{bad_lines}\n\n"
            "Re-emit the COMPLETE plan TopologyIR (all components[], blocks[], "
            "nets[]) with these nets now carrying BOTH endpoints. Keep everything "
            "else identical; do not drop or rename other nets. Use <ref>.<pin>."
        )
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, system=system_blocks,
                tools=[ir_tool_definition(strict=True)],
                tool_choice=ir_tool_choice(),
                messages=[{"role": "user", "content": user_text}],
                timeout=timeout)
        except Exception as exc:             # noqa: BLE001
            print(f"[architect] plan-contract completion failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return raw
        new_raw = ""
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                new_raw = json.dumps(getattr(block, "input", None) or {})
                break
        if not new_raw:
            return raw
        try:
            new_ir = TopologyIR.from_json(_extract_ir_json(new_raw))
        except Exception:
            return raw
        if len(_incomplete_signal_nets(new_ir)) >= len(incomplete):
            return raw                       # no progress -> stop (never regress/loop)
        raw = new_raw                        # progress -> keep, re-check
    return raw


@traceable(run_type="llm", name="Plan blocks")
def _architect_plan_call(prompt: str, render_mode: str = "hierarchy") -> str:
    """Forward-incremental PLAN call: emit the board SKELETON only -- all
    components[], grouped into blocks[], plus nets[] for ONLY the global power
    rails and the signals that CROSS between blocks. Intra-block wiring is
    omitted (each block is drawn separately by _architect_block_call), so the
    output stays small and reliable even at ~100 parts -- avoiding the one-shot
    overflow that tangles a large board. Returns raw JSON (same shape as
    _architect_call) or "" on failure."""
    import anthropic

    client = wrap_anthropic(anthropic.Anthropic(), chat_name="Claude")
    model = (os.environ.get("CLAUDE_MODEL_DEEP")
             or os.environ.get("ENVIL_MODEL")
             or "claude-sonnet-4-6")
    system_prompt = architect_system(extra_lib_ids=[], prompt=prompt,
                                     render_mode=render_mode)
    system_blocks = [{"type": "text", "text": system_prompt,
                      "cache_control": {"type": "ephemeral"}}]
    user_text = (
        "PLAN ONLY -- do NOT wire inside blocks. Decompose this board into "
        "functional blocks and emit a SKELETON TopologyIR:\n"
        "  - components[]: EVERY part on the board (ref, lib_id, value).\n"
        "  - blocks[]: group EVERY component into a functional block (POWER, "
        "MCU, comms, sensing, protection, ...); each block's component_refs[] "
        "lists its parts. Every component must be in exactly one block.\n"
        "  - nets[]: emit ONLY (a) the global power rails (GND, +3V3, +5V, ...) "
        "carrying each block's power/ground pin, and (b) signals that CROSS "
        "between two blocks (SPI/I2C/UART/CAN lines, interrupts, enables between "
        "the MCU and a peripheral). DO NOT emit a net that lives entirely inside "
        "one block (decoupling caps, pull-ups, crystal caps, series resistors, "
        "local dividers) -- those are drawn per-block afterwards.\n"
        "  - EVERY inter-block signal net MUST list BOTH endpoints -- the SOURCE "
        "pin AND the DESTINATION pin. e.g. an interrupt ALERT is "
        "[<sensor>.ALERT, <mcu>.<gpio>]; a control CFETOFF is "
        "[<mcu>.<gpio>, <driver>.<pin>]; an ADC sense is [<sensor>.OUT, "
        "<mcu>.<adc_pin>]. A signal net with only ONE pin is a FLOATING net and "
        "WILL FAIL -- if a signal leaves one block it MUST land on a REAL pin of "
        "another block. If you don't have an exact destination pin, assign a free "
        "GPIO/ADC pin on the consumer IC; never leave it one-ended.\n\n"
        "Keep nets[] SMALL: rails + inter-block signals only. Use <ref>.<pin> "
        "references.\n\n"
        f"Board to plan: {prompt}"
    )
    try:
        from ..intent.engine import _load_layout_config as _lc_t
        _bg = _lc_t().get("build_graph") or {}
        _to = float(_bg.get("architect_timeout_seconds", 60))
        _mt = int(_bg.get("architect_max_tokens", 16000))
    except Exception:
        _to, _mt = 60.0, 16000
    try:
        resp = client.messages.create(
            model=model, max_tokens=_mt, system=system_blocks,
            tools=[ir_tool_definition(strict=True)], tool_choice=ir_tool_choice(),
            messages=[{"role": "user", "content": user_text}], timeout=_to)
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                raw = json.dumps(getattr(block, "input", None) or {})
                # Close the cross-block contract: complete any one-ended signal
                # net (the floating ALERT/CFETOFF/... cause) before drawing.
                # Gated; returns `raw` unchanged when off or already complete.
                return _complete_plan_contract(raw, prompt, render_mode,
                                               client, model, _to, _mt)
    except Exception as exc:                 # noqa: BLE001
        print(f"[architect] plan call failed: {type(exc).__name__}: {exc}",
              flush=True)
    return ""


@traceable(run_type="llm", name="Fix block")
def _architect_block_call(block_name, block_type, components, errors,
                          boundary_nets, prompt):
    """Per-block retry (#9): regenerate ONE functional block instead of the whole
    board. Returns ``(components, nets)`` as IRComponent / IRNet lists for THIS
    block only, or ``(None, None)`` on failure (caller leaves the block as-is).

    The block is regenerated against a FROZEN interface so the splice can re-
    attach it without disturbing the rest of the board:
      components    : [(ref, lib_id, value), ...] — refdes the model must keep.
      errors        : the validate error dicts localised to this block.
      boundary_nets : [{name, is_power, external_pins, my_pins}, ...] — net names
                      that already exist elsewhere; the block reconnects to them
                      by the EXACT name, and must NOT restate their external pins.
    Reuses the same strict `emit_ir` tool + model + timeout as `_architect_call`,
    just with a focused, ~5-part user prompt (small + fast, no divergence)."""
    import anthropic
    from ..intent.ir import IRComponent, IRNet

    client = wrap_anthropic(anthropic.Anthropic(), chat_name="Claude")
    model = (os.environ.get("CLAUDE_MODEL_DEEP")
             or os.environ.get("ENVIL_MODEL")
             or "claude-sonnet-4-6")

    comp_lines = "\n".join(f"  {r}  {lib}   value={v}"
                            for (r, lib, v) in components) or "  (none)"
    err_lines = "\n".join(
        f"  [{e.get('code')}] @ {e.get('where')}: {e.get('text')}"
        for e in errors) or "  (none)"
    # Gated (build_graph.feedback_available_pins): render the FULL real pin
    # set for any part with a PIN_NOT_ON_SYMBOL error so the model picks the
    # actual library pin instead of re-emitting the hallucinated one.
    # Returns "" when the gate is off -> user_text below stays byte-identical.
    from ..intent.validate import render_available_pins_section as _render_avail
    _avail_section = _render_avail(errors)
    _avail_block = f"{_avail_section}\n\n" if _avail_section else ""
    bnd_lines = []
    for b in boundary_nets:
        tag = " (power rail)" if b.get("is_power") else ""
        ext = ", ".join(b.get("external_pins") or []) or "(no external pin yet)"
        bnd_lines.append(f"  {b['name']}{tag}  [also wired to: {ext}]")
    bnd_txt = "\n".join(bnd_lines) or "  (none)"

    lib_ids = [lib for (_r, lib, _v) in components]
    system_prompt = architect_system(extra_lib_ids=lib_ids, prompt=prompt,
                                     render_mode="")
    system_blocks = [{"type": "text", "text": system_prompt,
                      "cache_control": {"type": "ephemeral"}}]

    user_text = (
        "You are repairing ONE functional block of a larger schematic. "
        "Regenerate ONLY this block.\n\n"
        f"BLOCK: {block_name} ({block_type})\n\n"
        "COMPONENTS — keep these EXACT refdes, do not rename them, and only add "
        "a part if an error below requires it:\n"
        f"{comp_lines}\n\n"
        "ERRORS to fix in this block:\n"
        f"{err_lines}\n\n"
        f"{_avail_block}"
        "FROZEN INTERFACE — these net names already exist on the rest of the "
        "board. Wire this block's pins to them using the EXACT names; do NOT "
        "rename them and do NOT list their external pins (shown only for "
        "context):\n"
        f"{bnd_txt}\n\n"
        "Output a TopologyIR whose components[] are THIS block's parts and whose "
        "nets[] are the nets touching this block (its internal nets PLUS the "
        "frozen-interface nets above, each carrying this block's <ref>.<pin> "
        "endpoints). Fix every error. Use <ref>.<pin> pin references.\n\n"
        f"Original request (context only): {prompt}"
    )

    try:
        from ..intent.engine import _load_layout_config as _lc_t
        _bg = _lc_t().get("build_graph") or {}
        _to = float(_bg.get("architect_timeout_seconds", 60))
        _block_max_tokens = int(_bg.get("architect_block_max_tokens", 4000))
    except Exception:
        _to = 60.0
        _block_max_tokens = 4000

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=_block_max_tokens,
            system=system_blocks,
            tools=[ir_tool_definition(strict=True)],
            tool_choice=ir_tool_choice(),
            messages=[{"role": "user", "content": user_text}],
            timeout=_to,
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                d = getattr(block, "input", None) or {}
                comps = [IRComponent.from_dict(c) for c in d.get("components", [])]
                nets = [IRNet.from_dict(n) for n in d.get("nets", [])]
                if comps:
                    return comps, nets
    except Exception as exc:                 # noqa: BLE001
        print(f"[block_repair] {block_name}: block call failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
    return None, None


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_ir_json(raw: str) -> str:
    """Strip markdown fences if the architect added them despite being
    told not to. Returns the largest balanced {...} block."""
    m = _JSON_FENCE.search(raw)
    if m:
        return m.group(1)
    # Find the outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return raw[start:end + 1]
    return raw


def _partial_persist_on() -> bool:
    """Gate: layout_config.json -> build_graph.partial_persist_on_failure
    (default false). When on, a build that fails validation but drew some blocks
    cleanly SAVES those blocks (holding out the broken ones) instead of returning
    nothing."""
    try:
        from ..intent.engine import _load_layout_config as _lc
        return bool((_lc().get("build_graph") or {}).get(
            "partial_persist_on_failure", False))
    except Exception:
        return False


def _strip_blocks(ir: Any, held: set) -> Any:
    """Return a NEW IR with the held-out blocks' components + nets removed, so the
    partial render is electrically clean (the broken block is held out, not drawn
    with its conflict). Dynamic + part-agnostic: blocks are identified by their
    own `component_refs`, nets keep only the pins of kept components and are
    dropped if fewer than two survive (a 1-pin remnant is just a stub)."""
    from ..intent.ir import TopologyIR
    held_refs: set = set()
    for b in getattr(ir, "blocks", []) or []:
        if b.name in held:
            held_refs.update(getattr(b, "component_refs", []) or [])
    d = ir.to_dict()
    d["components"] = [c for c in d.get("components", [])
                       if c.get("ref") not in held_refs]
    new_nets = []
    for n in d.get("nets", []):
        pins = [p for p in n.get("pins", [])
                if str(p).split(".", 1)[0] not in held_refs]
        # Keep a power rail even if only one kept pin remains (it still connects
        # to its power port); drop a SIGNAL net that falls below two pins (a
        # 1-pin signal is just a floating stub once its other end was held out).
        if (bool(n.get("is_power")) and len(pins) >= 1) or len(pins) >= 2:
            nn = dict(n)
            nn["pins"] = pins
            new_nets.append(nn)
    d["nets"] = new_nets
    d["blocks"] = [b for b in d.get("blocks", [])
                   if b.get("name") not in held]
    return TopologyIR.from_dict(d)


def _try_partial_persist(final: Dict[str, Any], issues: list):
    """SALVAGE a failed incremental build: render the cleanly-drawn blocks and
    hold out the ones that failed/conflict, so the user keeps the work instead of
    a blank sheet. Returns a success-shaped tool result (JSON with `path` so the
    server refreshes eeschema via its existing safe-revert path) or None to fall
    back to the legacy all-or-nothing failure. Only fires on the incremental path
    (which leaves a partial IR with block metadata). Never raises."""
    try:
        ir = final.get("ir")
        if ir is None or not getattr(ir, "blocks", None):
            return None                       # not a block/incremental build
        drawn = [b.name for b in ir.blocks]   # blocks present in the partial IR
        from ..intent.block_repair import localize_issues
        by_block, _unloc = localize_issues(ir, issues)
        held = set(final.get("incremental_skipped") or [])
        held |= set(by_block.keys())          # blocks owning unresolved errors
        held |= set(getattr(ir, "_incremental_skipped", []) or [])
        held &= {b.name for b in ir.blocks}
        if not held:
            return None                       # nothing cleanly separable -> legacy
        clean = _strip_blocks(ir, held)
        if not getattr(clean, "blocks", None) or not clean.components:
            return None                       # everything held out -> nothing to show
        from ..graphs.nodes.render import render_node
        r = render_node({**final, "ir": clean})
        if r.get("error") or not r.get("stats"):
            return None
        stats = r["stats"]
        kept = [b.name for b in clean.blocks]
        held_sorted = sorted(held)
        summary = {
            "status": "partial",
            "path": stats.get("path", ""),
            "drawn": kept,
            "held_out": held_sorted,
            "note": (f"Built and saved {len(kept)} block(s) to the canvas; held out "
                     f"{len(held_sorted)} block(s) with unresolved wiring conflicts: "
                     f"{', '.join(held_sorted)}. The drawn blocks are on your sheet — "
                     f"fix or ask me to redraw just the held-out blocks."),
            "child_paths": [c.get("path", "") for c in stats.get("children", [])
                            if c.get("path")],
        }
        return {"content": [{"type": "text", "text": json.dumps(summary, indent=2)}]}
    except Exception as exc:                  # noqa: BLE001 - salvage is best-effort
        print(f"[build_circuit] partial-persist skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


@tool(
    name="build_circuit",
    description=(
        "Build a KiCad schematic from a natural-language description. "
        "Use this when the user asks to 'build', 'create', 'make', "
        "'design', or 'generate' a circuit. "
        "\n\n"
        "IMPORTANT: if the user is working with an attached schematic "
        "(mentioned in 'Working schematic:' context), pass `out_path` "
        "set to that schematic's full path so the new circuit OVERWRITES "
        "the open file instead of creating a separate one. Without "
        "`out_path`, the tool writes to `out_dir`/<auto-name>.kicad_sch. "
        "\n\n"
        "Args:\n"
        "  prompt: the circuit description (required)\n"
        "  out_path: full path to the .kicad_sch to overwrite (preferred "
        "when user has a schematic open — use the path from the "
        "'Working schematic:' context)\n"
        "  out_dir: fallback directory if out_path not given\n"
        "  project_name: optional — the project name the user accepted or "
        "typed in the build preview (Cursor-style). When given, the circuit "
        "folder + .kicad_sch file (+ child sheets + title block) are named "
        "after it instead of the auto architect name. Use a short slug with "
        "no spaces/extension (e.g. 'ne555_blinker'). Omit to keep the "
        "automatic name. Ignored when out_path is set (overwriting an open "
        "file keeps that file's name).\n"
        "  force_hierarchy: bool — when true, ALWAYS render as a "
        "multi-sheet hierarchy (parent + child sheets) regardless of "
        "component count. Use when the user asks for 'multi-sheet' / "
        "'hierarchy' / 'split sheets' / 'separate sheets' explicitly. "
        "Otherwise leave false and let the engine auto-decide.\n"
        "  force_single_sheet: bool — when true, ALWAYS render as ONE "
        "sheet with coloured numbered block rectangles around each "
        "functional block (the block-diagram style). Overrides the "
        "auto size-based decision but is overridden by force_hierarchy. "
        "Use when the user asks for 'single sheet', 'block diagram', "
        "'one page', 'single page schematic', or 'overview style'.\n"
        "\n"
        "Example prompts: '1Hz LED blinker with NE555', "
        "'5V regulator from 12V using LM317', "
        "'ATtiny85 minimal board with ICSP header', "
        "'USB-C STM32G031 board, single sheet block diagram style'."
    ),
    input_schema={"prompt": str, "out_dir": str, "out_path": str,
                   "project_name": str,
                   "force_hierarchy": bool, "force_single_sheet": bool},
)
async def build_circuit(args: Dict[str, Any]) -> Dict[str, Any]:
    """MCP tool entry. The classify -> architect -> validate -> render
    -> preview pipeline lives in envil_agent.graphs.router_graph;
    this function is now a thin adapter that maps tool args into
    RouterState, invokes the graph, and formats the MCP response.

    Why router_graph (not build_graph) is the entry: the top-level
    classify_intent node makes the routing decision visible in the
    LangSmith trace tree — without it, the 'router' step was invisible
    because tool-selection happened inside the agent SDK. Internal
    callers that only want the build pipeline can still import
    build_graph directly."""
    # Hard re-call guard: short-circuit the 2nd+ build_circuit of this turn so a
    # model that ignores the "at most once" rule can't restart the whole
    # architect loop and burn minutes (the 676s / 6-call chat failure).
    _guard = _build_guard_tripped()
    if _guard is not None:
        return _guard
    from ..graphs.router_graph import router_graph, router_graph_detailed

    # Pick the 7-node detailed graph (classify -> decide_render_mode ->
    # architect -> normalize -> validate -> render -> export) when
    # layout_config.json:build_graph.detailed_trace is true (default),
    # else the original 5-node router_graph. Flipping the flag reverts
    # with no code change.
    try:
        from ..intent.engine import _load_layout_config as _lc_t
        _detailed = bool((_lc_t().get("build_graph") or {})
                         .get("detailed_trace", True))
    except Exception:
        _detailed = True
    _graph = router_graph_detailed if _detailed else router_graph

    prompt = args.get("prompt", "").strip()
    if not prompt:
        return {
            "content": [{"type": "text", "text": "ERROR: empty prompt"}],
            "is_error": True,
        }

    from ..settings import out_dir as _out_dir
    initial: Dict[str, Any] = {
        "prompt": prompt,
        "out_dir": args.get("out_dir", "").strip() or str(_out_dir()),
        "out_path": args.get("out_path", "").strip(),
        "project_name": args.get("project_name", "").strip(),
        "force_hierarchy": bool(args.get("force_hierarchy", False)),
        "force_single_sheet": bool(args.get("force_single_sheet", False)),
        "attempt": 0,
        "feedback": "",
        "decision_log": [],
    }

    # recursion_limit: a large board can interleave architect -> validate ->
    # block_repair -> validate -> ... many times (max_attempts full regens +
    # max_block_attempts per-block passes). LangGraph's default ceiling of 25
    # supersteps can be hit on such a board, surfacing as an opaque
    # GraphRecursionError that DISCARDS the keep_best IR. Derive a generous
    # ceiling from the retry budget so a legitimate long repair finishes.
    try:
        _bg_rl = _lc_t().get("build_graph", {}) or {}
        _recursion_limit = int(_bg_rl.get("graph_recursion_limit", 60))
    except Exception:
        _recursion_limit = 60
    # Phase 0 (gated build_graph.offload_build_to_thread, default off): run the
    # SYNC graph in a worker thread so the asyncio loop stays free to push
    # per-block status + eeschema reverts DURING the build (today every node is
    # sync and the architect uses the sync anthropic client, so `await ainvoke`
    # blocks the loop for the whole ~6 min). asyncio.to_thread COPIES contextvars
    # into the worker, so LangSmith's @traceable run tree + _mark_run_failed keep
    # nesting under "Circuit Design" (run_in_executor would detach them). When the
    # gate is OFF this is the literal current `await ainvoke` line -> byte-stable.
    try:
        _bg_off = _lc_t().get("build_graph", {}) or {}
        _offload = bool(_bg_off.get("offload_build_to_thread", False))
    except Exception:
        _offload = False
    # Open ONE umbrella run around the whole graph invocation so Plan blocks +
    # every Fix block + the engine spans nest into a SINGLE route under the
    # conversation, instead of scattering as separate top-level traces. The CM
    # sets the langsmith run-tree contextvar, which BOTH paths below propagate
    # (to_thread copies contextvars; langchain copies context into node executor
    # threads on ainvoke). Best-effort + flag-gated -> byte-stable when off.
    try:
        _single_trace_on = bool(
            (_lc_t().get("build_graph") or {}).get("single_build_trace", True))
    except Exception:
        _single_trace_on = True
    try:
        with _single_build_trace(_single_trace_on):
            if _offload:
                final = await asyncio.to_thread(
                    _graph().invoke, initial,
                    {"recursion_limit": _recursion_limit})
            else:
                final = await _graph().ainvoke(
                    initial, config={"recursion_limit": _recursion_limit})
    except Exception as exc:
        _mark_run_failed(f"graph invocation failed: {type(exc).__name__}: {exc}")
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: graph invocation failed: {type(exc).__name__}: {exc}"}],
            "is_error": True,
        }

    # Terminal-failure shapes come through as `error` set on the state OR
    # as still-present validation errors after the retry budget ran out.
    err = final.get("error")
    if err:
        _mark_run_failed(str(err), attempt=int(final.get("attempt", 0) or 0))
        return {
            "content": [{"type": "text", "text": f"ERROR: {err}"}],
            "is_error": True,
        }

    # Prefer the BEST attempt's issues over the last one's: when the
    # architect diverges (5 -> 8 -> 27 errors across retries) the last
    # attempt is the worst. best_issues is recorded by validate's
    # _remember_best; fall back to the last attempt when it is absent
    # (keep_best_attempt disabled or a single-attempt run).
    best_issues = final.get("best_issues")
    last_issues = final.get("issues") or []
    issues = best_issues if best_issues is not None else last_issues
    if has_errors(issues) or final.get("ir") is None or not final.get("stats"):
        fail_lines = "\n".join(
            f"  [{i['severity']}] {i['code']} @ {i['where']}: {i['text']}"
            for i in issues[:10]
        )
        # Include the decision_log so the user can trace which node
        # rejected what — the "track which place use to fix the error"
        # ask. Truncated to the last 12 entries so the chat reply stays
        # readable when retries pile up.
        log_lines = "\n".join(f"  - {ln}" for ln in
                                (final.get("decision_log") or [])[-12:])
        # Gated salvage (build_graph.partial_persist_on_failure): before giving
        # up, render the blocks that drew cleanly and hold out the broken ones, so
        # the user keeps the work + sees it on the canvas. Returns a success-shaped
        # result (the server refreshes via its existing path-refresh). None ->
        # legacy all-or-nothing failure below (byte-identical when the gate off).
        if _partial_persist_on():
            _partial = _try_partial_persist(final, issues)
            if _partial is not None:
                return _partial
        _mark_run_failed(
            f"IR validation failed after {final.get('attempt', 0)} attempt(s): "
            f"{len(issues)} unresolved issue(s)",
            issue_count=len(issues), attempt=int(final.get("attempt", 0) or 0))
        return {
            "content": [{"type": "text",
                          "text": (f"IR validation failed after {final.get('attempt', 0)} "
                                   f"attempt(s) ({len(issues)} unique issue(s)):\n{fail_lines}"
                                   + (f"\n\nRouter trace:\n{log_lines}" if log_lines else ""))}],
            "is_error": True,
        }

    ir = final["ir"]
    stats = final["stats"]
    is_hierarchical = "blocks" in stats
    summary: Dict[str, Any] = {
        "name": ir.name,
        "circuit_type": ir.circuit_type,
        "path": stats["path"],
        "project": stats.get("project", ""),
        "hierarchical": is_hierarchical,
        "notes": ir.notes,
    }
    if is_hierarchical:
        summary["blocks"] = stats.get("blocks", 0)
        summary["children"] = stats.get("children", [])
        summary["cross_block_nets"] = stats.get("cross_block_nets", [])
        summary["components_total"] = stats.get("components_total", 0)
        summary["nets_total"] = stats.get("nets_total", 0)
        summary["child_paths"] = [c.get("path", "") for c in stats.get("children", [])
                                    if c.get("path")]
    else:
        summary["components_emitted"] = stats.get("components_emitted", 0)
        summary["wires_emitted"] = stats.get("wires_emitted", 0)
        summary["labels_emitted"] = stats.get("labels_emitted", 0)
        summary["junctions_emitted"] = stats.get("junctions_emitted", 0)
        summary["nc_flags_emitted"] = stats.get("nc_flags_emitted", 0)
        summary["nets"] = stats.get("nets", 0)

    summary["preview_svgs"] = final.get("preview_svgs") or []
    # Surface the router's audit trail so the chat reply can show
    # which node did what — "which place use to fix the error".
    summary["route"] = final.get("route", "")
    summary["decision_log"] = final.get("decision_log") or []

    # Phase 23 — drop an IR-block sidecar next to the .kicad_sch so the PCB
    # side can do functional placement (group footprints by block + role)
    # instead of the dumb refdes-prefix grid auto_place_pcb defaulted to.
    # Pure data; PCB tools read this lazily and fall back to old behaviour
    # when the file is missing, so old projects keep working.
    try:
        _write_blocks_sidecar(stats.get("path", ""), ir, stats)
    except Exception as _exc:
        # Sidecar is advisory — never fail the build because of it.
        print(f"[build_circuit] sidecar write skipped: "
              f"{type(_exc).__name__}: {_exc}", flush=True)

    # Direct PCB generation (Cursor-style, file-based): write a populated
    # .kicad_pcb next to the .kicad_sch with footprints placed + nets assigned,
    # so the board exists the instant the schematic does — no "Update PCB from
    # Schematic" / F8 step, no running KiCad app needed (KiCad just reloads it).
    # Gated by layout_config.json:pcb_gen.enabled (default true). Mirrors the
    # sidecar contract: a PCB-gen failure is recorded in the summary and NEVER
    # breaks the schematic build. The flat IR (all components + nets) drives it,
    # so flat and hierarchical schematics both yield one flat board.
    try:
        from ..intent.engine import _load_layout_config as _llc
        _pcbgen_cfg = (_llc() or {}).get("pcb_gen", {}) or {}
    except Exception:
        _pcbgen_cfg = {}
    if (_pcbgen_cfg.get("enabled", True)
            and str(stats.get("path", "")).endswith(".kicad_sch")):
        # ERC-CLEAN GATE (the schematic->PCB rule): never push a schematic with
        # unresolved ERC errors onto copper — the mistakes become real traces.
        # Gate generation on ERC being clean (require_erc_clean, default true).
        # Prefer the error count self_heal already computed on THIS schematic
        # (final["erc_summary"]["errors"], post-autofix); if it's missing or
        # unreliable, run ERC here so the gate never trusts a stale/absent value
        # (Cursor-style: verify by running). A checker that errors leaves the
        # count unknown -> we generate (don't fail the board on a tool hiccup),
        # but a KNOWN error count > 0 blocks generation with a clear note.
        _require_clean = bool(_pcbgen_cfg.get("require_erc_clean", True))
        _erc_errors: Optional[int] = None
        if _require_clean:
            _es = (final.get("erc_summary") or {})
            if isinstance(_es.get("errors"), int) and _es["errors"] >= 0:
                _erc_errors = int(_es["errors"])
            else:
                try:
                    from .erc_check import erc_check as _erc_check
                    _er = await _erc_check.handler({"path": stats["path"]})
                    if not _er.get("is_error"):
                        _erc_errors = int(json.loads(
                            _er["content"][0]["text"]).get("error_count", 0))
                except Exception as _exc:
                    print(f"[build_circuit] pcb_gen ERC gate check failed "
                          f"({type(_exc).__name__}: {_exc}); proceeding",
                          flush=True)
                    _erc_errors = None

        if _require_clean and _erc_errors is not None and _erc_errors > 0:
            summary["pcb"] = {
                "blocked": "erc_not_clean",
                "erc_errors": _erc_errors,
                "note": (f"PCB not generated: {_erc_errors} ERC error(s) still "
                         "open on the schematic. Fix the ERC errors first, then "
                         "rebuild — going to PCB with ERC errors copies the "
                         "mistakes onto the board."),
            }
            print(f"[build_circuit] pcb_gen BLOCKED by ERC gate: "
                  f"{_erc_errors} error(s)", flush=True)
        else:
            try:
                from ..layout.pcb_gen import generate_pcb_from_ir
                _pcb = generate_pcb_from_ir(ir, stats["path"])
                summary["pcb"] = {
                    "path": _pcb.get("pcb_path", ""),
                    "footprints_placed": _pcb.get("footprints_placed", 0),
                    "pads_netted": _pcb.get("pads_netted", 0),
                    "nets": _pcb.get("nets", 0),
                    "footprints_missing": _pcb.get("footprints_missing", []),
                    "erc_errors": _erc_errors if _erc_errors is not None else 0,
                }
                print(f"[build_circuit] pcb_gen: {_pcb.get('footprints_placed', 0)} "
                      f"footprints, {_pcb.get('pads_netted', 0)} pads netted -> "
                      f"{_pcb.get('pcb_path', '')}", flush=True)
            except Exception as _exc:
                print(f"[build_circuit] pcb_gen skipped: "
                      f"{type(_exc).__name__}: {_exc}", flush=True)
                summary["pcb"] = {"error": f"{type(_exc).__name__}: {_exc}"}

    # Standard-rules foundation on a freshly generated board: board outline
    # (Edge.Cuts) + fab design rules/netclasses + GND copper pour. Makes the
    # board fab-acceptable (edge), ruled (clearances/track widths), and gives a
    # ground plane so routing only has to handle signal nets. Each step is
    # best-effort and reuses the existing apply tools; a failure is logged and
    # never breaks the build. Gated by pcb_gen.finish_board (default true).
    _pcb_info = summary.get("pcb") or {}
    _pcb_path = _pcb_info.get("path", "")
    if (_pcb_path and not _pcb_info.get("error") and not _pcb_info.get("blocked")
            and bool(_pcbgen_cfg.get("finish_board", True))):
        _finished: list = []
        for _step in ("outline", "design_rules", "ground_pour"):
            try:
                if _step == "outline":
                    from .auto_outline_pcb import auto_outline_pcb as _ft
                    _fa = {"pcb_path": _pcb_path}
                elif _step == "design_rules":
                    from .set_design_rules import set_design_rules as _ft
                    _fa = {"pcb_path": _pcb_path}
                else:
                    from .auto_zones_pcb import auto_zones_pcb as _ft
                    _fa = {"pcb_path": _pcb_path, "net": "GND"}
                _fr = await _ft.handler(_fa)
                if not (_fr or {}).get("is_error"):
                    _finished.append(_step)
            except Exception as _exc:
                print(f"[build_circuit] finish_board {_step} skipped: "
                      f"{type(_exc).__name__}: {_exc}", flush=True)
        summary["pcb"]["finished"] = _finished
        print(f"[build_circuit] finish_board: {_finished}", flush=True)

    return {
        "content": [{"type": "text",
                      "text": json.dumps(summary, indent=2)}],
    }


def _write_blocks_sidecar(sch_path: str, ir: Any, stats: Dict[str, Any]) -> None:
    """Persist `<basename>.envil-blocks.json` next to the .kicad_sch.

    Schema (versioned so future bumps don't break old readers):
      {
        "_about": "...",
        "version": 1,
        "name": "<circuit name>",
        "circuit_type": "MCU_BOARD",
        "blocks": [
          {"name": "POWER", "block_type": "power",
           "flow_role": "regulator", "component_refs": ["U2","C1",…]},
          ...
        ]
      }

    Skipped when sch_path is empty or there are no blocks (single-IC
    circuits with no functional partitioning have nothing useful to
    persist — placer falls back to refdes grouping). Skipped silently
    so a write failure (locked file, read-only mount) doesn't break
    the build.
    """
    if not sch_path:
        return
    from pathlib import Path as _Path
    sch = _Path(sch_path)
    if sch.suffix.lower() != ".kicad_sch":
        return
    blocks = list(getattr(ir, "blocks", []) or [])
    if not blocks:
        return
    sidecar = sch.with_suffix(".envil-blocks.json")
    payload = {
        "_about": ("Functional-block sidecar emitted by envil build_circuit. "
                    "Read by auto_place_pcb to place footprints by block "
                    "instead of by refdes prefix. Safe to delete — the "
                    "PCB tools fall back to the legacy behaviour."),
        "version": 1,
        "name": getattr(ir, "name", "") or "",
        "circuit_type": getattr(ir, "circuit_type", "") or "",
        "blocks": [
            {
                "name": b.name,
                "block_type": getattr(b, "block_type", "") or "generic",
                "flow_role": getattr(b, "flow_role", "") or "",
                "component_refs": list(getattr(b, "component_refs", []) or []),
            }
            for b in blocks
        ],
    }
    sidecar.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
