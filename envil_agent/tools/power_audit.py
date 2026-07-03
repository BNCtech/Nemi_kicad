"""Tool: power_audit — the deterministic gate for step 8 (Power / Ground).

Proves the power delivery is physically sound before DRC. Read-only. It
COMPOSES the existing ``power_integrity_pcb`` tool for the Vdrop + IPC-2152
width work (rules 2/3) and adds the checks that tool doesn't do:

  power_width / voltage_drop — from power_integrity_pcb.             [rules 2/3]
  ground_plane   — a copper zone is poured on a power net.          [rule 1]
  zone_filled    — every defined zone actually carries fill.        [rule 4]
  via_current    — a power net's current per via stays under limit. [rule 5]

Power nets are the IR's real ``is_power`` roles (dynamic), so it works on any
circuit. It never fills a zone or widens a track — auto_zones_pcb /
set_track_widths_pcb do that.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "power_audit.json"
        _CFG_CACHE = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _CFG_CACHE = {}
    return _CFG_CACHE


def _rule(cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    r = (cfg.get("rules", {}) or {}).get(name, {})
    return r if isinstance(r, dict) else {}


def _rule_on(cfg: Dict[str, Any], name: str) -> bool:
    return bool(_rule(cfg, name).get("enabled", True))


def _rule_sev(cfg: Dict[str, Any], name: str, default: str = "warning") -> str:
    return str(_rule(cfg, name).get("severity", default))


def _parse_zones(root: list) -> List[Dict[str, Any]]:
    """Every (zone ...): its net id/name and whether it carries fill."""
    from ..layout.place_refine import _head, _child
    zones: List[Dict[str, Any]] = []
    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list) or _head(node) != "zone":
            continue
        netc = _child(node, "net")
        try:
            nid = int(netc[1]) if netc and len(netc) >= 2 else 0
        except (TypeError, ValueError):
            nid = 0
        nnc = _child(node, "net_name")
        nname = str(nnc[1]).strip('"') if nnc and len(nnc) >= 2 else ""
        filled = any(isinstance(c, list) and _head(c) == "filled_polygon"
                     for c in node[1:])
        zones.append({"net": nid, "name": nname, "filled": filled})
    return zones


@tool(
    name="power_audit",
    description=(
        "Validate power/ground delivery (step 8) on a .kicad_pcb before DRC. "
        "Read-only. Composes power_integrity_pcb (Vdrop + IPC-2152 width per "
        "power net) and adds: a ground/power plane is actually poured, every "
        "zone is filled, and no power net's current-per-via exceeds the limit. "
        "Power nets come from the IR's is_power roles, so it works on any "
        "circuit. Run after zones/routing, before drc_check.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_pcb"}   # or .kicad_sch / .kicad_pro / dir\n'
        "Verdict in words. Policy in config/power_audit.json. It reports — "
        "auto_zones_pcb / set_track_widths_pcb fix."
    ),
    input_schema={"path": str},
)
async def power_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "power_audit disabled in config"}],
                "is_error": True}

    from .placement_audit import _resolve_pcb
    pcb = _resolve_pcb(str(args.get("path", "")).strip())
    if pcb is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: no .kicad_pcb found for "
                                     f"{args.get('path', '')!r}"}],
                "is_error": True}

    vt = cfg.get("verdict", {}) or {}
    cap = int(cfg.get("max_examples_per_rule", 15))
    per_via = float(cfg.get("per_via_current_a", 1.0))

    # --- compose power_integrity_pcb for Vdrop + width (rules 2/3) ---
    from .power_integrity_pcb import power_integrity_pcb as _pi_tool
    pi = await _pi_tool.handler({"pcb_path": str(pcb)})
    if pi.get("is_error"):
        detail = str(pi.get("content", [{}])[0].get("text", ""))[:160]
        return {"content": [{"type": "text",
                             "text": f"# Power audit — {pcb.name}\n  power "
                                     f"analysis unavailable: {detail}"}],
                "ok": None, "is_error": True}
    rows = list(pi.get("nets") or [])

    # --- zones + power nets (dynamic) ---
    import sexpdata
    try:
        root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        root = []
    zones = _parse_zones(root)
    try:
        from .board_setup_audit import _power_nets_from_ir
        power_set = _power_nets_from_ir(pcb)
    except Exception:                                       # noqa: BLE001
        power_set = None

    findings: List[Dict[str, str]] = []

    def add(scope: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"scope": scope, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    # --- power_width + voltage_drop (translate power_integrity warnings) ---
    n_unrouted_power = 0
    for r in rows:
        if r.get("verdict") == "UNROUTED":
            n_unrouted_power += 1
            continue
        assumed = r.get("current_source") == "class_default"
        for wtext in (r.get("warnings") or []):
            wl = str(wtext).lower()
            if "ipc" in wl or "width" in wl:
                # No-hardcode: only hard-error a width shortfall when the
                # current is DERIVED from the design. If it's just the class
                # default (an assumption), warn — don't fail the board on it.
                if assumed:
                    add(r["net"], "power_width_assumed",
                        f"{r.get('current_a')}A (assumed — annotate real "
                        f"current): {wtext}")
                else:
                    add(r["net"], "power_width",
                        f"{r.get('current_a')}A: {wtext}")
            else:
                add(r["net"], "voltage_drop",
                    f"{r.get('current_a')}A: {wtext}")
        # via_current (rule 5)
        vias = int(r.get("vias") or 0)
        cur = float(r.get("current_a") or 0.0)
        if vias > 0 and per_via > 0 and cur / vias > per_via + 1e-9:
            add(r["net"], "via_current",
                f"{cur:g}A through {vias} via(s) = {cur / vias:.2f}A/via "
                f"> {per_via:g}A/via — add vias")

    # --- ground_plane (rule 1) — only required when there ARE power nets ---
    has_power = len(rows) > 0 or bool(power_set)
    if _rule_on(cfg, "ground_plane") and has_power:
        if power_set is not None:
            plane_zones = [z for z in zones if z["name"] in power_set]
        else:
            plane_zones = [z for z in zones if z["net"] > 0 or z["name"]]
        if not plane_zones:
            if not zones:
                add("board", "ground_plane",
                    "no copper zone poured — add a ground/power plane")
            else:
                add("board", "ground_plane",
                    "no zone assigned to a power net — the pour isn't a "
                    "ground/power plane")

    # --- zone_filled (rule 4) ---
    for z in zones:
        if not z["filled"]:
            add(z["name"] or f"net{z['net']}", "zone_filled",
                f"zone on '{z['name'] or z['net']}' is not filled (run zone fill)")

    # --- aggregate ---
    counts = {"error": 0, "warning": 0, "info": 0, "review": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w = counts["error"], counts["warning"]
    n = len(rows)

    if e > 0 or w > 0:                       # findings take precedence
        verdict = vt.get("issues",
                         "POWER NOT CLEAN — {e} error(s), {w} warning(s) "
                         "across {n} power nets").format(e=e, w=w, n=n)
        ok = (e == 0)
    elif n == 0 and not zones:
        verdict = vt.get("no_power", "NO POWER NETS — nothing to check")
        ok = True
    else:
        verdict = vt.get("clean", "POWER OK — {n} power nets").format(n=n)
        ok = True

    icon = {"error": "✗", "warning": "!", "info": "·", "review": "?"}
    lines = [f"# Power audit — {pcb.name}",
             f"  {n} power nets · {len(zones)} zone(s) "
             f"({sum(1 for z in zones if z['filled'])} filled) · "
             f"{n_unrouted_power} power net(s) unrouted"]
    for sev in ["error", "warning", "info"]:
        group = [f for f in findings if f["severity"] == sev]
        if not group:
            continue
        lines.append("")
        lines.append(f"  {sev.upper()} ({len(group)})")
        for f in group[:cap]:
            lines.append(f"    {icon[sev]} {f['scope']}: {f['msg']}")
        if len(group) > cap:
            lines.append(f"    … +{len(group) - cap} more")
    lines.append("")
    lines.append(f"**{verdict}**")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": ok,
        "pcb": str(pcb).replace("\\", "/"),
        "power_nets": n,
        "zones": len(zones),
        "zones_filled": sum(1 for z in zones if z["filled"]),
        "unrouted_power": n_unrouted_power,
        "verdict": verdict,
        "counts": counts,
        "findings": findings,
    }
