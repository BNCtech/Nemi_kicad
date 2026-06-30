"""Tool: ensure_ground_return — the GND pour-or-route fallback (Phase 3, Fix 1).

``route_pcb_simple`` skips GND-family nets on the assumption that the copper pour
(``auto_zones_pcb``) connects them. When the pour step no-ops — no zone, or a zone
that is never filled — ground is left ORPHANED. That is the NE555 failure: 5 of 9
unconnected pads were GND, skipped by the router AND never poured.

This tool runs AFTER the pour: it PROVES each skipped ground net is served by a
FILLED zone, and for any that is not, it routes that net (via
``route_pcb_simple``'s ``force_route_nets``) on the back layer so ground is never
silently left open. THT pads exist on every copper layer, so a back-layer track
connects them without a via.

Universal: the ground-net set is the config ``route_pcb_simple.skip_nets`` (no
hardcoded list here). Gated by ``ensure_ground_return.enabled``; never raises.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional

import sexpdata
from claude_agent_sdk import tool


def _head(n: Any) -> Optional[str]:
    if isinstance(n, list) and n:
        f = n[0]
        if isinstance(f, sexpdata.Symbol):
            return f.value()
        if isinstance(f, str):
            return f
    return None


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config()
    except Exception:
        return {}


def _skip_nets() -> set:
    rc = (_load_cfg().get("route_pcb_simple", {}) or {})
    names = rc.get("skip_nets",
                   ["GND", "AGND", "DGND", "PGND", "EGND", "SGND", "VSS", "0", ""])
    return set(str(s).upper() for s in names)


def _pads_by_netname(root: list) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for fp in root[1:]:
        if not (isinstance(fp, list) and _head(fp) == "footprint"):
            continue
        for ch in fp[1:]:
            if isinstance(ch, list) and _head(ch) == "pad":
                for sub in ch[2:]:
                    if isinstance(sub, list) and _head(sub) == "net" and len(sub) >= 3:
                        nm = str(sub[2])
                        out[nm] = out.get(nm, 0) + 1
    return out


def _filled_zone_nets(root: list) -> set:
    """Net names that have at least one FILLED copper zone (a zone with a
    ``filled_polygon``). An empty/unfilled zone does not count as served."""
    nets: set = set()
    for ch in root[1:]:
        if not (isinstance(ch, list) and _head(ch) == "zone"):
            continue
        nm: Optional[str] = None
        filled = False
        for s in ch[1:]:
            if isinstance(s, list) and _head(s) == "net_name" and len(s) >= 2:
                nm = str(s[1])
            if isinstance(s, list) and _head(s) == "filled_polygon":
                filled = True
        if nm and filled:
            nets.add(nm)
    return nets


@tool(
    name="ensure_ground_return",
    description=(
        "GND pour-or-route fallback. Run AFTER the copper pour (auto_zones_pcb): "
        "proves every skipped ground net is served by a FILLED zone, and routes "
        "any that is not (on the back layer) so ground is never left orphaned. "
        "Fixes the 'GND skipped by router AND not poured' failure. "
        'Args: {"pcb_path": "C:/.../board.kicad_pcb"}'
    ),
    input_schema={"pcb_path": str},
)
async def ensure_ground_return(args: dict[str, Any]) -> dict[str, Any]:
    cfg = (_load_cfg().get("ensure_ground_return", {}) or {})
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text", "text": "ensure_ground_return disabled"}],
                "ok": True}

    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists() or pcb_path.suffix.lower() != ".kicad_pcb":
        return {"content": [{"type": "text", "text": f"ERROR: bad .kicad_pcb: {pcb_path}"}],
                "is_error": True}

    try:
        root = sexpdata.loads(pcb_path.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        return {"content": [{"type": "text", "text": f"ERROR: parse: {exc}"}],
                "is_error": True}

    skip = _skip_nets()
    pads = _pads_by_netname(root)
    poured = _filled_zone_nets(root)

    # ground nets present with >=2 pads, normally skipped, NOT served by a pour
    need: List[str] = sorted(
        nm for nm, cnt in pads.items()
        if cnt >= 2 and nm.upper() in skip and nm not in poured)

    if not need:
        served = sorted(nm for nm in pads if nm.upper() in skip and nm in poured)
        return {"content": [{"type": "text",
                             "text": ("ensure_ground_return: all ground nets served "
                                      f"by a pour ({', '.join(served) or 'none present'}); "
                                      "nothing to route.")}],
                "ok": True, "routed": [], "poured": served}

    back_layer = str(cfg.get("route_layer", "B.Cu"))
    route_mod = importlib.import_module("envil_agent.tools.route_pcb_simple")
    r = await route_mod.route_pcb_simple.handler({
        "pcb_path": str(pcb_path),
        "force_route_nets": need,
        "skip_nets": [],
        "layer": back_layer,
        "max_pads_per_net": int(cfg.get("max_pads_per_net", 64)),
    })
    rtext = (r.get("content", [{}])[0].get("text", "") or "").strip().splitlines()
    head = rtext[0] if rtext else "(no output)"

    return {"content": [{"type": "text",
                         "text": (f"ensure_ground_return: pour did NOT serve "
                                  f"{', '.join(need)} -> routed on {back_layer}.\n"
                                  f"  router: {head}")}],
            "ok": not r.get("is_error", False),
            "routed": need, "layer": back_layer, "router_result": head}
