"""Tool: placement_audit — the deterministic gate for step 6 (Placement).

After parts are placed (auto_place_pcb + place_refine), this proves the
placement is sound before routing: no courtyard overlaps, everything inside
the board outline, and decoupling caps close to the ICs they serve. Read-
only — it reuses ``layout/place_refine.py``'s courtyard/bbox/overlap math so
its verdict agrees with the de-collision pass that produced the board, but
it never moves a part (place_refine does that).

Checks (all gated in ``config/placement_audit.json``):
  courtyard_overlap    — no two footprint courtyards overlap.      [rule 8]
  edge_clearance       — every part inside Edge.Cuts − copper-to-edge. [rule 8]
  decoupling_proximity — a cap on a power net sits within max_mm of the
                         nearest IC on that net.                    [rule 3]
  stacked_parts        — no two footprints share the same (x,y).

Classification is DYNAMIC — refdes prefix + the IR's real ``is_power`` net
roles — so it works on any circuit (feedback_dynamic_derive_not_list).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "placement_audit.json"
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


def _resolve_pcb(raw: str) -> Optional[Path]:
    p = Path(raw).expanduser()
    if p.is_dir():
        cands = sorted(p.glob("*.kicad_pcb"))
        return cands[0] if cands else None
    if p.suffix.lower() == ".kicad_pcb":
        return p if p.exists() else None
    if p.suffix.lower() in (".kicad_sch", ".kicad_pro", ".kicad_prl"):
        sib = p.with_suffix(".kicad_pcb")
        if sib.exists():
            return sib
        cands = sorted(p.parent.glob("*.kicad_pcb"))
        return cands[0] if cands else None
    return p if p.exists() else None


def _pad_net_name(pad: list) -> str:
    from ..layout.place_refine import _child
    n = _child(pad, "net")
    return str(n[2]).strip('"') if n and len(n) >= 3 else ""


def _board_outline_bbox(root: list):
    """Axis-aligned bbox of all Edge.Cuts graphics, or None if there's no
    board outline at all."""
    from ..layout.place_refine import _head, _child, _children
    xs: List[float] = []
    ys: List[float] = []
    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list):
            continue
        if not str(_head(node) or "").startswith("gr_"):
            continue
        layer = _child(node, "layer")
        if not (layer and len(layer) >= 2 and "Edge.Cuts" in str(layer[1])):
            continue
        for tag in ("start", "end", "center", "mid"):
            c = _child(node, tag)
            if c and len(c) >= 3:
                try:
                    xs.append(float(c[1])); ys.append(float(c[2]))
                except (TypeError, ValueError):
                    pass
        ptsnode = _child(node, "pts")
        if ptsnode:
            for xy in _children(ptsnode, "xy"):
                if len(xy) >= 3:
                    try:
                        xs.append(float(xy[1])); ys.append(float(xy[2]))
                    except (TypeError, ValueError):
                        pass
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _edge_clearance_mm(pcb: Path, default: float = 0.3) -> float:
    """Board copper-to-edge clearance from the sibling .kicad_pro rules
    (what DRC will enforce), else a safe default."""
    pro = pcb.with_suffix(".kicad_pro")
    if pro.exists():
        try:
            data = json.loads(pro.read_text(encoding="utf-8"))
            v = (((data.get("board") or {}).get("design_settings") or {})
                 .get("rules") or {}).get("min_copper_edge_clearance")
            if v is not None:
                return float(v)
        except (OSError, ValueError, TypeError):
            pass
    return default


@tool(
    name="placement_audit",
    description=(
        "Validate component placement (step 6) on a .kicad_pcb before routing. "
        "Read-only. Checks: no courtyard overlaps, every part inside the "
        "Edge.Cuts outline minus copper-to-edge clearance, decoupling caps "
        "within max distance of the IC they serve (dynamic — from the IR's "
        "power nets), and no two parts stacked at the same point. Run it after "
        "auto_place_pcb / place_refine, before routing.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_pcb"}   # or .kicad_sch / .kicad_pro / dir\n'
        "Reuses place_refine geometry so it agrees with de-collision. Verdict "
        "in words. Policy in config/placement_audit.json. It reports — "
        "place_refine / auto_place_pcb fix."
    ),
    input_schema={"path": str},
)
async def placement_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "placement_audit disabled in config"}],
                "is_error": True}

    pcb = _resolve_pcb(str(args.get("path", "")).strip())
    if pcb is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: no .kicad_pcb found for "
                                     f"{args.get('path', '')!r}"}],
                "is_error": True}

    import sexpdata
    from ..layout.place_refine import (
        _head, _children, _ref_of_footprint, _at_xyr, _local_bbox,
        _board_bbox, _overlap_amounts, _pad_board_xy, _pad_net_id, _prefix_of_ref,
    )
    try:
        root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"content": [{"type": "text",
                             "text": f"ERROR: cannot parse {pcb.name}: {exc}"}],
                "is_error": True}

    vt = cfg.get("verdict", {}) or {}
    use_court = bool(cfg.get("use_courtyard", True))
    pad_margin = float(cfg.get("pad_bbox_margin_mm", 0.25))
    clearance = float(cfg.get("clearance_mm", 0.0))
    eps = 1e-6
    skip_prefixes = list((cfg.get("skip_ref_prefixes", {}) or {})
                         .get("prefixes", ["#", "REF"]))
    cap = int(cfg.get("max_examples_per_rule", 15))

    # --- enumerate footprints ---
    parts: List[Dict[str, Any]] = []
    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list) or _head(node) != "footprint":
            continue
        ref = _ref_of_footprint(node) or ""
        if not ref or any(ref.startswith(p) for p in skip_prefixes):
            continue
        x, y, rot = _at_xyr(node)
        local = _local_bbox(node, use_court, pad_margin)
        if local is None:
            continue
        bbox = _board_bbox(local, x, y, rot)
        pads = []
        for pad in _children(node, "pad"):
            nid = _pad_net_id(pad)
            if nid <= 0:
                continue
            bx, by = _pad_board_xy(x, y, rot, pad)
            pads.append((nid, _pad_net_name(pad), bx, by))
        parts.append({"ref": ref, "prefix": _prefix_of_ref(ref),
                      "x": x, "y": y, "rot": rot, "bbox": bbox, "pads": pads})

    n = len(parts)
    if n == 0:
        verdict = vt.get("no_parts", "NO PARTS — nothing to check")
        return {"content": [{"type": "text",
                             "text": f"# Placement audit — {pcb.name}\n\n"
                                     f"**{verdict}**"}],
                "ok": True, "verdict": verdict}

    findings: List[Dict[str, str]] = []

    def add(scope: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"scope": scope, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    # --- courtyard_overlap ---
    for i in range(n):
        for j in range(i + 1, n):
            ox, oy = _overlap_amounts(parts[i]["bbox"], parts[j]["bbox"], clearance)
            if ox > eps and oy > eps:
                add(f"{parts[i]['ref']}<->{parts[j]['ref']}", "courtyard_overlap",
                    f"courtyards overlap by {min(ox, oy):.2f} mm")

    # --- edge_clearance ---
    outline = _board_outline_bbox(root)
    if outline is None:
        add("board", "edge_clearance",
            "no Edge.Cuts outline — define the board outline (step 6 rule 1)")
    else:
        ecl = _edge_clearance_mm(pcb)
        ox0, oy0, ox1, oy1 = outline
        for p in parts:
            bx0, by0, bx1, by1 = p["bbox"]
            if (bx0 < ox0 + ecl - eps or by0 < oy0 + ecl - eps
                    or bx1 > ox1 - ecl + eps or by1 > oy1 - ecl + eps):
                add(p["ref"], "edge_clearance",
                    f"extends past board edge or within {ecl:g} mm of it")

    # --- stacked_parts ---
    slots: Dict[Tuple[float, float], List[str]] = {}
    for p in parts:
        slots.setdefault((round(p["x"], 2), round(p["y"], 2)), []).append(p["ref"])
    for (sx, sy), refs in slots.items():
        if len(refs) > 1:
            add(",".join(refs), "stacked_parts",
                f"{len(refs)} parts stacked at ({sx}, {sy}) — unplaced")

    # --- decoupling_proximity (dynamic: IR power nets) ---
    decoup_checked = False
    dcfg = cfg.get("decoupling", {}) or {}
    if _rule_on(cfg, "decoupling_proximity"):
        try:
            from .board_setup_audit import _power_nets_from_ir
            power_set = _power_nets_from_ir(pcb)
        except Exception:                                   # noqa: BLE001
            power_set = None
        if power_set:
            decoup_checked = True
            cap_pre = set(dcfg.get("cap_prefixes", ["C"]))
            ic_pre = set(dcfg.get("ic_prefixes", ["U", "IC"]))
            max_mm = float(dcfg.get("max_mm", 5.0))
            ics = [p for p in parts if p["prefix"] in ic_pre]
            for c in parts:
                if c["prefix"] not in cap_pre:
                    continue
                # this cap's pads that sit on a power net
                for (nid, nn, cbx, cby) in c["pads"]:
                    if nn not in power_set:
                        continue
                    # nearest IC pad on the SAME power net
                    best = None
                    for ic in ics:
                        for (inid, inn, ibx, iby) in ic["pads"]:
                            if inn != nn:
                                continue
                            d = math.hypot(cbx - ibx, cby - iby)
                            if best is None or d < best[0]:
                                best = (d, ic["ref"])
                    if best and best[0] > max_mm + eps:
                        add(c["ref"], "decoupling_proximity",
                            f"decouples {nn} but is {best[0]:.1f} mm from "
                            f"{best[1]} (max {max_mm:g})")
                    break                    # one power connection is enough

    # --- aggregate ---
    counts = {"error": 0, "warning": 0, "info": 0, "review": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w = counts["error"], counts["warning"]

    if e == 0 and w == 0:
        verdict = vt.get("clean", "PLACEMENT VERIFIED — {n} parts").format(n=n)
        ok = True
    else:
        verdict = vt.get("issues",
                         "PLACEMENT NOT CLEAN — {e} error(s), {w} warning(s) "
                         "across {n} parts").format(e=e, w=w, n=n)
        ok = (e == 0)

    icon = {"error": "✗", "warning": "!", "info": "·", "review": "?"}
    lines = [f"# Placement audit — {pcb.name}",
             f"  {n} parts · outline {'yes' if outline else 'MISSING'} · "
             f"decoupling {'IR-derived' if decoup_checked else 'skipped (no IR sidecar)'}"]
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
        "parts": n,
        "verdict": verdict,
        "counts": counts,
        "has_outline": outline is not None,
        "decoupling_checked": decoup_checked,
        "findings": findings,
    }
