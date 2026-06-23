"""Declarative lint engine.

Public API:
  selectors.<fn>          --- pure detection helpers
                              (see lint/selectors.py for the list)
  engine.run_lint(ctx)    --- dispatcher reading config/lint_rules.json
  engine.LintEngine       --- OO wrapper around run_lint

Additive: this module does NOT replace `tools/audit_wires.py`. The
legacy inline implementation in audit_wires keeps working; new
callers (self_heal node, future CI) use the engine here. A follow-up
can delete the inline implementation once parity is verified.
"""
from .engine import LintEngine, run_lint  # noqa: F401  (re-exports)
from . import selectors  # noqa: F401
