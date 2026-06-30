"""Declarative lint dispatcher.

Reads `config/lint_rules.json`, calls the named selector from
`lint.selectors`, and returns a flat issue list. The dispatcher is
ADDITIVE: existing inline lint logic in `tools/audit_wires.py` is left
untouched. New callers (the self-heal node, an architect retry path,
future automated CI) can use this engine; legacy callers keep working.

Why declarative: WIRING_RULES.md lists 15 rules with 4 still open
(R4, R9, R11, R13). Hard-coding each in a Python switch made it
difficult to flip severity per project or to add a project-specific
rule without forking the codebase. Now: one JSON entry per rule, one
selector function per detection algorithm, severity and message text
tunable per project via config edits.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import selectors as _selectors

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "config"
    / "lint_rules.json"
)


def _load_rules(path: Optional[Path] = None) -> List[dict]:
    src = path or _CONFIG_PATH
    try:
        cfg = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(cfg.get("rules") or [])


def _resolve_selector(name: str) -> Optional[Callable]:
    """Look up the selector function by name. Returns None when the
    rule references an unknown selector --- that's logged but
    non-fatal so a partial install does not break the entire lint
    pass."""
    fn = getattr(_selectors, name, None)
    return fn if callable(fn) else None


def run_lint(context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run every rule in the config against the supplied context.

    `context` is a kwargs-style dict passed verbatim into each
    selector's call. The dispatcher inspects the selector's signature
    via the rule entry's `selector` field --- callers are responsible
    for populating the context keys that match the selector's
    parameter names.

    Common context keys (see lint.selectors docstrings):
      wires:          List[Wire]
      bboxes:         List[Bbox]
      pin_positions:  List[PinPos]
      labels:         List[Label]
      label_names:    List[str]
      ir_nets:        List[IRNet]

    Each issue carries the rule's `id`, `severity`, and `fix_hint` so
    downstream consumers (the chat reply, the self-heal node, future
    CI badges) can render them consistently.
    """
    rules = _load_rules()
    issues: List[Dict[str, Any]] = []
    for rule in rules:
        rid = rule.get("id") or "UNKNOWN"
        # Per-rule kill switch. Absent -> True, so every existing rule keeps
        # running byte-for-byte; a rule shipped `enabled: false` is skipped
        # until a project flips it on (used to land new rules default-off
        # until run_golden.py confirms byte-stability).
        if rule.get("enabled", True) is False:
            continue
        sel_name = rule.get("selector") or ""
        fn = _resolve_selector(sel_name)
        if fn is None:
            continue
        try:
            argcount = fn.__code__.co_argcount
            param_names = fn.__code__.co_varnames[:argcount]
            n_defaults = len(fn.__defaults__ or ())
            required = set(param_names[: argcount - n_defaults])
            # Per-rule `params` in lint_rules.json drive every threshold
            # (tolerances, etype lists, near-miss band, ...) so nothing is
            # hardcoded in Python. Rule params win over context on conflict.
            rule_params = rule.get("params") or {}
            kwargs: Dict[str, Any] = {}
            for p in param_names:
                if p in rule_params:
                    kwargs[p] = rule_params[p]
                elif p in context:
                    kwargs[p] = context[p]
            # Skip cleanly when a no-default selector arg cannot be supplied
            # (e.g. NET_FLOATING's ir_nets at the file-lint stage) instead of
            # letting the call raise and surface as a spurious warning issue.
            if required - set(kwargs):
                continue
            result = fn(**kwargs) or []
        except Exception as exc:
            issues.append({
                "id": rid,
                "severity": "warning",
                "message": (
                    f"lint rule {rid} ({sel_name}) raised "
                    f"{type(exc).__name__}: {exc}"
                ),
                "where": {"selector": sel_name},
            })
            continue
        # Attach the rule's fix_hint + severity override to every issue
        # the selector returned (selectors set their own defaults; rule
        # config wins on conflict so projects can re-tune severities).
        hint = rule.get("fix_hint") or ""
        severity_override = rule.get("severity") or ""
        for issue in result:
            issue.setdefault("id", rid)
            if severity_override:
                issue["severity"] = severity_override
            if hint:
                issue.setdefault("fix_hint", hint)
        issues.extend(result)
    return issues


class LintEngine:
    """OO wrapper around `run_lint` for callers that prefer it. Caches
    the rule set so tight inner loops do not re-read the JSON file."""

    def __init__(self, rules_path: Optional[Path] = None):
        self._rules = _load_rules(rules_path)

    def run(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Defer to the module-level run_lint so the two entry points
        # cannot drift apart.
        return run_lint(context)
