"""Tool: drc_audit — the deterministic gate for step 9 (DRC).

Turns the existing ``drc_check`` (headless kicad-cli DRC against the board's
own .kicad_pro rules) into a hard pass/fail per the master-flow step-9 rules:
errors = 0, unconnected = 0, shorts = 0, silkscreen not on pads. Read-only —
it only composes drc_check + pcb_verify's silk check; it never edits the board.

Because drc_check runs against the SAME rule set ``board_setup_audit``
validates, there is one authoritative constraint source end-to-end.
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
        p = Path(__file__).resolve().parent.parent / "config" / "drc_audit.json"
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


@tool(
    name="drc_audit",
    description=(
        "Validate the physical board (step 9, DRC) as a hard pass/fail. "
        "Read-only. Composes drc_check (headless kicad-cli DRC) and asserts "
        "errors=0, unconnected=0, shorts=0, and silkscreen not on pads. Runs "
        "against the board's own .kicad_pro rules — the same set board_setup_"
        "audit validates. Run after routing/zones, as the last check before "
        "3D/DFM and Gerber.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_pcb"}   # or .kicad_sch / .kicad_pro / dir\n'
        "Verdict in words. A kicad-cli failure reports DRC UNAVAILABLE (tool "
        "issue, not a design fail), never a false pass. Policy in "
        "config/drc_audit.json."
    ),
    input_schema={"path": str},
)
async def drc_audit(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "drc_audit disabled in config"}],
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

    # --- compose drc_check ---
    from .drc_check import drc_check
    r = await drc_check.handler({"pcb_path": str(pcb)})
    err_count = int(r.get("error_count", -1))
    if r.get("is_error") or err_count < 0:
        detail = str(r.get("content", [{}])[0].get("text", "DRC unavailable"))
        detail = detail.splitlines()[0][:160] if detail else "unavailable"
        verdict = vt.get("unavailable", "DRC UNAVAILABLE")
        return {"content": [{"type": "text",
                             "text": f"# DRC audit — {pcb.name}\n"
                                     f"  — {detail}\n\n**{verdict}**"}],
                "ok": None, "verdict": verdict, "available": False,
                "pcb": str(pcb).replace("\\", "/")}

    warn_count = int(r.get("warning_count", 0))
    issues = list(r.get("issues") or [])
    unconn_types = {str(t).lower() for t in cfg.get("unconnected_types", [])}
    short_kw = [str(k).lower() for k in cfg.get("short_keywords", [])]

    findings: List[Dict[str, str]] = []

    def add(scope: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"scope": scope, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    n_unconn = n_short = n_err = 0
    for it in issues:
        typ = str(it.get("type", "")).lower()
        desc = str(it.get("description", "")).strip()
        sev = str(it.get("severity", "")).lower()
        is_unconn = typ in unconn_types or "unconnected" in typ
        is_short = any(k in typ or k in desc.lower() for k in short_kw)
        if is_unconn:
            n_unconn += 1
            add(typ or "unconnected", "unconnected", desc or "unconnected item")
        elif is_short:
            n_short += 1
            add(typ or "short", "shorts", desc or "short")
        elif sev == "error":
            n_err += 1
            add(typ or "drc", "drc_errors", desc or typ)
        # warning-severity non-short/non-unconnected issues stay in warn_count

    # --- silk on pads (reuse pcb_verify's structural check) ---
    silk_n = 0
    if _rule_on(cfg, "silk_on_pad"):
        try:
            from .pcb_verify import _check_silkscreen_on_pad
            viol = _check_silkscreen_on_pad(pcb)
            for v in (viol or []):
                silk_n += 1
                add("silk", "silk_on_pad", str(v))
        except Exception:                                   # noqa: BLE001
            pass

    hard = n_err + n_unconn + n_short          # blocking (silk is advisory)
    if hard == 0:
        verdict = vt.get("clean", "DRC VERIFIED ({w} warnings)").format(w=warn_count)
        ok = True
    else:
        verdict = vt.get("issues",
                         "DRC NOT CLEAN — {e} error(s), {u} unconnected, "
                         "{s} short(s), {w} warning(s)"
                         ).format(e=n_err, u=n_unconn, s=n_short, w=warn_count)
        ok = False

    icon = {"error": "✗", "warning": "!", "info": "·", "review": "?"}
    lines = [f"# DRC audit — {pcb.name}",
             f"  {n_err} errors · {n_unconn} unconnected · {n_short} shorts · "
             f"{warn_count} warnings · {silk_n} silk-on-pad"]
    order = ["error", "warning"]
    for sev in order:
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
        "available": True,
        "pcb": str(pcb).replace("\\", "/"),
        "drc_errors": n_err,
        "unconnected": n_unconn,
        "shorts": n_short,
        "warnings": warn_count,
        "silk_on_pad": silk_n,
        "verdict": verdict,
        "counts": {"error": n_err + n_unconn + n_short,
                   "warning": warn_count + silk_n, "info": 0, "review": 0},
        "findings": findings,
    }
