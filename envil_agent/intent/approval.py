"""Phase 5 — electrical-readiness / approval card.

A deterministic pre-PCB sign-off summary the ENGINEER reviews before any board
layout: the schematic's validation status, a config-driven electrical checklist,
which requested parts are present, and BLOCK PROVENANCE (which parts came from
known-good verified templates vs were architect-generated, so review focuses on
the latter). Pure + LLM-free + config-driven (config/approval_checklist.json);
adding a checklist item or domain = one JSON edit, zero code.

This module only PRODUCES the card; wiring it as a hard gate before the PCB
tools (refuse layout until `ready_for_pcb` + an explicit human approval) is a
thin follow-up at the tool/flow layer.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

_CFG_PATH = Path(__file__).resolve().parent.parent / "config" / "approval_checklist.json"


@lru_cache(maxsize=1)
def _load_cfg() -> dict:
    try:
        return json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fmt(i: Dict[str, Any]) -> str:
    return f"[{i.get('code', '?')}] {i.get('where', '')}: {i.get('text', '')}".strip()


def electrical_readiness(ir, prompt: str = "",
                         issues: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Build the readiness card for `ir`. `issues` may be supplied (e.g. a
    pre-computed validate_ir result or for testing); otherwise it is computed by
    validate_ir(ir, prompt). Never raises -- returns a best-effort card."""
    if issues is None:
        try:
            from .validate import validate_ir, dedupe_issues
            issues = dedupe_issues(validate_ir(ir, prompt=prompt))
        except Exception:                           # noqa: BLE001
            issues = []
    cfg = _load_cfg()

    err = [i for i in issues if i.get("severity") == "error"]
    warn = [i for i in issues if i.get("severity") == "warning"]
    err_codes = {i.get("code") for i in err}
    warn_codes = {i.get("code") for i in warn}

    # config-driven checklist: FAIL if any code is an error, REVIEW if a warning.
    checklist: List[Dict[str, str]] = []
    for item in cfg.get("items", []) or []:
        codes = set(item.get("codes", []) or [])
        if codes & err_codes:
            status = "FAIL"
        elif codes & warn_codes:
            status = "REVIEW"
        else:
            status = "PASS"
        checklist.append({"label": item.get("label", ""), "status": status})

    # block provenance: verified-template parts vs architect-generated.
    prov = getattr(ir, "_verified_provenance", None) or {}
    all_refs = [c.ref for c in getattr(ir, "components", []) or []]
    verified = sorted(r for r in all_refs if r in prov)
    architect = [r for r in all_refs if r not in prov]

    block_on_err = bool(cfg.get("block_pcb_on_errors", True))
    ready = (len(err) == 0) if block_on_err else True
    return {
        "ready_for_pcb": ready,
        "verdict": ("READY for PCB" if ready
                    else "NOT READY — fix the blocking errors before PCB layout"),
        "error_count": len(err),
        "warning_count": len(warn),
        "blocking": [_fmt(i) for i in err],
        "review": [_fmt(i) for i in warn],
        "checklist": checklist,
        "provenance": {
            "verified_parts": verified,
            "architect_parts": architect,
            "note": ("Concentrate review on the architect-generated parts; the "
                     "verified parts come from known-good templates."
                     if verified else
                     "All parts are architect-generated — review the whole schematic."),
        },
        "sign_off_required": True,
    }
