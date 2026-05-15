"""Hierarchical-schematic walker.

A KiCad project is usually NOT a single .kicad_sch — it's a root sheet that
embeds child sheets via (sheet ...) blocks. The single-file extractor only
sees the parent, so BOM under-counts and basic_checks misses every defect in
the child sheets. This module walks the whole tree.

Public API:
  iter_sheet_instances(root)  -> yields (hierarchy_path, kicad_sch path)
  aggregate_components(root)  -> components from every instance, tagged with `sheet`
  aggregate_labels(root)
  aggregate_wires(root)

KiCad "complex hierarchies" deliberately reuse the same child .kicad_sch from
multiple parent sheets — each reuse is a SEPARATE instance, with its own
physical parts. We yield one entry per instance, not per unique file. A real
cycle (A includes B, B includes A) is detected via the ancestor stack and
skipped with a warning.
"""

from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import sexpdata

from .schematic_extractor import SchematicExtractor


def _to_str(token) -> str:
    return token.value() if isinstance(token, sexpdata.Symbol) else str(token)


def _walk(node, tag: str):
    if not isinstance(node, list):
        return
    for child in node[1:]:
        if isinstance(child, list) and child and _to_str(child[0]) == tag:
            yield child


def _sheetfile_of(sheet_node: list) -> str:
    """Read the (property "Sheetfile" "child.kicad_sch") value from a (sheet ...) block."""
    for child in sheet_node[1:]:
        if (
            isinstance(child, list)
            and _to_str(child[0]) == "property"
            and len(child) >= 3
            and _to_str(child[1]) == "Sheetfile"
        ):
            return _to_str(child[2])
    return ""


def _sheetname_of(sheet_node: list) -> str:
    for child in sheet_node[1:]:
        if (
            isinstance(child, list)
            and _to_str(child[0]) == "property"
            and len(child) >= 3
            and _to_str(child[1]) == "Sheetname"
        ):
            return _to_str(child[2])
    return ""


def iter_sheet_instances(
    root_path,
) -> Iterator[Tuple[str, Path]]:
    """Yield (hierarchy_path, file_path) for every sheet instance, root first.

    hierarchy_path is a forward-slash-separated breadcrumb like "/" for the
    root and "/amp_left/output_stage" for a deeper child. Duplicate-file reuse
    is preserved (yielded multiple times); true cycles are detected via the
    ancestor stack and emit no result for the cycling edge.
    """
    root = Path(root_path).resolve()
    yield ("/", root)
    yield from _walk_children(root, "/", ancestors=(root,))


def _walk_children(
    parent_path: Path, parent_hpath: str, ancestors: Tuple[Path, ...]
) -> Iterator[Tuple[str, Path]]:
    try:
        text = parent_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    try:
        tree = sexpdata.loads(text)
    except Exception:
        return

    for sheet in _walk(tree, "sheet"):
        sheetfile = _sheetfile_of(sheet)
        sheetname = _sheetname_of(sheet) or sheetfile
        if not sheetfile:
            continue
        child = (parent_path.parent / sheetfile).resolve()
        if not child.exists():
            continue
        if child in ancestors:
            # Real cycle in the ancestor chain — skip this edge to avoid infinite recursion.
            # Sibling reuse (same file referenced twice from one parent) is NOT a cycle.
            continue
        hpath = f"{parent_hpath.rstrip('/')}/{sheetname}" if parent_hpath != "/" else f"/{sheetname}"
        yield (hpath, child)
        yield from _walk_children(child, hpath, ancestors + (child,))


def aggregate_components(root_path) -> List[Dict[str, Any]]:
    """Components from every sheet instance, each tagged with `sheet` (its hierarchy path)
    and `sheet_file` (the .kicad_sch the symbol lives in)."""
    out: List[Dict[str, Any]] = []
    for hpath, path in iter_sheet_instances(root_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        for c in extractor.components():
            c = dict(c)
            c["sheet"] = hpath
            c["sheet_file"] = str(path)
            out.append(c)
    return out


def aggregate_labels(root_path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for hpath, path in iter_sheet_instances(root_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        for lb in extractor.labels():
            lb = dict(lb)
            lb["sheet"] = hpath
            lb["sheet_file"] = str(path)
            out.append(lb)
    return out


def aggregate_wires(root_path) -> List[List[Tuple[float, float]]]:
    """Wires across all sheets, flattened. (Wires don't carry refdes; the sheet
    provenance is dropped here because the off-grid check doesn't need it.)
    Geometry checks that DO need provenance use aggregate_wires_with_sheet."""
    out: List[List[Tuple[float, float]]] = []
    for _hpath, path in iter_sheet_instances(root_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        out.extend(extractor.wires())
    return out


def aggregate_wires_with_sheet(
    root_path,
) -> List[Tuple[str, List[Tuple[float, float]]]]:
    """Same as aggregate_wires but each wire is paired with its sheet hierarchy
    path. Used by the geometry checks so cross-sheet wires (which can't really
    overlap — they're in different files) aren't compared against each other."""
    out: List[Tuple[str, List[Tuple[float, float]]]] = []
    for hpath, path in iter_sheet_instances(root_path):
        try:
            extractor = SchematicExtractor(path)
        except Exception:
            continue
        for poly in extractor.wires():
            out.append((hpath, poly))
    return out


def sheet_count(root_path) -> int:
    return sum(1 for _ in iter_sheet_instances(root_path))


def format_for_claude(root_path) -> str:
    """Project-wide text dump for the LLM. Each sheet section is preceded by
    its hierarchy path so Claude knows which sheet a refdes lives in.

    Drop-in replacement for SchematicExtractor.format_for_claude() when the
    caller wants whole-project context (validator, fixer)."""
    parts: List[str] = []
    instances = list(iter_sheet_instances(root_path))
    parts.append(f"PROJECT: {Path(root_path).name}")
    parts.append(f"SHEETS: {len(instances)}")
    parts.append("")
    for hpath, path in instances:
        try:
            extractor = SchematicExtractor(path)
            section = extractor.format_for_claude()
        except Exception as e:
            section = f"(unable to read: {e})"
        parts.append(f"=== SHEET {hpath}  ({path.name}) ===")
        parts.append(section)
        parts.append("")
    return "\n".join(parts)
