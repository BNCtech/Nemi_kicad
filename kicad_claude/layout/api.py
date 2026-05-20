"""Programmatic interface for the universal layout engine.

Thin wrapper around `pipeline.run_layout` for orchestrator / chat-agent
callers that need a typed result and a single function to import. Web
endpoints and the chat bridge should use this module — not pipeline.py
directly — so the public contract stays stable as internal stages evolve.

  from ai_backend.kicad_layout.api import run_layout, LayoutResult
  result = run_layout("circuit.kicad_sch", "./out/")
  if result.has_errors:
      ...

The api intentionally does NOT raise on quality errors; the caller
inspects `result.quality_totals` and decides. Hard failures (missing
input file, parser errors) still propagate as exceptions so the caller
sees the real cause."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from . import pipeline as _pipeline


@dataclass
class LayoutResult:
    """Stable public shape of a layout run.

    `quality_totals` is `{severity: count}` (e.g. {'error': 0, 'warning': 3});
    `has_errors` is the convenience predicate. `artifacts` carries every
    intermediate JSON path so the caller can re-render selectively without
    re-running upstream stages."""
    schematic:        str
    output_dir:       str
    artifacts:        Dict[str, str]
    emit:             Dict[str, Any]
    quality_totals:   Dict[str, int] = field(default_factory=dict)
    quality_issues:   list = field(default_factory=list)
    hierarchical:     Optional[Dict[str, Any]] = None

    @property
    def has_errors(self) -> bool:
        return self.quality_totals.get("error", 0) > 0

    @property
    def has_warnings(self) -> bool:
        return self.quality_totals.get("warning", 0) > 0


def run_layout(
    input_path,
    output_dir,
    *,
    skip_hierarchical: bool = False,
    skip_quality: bool = False,
    auto_display: bool = True,
    verbose: bool = False,
) -> LayoutResult:
    """Run the layout pipeline end-to-end. See pipeline.run_layout for the
    full kwarg list. Raises FileNotFoundError if `input_path` doesn't exist
    so callers fail fast on a missing source schematic.

    auto_display=True (default) POSTs the generated path to the chat
    server's /api/open_in_eeschema so eeschema auto-opens it. Set False
    when calling from a context that already owns the eeschema view (the
    chat server itself, for instance, broadcasts directly)."""
    if not Path(input_path).exists():
        raise FileNotFoundError(f"source schematic not found: {input_path}")
    raw = _pipeline.run_layout(
        input_path, output_dir,
        skip_hierarchical=skip_hierarchical,
        skip_quality=skip_quality,
        auto_display=auto_display,
        verbose=verbose,
    )
    quality = raw.get("quality") or {}
    return LayoutResult(
        schematic=raw["artifacts"]["schematic"],
        output_dir=raw["output_dir"],
        artifacts=raw["artifacts"],
        emit=raw["emit"],
        quality_totals=dict(quality.get("totals") or {}),
        quality_issues=list(quality.get("issues") or []),
        hierarchical=raw.get("hierarchical"),
    )
