"""Shared PCB helpers — s-expr primitives, tool dispatch, score banding.

Every PCB tool needs the same handful of s-expr accessors (``head`` / ``child``
/ ``children`` / ``at`` / ``prop``), and the score-driven tools need the same
"import a sibling tool module and await its handler" dispatch and the same
score→verdict banding. Historically each module re-defined these privately
(``_head`` appears in ~20 files). New code routes through this ONE module so the
logic exists once.

Deliberately dependency-light (only ``sexpdata`` + stdlib) so any tool can import
it without pulling in heavy siblings. Existing modules keep their own copies for
now (non-breaking); they can migrate here incrementally.
"""
from __future__ import annotations

import importlib
import math
from typing import Any, Dict, List, Optional, Tuple

import sexpdata


# --------------------------------------------------------------------------- #
# s-expr accessors
# --------------------------------------------------------------------------- #

def head(node: Any) -> Optional[str]:
    """The tag of an s-expr node, e.g. ``head(["footprint", ...]) == "footprint"``."""
    if isinstance(node, list) and node:
        h = node[0]
        if isinstance(h, sexpdata.Symbol):
            return h.value()
        if isinstance(h, str):
            return h
    return None


def child(node: list, name: str) -> Optional[list]:
    """First direct child sub-list whose tag is ``name``."""
    for c in node[1:] if isinstance(node, list) else []:
        if isinstance(c, list) and head(c) == name:
            return c
    return None


def children(node: list, name: str) -> List[list]:
    """All direct child sub-lists whose tag is ``name``."""
    return [c for c in (node[1:] if isinstance(node, list) else [])
            if isinstance(c, list) and head(c) == name]


def atom(node: Optional[list], i: int) -> Optional[str]:
    """The i-th atom of a node as a quote-stripped string (None if absent)."""
    if node and len(node) > i:
        return str(node[i]).strip('"')
    return None


def at(node: list) -> Tuple[float, float, float]:
    """``(at x y [rot])`` of a node as (x, y, rot); (0,0,0) if missing."""
    a = child(node, "at")
    if a and len(a) >= 3:
        try:
            rot = float(a[3]) if len(a) >= 4 else 0.0
            return float(a[1]), float(a[2]), rot
        except (TypeError, ValueError):
            return 0.0, 0.0, 0.0
    return 0.0, 0.0, 0.0


def prop(fp: list, key: str) -> str:
    """Value of a footprint ``(property "Key" "Value")`` ('' if absent)."""
    for c in children(fp, "property"):
        if atom(c, 1) == key:
            return atom(c, 2) or ""
    return ""


def net_table(root: list) -> Dict[int, str]:
    """``{idx: name}`` from a board's top-level ``(net idx "name")`` entries."""
    out: Dict[int, str] = {}
    for c in root[1:] if isinstance(root, list) else []:
        if head(c) == "net" and len(c) >= 3:
            try:
                out[int(c[1])] = str(c[2]).strip('"')
            except (TypeError, ValueError):
                pass
    return out


def rot(x: float, y: float, deg: float) -> Tuple[float, float]:
    """Rotate a local point by ``deg`` degrees about the origin."""
    if not deg:
        return x, y
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x * c - y * s, x * s + y * c


# --------------------------------------------------------------------------- #
# Sibling-tool dispatch (used by every score-driven / orchestrating tool)
# --------------------------------------------------------------------------- #

async def dispatch_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Import ``envil_agent.tools.<name>`` and await its ``@tool`` handler.

    Returns the handler's result dict. A missing module or a raised exception
    is normalised to ``{"is_error": True, "content": [...]}`` so callers can
    treat "tool failed to run" uniformly without their own try/except.
    """
    try:
        mod = importlib.import_module(f"envil_agent.tools.{name}")
        fn = getattr(mod, name)
        return await fn.handler(args)
    except Exception as exc:                                # noqa: BLE001
        return {"is_error": True,
                "content": [{"type": "text",
                             "text": f"{name} failed: {type(exc).__name__}: {exc}"}]}


# --------------------------------------------------------------------------- #
# Score → verdict banding (shared by pcb_quality + pcb_improve)
# --------------------------------------------------------------------------- #

def band_for(score: float, bands: List[Tuple[float, str]]) -> str:
    """First band whose threshold ``score`` meets, scanning highest-first."""
    for thr, label in bands:
        if score >= thr:
            return label
    return bands[-1][1] if bands else "?"
