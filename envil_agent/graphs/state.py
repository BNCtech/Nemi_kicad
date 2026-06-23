"""TypedDict state schemas shared by every graph in this package.

Each graph reads + writes a subset of one TypedDict; LangGraph merges
node returns into the running state. Keeping the schemas in one file
makes the data contract reviewable in one screen.
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict


class RouterState(TypedDict, total=False):
    """State for router_graph (classify -> build).

    Superset of BuildState: every BuildState key is also valid here so
    the build subgraph reads/writes through the same dict. The router-
    only fields (`route`, `decision_log`) record WHICH path was chosen
    and which node logged what, so LangSmith shows the routing decision
    as a discrete step instead of vanishing into the system prompt.
    """
    # Inputs (carried through to the build subgraph)
    prompt: str
    out_path: str
    out_dir: str
    # Cursor-style user-chosen project name (gated project_naming.propose_in_preview).
    # The name the user accepted/typed in the build preview; when set AND out_path is
    # empty, render_node renames the circuit (folder + .kicad_sch + child sheets +
    # title block) to this instead of the architect's auto ir.name. Empty => unchanged.
    project_name: str
    force_hierarchy: bool
    force_single_sheet: bool          # set by decide_render_mode_node
    render_mode_decision: str         # human-readable reason for the picked mode

    # Routing decision — set by classify_intent_node
    route: str               # "build" | "fix" | "edit" | "analyze"
    route_reason: str        # one-line explanation of WHY this route

    # Split-node channel: normalize_node writes the IR-normalization
    # warnings here so the downstream validate_only_node can merge them
    # into `issues` (in the detailed graph the two steps are separate
    # nodes so each shows as its own LangSmith span).
    norm_warnings: List[Dict[str, Any]]

    # Audit trail — each node appends "{node}: {what it did}" so the
    # caller can see exactly which node fixed which error. Useful for
    # the chat "track which place use to fix the error" requirement.
    decision_log: List[str]

    # Architect -> validate loop
    attempt: int
    feedback: str
    ir_json: str
    ir: Any
    issues: List[Dict[str, Any]]

    # Per-block retry (#9) — counts Fix-block passes so the validate->Fix block
    # ->validate loop is bounded (build_graph.max_block_attempts) before falling
    # back to a full architect regen. Only router_graph_detailed uses it.
    block_attempt: int

    # Full-board handoff budget — when per-block repair AND the architect
    # retry budget are both exhausted with errors remaining, the build would
    # die at MAX_ATTEMPTS even though a fresh full regen (now armed with the
    # real available-pins feedback) could converge. Counts how many times the
    # attempt budget has been reset for a full handoff, bounded by
    # build_graph.max_full_handoffs so it can never loop. Absent => 0.
    full_handoffs: int

    # Best-attempt memory — the architect retry can DIVERGE (errors go
    # 5 -> 8 -> 27 across attempts); without this the loop returns the
    # LAST (worst) attempt. validate_node records the FEWEST-error IR seen
    # so the terminal handler can report/keep it instead of the last.
    best_error_count: int
    best_ir: Any
    best_ir_json: str
    best_issues: List[Dict[str, Any]]

    # Render output
    stats: Dict[str, Any]
    preview_svgs: List[str]

    # Terminal failure
    error: str


class BuildState(TypedDict, total=False):
    """State for build_graph (architect → validate → render → preview).

    The graph is invoked by tools/build_circuit.py with `prompt`,
    `out_path`, and `force_hierarchy` set; downstream nodes accumulate
    the rest. `error` is populated only on terminal failures so the
    caller can surface a clean message to the user.
    """
    # Inputs
    prompt: str
    out_path: str            # explicit overwrite target (overrides out_dir)
    out_dir: str             # fallback root when out_path not given
    project_name: str        # user-chosen circuit name (see RouterState); empty => auto
    force_hierarchy: bool
    force_single_sheet: bool
    render_mode_decision: str  # human-readable explanation of the auto-picked mode

    # Architect → validate loop
    attempt: int             # 1-indexed; bumped by architect_node
    feedback: str            # validation errors fed back to architect on retry
    ir_json: str             # raw text returned by Claude
    ir: Any                  # parsed TopologyIR
    issues: List[Dict[str, Any]]

    # Best-attempt memory (see RouterState) — fewest-error IR across retries
    # so a diverging architect (5 -> 8 -> 27 errors) doesn't make the loop
    # return the last/worst attempt.
    best_error_count: int
    best_ir: Any
    best_ir_json: str
    best_issues: List[Dict[str, Any]]

    # Render output
    stats: Dict[str, Any]    # whatever engine.render returned
    preview_svgs: List[str]

    # Self-heal loop (P1) — populated by graphs/nodes/self_heal.py.
    # erc_summary carries the final-state ERC count (and flags such as
    # `deterministic_fixed`, `escalating`, `exhausted`, `stuck`). When
    # the residual errors need IR-level work, self_heal sets
    # `fix_feedback` (also mirrored into `feedback` so architect_node
    # picks it up) and bumps `fix_attempt`. The router edge re-enters
    # the architect when fix_feedback is non-empty.
    erc_summary: Dict[str, Any]
    fix_attempt: int
    fix_feedback: str

    # Terminal failure
    error: str
