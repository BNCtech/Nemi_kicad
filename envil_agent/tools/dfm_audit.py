"""Tool: dfm_audit — the deterministic gate for step 10 (3D + DFM).

Pure-Python, read-only. Catches the manufacturability problems DRC can't
see: every reference designator is present (annotated), silkscreen text is
legible (>= the board's own minimums), connectors are flagged for a required
human orientation/keying confirm (Appendix A — the highest-risk category),
and the board outline dimensions are reported for a final check.

Connector classification reuses ``footprint_audit``'s DYNAMIC refdes triage,
so "what is a connector" has one source of truth and works on any circuit.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool

_CFG_CACHE: Optional[Dict[str, Any]] = None


def _load_cfg() -> Dict[str, Any]:
    global _CFG_CACHE
    if _CFG_CACHE is not None:
        return _CFG_CACHE
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "dfm_audit.json"
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


def _silk_mins(pcb: Path) -> Tuple[float, float]:
    """(min_text_height, min_text_thickness) from the sibling .kicad_pro
    rules, else safe fab-floor defaults."""
    h, t = 0.8, 0.15
    pro = pcb.with_suffix(".kicad_pro")
    if pro.exists():
        try:
            rules = (((json.loads(pro.read_text(encoding="utf-8")).get("board")
                       or {}).get("design_settings") or {}).get("rules") or {})
            h = float(rules.get("min_text_height", h))
            t = float(rules.get("min_text_thickness", t))
        except (OSError, ValueError, TypeError):
            pass
    return h, t


def _font_of(node: list):
    """(height, thickness) of a text node's font, or (None, None)."""
    from ..layout.place_refine import _child
    eff = _child(node, "effects")
    font = _child(eff, "font") if eff else None
    if not font:
        return (None, None)
    size = _child(font, "size")
    thick = _child(font, "thickness")
    h = t = None
    if size and len(size) >= 3:
        try:
            h = min(float(size[1]), float(size[2]))
        except (TypeError, ValueError):
            pass
    if thick and len(thick) >= 2:
        try:
            t = float(thick[1])
        except (TypeError, ValueError):
            pass
    return (h, t)


@tool(
    name="dfm_audit",
    description=(
        "Validate 3D/DFM manufacturability (step 10) on a .kicad_pcb. Read-"
        "only, pure-Python. Checks: every footprint has a real refdes (not "
        "REF**), silkscreen text is legible (>= the board's min height/width), "
        "every connector/mechanical part is flagged for a human orientation & "
        "keying confirm (Appendix A), and the board outline dimensions are "
        "reported. Run before Gerber export.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_pcb"}   # or .kicad_sch / .kicad_pro / dir\n'
        "Verdict in words. Connector triage is dynamic (refdes), shared with "
        "footprint_audit. Policy in config/dfm_audit.json."
    ),
    input_schema={"path": str},
)
async def dfm_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "dfm_audit disabled in config"}],
                "is_error": True}

    from .placement_audit import _resolve_pcb, _board_outline_bbox
    pcb = _resolve_pcb(str(args.get("path", "")).strip())
    if pcb is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: no .kicad_pcb found for "
                                     f"{args.get('path', '')!r}"}],
                "is_error": True}

    import sexpdata
    from ..layout.place_refine import _head, _child, _children, _ref_of_footprint, _at_xyr
    from .footprint_audit import _mech_category, _load_cfg as _fa_cfg, _atom
    try:
        root = sexpdata.loads(pcb.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"content": [{"type": "text",
                             "text": f"ERROR: cannot parse {pcb.name}: {exc}"}],
                "is_error": True}

    vt = cfg.get("verdict", {}) or {}
    cap = int(cfg.get("max_examples_per_rule", 15))
    placeholders = {str(p).strip() for p in cfg.get("placeholder_refs", [])}
    silk_layers = set(cfg.get("silk_layers", ["F.SilkS", "B.SilkS"]))
    skip_pre = list((cfg.get("skip_ref_prefixes", {}) or {}).get("prefixes", ["#"]))
    min_h, min_t = _silk_mins(pcb)
    fa_cfg = _fa_cfg()
    exempt_attrs = {str(a) for a in cfg.get("bom_exempt_attrs",
                    ["exclude_from_bom", "board_only"])}

    findings: List[Dict[str, str]] = []

    def add(scope: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"scope": scope, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    n = 0
    fab_features = 0
    for node in root[1:] if isinstance(root, list) else []:
        if not isinstance(node, list) or _head(node) != "footprint":
            continue
        ref = (_ref_of_footprint(node) or "").strip()
        if ref and any(ref.startswith(p) for p in skip_pre):
            continue

        # silk legibility (rule 3) — applies to every footprint's silk text
        for c in node[1:]:
            if not isinstance(c, list):
                continue
            if _head(c) not in ("fp_text", "property"):
                continue
            lay = _child(c, "layer")
            if not (lay and len(lay) >= 2 and str(lay[1]).strip('"') in silk_layers):
                continue
            h, t = _font_of(c)
            if h is not None and h + 1e-6 < min_h:
                add(ref or "?", "silk_legible",
                    f"silk text {h:g}mm < min {min_h:g}mm")
            if t is not None and t + 1e-6 < min_t:
                add(ref or "?", "silk_legible",
                    f"silk stroke {t:g}mm < min {min_t:g}mm")

        # connector orientation human-confirm (rule 2) — runs on EVERY
        # footprint (refdes-driven), so a THT connector is caught even if it's
        # pos-file-excluded; fab features have no connector refdes so it no-ops.
        cat = _mech_category(ref, "", fa_cfg) if ref else ""
        if cat:
            add(ref, "connector_orientation",
                f"[{cat}] confirm orientation / pin-1 / keying vs datasheet "
                f"(human-confirm)")

        # DYNAMIC: a footprint with no NUMBERED pad (mounting hole, logo) — or
        # one KiCad marks exclude_from_pos_files/bom/board_only (fiducials +
        # mounting holes) — is a fab feature, not an assembled BOM part. Exempt
        # it from the refdes + 3D-model checks.
        numbered = any(_atom(p[1]).strip('"') not in ("", "?")
                       for p in _children(node, "pad") if len(p) >= 2)
        attr_node = _child(node, "attr")
        attr_flags = {_atom(a) for a in attr_node[1:]} if attr_node else set()
        if (not numbered) or (exempt_attrs & attr_flags):
            fab_features += 1
            continue

        n += 1
        x, y, _r = _at_xyr(node)

        # refdes present (rule 3)
        if ref in placeholders or "**" in ref or not ref:
            add(ref or "?", "refdes_present",
                f"placeholder/empty refdes at ({x:.1f}, {y:.1f}) — annotate")

        # 3D model presence (rule 1, info)
        if not any(isinstance(c, list) and _head(c) == "model" for c in node[1:]):
            add(ref or "?", "model_3d", "no 3D model — can't height/clash-check")

    # board outline (rule 5)
    outline = _board_outline_bbox(root)
    if outline is None:
        dims = "MISSING"
        add("board", "board_outline", "no Edge.Cuts outline — define board dimensions")
    else:
        w = outline[2] - outline[0]
        hgt = outline[3] - outline[1]
        dims = f"{w:.1f}x{hgt:.1f}mm"
        exp = (cfg.get("expected_board_mm", {}) or {}).get("value")
        tol = float((cfg.get("expected_board_mm", {}) or {}).get("tolerance_mm", 1.0))
        if isinstance(exp, list) and len(exp) == 2:
            if abs(w - float(exp[0])) > tol or abs(hgt - float(exp[1])) > tol:
                add("board", "board_outline",
                    f"outline {dims} != spec {exp[0]}x{exp[1]}mm (±{tol}mm)")

    # --- aggregate ---
    counts = {"error": 0, "warning": 0, "review": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w_, r, i = counts["error"], counts["warning"], counts["review"], counts["info"]

    if n == 0:
        verdict = vt.get("no_parts", "NO PARTS — nothing to check")
        ok = True
    elif e == 0 and w_ == 0 and r == 0:
        verdict = vt.get("clean", "DFM VERIFIED — {n} parts").format(n=n, dims=dims)
        ok = True
    elif e == 0 and w_ == 0 and r > 0:
        verdict = vt.get("review_only",
                         "DFM OK — {n} parts, {r} human-confirm; outline {dims}"
                         ).format(n=n, r=r, dims=dims)
        ok = True
    else:
        verdict = vt.get("issues",
                         "DFM NOT CLEAN — {e} error(s), {w} warning(s), "
                         "{r} human-confirm across {n} parts"
                         ).format(e=e, w=w_, r=r, n=n)
        ok = (e == 0)

    icon = {"error": "✗", "warning": "!", "review": "?", "info": "·"}
    lines = [f"# DFM audit — {pcb.name}",
             f"  {n} parts · {fab_features} fab feature(s) exempt · "
             f"outline {dims} · silk min {min_h:g}/{min_t:g}mm"]
    for sev in ["error", "warning", "review", "info"]:
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
        "fab_features": fab_features,
        "outline": dims,
        "verdict": verdict,
        "counts": counts,
        "findings": findings,
    }
