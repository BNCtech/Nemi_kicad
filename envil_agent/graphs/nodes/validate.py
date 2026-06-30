"""Validate node — runs normalize + validate, returns issues + feedback string.

When errors exist, builds a tight feedback string the architect will see
on its retry. When the prior architect node failed to even parse JSON,
the IR is None and we mark the run as needing another architect attempt
without re-running the validator.
"""
from __future__ import annotations

from typing import Any, Dict

from ...intent.normalize import normalize_ir
from ...intent.validate import (
    dedupe_issues,
    has_errors,
    render_available_pins_section,
    validate_ir,
)


def _remember_best(state: Dict[str, Any], ir: Any,
                   issues: list) -> Dict[str, Any]:
    """Return best_* fields when THIS attempt has fewer errors than the
    best seen so far. Lets the retry loop keep the fewest-error IR instead
    of the last one when the architect diverges (5 -> 8 -> 27 errors)."""
    err_count = sum(1 for i in issues if i.get("severity") == "error")
    prev = state.get("best_error_count")
    if prev is None or err_count < prev:
        return {
            "best_error_count": err_count,
            "best_ir": ir,
            "best_ir_json": state.get("ir_json", ""),
            "best_issues": list(issues),
        }
    return {}


def validate_node(state: Dict[str, Any]) -> Dict[str, Any]:
    ir = state.get("ir")
    if ir is None:
        # architect couldn't produce parseable JSON; surface as an issue
        # so the routing edge can either retry or give up consistently.
        return {
            "issues": [{
                "code": "IR_PARSE_FAILED",
                "severity": "error",
                "where": "architect",
                "text": "architect did not return parseable JSON",
            }],
        }

    norm_warnings = normalize_ir(ir)
    # Pass the user's prompt so block-justification checks
    # (BLOCK_NOT_JUSTIFIED) can verify hallucinated blocks like RF
    # against the actual request text.
    prompt = str(state.get("prompt") or "")
    issues = dedupe_issues(validate_ir(ir, prompt=prompt)) + norm_warnings

    feedback = ""
    if has_errors(issues):
        feedback = "\n".join(
            f"  [{i['severity']}] {i['code']} @ {i['where']}: {i['text']}"
            for i in issues if i["severity"] == "error"
        )
        # Gated (build_graph.feedback_available_pins): append the FULL real
        # pin list for any mis-pinned part so the full-architect retry sees
        # the actual pins. Returns "" when off -> feedback byte-identical.
        _avail = render_available_pins_section(
            [i for i in issues if i.get("severity") == "error"])
        if _avail:
            feedback = f"{feedback}\n\n{_avail}"
    out = {"issues": issues, "feedback": feedback}
    out.update(_remember_best(state, ir, issues))
    return out


# ---------------------------------------------------------------------------
# Split variants for the DETAILED graph (router_graph_detailed).
#
# validate_node above does normalize + validate in one step. The detailed
# graph promotes each to its own node so the LangSmith trace shows them as
# separate spans. Behaviour is identical to validate_node — the same
# normalize_ir + validate_ir run, just across two nodes that hand off via
# the `norm_warnings` state channel. The combined validate_node stays
# untouched so the original build_graph / router_graph keep working.
# ---------------------------------------------------------------------------

def normalize_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """Run normalize_ir and stash its warnings for validate_only_node.

    No-op when the architect produced no parseable IR — validate_only_node
    emits the IR_PARSE_FAILED issue so the routing edge stays consistent.
    """
    ir = state.get("ir")
    if ir is None:
        return {"norm_warnings": []}
    norm_warnings = normalize_ir(ir)
    return {"ir": ir, "norm_warnings": norm_warnings}


def validate_only_node(state: Dict[str, Any]) -> Dict[str, Any]:
    """validate_ir only — assumes normalize_node already ran. Merges the
    normalize warnings carried on `norm_warnings` into the issue list."""
    ir = state.get("ir")
    if ir is None:
        return {
            "issues": [{
                "code": "IR_PARSE_FAILED",
                "severity": "error",
                "where": "architect",
                "text": "architect did not return parseable JSON",
            }],
        }
    prompt = str(state.get("prompt") or "")
    norm_warnings = state.get("norm_warnings") or []
    # Defensive: coerce any stray non-dict issue (a bare string from a producer)
    # into a proper warning dict, so the `i["severity"]` iteration below can never
    # crash the whole build on a type mismatch (TypeError: string indices).
    def _as_issue(x: Any) -> Dict[str, Any]:
        if isinstance(x, dict):
            return x
        return {"code": "NORMALIZE", "severity": "warning",
                "where": "normalize", "text": str(x)}
    issues = [_as_issue(i) for i in
              (dedupe_issues(validate_ir(ir, prompt=prompt)) + list(norm_warnings))]

    feedback = ""
    if has_errors(issues):
        feedback = "\n".join(
            f"  [{i['severity']}] {i['code']} @ {i['where']}: {i['text']}"
            for i in issues if i["severity"] == "error"
        )
        # Gated (build_graph.feedback_available_pins): append the FULL real
        # pin list for any mis-pinned part so the full-architect retry sees
        # the actual pins. Returns "" when off -> feedback byte-identical.
        _avail = render_available_pins_section(
            [i for i in issues if i.get("severity") == "error"])
        if _avail:
            feedback = f"{feedback}\n\n{_avail}"
    out = {"issues": issues, "feedback": feedback}
    out.update(_remember_best(state, ir, issues))
    return out
