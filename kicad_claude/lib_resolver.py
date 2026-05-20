"""Pull procurement-relevant fields out of the KiCad symbol library.

Every placed symbol carries a `lib_id` that points at a definition inside the
user's KiCad symbol libraries. Those definitions ALREADY carry Description /
Datasheet / Footprint / ki_keywords on disk — yet a freshly written schematic
often has only Reference + Value + Footprint. This module bridges that gap:
given a lib_id, it opens the library file, parses its (symbol ...) sexp, and
returns every property the lib defines.

Re-uses the same library-discovery machinery as `_lib_symbol_cache.py` so we
do NOT hardcode any path. Whatever sym-lib-table KiCad actually uses on this
machine is what we read. When a lib_id is missing or fuzzy-resolved, the
returned dict carries that status so callers can warn the user.

Cached per process — symbol files are large (~MB) and are read repeatedly
during BOM generation.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import _lib_symbol_cache as _lsc
from ._config_loader import load as _load_config


def _missing_sentinels() -> set:
    return {s.lower() for s in _load_config("conventions")["missing_value_sentinels"]["values"]}


def _is_blank(v: str) -> bool:
    """KiCad writes `~` for 'no value' on properties. Treat the conventions
    sentinel list as blank so lib-derived data can fill in."""
    if v is None:
        return True
    return str(v).strip().lower() in _missing_sentinels()


def _to_str(t) -> str:
    return _lsc._to_str(t)


def _properties_of(sym_node: list) -> Dict[str, str]:
    """Return every (property "Name" "Value" ...) on the symbol node as a dict.
    Inherited properties from a parent (extends) are merged underneath, so the
    child's property wins on conflict — same behaviour eeschema shows."""
    props: Dict[str, str] = {}
    for sub in sym_node[1:] if isinstance(sym_node, list) else []:
        if not (isinstance(sub, list) and _to_str(sub[0]) == "property"):
            continue
        if len(sub) < 3:
            continue
        name = _to_str(sub[1])
        value = _to_str(sub[2])
        if name and name not in props:
            props[name] = value
    return props


@lru_cache(maxsize=1)
def _live_table(project_dir: Optional[str] = None) -> Dict[str, str]:
    pd = Path(project_dir) if project_dir else None
    table_path = _lsc._discover_user_sym_lib_table(pd)
    # Pass project_dir as ${KIPRJMOD} so the BOM/property resolver sees the
    # same expanded URIs as the chat-apply path. Without this, BOM regresses
    # to "every library missing" on projects whose sym-lib-table uses
    # ${KIPRJMOD}/... (the F:/Z: portable layout — see kiprjmod-expansion-in-
    # parser note).
    return _lsc._parse_sym_lib_table(table_path, kiprjmod=pd) if table_path else {}


@lru_cache(maxsize=4096)
def resolve(lib_id: str, project_dir: Optional[str] = None) -> Dict[str, Any]:
    """Resolve a lib_id against the user's libraries and return:
        {
          "status":      "exact" | "fuzzy" | "missing",
          "resolved_id": "<lib:sym>",
          "properties":  {<every property the symbol carries>},
          "extends":     <parent symbol name or None>,
        }

    `properties` includes Reference, Value, Footprint, Datasheet, Description,
    ki_keywords, ki_fp_filters, plus anything custom the library defines. When
    a parent is involved (extends), the parent's properties are merged in.
    """
    out: Dict[str, Any] = {
        "status": "missing",
        "resolved_id": lib_id,
        "properties": {},
        "extends": None,
    }
    if ":" not in (lib_id or ""):
        return out

    table = _live_table(project_dir)
    # Apply the K9 -> K10 / shop rename map BEFORE the lookup so BOM and
    # chat-apply agree on what the canonical lib_id is for a given placed
    # symbol (mirrors _lib_symbol_cache.ensure_lib_symbols_for_doc).
    effective_lib_id = _lsc._alias_lib_id(lib_id)
    lib_name, sym_name = effective_lib_id.split(":", 1)
    uri = table.get(lib_name)

    sym_node: Optional[list] = None
    resolved_lib = lib_name
    resolved_sym = sym_name

    if uri:
        sym_node = _lsc._find_symbol_in_library(uri, sym_name)
        if sym_node is not None:
            out["status"] = "exact"

    if sym_node is None:
        fuzzy = _lsc._fuzzy_find_anywhere(table, lib_id)
        if fuzzy:
            resolved_lib, resolved_sym, sym_node, alts = fuzzy
            out["status"] = "fuzzy"
            if alts:
                out["alternates"] = alts

    if sym_node is None:
        return out

    out["resolved_id"] = f"{resolved_lib}:{resolved_sym}"
    out["extends"] = _lsc._symbol_extends(sym_node)

    props = _properties_of(sym_node)

    # If this symbol extends a parent, merge parent's properties UNDER ours.
    parent_name = out["extends"]
    if parent_name:
        parent_lib = resolved_lib
        parent_uri = table.get(parent_lib)
        parent_node = (
            _lsc._find_symbol_in_library(parent_uri, parent_name)
            if parent_uri else None
        )
        if parent_node is None:
            fuzzy = _lsc._fuzzy_find_anywhere(table, f"{parent_lib}:{parent_name}")
            if fuzzy:
                _, _, parent_node, _alts = fuzzy
        if parent_node is not None:
            for k, v in _properties_of(parent_node).items():
                props.setdefault(k, v)

    out["properties"] = props
    return out


def enrich_component(comp: Dict[str, Any], project_dir: Optional[str] = None) -> Dict[str, Any]:
    """Return a shallow copy of `comp` with library-derived fields filled in
    where the schematic instance left them blank.

    Schematic instance ALWAYS wins over the lib (the user may have intentionally
    overridden the lib value on the placed symbol). Only blanks are filled.
    """
    out = dict(comp)
    lib_id = comp.get("lib_id", "")
    if not lib_id:
        return out

    res = resolve(lib_id, project_dir)
    lib_props = res.get("properties", {})
    if not lib_props:
        return out

    # Native fields
    if _is_blank(out.get("footprint", "")):
        out["footprint"] = lib_props.get("Footprint", "") or ""

    # Custom properties (preserve schematic's choices). KiCad's `~` sentinel
    # counts as blank so the lib's real Datasheet/Description fill in.
    sch_props = dict(out.get("properties") or {})
    for k, v in lib_props.items():
        if k in ("Reference", "Value", "Footprint"):
            continue
        if k not in sch_props or _is_blank(sch_props.get(k, "")):
            sch_props[k] = v
    out["properties"] = sch_props

    out["_lib_status"] = res["status"]
    out["_lib_resolved_id"] = res["resolved_id"]
    return out
