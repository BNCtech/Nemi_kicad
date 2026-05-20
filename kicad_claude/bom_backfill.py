"""Back-fill placed-symbol instance properties from lib + bomdoc.

KiCad's GUI BOM dialog reads ONLY the properties on the placed `(symbol ...)`
instances inside a .kicad_sch — it does NOT look up the source library nor
any sidecar file. So when an AI bridge writes a schematic with bare
Value+Footprint, the GUI BOM CSV ships with blank Description / Datasheet /
MPN / Manufacturer columns even though every fact is sitting either in the
library file or in <project>.bomdoc.json.

This module walks the schematic, looks each component up in:
  1. The .kicad_sym source (for Description + Datasheet)
  2. The bomdoc (for MPN + Manufacturer + alternates resolved by --enrich)
and writes those values onto the placed instance. Then KiCad's native BOM
tool exports a complete CSV.

Pure local. No Claude calls (run `bom --enrich` first to populate bomdoc).
Schematic backup is automatic via SchematicDocument.save(backup=True).
"""

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata

from . import bomdoc as _bomdoc
from . import lib_resolver
from ._config_loader import load as _load_config
from .schematic_modifier import SchematicDocument, _make_property

Sym = sexpdata.Symbol


def _to_str(t) -> str:
    return t.value() if isinstance(t, Sym) else str(t)


def _head(node) -> Optional[str]:
    if isinstance(node, list) and node and isinstance(node[0], Sym):
        return node[0].value()
    return None


def _missing_sentinels() -> set:
    return {s.lower() for s in _load_config("conventions")["missing_value_sentinels"]["values"]}


def _bom_cfg() -> Dict[str, Any]:
    return _load_config("bom_config")


def _is_blank(v: str) -> bool:
    return v is None or str(v).strip().lower() in _missing_sentinels()


def _placed_symbols(tree: list) -> List[list]:
    return [c for c in tree[1:] if isinstance(c, list) and _head(c) == "symbol"]


def _get_lib_id(sym_node: list) -> str:
    for sub in sym_node[1:]:
        if isinstance(sub, list) and _head(sub) == "lib_id" and len(sub) > 1:
            return _to_str(sub[1])
    return ""


def _get_property(sym_node: list, name: str) -> Optional[list]:
    for sub in sym_node[1:]:
        if (isinstance(sub, list) and _head(sub) == "property"
                and len(sub) >= 3 and _to_str(sub[1]) == name):
            return sub
    return None


def _existing_value(sym_node: list, name: str) -> str:
    p = _get_property(sym_node, name)
    return _to_str(p[2]) if p else ""


def _set_property_value(sym_node: list, name: str, value: str, hidden: bool = True) -> str:
    """Set property `name` to `value` on this symbol. If absent, append a new
    hidden property anchored at the symbol's `at` position so KiCad doesn't
    place a stray label on screen. Returns the action: 'created' / 'updated'
    / 'unchanged'."""
    existing = _get_property(sym_node, name)
    if existing is not None:
        cur = _to_str(existing[2])
        if cur == value:
            return "unchanged"
        existing[2] = value
        return "updated"

    # Anchor new property at the symbol's (at x y rot) so KiCad has somewhere
    # legal to place it. Hidden — invisible on the canvas; visible in props.
    x, y = 0.0, 0.0
    for sub in sym_node[1:]:
        if isinstance(sub, list) and _head(sub) == "at" and len(sub) >= 3:
            try:
                x = float(sub[1])
                y = float(sub[2])
            except (TypeError, ValueError):
                pass
            break
    new = _make_property(name, value, x, y, hide=hidden)
    insert_at = len(sym_node)
    for i, sub in enumerate(sym_node[1:], start=1):
        if isinstance(sub, list) and _head(sub) == "property":
            insert_at = i + 1
    sym_node.insert(insert_at, new)
    return "created"


def back_fill(
    schematic_path,
    *,
    use_bomdoc: bool = True,
    fields: Optional[List[str]] = None,
    overwrite_existing: Optional[bool] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Walk every placed component and back-fill its properties from lib +
    bomdoc data.

    fields: which property names to back-fill. Default comes from
            bom_config.json:backfill.default_fields.
    overwrite_existing: when True, replace non-blank values too. When None,
            reads bom_config.json:backfill.overwrite_existing (default False).

    Returns a report: {touched, summary[ref → field → action], skipped}.
    Saves with `.bak` backup unless dry_run.
    """
    bf_cfg = _bom_cfg().get("backfill", {})
    fields = fields or bf_cfg.get("default_fields",
                                  ["Description", "Datasheet", "MPN", "Manufacturer"])
    if overwrite_existing is None:
        overwrite_existing = bool(bf_cfg.get("overwrite_existing", False))
    hidden = bool(bf_cfg.get("hidden_by_default", True))
    project_dir = str(Path(schematic_path).parent)

    doc_data = _bomdoc.load(schematic_path) if use_bomdoc else {"lines": {}}

    sd = SchematicDocument(schematic_path)
    sd._snapshot()

    summary: Dict[str, Dict[str, str]] = {}
    touched_refs: List[str] = []
    skipped: List[str] = []

    for sym in _placed_symbols(sd.tree):
        ref = _existing_value(sym, "Reference")
        if not ref or ref.startswith("#"):
            skipped.append(ref or "(no reference)")
            continue

        lib_id = _get_lib_id(sym)
        sch_value = _existing_value(sym, "Value")
        sch_footprint = _existing_value(sym, "Footprint")

        # 1. Lib-derived fields.
        lib_props: Dict[str, str] = {}
        if lib_id:
            res = lib_resolver.resolve(lib_id, project_dir)
            lib_props = res.get("properties", {})

        # 2. BomDoc-derived fields. group_key is stable on value+footprint so
        # the same key round-trips before and after back-fill — one lookup.
        lines_map = doc_data.get("lines", {}) if use_bomdoc else {}
        ld = lines_map.get(_bomdoc.group_key("", sch_value, sch_footprint), {})

        # Resolve each requested field.
        candidates: Dict[str, str] = {}
        if "Description" in fields:
            candidates["Description"] = lib_props.get("Description", "")
        if "Datasheet" in fields:
            candidates["Datasheet"] = lib_props.get("Datasheet", "")
        if "MPN" in fields:
            mpns = ld.get("approved_mpns") or []
            if mpns:
                candidates["MPN"] = mpns[0]
        if "Manufacturer" in fields:
            mfr = ld.get("manufacturer", "")
            if mfr:
                candidates["Manufacturer"] = mfr

        per_ref_actions: Dict[str, str] = {}
        for fname in fields:
            new_value = candidates.get(fname, "")
            if not new_value:
                continue
            current = _existing_value(sym, fname)
            if not overwrite_existing and not _is_blank(current):
                continue  # keep user's value
            action = _set_property_value(sym, fname, new_value, hidden=hidden)
            if action != "unchanged":
                per_ref_actions[fname] = action

        if per_ref_actions:
            touched_refs.append(ref)
            summary[ref] = per_ref_actions

    out_path = sd.path
    if not dry_run and touched_refs:
        out_path = sd.save(backup=True)

    return {
        "schematic": str(sd.path),
        "saved_to": str(out_path) if (not dry_run and touched_refs) else None,
        "dry_run": dry_run,
        "touched": len(touched_refs),
        "touched_refs": touched_refs,
        "skipped": skipped,
        "fields": fields,
        "summary": summary,
    }


def to_text(report: Dict[str, Any]) -> str:
    """Pretty-print a back-fill report for the CLI."""
    lines = []
    lines.append(f"Schematic:  {report['schematic']}")
    if report["dry_run"]:
        lines.append("Mode:       dry-run (no file written)")
    elif report["saved_to"]:
        lines.append(f"Saved to:   {report['saved_to']}  (backup: .bak)")
    else:
        lines.append("Saved to:   (nothing changed; no save needed)")
    lines.append(f"Fields:     {', '.join(report['fields'])}")
    lines.append(f"Touched:    {report['touched']} component(s)")
    if report["summary"]:
        lines.append("")
        lines.append("Per-component changes:")
        for ref, actions in report["summary"].items():
            for fname, action in actions.items():
                lines.append(f"  {ref:6s}  {fname:14s}  {action}")
    return "\n".join(lines)
