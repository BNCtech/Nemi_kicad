"""Block-repair node — per-block retry (#9).

Instead of re-emitting the FULL TopologyIR on a validate failure (which diverges
+ times out on 30+ part boards), regenerate ONLY the block(s) that own the
errors and splice them back, keeping every passing block byte-frozen.

  validate --block--> Fix block (this node) --> validate

Gated by build_graph.per_block_retry (default false). The routing helper
`should_block_repair` decides whether this path applies; when it doesn't (flag
off, no blocks, an error that crosses blocks, or the block-attempt budget is
spent) the graph routes to a normal FULL architect regen instead. Only
router_graph_detailed wires this node; build_graph + the 5-node router are
untouched. See intent/block_repair.py for the deterministic core
(attribution + interface + splice, proven at 0 shorts).
"""
from __future__ import annotations

import json
from typing import Any, Dict

from ...intent.block_repair import (
    localize_issues, block_interface, splice_block, _block_by_name,
)
from ...intent.normalize import normalize_ir


def _bg_cfg(key: str, default):
    """Read a build_graph.<key> from layout_config.json with a fallback."""
    try:
        from ...intent.engine import _load_layout_config
        return (_load_layout_config().get("build_graph") or {}).get(key, default)
    except Exception:
        return default


def should_block_repair(state: Dict[str, Any]) -> bool:
    """True when the retry should regenerate single blocks rather than the whole
    board: the flag is on, the board is LARGE ENOUGH that a single-shot full
    regen degrades, the IR has blocks, the block-attempt budget is not spent,
    and EVERY current error localises to a block (no cross-block / global error —
    those need a full regen). Pure; safe to call from the routing edge.

    LARGE-BOARD GATE: per-block (decomposition) generation is the documented fix
    for the size-specific failure (output-length ceiling / context rot /
    lost-in-the-middle make a one-shot ~100-part netlist degrade). Small + medium
    boards converge fine on the proven single-shot full-regen path, so we only
    switch to decomposition once the board crosses block_repair_min_components
    (or block_repair_min_blocks). Below the threshold a failed board still routes
    to a normal full regen — its behaviour is unchanged."""
    if not bool(_bg_cfg("per_block_retry", False)):
        return False
    ir = state.get("ir")
    if ir is None or not getattr(ir, "blocks", None):
        return False
    # Large-board gate: only decompose when the board is big enough to need it.
    n_comps = len(getattr(ir, "components", []) or [])
    n_blocks = len(getattr(ir, "blocks", []) or [])
    min_comps = int(_bg_cfg("block_repair_min_components", 40))
    min_blocks = int(_bg_cfg("block_repair_min_blocks", 0))
    is_large = (n_comps >= min_comps) or (min_blocks > 0 and n_blocks >= min_blocks)
    if not is_large:
        return False
    if int(state.get("block_attempt", 0)) >= int(_bg_cfg("max_block_attempts", 3)):
        return False
    issues = state.get("issues") or []
    by_block, unlocalized = localize_issues(ir, issues)
    return bool(by_block) and not unlocalized


def block_repair_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Regenerate each failing block (up to max_blocks_per_pass) and splice it
    back, then normalise the seams. Bumps `block_attempt` so the
    validate->Fix block->validate loop is bounded. Strictly best-effort: a
    block whose LLM call fails is left untouched (its errors persist and the
    next route falls back to a full regen)."""
    # Import the MODULE, not `from ...tools import build_circuit`: the tools
    # package __init__ re-exports the @tool-decorated `build_circuit` coroutine
    # (an SdkMcpTool), shadowing the submodule -- so the `from` form binds _bc
    # to the SdkMcpTool and `_bc._architect_block_call` raises AttributeError,
    # silently caught below -> every block was "skipped" and per-block repair
    # never actually ran. importlib returns the real module past the shadow.
    import importlib
    _bc = importlib.import_module("envil_agent.tools.build_circuit")

    ir = state.get("ir")
    issues = state.get("issues") or []
    if ir is None:
        return {}
    by_block, _unloc = localize_issues(ir, issues)
    if not by_block:
        return {}

    block_attempt = int(state.get("block_attempt", 0)) + 1
    prompt = str(state.get("prompt") or "")
    max_blocks = int(_bg_cfg("max_blocks_per_pass", 2))

    repaired, skipped = [], []
    for bname, berrs in list(by_block.items())[:max_blocks]:
        block = _block_by_name(ir, bname)
        if block is None:
            continue
        iface = block_interface(ir, block)
        comps = [(c.ref, c.lib_id, c.value) for c in iface["components"]]
        try:
            new_comps, new_nets = _bc._architect_block_call(
                bname, getattr(block, "block_type", "") or "",
                comps, berrs, iface["boundary_nets"], prompt)
        except Exception:                    # noqa: BLE001
            new_comps, new_nets = None, None
        if new_comps:
            try:
                splice_block(ir, bname, new_comps, new_nets or [])
                repaired.append(bname)
                # A successful redraw clears this block's incremental-skip
                # mark so validate stops re-raising INCREMENTAL_BLOCK_SKIPPED
                # for a block that is now wired.
                _sk = getattr(ir, "_incremental_skipped", None)
                if _sk and bname in _sk:
                    ir._incremental_skipped = [b for b in _sk if b != bname]
            except Exception:                # noqa: BLE001
                skipped.append(bname)
        else:
            skipped.append(bname)

    # Clean the splice seams (dedupe / merge / multinet resolve). Never raises.
    try:
        normalize_ir(ir)
    except Exception:                        # noqa: BLE001
        pass

    out: Dict[str, Any] = {"ir": ir, "block_attempt": block_attempt,
                           "blocks_repaired": repaired, "blocks_skipped": skipped}
    try:
        out["ir_json"] = json.dumps(ir.to_dict())
    except Exception:                        # noqa: BLE001
        pass
    return out
