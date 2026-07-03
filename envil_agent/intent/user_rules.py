"""Composer for USER-ADDED design rules — the Python half of "each user has
different rules".

Philosophy mirrors the rest of Envil: the LLM is the architect (it reads the
user's plain-language rule and fills the structured fields), this module is the
composer (it validates, conflict-checks against the IPC/fab floors, persists to
the right overlay file, and resolves the layered rule set deterministically).

Layers (precedence PROJECT > USER > DEFAULT):
  Layer 0  Envil defaults   — fab_profiles.json / ipc_constraints.json / net_classes.json
  Layer 1  per-user overlay — config/user_rules/<user>.json   (all that user's boards)
  Layer 2  per-project sidecar — <project>.envil-rules.json   (this board only)

Everything is config-driven (config/user_rules_schema.json); no per-user
constants live here. Safe by construction: every function degrades to a no-op /
empty result rather than raising into the tool layer.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..settings import envil_home

# ---------------------------------------------------------------------------
# Config loaders
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _load_json(name: str) -> Dict[str, Any]:
    try:
        return json.loads((_config_dir() / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def load_schema() -> Dict[str, Any]:
    return _load_json("user_rules_schema.json")


def is_enabled() -> bool:
    return bool(load_schema().get("enabled", False))


def apply_in_set_design_rules() -> bool:
    s = load_schema()
    return bool(s.get("enabled", False) and s.get("apply_in_set_design_rules", False))


# ---------------------------------------------------------------------------
# Overlay file locations
# ---------------------------------------------------------------------------

def _user_overlay_path(user_id: str) -> Path:
    s = load_schema().get("storage", {}) or {}
    rel = s.get("user_dir", "ai_backend/envil_agent/config/user_rules")
    fname = (s.get("user_file", "{user}.json")
             .replace("{user}", _safe_user(user_id)))
    return envil_home() / rel / fname


def _project_overlay_path(project_path: str) -> Optional[Path]:
    if not project_path:
        return None
    s = load_schema().get("storage", {}) or {}
    suffix = s.get("project_sidecar_suffix", ".envil-rules.json")
    p = Path(project_path).expanduser()
    # accept a .kicad_pro/.kicad_pcb/.kicad_sch or a folder
    if p.suffix:
        return p.with_suffix(suffix) if p.suffix != suffix else p
    return p / ("project" + suffix)


def _safe_user(user_id: str) -> str:
    u = (user_id or "default").strip() or "default"
    return "".join(c for c in u if c.isalnum() or c in ("-", "_", ".")) or "default"


def _load_overlay(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {"rules": []}
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("rules"), list):
            return d
    except (json.JSONDecodeError, OSError):
        pass
    return {"rules": []}


def _save_overlay(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Validation + conflict check (against the IPC/fab floor)
# ---------------------------------------------------------------------------

def _fab_profile(fab_profile: Optional[str]) -> Dict[str, Any]:
    fab = _load_json("fab_profiles.json")
    profiles = fab.get("profiles", {}) or {}
    name = fab_profile or fab.get("_default_profile") or "jlcpcb_standard"
    return profiles.get(name, {}) or {}


def _floor_for(parameter: str, fab_profile: Optional[str]) -> Tuple[Optional[float], str]:
    """Return (floor_value, floor_source) for an overridable parameter."""
    cat = (load_schema().get("overridable_parameters", {}) or {}).get(parameter)
    if not cat:
        return None, ""
    prof = _fab_profile(fab_profile)
    fk = cat.get("fab_key")
    if fk and fk in prof:
        return float(prof[fk]), f"fab '{fab_profile or 'default'}'"
    return None, ""


def check_conflict(rule: Dict[str, Any], fab_profile: Optional[str] = None
                   ) -> Optional[str]:
    """Return a warning string if the rule is LOOSER than the floor, else None.
    Only 'override' kind is floor-checked (directives are usually stricter)."""
    if rule.get("kind") != "override":
        return None
    parameter = rule.get("parameter")
    cat = (load_schema().get("overridable_parameters", {}) or {}).get(parameter)
    if not cat:
        return None
    try:
        value = float(rule.get("value"))
    except (TypeError, ValueError):
        return None
    floor, source = _floor_for(parameter, fab_profile)
    if floor is None:
        return None
    safe_dir = cat.get("safe_direction", "larger")
    looser = (value < floor) if safe_dir == "larger" else (value > floor)
    if not looser:
        return None
    pol = load_schema().get("safety_policy", {}) or {}
    tmpl = pol.get("warning_template",
                   "{text}: {parameter}={value}{unit} looser than {floor_source} floor {floor}{unit}")
    return tmpl.format(text=rule.get("text", parameter), parameter=parameter,
                       value=value, unit="mm", floor=floor, floor_source=source)


def validate(rule: Dict[str, Any]) -> Optional[str]:
    """Return an error string if the rule is structurally invalid, else None."""
    schema = load_schema()
    kind = rule.get("kind")
    if kind not in (schema.get("kinds", {}) or {}):
        return f"unknown kind '{kind}'; expected one of {list((schema.get('kinds') or {}).keys())}"
    if kind == "override":
        if rule.get("parameter") not in (schema.get("overridable_parameters", {}) or {}):
            return (f"unknown parameter '{rule.get('parameter')}'; allowed: "
                    f"{list((schema.get('overridable_parameters') or {}).keys())}")
        try:
            float(rule.get("value"))
        except (TypeError, ValueError):
            return "override requires a numeric 'value'"
    elif kind == "directive":
        ctype = (rule.get("constraint") or {}).get("type")
        if ctype not in (schema.get("directive_constraints", {}) or {}):
            return (f"unknown constraint type '{ctype}'; allowed: "
                    f"{list((schema.get('directive_constraints') or {}).keys())}")
        if not rule.get("condition"):
            return "directive requires a 'condition' (KiCad rule expression)"
    elif kind == "selection":
        if not rule.get("parameter") or rule.get("value") in (None, ""):
            return "selection requires 'parameter' (fab_profile|stackup_profile|acceptance_class|copper_weight) and 'value'"
    elif kind == "preference":
        if not (rule.get("text") or "").strip():
            return "preference requires non-empty 'text'"
    return None


# ---------------------------------------------------------------------------
# Public: add / list / remove
# ---------------------------------------------------------------------------

def add_rule(rule: Dict[str, Any], scope: str, user_id: str,
             project_path: str, fab_profile: Optional[str] = None
             ) -> Dict[str, Any]:
    """Persist one analysed rule to the user or project overlay.
    Returns {ok, rule, warning, error, path}."""
    if not is_enabled():
        return {"ok": False, "error": "user rules disabled (user_rules_schema.json:enabled=false)"}
    err = validate(rule)
    if err:
        return {"ok": False, "error": err}

    scope = (scope or "user").strip().lower()
    if scope == "project":
        path = _project_overlay_path(project_path)
        if path is None:
            return {"ok": False, "error": "scope=project needs project_path"}
    else:
        scope = "user"
        path = _user_overlay_path(user_id)

    warning = check_conflict(rule, fab_profile)
    overlay = _load_overlay(path)
    rules = overlay.get("rules", [])
    rid = rule.get("id") or f"r{len(rules) + 1}"
    entry = dict(rule)
    entry["id"] = rid
    entry.setdefault("enabled", True)
    entry["warning"] = warning
    entry["added"] = _now()

    # Dedup: an override/selection on the same parameter replaces the old one.
    if rule.get("kind") in ("override", "selection"):
        rules = [r for r in rules
                 if not (r.get("kind") == rule.get("kind")
                         and r.get("parameter") == rule.get("parameter"))]
    rules.append(entry)
    overlay["rules"] = rules
    overlay.setdefault("_about", "Envil user/project design-rule overlay. Edited by add_design_rule.")
    overlay["scope"] = scope
    overlay["owner"] = _safe_user(user_id) if scope == "user" else str(path)
    try:
        _save_overlay(path, overlay)
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "rule": entry, "warning": warning, "path": str(path)}


def list_rules(user_id: str, project_path: str) -> Dict[str, Any]:
    """Return raw user + project overlays and the resolved effective set."""
    user_path = _user_overlay_path(user_id)
    proj_path = _project_overlay_path(project_path)
    return {
        "user": _load_overlay(user_path).get("rules", []),
        "project": _load_overlay(proj_path).get("rules", []),
        "user_path": str(user_path),
        "project_path": str(proj_path) if proj_path else None,
    }


def remove_rule(rule_id: str, scope: str, user_id: str, project_path: str
                ) -> Dict[str, Any]:
    scope = (scope or "user").strip().lower()
    path = (_project_overlay_path(project_path) if scope == "project"
            else _user_overlay_path(user_id))
    if path is None:
        return {"ok": False, "error": "scope=project needs project_path"}
    overlay = _load_overlay(path)
    before = len(overlay.get("rules", []))
    overlay["rules"] = [r for r in overlay.get("rules", []) if r.get("id") != rule_id]
    if len(overlay["rules"]) == before:
        return {"ok": False, "error": f"rule id '{rule_id}' not found in {scope} overlay"}
    try:
        _save_overlay(path, overlay)
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "removed": rule_id, "remaining": len(overlay["rules"])}


# ---------------------------------------------------------------------------
# Public: resolve (merge layers) — consumed by set_design_rules
# ---------------------------------------------------------------------------

def resolve(user_id: str, project_path: str, fab_profile: Optional[str] = None
            ) -> Dict[str, Any]:
    """Merge user + project overlays (project wins) into the effective set.

    Returns:
      overrides   {kicad_key: value}        — patch board.design_settings.rules
      directives  [ {name, condition, dru, bound, value, unit, disallow} ]
      selections  {parameter: value}        — fab_profile / acceptance_class / …
      preferences [ text, … ]
      warnings    [ str, … ]                 — looser-than-floor notices
    """
    schema = load_schema()
    cat = schema.get("overridable_parameters", {}) or {}
    dcat = schema.get("directive_constraints", {}) or {}

    overrides: Dict[str, Any] = {}
    override_param_to_key: Dict[str, str] = {}
    directives: List[Dict[str, Any]] = []
    selections: Dict[str, Any] = {}
    preferences: List[str] = []
    warnings: List[str] = []

    # USER first, then PROJECT so project entries overwrite by parameter.
    layered = (_load_overlay(_user_overlay_path(user_id)).get("rules", [])
               + _load_overlay(_project_overlay_path(project_path)).get("rules", []))

    for r in layered:
        if not r.get("enabled", True):
            continue
        kind = r.get("kind")
        if kind == "override":
            p = r.get("parameter")
            c = cat.get(p)
            if not c:
                continue
            try:
                val = float(r.get("value"))
            except (TypeError, ValueError):
                continue
            overrides[c["kicad_key"]] = val
            override_param_to_key[p] = c["kicad_key"]
            w = check_conflict(r, fab_profile)
            if w:
                warnings.append(w)
        elif kind == "directive":
            con = r.get("constraint") or {}
            d = dcat.get(con.get("type"))
            if not d:
                continue
            directives.append({
                "name": "envil_" + str(r.get("id", "rule")),
                "condition": str(r.get("condition", "")),
                "dru": d["dru"],
                "bound": d.get("bound", "min"),
                "value": con.get("value"),
                "unit": d.get("unit", "mm"),
                "disallow": con.get("object") if d["dru"] == "disallow" else None,
                "text": r.get("text", ""),
            })
        elif kind == "selection":
            selections[str(r.get("parameter"))] = r.get("value")
        elif kind == "preference":
            preferences.append(str(r.get("text", "")).strip())

    return {
        "overrides": overrides,
        "directives": directives,
        "selections": selections,
        "preferences": [p for p in preferences if p],
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Public: emit a KiCad custom-rule (.kicad_dru) file from directives
# ---------------------------------------------------------------------------

def emit_dru(directives: List[Dict[str, Any]], project_path: str
             ) -> Dict[str, Any]:
    """Write the directive rules to KiCad's `<project>.kicad_dru`.

    Non-breaking: if a .kicad_dru already exists that we did NOT write (no
    Envil marker), we do NOT clobber it — we write a `<project>.envil.kicad_dru`
    sidecar and report that the user must merge. Returns {ok, path, wrote, note}.
    """
    if not directives:
        return {"ok": True, "wrote": 0, "path": None, "note": "no directives"}
    p = Path(project_path).expanduser()
    base = p.with_suffix(".kicad_dru") if p.suffix else p / "project.kicad_dru"
    marker = (load_schema().get("storage", {}) or {}).get(
        "managed_dru_marker", "# ENVIL-MANAGED")

    target = base
    note = ""
    if base.exists():
        try:
            head = base.read_text(encoding="utf-8")
        except OSError:
            head = ""
        if marker not in head:
            target = base.with_suffix(".envil.kicad_dru")
            note = (f"{base.name} already exists and is not Envil-managed; "
                    f"wrote {target.name} instead — merge it into {base.name} "
                    f"(KiCad only auto-loads {base.name}).")

    lines = ["(version 1)", marker, ""]
    for d in directives:
        lines.append(f'(rule "{d["name"]}"')
        if d.get("text"):
            lines.append(f'  ;; {d["text"]}')
        if d.get("condition"):
            lines.append(f'  (condition "{d["condition"]}")')
        if d["dru"] == "disallow":
            obj = d.get("disallow") or "via"
            lines.append(f'  (disallow {obj})')
        else:
            unit = d.get("unit", "mm")
            val = d.get("value")
            lines.append(f'  (constraint {d["dru"]} ({d["bound"]} {val}{unit}))')
        lines.append(')')
        lines.append("")
    try:
        target.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}
    return {"ok": True, "wrote": len(directives), "path": str(target), "note": note}
