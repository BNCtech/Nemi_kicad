"""Tool: gerber_gate — the deterministic gate for step 11 (Gerber Export).

The last stage. Read-only: it does NOT export (export_pcb / ship_design do
that) — it verifies the manufacturing outputs in the ``gerbers/`` folder next
to the .kicad_pcb: the required copper/mask/silk/edge layers are present as
.gbr files, there's an Excellon drill file, the .gbrjob is there, and the
pick-and-place exists. Paste layers are required only when the board actually
carries SMD pads (dynamic).

"Block export unless every prior gate passed" (rule 5) is enforced by
pipeline_gate ordering — this is step 11, so any earlier NO-GO makes the whole
run NO-GO. With no gerbers exported yet it reports NOT EXPORTED (pending), not
a failure.
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
        p = Path(__file__).resolve().parent.parent / "config" / "gerber_gate.json"
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


def _layer_token(layer: str) -> str:
    """The token KiCad puts in a gerber filename for a layer: '.' -> '_'
    (F.Cu -> f_cu), and 'SilkS' -> 'silkscreen' (KiCad writes the full word)."""
    return layer.replace(".", "_").lower().replace("silks", "silkscreen")


# Non-layer outputs in the gerbers/ folder — everything else is a layer file,
# regardless of extension (KiCad uses Protel exts: .gtl/.gbl/.gts/.gto/.gm1…).
_NON_LAYER_EXT = (".drl", ".gbrjob", ".csv", ".zip", ".pdf", ".step",
                  ".json", ".txt", ".rpt")


@tool(
    name="gerber_gate",
    description=(
        "Validate the Gerber export (step 11) — the last gate. Read-only: it "
        "checks the gerbers/ folder next to the .kicad_pcb for the required "
        "fab layer set (copper/mask/silk/edge, +paste if SMD), an Excellon "
        "drill file, the .gbrjob, and the pick-and-place. It does NOT export — "
        "run export_pcb / ship_design first. No gerbers yet => NOT EXPORTED "
        "(pending). Prior-gate blocking is enforced by pipeline_gate order.\n"
        "Args:\n"
        '  {"path": "C:/.../proj.kicad_pcb"}   # or .kicad_sch / .kicad_pro / dir\n'
        "Verdict in words. Policy in config/gerber_gate.json."
    ),
    input_schema={"path": str},
)
async def gerber_gate(args: dict[str, Any]) -> dict[str, Any]:
    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {"content": [{"type": "text",
                             "text": "gerber_gate disabled in config"}],
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
    gdir = pcb.parent / str(cfg.get("gerbers_subdir", "gerbers"))

    files = [p for p in gdir.iterdir() if p.is_file()] if gdir.is_dir() else []
    if not files:
        verdict = vt.get("not_exported",
                         "NOT EXPORTED — run export_pcb / ship_design first")
        return {"content": [{"type": "text",
                             "text": f"# Gerber gate — {pcb.name}\n"
                                     f"  no gerbers/ folder\n\n**{verdict}**"}],
                "ok": None, "exported": False, "verdict": verdict,
                "pcb": str(pcb).replace("\\", "/")}

    names = [f.name.lower() for f in files]
    layer_files = [n for n in names if not n.endswith(_NON_LAYER_EXT)]

    # SMD? -> paste layers required too (dynamic, from the board's pads)
    smd = False
    try:
        smd = "smd" in pcb.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        pass

    required = list(cfg.get("required_layers", []))
    if smd:
        required += list(cfg.get("paste_layers_if_smd", []))

    findings: List[Dict[str, str]] = []

    def add(scope: str, rule: str, msg: str) -> None:
        if not _rule_on(cfg, rule):
            return
        findings.append({"scope": scope, "rule": rule,
                         "severity": _rule_sev(cfg, rule), "msg": msg})

    # --- gerber_layers ---
    found = 0
    for layer in required:
        tok = _layer_token(layer)
        if any(tok in n for n in layer_files):
            found += 1
        else:
            add(layer, "gerber_layers", f"missing gerber layer {layer} (*{tok}*)")

    # --- drill_file ---
    if not any(n.endswith(".drl") for n in names):
        add("drill", "drill_file", "no Excellon drill file (.drl)")

    # --- gerber_job ---
    if not any(n.endswith(".gbrjob") for n in names):
        add("job", "gerber_job", "no .gbrjob file (fab stackup/format)")

    # --- place_file ---
    if not any(n.endswith("-pos.csv") or n.endswith("_pos.csv") for n in names):
        add("place", "place_file", "no pick-and-place (-pos.csv)")

    # --- aggregate ---
    counts = {"error": 0, "warning": 0, "info": 0, "review": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
    e, w = counts["error"], counts["warning"]

    if e == 0 and w == 0:
        verdict = vt.get("verified",
                         "GERBERS VERIFIED — {found}/{req} layers + drill"
                         ).format(found=found, req=len(required))
        ok = True
    else:
        verdict = vt.get("issues",
                         "GERBERS NOT CLEAN — {e} error(s), {w} warning(s)"
                         ).format(e=e, w=w)
        ok = (e == 0)

    icon = {"error": "✗", "warning": "!", "info": "·", "review": "?"}
    lines = [f"# Gerber gate — {pcb.name}",
             f"  {len(layer_files)} layer file(s) · {found}/{len(required)} "
             f"required · {'SMD' if smd else 'THT'} · {len(files)} files in gerbers/"]
    for sev in ["error", "warning"]:
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
        "exported": True,
        "pcb": str(pcb).replace("\\", "/"),
        "gerbers_dir": str(gdir).replace("\\", "/"),
        "layers_found": found,
        "layers_required": len(required),
        "verdict": verdict,
        "counts": counts,
        "findings": findings,
    }
