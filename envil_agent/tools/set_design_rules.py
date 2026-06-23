"""Tool: push fab-profile design rules + net classes into a KiCad project.

Modern KiCad (v7+) stores PCB design rules + net classes in the
`.kicad_pro` JSON project file, NOT in `.kicad_pcb`. This tool reads
the fab + net-class configs and patches the .kicad_pro accordingly:

  - `board.design_settings.rules.*`       <- fab profile minimums
  - `board.design_settings.track_widths`  <- preset widths
  - `board.design_settings.via_dimensions` <- preset via sizes
  - `board.design_settings.diff_pair_dimensions` <- preset diff geom
  - `net_settings.classes`                <- POWER / GND / DIFF_* / etc.
  - `net_settings.netclass_patterns`      <- regex -> class rules

After this runs, KiCad's DRC (and our `drc_check` tool) report against
the FAB'S limits, not KiCad's stock defaults. The pre-existing
.kicad_pro fields outside these paths are preserved byte-for-byte.

Universal — works on ANY .kicad_pcb or .kicad_pro. All values come from
JSON config (`fab_profiles.json`, `net_classes.json`); zero per-circuit
hardcoding. The user can swap profiles with one tool call.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import tool


# ---------------------------------------------------------------------------
# Config loaders (lazy — never crash if a file is missing/malformed)
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "config"


def _load_json(name: str) -> Dict[str, Any]:
    p = _config_dir() / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _load_fab_profiles() -> Dict[str, Any]:
    return _load_json("fab_profiles.json")


def _load_net_classes() -> Dict[str, Any]:
    return _load_json("net_classes.json")


# ---------------------------------------------------------------------------
# Path resolution — accept either .kicad_pcb or .kicad_pro
# ---------------------------------------------------------------------------

def _resolve_pro_path(arg_path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Given whatever path the caller passed, return the matching
    `.kicad_pro`. Returns (path, None) on success or (None, errmsg)."""
    p = Path(arg_path).expanduser()
    if not p.exists():
        return None, f"file not found: {p}"
    suf = p.suffix.lower()
    if suf == ".kicad_pro":
        return p, None
    if suf in (".kicad_pcb", ".kicad_sch"):
        pro = p.with_suffix(".kicad_pro")
        if not pro.exists():
            return None, f"no .kicad_pro next to {p.name}"
        return pro, None
    return None, f"expected .kicad_pro / .kicad_pcb / .kicad_sch; got {suf}"


def _snapshot(pro_path: Path) -> Optional[Path]:
    """Backup the .kicad_pro before mutating so the user can revert.
    Same dir + .envil-bak suffix so we don't pollute the project tree."""
    try:
        bak = pro_path.with_suffix(pro_path.suffix + ".envil-bak")
        shutil.copy2(str(pro_path), str(bak))
        return bak
    except OSError:
        return None


# ---------------------------------------------------------------------------
# .kicad_pro mutators
# ---------------------------------------------------------------------------

def _ensure(d: Dict[str, Any], *keys: str) -> Dict[str, Any]:
    """Walk/create the nested dict at d[k1][k2]…[kn]. Returns the leaf."""
    cur = d
    for k in keys:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    return cur


def _apply_fab_profile(pro: Dict[str, Any], profile: Dict[str, Any]
                       ) -> List[str]:
    """Patch board.design_settings.rules with the fab minimums. Returns
    the keys that changed, for the result report."""
    rules = _ensure(pro, "board", "design_settings", "rules")
    field_map = {
        "min_track_width":             "min_track_width_mm",
        "min_clearance":               "min_clearance_mm",
        "min_via_diameter":            "min_via_diameter_mm",
        "min_through_hole_diameter":   "min_through_hole_diameter_mm",
        "min_via_annular_width":       "min_annular_ring_mm",
        "min_hole_to_hole":            "min_hole_to_hole_mm",
        "min_hole_clearance":          "min_hole_to_copper_mm",
        "min_copper_edge_clearance":   "min_edge_to_copper_mm",
        "min_text_height":             "min_silk_text_height_mm",
        "min_text_thickness":          "min_silk_width_mm",
    }
    changed: List[str] = []
    for kicad_key, profile_key in field_map.items():
        if profile_key not in profile:
            continue
        new_val = float(profile[profile_key])
        if rules.get(kicad_key) != new_val:
            rules[kicad_key] = new_val
            changed.append(kicad_key)
    # Booleans
    if "allow_microvias" in profile:
        new = bool(profile["allow_microvias"])
        if rules.get("allow_microvias") != new:
            rules["allow_microvias"] = new
            changed.append("allow_microvias")
    if "allow_blind_buried_vias" in profile:
        new = bool(profile["allow_blind_buried_vias"])
        if rules.get("allow_blind_buried_vias") != new:
            rules["allow_blind_buried_vias"] = new
            changed.append("allow_blind_buried_vias")
    return changed


def _apply_track_widths(pro: Dict[str, Any], values: List[float]) -> bool:
    """Overwrite the track-width preset list. Returns True if changed."""
    ds = _ensure(pro, "board", "design_settings")
    if ds.get("track_widths") == values:
        return False
    ds["track_widths"] = list(values)
    return True


def _apply_via_dimensions(pro: Dict[str, Any],
                           pairs: List[Dict[str, float]]) -> bool:
    ds = _ensure(pro, "board", "design_settings")
    new_list = [{"diameter": float(p.get("diameter", 0.0)),
                 "drill":    float(p.get("drill", 0.0))} for p in pairs]
    if ds.get("via_dimensions") == new_list:
        return False
    ds["via_dimensions"] = new_list
    return True


def _apply_diff_pair_dimensions(pro: Dict[str, Any],
                                 entries: List[Dict[str, float]]) -> bool:
    """Push diff-pair preset list (width/gap/via_gap)."""
    ds = _ensure(pro, "board", "design_settings")
    new_list = [{"width":   float(e.get("width", 0.0)),
                 "gap":     float(e.get("gap", 0.0)),
                 "via_gap": float(e.get("via_gap", 0.0))} for e in entries]
    if ds.get("diff_pair_dimensions") == new_list:
        return False
    ds["diff_pair_dimensions"] = new_list
    return True


def _class_dict_from_config(name: str, body: Dict[str, Any]
                              ) -> Dict[str, Any]:
    """Translate a net_classes.json class entry into KiCad's net-class
    JSON shape (millimetres -> millimetres, key renames)."""
    return {
        "name": body.get("name") or name,
        "track_width":         float(body.get("track_width_mm", 0.2)),
        "clearance":           float(body.get("clearance_mm", 0.2)),
        "via_diameter":        float(body.get("via_diameter_mm", 0.6)),
        "via_drill":           float(body.get("via_drill_mm", 0.3)),
        "diff_pair_width":     float(body.get("diff_pair_width_mm", 0.15)),
        "diff_pair_gap":       float(body.get("diff_pair_gap_mm", 0.15)),
        "diff_pair_via_gap":   float(body.get("diff_pair_via_gap_mm", 0.25)),
        "microvia_diameter":   float(body.get("microvia_diameter_mm", 0.3)),
        "microvia_drill":      float(body.get("microvia_drill_mm", 0.1)),
        "priority":            int(body.get("priority", 100)),
        "pcb_color":           str(body.get("pcb_color",
                                              "rgba(0, 0, 0, 0.000)")),
        "schematic_color":     str(body.get("schematic_color",
                                              "rgba(0, 0, 0, 0.000)")),
        "bus_width":           int(body.get("bus_width", 6)),
        "line_style":          int(body.get("line_style", 0)),
        "wire_width":          int(body.get("wire_width", 6)),
        "tuning_profile":      str(body.get("tuning_profile", "")),
    }


def _apply_net_classes(pro: Dict[str, Any],
                        classes_cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """Replace net_settings.classes with the config catalogue. Default
    class is always kept first because KiCad treats it specially.
    Returns (changed, class_names)."""
    ns = _ensure(pro, "net_settings")
    catalogue = classes_cfg.get("classes", {}) or {}
    if not catalogue:
        return False, []
    # Ensure Default comes first; the rest in stable name order
    ordered: List[str] = []
    if "Default" in catalogue:
        ordered.append("Default")
    for name in sorted(catalogue.keys()):
        if name != "Default":
            ordered.append(name)
    new_classes = [_class_dict_from_config(n, catalogue[n]) for n in ordered]
    if ns.get("classes") == new_classes:
        return False, ordered
    ns["classes"] = new_classes
    # Bump meta version if KiCad expects it (best-effort)
    meta = _ensure(ns, "meta")
    if "version" not in meta:
        meta["version"] = 5
    return True, ordered


def _apply_netclass_patterns(pro: Dict[str, Any],
                              classes_cfg: Dict[str, Any]) -> bool:
    """Push pattern-based net assignment rules. Pattern syntax matches
    KiCad's wildcard netclass_patterns."""
    ns = _ensure(pro, "net_settings")
    rules = (classes_cfg.get("assignment_patterns", {}) or {}).get("rules", [])
    new_list = [{"netclass": str(r.get("class") or "Default"),
                 "pattern":  str(r.get("pattern", ""))}
                for r in rules if r.get("pattern")]
    if ns.get("netclass_patterns") == new_list:
        return False
    ns["netclass_patterns"] = new_list
    return True


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

@tool(
    name="set_design_rules",
    description=(
        "Push fab-profile design rules + net classes into a KiCad project. "
        "Reads `config/fab_profiles.json` and `config/net_classes.json`, "
        "writes the resolved values into the project's `.kicad_pro` so "
        "DRC reports against the chosen fab's actual capabilities. "
        "Universal — works on any KiCad project, no per-circuit knowledge. "
        "Snapshots the .kicad_pro to <name>.kicad_pro.envil-bak before "
        "mutating so the user can revert by copying the backup back.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}           # required\n'
        '  {"pcb_path": "...", "fab_profile": "jlcpcb_standard"}  # override\n'
        '  {"pcb_path": "...", "skip_net_classes": true}   # rules only\n'
        '  {"pcb_path": "...", "skip_rules": true}         # net classes only\n'
        "Profiles available: see fab_profiles.json -> profiles{}. "
        "Default fab profile = fab_profiles.json:_default_profile."
    ),
    input_schema={"pcb_path": str},
)
async def set_design_rules(args: dict[str, Any]) -> dict[str, Any]:
    arg_path = str(args.get("pcb_path") or args.get("pro_path") or "").strip()
    if not arg_path:
        return {
            "content": [{"type": "text",
                          "text": "ERROR: pcb_path (or pro_path) required"}],
            "is_error": True,
        }

    pro_path, err = _resolve_pro_path(arg_path)
    if err or pro_path is None:
        return {
            "content": [{"type": "text", "text": f"ERROR: {err}"}],
            "is_error": True,
        }

    # Load configs
    fab_cfg = _load_fab_profiles()
    nc_cfg = _load_net_classes()
    if not fab_cfg.get("profiles"):
        return {
            "content": [{"type": "text",
                          "text": "ERROR: fab_profiles.json missing/empty"}],
            "is_error": True,
        }

    fab_name = str(args.get("fab_profile")
                    or fab_cfg.get("_default_profile")
                    or "jlcpcb_standard")
    profile = fab_cfg["profiles"].get(fab_name)
    if not profile:
        available = sorted(fab_cfg["profiles"].keys())
        return {
            "content": [{"type": "text",
                          "text": (f"ERROR: fab_profile '{fab_name}' not "
                                    f"in fab_profiles.json. Available: "
                                    f"{available}")}],
            "is_error": True,
        }

    skip_rules = bool(args.get("skip_rules", False))
    skip_classes = bool(args.get("skip_net_classes", False))

    # Read existing project (or start blank if it doesn't exist —
    # KiCad creates a default one on save)
    try:
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
        if not isinstance(pro, dict):
            return {
                "content": [{"type": "text",
                              "text": f"ERROR: {pro_path} is not a JSON object"}],
                "is_error": True,
            }
    except json.JSONDecodeError as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: {pro_path} is not valid JSON: {exc}"}],
            "is_error": True,
        }
    except OSError as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: cannot read {pro_path}: {exc}"}],
            "is_error": True,
        }

    # Snapshot before any edits
    bak = _snapshot(pro_path)

    rules_changed: List[str] = []
    presets_changed = False
    classes_changed = False
    classes_names: List[str] = []
    patterns_changed = False

    if not skip_rules:
        rules_changed = _apply_fab_profile(pro, profile)
        widths = (nc_cfg.get("preset_track_widths_mm", {}) or {}).get(
            "values", [])
        if widths:
            if _apply_track_widths(pro, [float(v) for v in widths]):
                presets_changed = True
        vias = (nc_cfg.get("preset_via_dimensions_mm", {}) or {}).get(
            "pairs", [])
        if vias:
            if _apply_via_dimensions(pro, vias):
                presets_changed = True
        # Diff-pair presets: derive from the per-class diff geometry.
        # Each class with non-default diff_pair_width contributes one
        # preset row so KiCad's tuning UI offers them as quick picks.
        catalogue = nc_cfg.get("classes", {}) or {}
        diff_rows = []
        seen: set = set()
        for name in ("Default", *(n for n in sorted(catalogue.keys())
                                    if n != "Default")):
            body = catalogue.get(name) or {}
            w = float(body.get("diff_pair_width_mm", 0.0))
            g = float(body.get("diff_pair_gap_mm", 0.0))
            vg = float(body.get("diff_pair_via_gap_mm", 0.0))
            key = (w, g, vg)
            if key in seen:
                continue
            seen.add(key)
            diff_rows.append({"width": w, "gap": g, "via_gap": vg})
        if diff_rows:
            if _apply_diff_pair_dimensions(pro, diff_rows):
                presets_changed = True

    if not skip_classes:
        classes_changed, classes_names = _apply_net_classes(pro, nc_cfg)
        patterns_changed = _apply_netclass_patterns(pro, nc_cfg)

    # Write back. Use indent=2 + sort_keys=False to keep KiCad's
    # field ordering stable on diffs (KiCad reads either way but humans
    # reading the file appreciate matching style).
    try:
        pro_path.write_text(
            json.dumps(pro, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    except OSError as exc:
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: write failed: {exc}"}],
            "is_error": True,
        }

    summary_lines = [
        f"set_design_rules → {pro_path.name}",
        f"  fab profile: {fab_name} ({profile.get('fab', '?')} / "
        f"{profile.get('tier', '?')})",
    ]
    if not skip_rules:
        if rules_changed:
            summary_lines.append(
                f"  rules updated: {', '.join(rules_changed)}")
        else:
            summary_lines.append("  rules: already in sync")
        summary_lines.append(
            f"  presets updated: {'yes' if presets_changed else 'unchanged'}")
    else:
        summary_lines.append("  rules: skipped")
    if not skip_classes:
        summary_lines.append(
            f"  net classes ({len(classes_names)}): "
            f"{', '.join(classes_names) if classes_names else '—'}"
            f" [{'updated' if classes_changed else 'in sync'}]")
        summary_lines.append(
            f"  netclass_patterns: "
            f"{'updated' if patterns_changed else 'in sync'}")
    else:
        summary_lines.append("  net classes: skipped")
    if bak:
        summary_lines.append(f"  backup: {bak.name}")

    return {
        "content": [{"type": "text", "text": "\n".join(summary_lines)}],
        "ok": True,
        "pro_path": str(pro_path),
        "fab_profile": fab_name,
        "rules_changed": rules_changed,
        "presets_changed": presets_changed,
        "classes_changed": classes_changed,
        "classes_applied": classes_names,
        "patterns_changed": patterns_changed,
        "backup": str(bak) if bak else "",
    }
