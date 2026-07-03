"""Tool: board_setup_audit — the deterministic gate for step 5 (Board Setup).

Board Setup has a generator already (`set_design_rules` writes fab-profile
minimums into the `.kicad_pro` and emits `.kicad_dru`). This is the missing
*verify* half: it parses the `.kicad_pro` (design rules + via presets + net
classes) and the sibling `.kicad_pcb`, and proves the envelope is legal —

  rules_vs_fab       — every design min_* >= fab floor (× rule-2 margin on
                       the geometric keys).                    [rules 1/2]
  via_annular        — (via_dia − drill)/2 >= fab annular, drill >= fab min,
                       optional aspect ratio.                  [rule 5]
  class_consistency  — each net class's width/clearance/via >= the board's
                       own min_* rules, and its via annular is sound. [rule 5]
  netclass_coverage  — every power-like net is assigned a class, not left on
                       Default.                                [rules 6/8]

Read-only: it never writes rules (that's set_design_rules' job) — keeping
"generate the setup" separate from "verify it's legal". All arithmetic is
kept out of the model's hands. Verdict is in words.
"""
from __future__ import annotations

import fnmatch
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
        p = Path(__file__).resolve().parent.parent / "config" / "board_setup_audit.json"
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


def _dig(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _num(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _power_nets_from_ir(pro_path: Path) -> Optional[set]:
    """DYNAMIC power-net detection: the set of net names the architect marked
    ``is_power`` in the ``.envil-ir.json`` sidecar — the circuit's real net
    roles, not a name-pattern guess. None when no sidecar (caller falls back
    to the name globs)."""
    side = pro_path.with_suffix(".envil-ir.json")
    if not side.exists():
        return None
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        nets = (data.get("ir") or {}).get("nets") or []
        return {str(n.get("name", "")) for n in nets if n.get("is_power")}
    except (OSError, ValueError):
        return None


def _resolve_pro(raw: str) -> Optional[Path]:
    """Return the .kicad_pro to audit from a .kicad_pro / .kicad_pcb /
    .kicad_sch / project dir, or None."""
    p = Path(raw).expanduser()
    if p.is_dir():
        cands = sorted(p.glob("*.kicad_pro"))
        return cands[0] if cands else None
    if p.suffix.lower() == ".kicad_pro":
        return p if p.exists() else None
    if p.suffix.lower() in (".kicad_pcb", ".kicad_sch", ".kicad_prl"):
        sib = p.with_suffix(".kicad_pro")
        if sib.exists():
            return sib
        cands = sorted(p.parent.glob("*.kicad_pro"))
        return cands[0] if cands else None
    return p if p.exists() else None


@tool(
    name="board_setup_audit",
    description=(
        "Validate a board's Board Setup (step 5) against its fab profile. "
        "Read-only deterministic gate. Parses the .kicad_pro design rules, "
        "via presets and net classes + the .kicad_pcb, and asserts: every "
        "min_* rule >= fab floor × safety margin (rules 1/2), via annular "
        "(via−drill)/2 >= fab minimum + drill >= fab min (rule 5), each net "
        "class >= the board's own floors (rule 5), and every power net is "
        "assigned a class not left on Default (rules 6/8). Run it after "
        "set_design_rules, before placement/routing.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_pcb"}   # or .kicad_pro / .kicad_sch / dir\n'
        '  {"path": "...", "fab_profile": "jlcpcb_standard", "margin": 1.5}\n'
        "Defaults: fab_profile = fab_profiles.json:_default_profile, margin = "
        "its _default_margin. Policy in config/board_setup_audit.json. Verdict "
        "in words. It reports — set_design_rules fixes."
    ),
    input_schema={"path": str},
)
async def board_setup_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "board_setup_audit disabled in config"}],
                "is_error": True}

    pro_path = _resolve_pro(str(args.get("path", "")).strip())
    if pro_path is None:
        return {"content": [{"type": "text",
                             "text": f"ERROR: no .kicad_pro found for "
                                     f"{args.get('path', '')!r}"}],
                "is_error": True}

    vt = cfg.get("verdict", {}) or {}
    try:
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"content": [{"type": "text",
                             "text": f"ERROR: cannot read {pro_path.name}: {exc}"}],
                "is_error": True}

    rules = _dig(pro, "board", "design_settings", "rules") or {}
    if not rules:
        verdict = vt.get("no_pro", "NO BOARD SETUP — run set_design_rules first")
        return {"content": [{"type": "text",
                             "text": f"# Board setup audit — {pro_path.name}\n\n"
                                     f"**{verdict}**"}],
                "ok": False, "verdict": verdict,
                "pro": str(pro_path).replace("\\", "/")}

    via_dims = _dig(pro, "board", "design_settings", "via_dimensions") or []
    classes = _dig(pro, "net_settings", "classes") or []
    patterns = _dig(pro, "net_settings", "netclass_patterns") or []

    # --- fab profile + margin (same precedence as set_design_rules) ---
    try:
        from .set_design_rules import _load_fab_profiles
        fab_cfg = _load_fab_profiles()
    except Exception:                                       # noqa: BLE001
        fab_cfg = {}
    profiles = (fab_cfg.get("profiles") or {})
    fab_name = str(args.get("fab_profile") or fab_cfg.get("_default_profile")
                   or "jlcpcb_standard")
    profile = profiles.get(fab_name)
    if not profile:
        return {"content": [{"type": "text",
                             "text": f"ERROR: fab_profile '{fab_name}' not in "
                                     f"fab_profiles.json. Available: "
                                     f"{sorted(profiles)}"}],
                "is_error": True}
    try:
        margin = float(args.get("margin") or profile.get("design_margin")
                       or fab_cfg.get("_default_margin", 1.0) or 1.0)
    except (TypeError, ValueError):
        margin = 1.0

    eps = float(cfg.get("epsilon_mm", 0.0001))
    floor_map = cfg.get("rule_floor_map", {}) or {}

    findings: List[Dict[str, str]] = []

    def add(scope: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"scope": scope, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    # --- rules_vs_fab ---
    n_checked = 0
    for key, spec in floor_map.items():
        if key.startswith("_") or not isinstance(spec, list) or len(spec) < 2:
            continue
        pkey, is_geo = spec[0], bool(spec[1])
        fabfloor = _num(profile.get(pkey))
        if fabfloor is None:
            continue
        required = fabfloor * (margin if is_geo else 1.0)
        n_checked += 1
        actual = _num(rules.get(key))
        if actual is None:
            add(key, "rules_vs_fab",
                f"{key} not set in .kicad_pro (fab floor {fabfloor:g}"
                f"{f' ×{margin:g}' if is_geo and margin != 1.0 else ''})")
        elif actual + eps < required:
            add(key, "rules_vs_fab",
                f"{key} {actual:g} < required {required:.4g} "
                f"(fab {fabfloor:g}{f' ×{margin:g}' if is_geo and margin != 1.0 else ''})")

    # --- via_annular ---
    min_ann = _num(profile.get("min_annular_ring_mm"))
    min_drill = _num(profile.get("min_via_drill_mm"))
    thick = _num(profile.get("default_board_thickness_mm"))
    aspect_limit = _num(_dig(cfg, "via_aspect_ratio_limit", "value"))
    for v in via_dims:
        if not isinstance(v, dict):
            continue
        dia, dr = _num(v.get("diameter")), _num(v.get("drill"))
        if dia is None or dr is None or (dia <= 0 and dr <= 0):
            continue                         # {0,0} = "use netclass default"
        ann = (dia - dr) / 2.0
        if min_ann is not None and ann + eps < min_ann:
            add(f"via {dia:g}/{dr:g}", "via_annular",
                f"annular {ann:.3f} < fab min {min_ann:g}")
        if min_drill is not None and dr > 0 and dr + eps < min_drill:
            add(f"via {dia:g}/{dr:g}", "via_annular",
                f"drill {dr:g} < fab min via drill {min_drill:g}")
        if aspect_limit and thick and dr > 0 and thick / dr > aspect_limit + eps:
            add(f"via {dia:g}/{dr:g}", "via_annular",
                f"aspect {thick / dr:.1f}:1 > limit {aspect_limit:g}:1")

    # rule-set self-consistency: (min_via_dia − min_through_hole)/2 >= min_ann_width
    mvd, mth = _num(rules.get("min_via_diameter")), _num(rules.get("min_through_hole_diameter"))
    mva = _num(rules.get("min_via_annular_width"))
    if mvd is not None and mth is not None and mva is not None:
        if (mvd - mth) / 2.0 + eps < mva:
            add("rules", "via_annular",
                f"(min_via_diameter {mvd:g} − min_through_hole {mth:g})/2 = "
                f"{(mvd - mth) / 2:.3f} < min_via_annular_width {mva:g}")

    # --- class_consistency ---
    class_map = [("track_width", "min_track_width"),
                 ("clearance", "min_clearance"),
                 ("via_diameter", "min_via_diameter"),
                 ("via_drill", "min_through_hole_diameter")]
    for c in classes:
        if not isinstance(c, dict):
            continue
        nm = str(c.get("name", "?"))
        for cfield, rkey in class_map:
            cv, rv = _num(c.get(cfield)), _num(rules.get(rkey))
            if cv is not None and rv is not None and cv + eps < rv:
                add(f"class {nm}", "class_consistency",
                    f"{cfield} {cv:g} < board {rkey} {rv:g}")
        cvd, cdr = _num(c.get("via_diameter")), _num(c.get("via_drill"))
        if cvd is not None and cdr is not None and mva is not None:
            if (cvd - cdr) / 2.0 + eps < mva:
                add(f"class {nm}", "class_consistency",
                    f"via annular {(cvd - cdr) / 2:.3f} < min_via_annular_width {mva:g}")

    # --- netclass_coverage (needs board nets) ---
    coverage_checked = False
    power_method: Optional[str] = None
    pcb = pro_path.with_suffix(".kicad_pcb")
    if _rule_on(cfg, "netclass_coverage") and pcb.exists():
        try:
            from .update_audit import _parse_board
            board = _parse_board(pcb)
        except Exception:                                   # noqa: BLE001
            board = None
        if board:
            coverage_checked = True
            # DYNAMIC: prefer the IR's real is_power roles; fall back to
            # name globs only when there's no sidecar to read them from.
            power_set = _power_nets_from_ir(pro_path)
            power_method = "ir_is_power" if power_set is not None else "name_glob"
            pglobs = [str(g).upper() for g in
                      (_dig(cfg, "power_net_globs", "patterns") or [])]
            pats = [(str(pp.get("pattern", "")), str(pp.get("netclass", "")))
                    for pp in patterns if isinstance(pp, dict)]
            for net in sorted(board["net_names"]):
                if not net:
                    continue
                if power_set is not None:
                    is_power = net in power_set
                else:
                    is_power = any(fnmatch.fnmatchcase(net.upper(), g)
                                   for g in pglobs)
                if not is_power:
                    continue                 # not a power net → Default is fine
                assigned = None
                for pat, klass in pats:
                    if pat and fnmatch.fnmatchcase(net.upper(), pat.upper()):
                        assigned = klass
                        break
                if not assigned or assigned == "Default":
                    add(net, "netclass_coverage",
                        f"power net '{net}' not assigned to a class (Default)")

    # --- aggregate ---
    counts = {"error": 0, "warning": 0, "info": 0, "review": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w = counts["error"], counts["warning"]

    if e == 0 and w == 0:
        verdict = vt.get("clean",
                         "BOARD SETUP VERIFIED — {n} rules >= {fab} (x{margin})"
                         ).format(n=n_checked, fab=fab_name, margin=f"{margin:g}")
        ok = True
    else:
        verdict = vt.get("issues",
                         "BOARD SETUP NOT CLEAN — {e} error(s), {w} warning(s) "
                         "vs {fab} (x{margin})"
                         ).format(e=e, w=w, fab=fab_name, margin=f"{margin:g}")
        ok = (e == 0)

    icon = {"error": "✗", "warning": "!", "info": "·", "review": "?"}
    cap = int(cfg.get("max_examples_per_rule", 15))
    lines = [f"# Board setup audit — {pro_path.name}",
             f"  fab {fab_name} · margin ×{margin:g} · {n_checked} rules checked · "
             f"{len(via_dims)} via presets · {len(classes)} net classes"]
    if not coverage_checked and _rule_on(cfg, "netclass_coverage"):
        lines.append("  netclass coverage skipped: no sibling .kicad_pcb")
    elif power_method:
        lines.append(f"  power nets via {power_method}")
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
        "pro": str(pro_path).replace("\\", "/"),
        "fab_profile": fab_name,
        "margin": margin,
        "verdict": verdict,
        "counts": counts,
        "rules_checked": n_checked,
        "coverage_checked": coverage_checked,
        "power_method": power_method,
        "findings": findings,
    }
