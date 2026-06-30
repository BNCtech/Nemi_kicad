"""Tools: let each user add / list / remove their OWN design rules.

Envil ships a default rule set (IPC_STANDARDS.md / ipc_constraints.json /
fab_profiles.json). On top of that, every user — and every project — can add
their own rules in plain language. This is the AI-analysis surface:

  The agent READS the user's words ("my fab min trace is 0.2 mm", "keep mains
  nets 8 mm apart", "I use JLCPCB 4-layer", "no via under the BGA"), works out
  which KIND of rule it is and the structured fields, then calls
  `add_design_rule`. The Python layer (intent/user_rules.py) validates it,
  checks it against the IPC/fab safety floor, warns if it is looser, and saves
  it to that user's overlay. `set_design_rules` then applies the overlay so the
  rule actually takes effect on the board.

Per-user + per-project: precedence PROJECT > USER > Envil default.
"""
from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool

from ..intent import user_rules


def _txt(obj: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, indent=2, ensure_ascii=False)}]}


@tool(
    name="add_design_rule",
    description=(
        "Record ONE design rule the user stated, after YOU analyse their plain "
        "language into structured fields. Use whenever a user expresses a "
        "fab/standard/policy preference (\"my fab min trace is 0.2mm\", \"keep "
        "mains 8mm apart\", \"I use JLCPCB 4-layer\", \"no via under the BGA\", "
        "\"green soldermask\"). Each user has their own rules; pick scope.\n"
        "Pick exactly one `kind`:\n"
        "  override   — a numeric design-rule parameter. Fields: parameter "
        "(one of: min_track_width_mm, min_clearance_mm, min_via_diameter_mm, "
        "min_through_hole_diameter_mm, min_annular_ring_mm, min_hole_to_hole_mm, "
        "min_hole_to_copper_mm, min_edge_to_copper_mm, min_silk_text_height_mm, "
        "min_silk_width_mm), value (number, mm). Floor-checked vs the fab.\n"
        "  directive  — a conditional/spatial rule -> KiCad .kicad_dru. Fields: "
        "condition (a KiCad rule expr, e.g. \"A.NetClass=='POWER'\" or "
        "\"A.insideArea('bga')\"), constraint {type: clearance|track_width|"
        "annular_width|hole_clearance|edge_clearance|length|via_count|disallow, "
        "value: number, object: (for disallow) track|via|hole|zone|footprint}.\n"
        "  selection  — pick a named option. Fields: parameter "
        "(fab_profile|stackup_profile|acceptance_class|copper_weight), value.\n"
        "  preference — a non-DRC note. Fields: text.\n"
        "Always pass `text` = the user's original words. Optional: scope "
        "('user' default, or 'project'), user_id, project_path (the .kicad_pro/"
        "_pcb/_sch or folder — required for scope=project and for floor checks), "
        "fab_profile, rationale. Returns the saved rule and any safety warning."
    ),
    input_schema={"kind": str},
)
async def add_design_rule(args: dict[str, Any]) -> dict[str, Any]:
    if not user_rules.is_enabled():
        return _txt({"ok": False, "error": "user rules disabled"})

    rule: dict[str, Any] = {
        "kind": str(args.get("kind", "") or "").strip().lower(),
        "text": str(args.get("text", "") or "").strip(),
    }
    for k in ("parameter", "comparator", "condition", "rationale", "id"):
        if args.get(k) not in (None, ""):
            rule[k] = args.get(k)
    if "value" in args and args.get("value") not in (None, ""):
        rule["value"] = args.get("value")
    if isinstance(args.get("constraint"), dict):
        rule["constraint"] = args.get("constraint")

    res = user_rules.add_rule(
        rule=rule,
        scope=str(args.get("scope", "user") or "user"),
        user_id=str(args.get("user_id", "default") or "default"),
        project_path=str(args.get("project_path", "") or ""),
        fab_profile=(str(args["fab_profile"]) if args.get("fab_profile") else None),
    )
    return _txt(res)


@tool(
    name="list_design_rules",
    description=(
        "Show the user's and project's custom design rules (the overlays on top "
        "of the Envil defaults). Optional: user_id (default 'default'), "
        "project_path (.kicad_pro/_pcb/_sch or folder). Use to review what rules "
        "are active before a build, or when the user asks 'what are my rules'."
    ),
    input_schema={"user_id": str},
)
async def list_design_rules(args: dict[str, Any]) -> dict[str, Any]:
    if not user_rules.is_enabled():
        return _txt({"ok": False, "error": "user rules disabled"})
    out = user_rules.list_rules(
        user_id=str(args.get("user_id", "default") or "default"),
        project_path=str(args.get("project_path", "") or ""),
    )
    # Also show the resolved effective set (project wins over user).
    out["effective"] = user_rules.resolve(
        user_id=str(args.get("user_id", "default") or "default"),
        project_path=str(args.get("project_path", "") or ""),
        fab_profile=(str(args["fab_profile"]) if args.get("fab_profile") else None),
    )
    return _txt(out)


@tool(
    name="remove_design_rule",
    description=(
        "Delete one custom design rule by its id. Fields: rule_id (required), "
        "scope ('user' default or 'project'), user_id, project_path. Get ids "
        "from list_design_rules."
    ),
    input_schema={"rule_id": str},
)
async def remove_design_rule(args: dict[str, Any]) -> dict[str, Any]:
    if not user_rules.is_enabled():
        return _txt({"ok": False, "error": "user rules disabled"})
    res = user_rules.remove_rule(
        rule_id=str(args.get("rule_id", "") or "").strip(),
        scope=str(args.get("scope", "user") or "user"),
        user_id=str(args.get("user_id", "default") or "default"),
        project_path=str(args.get("project_path", "") or ""),
    )
    return _txt(res)
