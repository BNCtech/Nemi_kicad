"""Tool: combine N child sheets into ONE merged .kicad_sch.

Use case: user has a hierarchy with multiple small child sheets (POWER,
USB, CLOCK, RESET, etc.) and wants to consolidate two or more of them
into a single sheet. Works for ANY combination of child sheets — not
just specific names — so the same tool handles "combine POWER + USB",
"combine all four children", "combine MCU + IO + COMM", etc.

Strategy:
  1. Parse every source .kicad_sch via sexpdata
  2. Extract (symbol ...), (wire ...), (junction ...), (label ...),
     (global_label ...), (no_connect ...) blocks from each
  3. Detect refdes collisions across sources; auto-renumber the
     second+ occurrence using the next-free integer per letter prefix
     (R5 -> R6, etc.) — refs that uniquely exist stay unchanged
  4. Offset source N's coordinates by an x-shift = max(prior sheets'
     x extent) + gap_mm so the layouts don't overlap
  5. Same-named nets merge electrically (KiCad treats matching labels
     as one net — no transformation needed)
  6. Emit a new combined .kicad_sch via sexpdata serialization
  7. Optionally update a parent hierarchy sheet: remove the (sheet ...)
     entries for the merged source files, add one new (sheet ...)
     pointing at the combined file

Safety checks (configurable via layout_config.json -> combine_sheets):
  - max_combined_components: refuse to merge if the result has more
    than N components (default 30 — past that, hierarchy is preferred)
  - require_shared_net: if true, refuse to merge sheets that don't
    share at least one electrical net (default true; combining two
    unrelated circuits onto one page is rarely useful)
  - lateral_gap_mm: horizontal gap between merged source blocks
    (default 25 mm)

Pure data-driven — no per-circuit or per-block-name hardcoding."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import sexpdata
from claude_agent_sdk import tool


# ---------------------------------------------------------------------------
# sexpdata helpers (same patterns as apply_ops.py)
# ---------------------------------------------------------------------------

def _head(node: Any) -> Optional[str]:
    if isinstance(node, list) and node:
        first = node[0]
        if isinstance(first, sexpdata.Symbol):
            return first.value()
        if isinstance(first, str):
            return first
    return None


def _children(node: Any, name: str) -> List[list]:
    if not isinstance(node, list):
        return []
    return [c for c in node[1:] if isinstance(c, list) and _head(c) == name]


def _first_child(node: Any, name: str) -> Optional[list]:
    cs = _children(node, name)
    return cs[0] if cs else None


def _ref_of_symbol(symbol_node: list) -> Optional[str]:
    """Read the Reference property from a (symbol ...) block."""
    for c in symbol_node[1:]:
        if (isinstance(c, list) and _head(c) == "property"
                and len(c) >= 3 and str(c[1]) == "Reference"):
            return str(c[2])
    return None


def _set_ref_in_symbol(symbol_node: list, new_ref: str) -> None:
    """Mutate the Reference property + every (path ... (reference X))
    inside the symbol's (instances ...) block to `new_ref`."""
    for c in symbol_node[1:]:
        if (isinstance(c, list) and _head(c) == "property"
                and len(c) >= 3 and str(c[1]) == "Reference"):
            c[2] = new_ref
        if isinstance(c, list) and _head(c) == "instances":
            for proj in c[1:]:
                if not isinstance(proj, list):
                    continue
                for path in proj[1:]:
                    if not isinstance(path, list):
                        continue
                    for child in path[1:]:
                        if (isinstance(child, list)
                                and _head(child) == "reference"
                                and len(child) >= 2):
                            child[1] = new_ref


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _shift_at_clause(node: list, dx: float, dy: float) -> None:
    """Shift every `(at x y [r])` clause inside `node` by (dx, dy)."""
    if not isinstance(node, list):
        return
    head = _head(node)
    if head == "at" and len(node) >= 3:
        try:
            node[1] = float(node[1]) + dx
            node[2] = float(node[2]) + dy
        except (TypeError, ValueError):
            pass
        return
    if head == "xy" and len(node) >= 3:
        # `(xy x y)` clauses inside `(pts ...)` — same shift
        try:
            node[1] = float(node[1]) + dx
            node[2] = float(node[2]) + dy
        except (TypeError, ValueError):
            pass
        return
    for c in node[1:]:
        if isinstance(c, list):
            _shift_at_clause(c, dx, dy)


def _component_bbox(symbol_node: list) -> Optional[Tuple[float, float]]:
    """Return the position of a (symbol ...) instance from its (at X Y)
    clause. Used to estimate the source sheet's spatial extent."""
    at = _first_child(symbol_node, "at")
    if at and len(at) >= 3:
        try:
            return float(at[1]), float(at[2])
        except (TypeError, ValueError):
            return None
    return None


def _sheet_extent(root: list) -> Tuple[float, float, float, float]:
    """Approximate (min_x, min_y, max_x, max_y) of every (symbol) in
    the source. Treats each instance as a single point; the result is
    the *positional* bbox not accounting for symbol geometry."""
    xs = []
    ys = []
    for c in root[1:]:
        if isinstance(c, list) and _head(c) == "symbol":
            pos = _component_bbox(c)
            if pos:
                xs.append(pos[0])
                ys.append(pos[1])
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# Ref-collision resolution
# ---------------------------------------------------------------------------

_REFDES_RE = re.compile(r"^([A-Za-z#]+)(\d+)$")


def _split_refdes(ref: str) -> Optional[Tuple[str, int]]:
    m = _REFDES_RE.match(ref)
    return (m.group(1), int(m.group(2))) if m else None


def _rename_collisions(source_roots: List[list]) -> Dict[int, Dict[str, str]]:
    """For each source root (index i > 0), produce a {old_ref: new_ref}
    mapping. The first source keeps its refs; subsequent sources get
    bumped to next-free integer per letter prefix.

    Example: target has R1, R2, R3, C1, C2.
             source has R1, R2, C1.
             Result: {R1->R4, R2->R5, C1->C3}."""
    used_per_prefix: Dict[str, set] = {}
    # Seed with source 0's refs (kept as-is).
    if source_roots:
        for c in source_roots[0][1:]:
            if isinstance(c, list) and _head(c) == "symbol":
                ref = _ref_of_symbol(c)
                if ref:
                    parts = _split_refdes(ref)
                    if parts:
                        used_per_prefix.setdefault(parts[0], set()).add(parts[1])

    mappings: Dict[int, Dict[str, str]] = {}
    for i, root in enumerate(source_roots[1:], start=1):
        m: Dict[str, str] = {}
        for c in root[1:]:
            if not (isinstance(c, list) and _head(c) == "symbol"):
                continue
            ref = _ref_of_symbol(c)
            if not ref:
                continue
            parts = _split_refdes(ref)
            if not parts:
                continue
            prefix, num = parts
            seen = used_per_prefix.setdefault(prefix, set())
            if num in seen:
                # Collision — pick the next free integer
                new_num = max(seen) + 1 if seen else 1
                while new_num in seen:
                    new_num += 1
                new_ref = f"{prefix}{new_num}"
                m[ref] = new_ref
                seen.add(new_num)
            else:
                seen.add(num)
        mappings[i] = m
    return mappings


def _apply_ref_rename(root: list, mapping: Dict[str, str]) -> None:
    """Walk every (symbol ...) in root and update its Reference if it
    appears in `mapping`."""
    if not mapping:
        return
    for c in root[1:]:
        if isinstance(c, list) and _head(c) == "symbol":
            old = _ref_of_symbol(c)
            if old and old in mapping:
                _set_ref_in_symbol(c, mapping[old])


# ---------------------------------------------------------------------------
# Net extraction (for the shared-net safety check)
# ---------------------------------------------------------------------------

def _net_names(root: list) -> set:
    """Collect every label / global_label / hierarchical_label name in
    the sheet. Approximates the net namespace for the shared-net check."""
    names = set()
    for c in root[1:]:
        if not isinstance(c, list):
            continue
        h = _head(c)
        if h in ("label", "global_label", "hierarchical_label"):
            if len(c) >= 2:
                names.add(str(c[1]).strip())
        if h == "symbol":
            # power port (e.g. power:+5V) — its Value is the net name
            for cc in c[1:]:
                if (isinstance(cc, list) and _head(cc) == "lib_id"
                        and len(cc) >= 2):
                    lib = str(cc[1])
                    if lib.lower().startswith("power:"):
                        # Find the Value property
                        for d in c[1:]:
                            if (isinstance(d, list) and _head(d) == "property"
                                    and len(d) >= 3
                                    and str(d[1]) == "Value"):
                                names.add(str(d[2]).strip())
                                break
                    break
    return names


# ---------------------------------------------------------------------------
# Top-level merge logic
# ---------------------------------------------------------------------------

def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("combine_sheets", {}) or {}
    except Exception:
        return {}


def _merge_into(target: list, source: list, dx: float, dy: float) -> int:
    """Copy every (symbol/wire/junction/label/global_label/hierarchical_label/
    no_connect/text) child from source root into target root, shifting by
    (dx, dy). Returns the count of items copied."""
    n = 0
    keep = {"symbol", "wire", "junction", "label", "global_label",
            "hierarchical_label", "no_connect", "text", "bus", "bus_entry",
            "rectangle", "polyline", "arc", "circle"}
    for c in list(source[1:]):
        if not isinstance(c, list):
            continue
        if _head(c) in keep:
            shifted = sexpdata.loads(sexpdata.dumps(c))  # deep copy
            _shift_at_clause(shifted, dx, dy)
            target.append(shifted)
            n += 1
    return n


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

@tool(
    name="combine_sheets",
    description=(
        "Combine 2 or more child .kicad_sch sheets into one merged sheet. "
        "Use when the user says: 'combine POWER and USB', 'merge the two "
        "small sheets', 'combine these pages into one', 'flatten POWER + "
        "USB + RESET into single sheet', 'combine sheets pannu' (Tanglish). "
        "Works for ANY combination — pass any list of source .kicad_sch "
        "paths. Auto-resolves refdes collisions, merges same-name nets, "
        "places sources side-by-side with a gap.\n"
        "Args:\n"
        '  {"sources": ["F:/.../01_power.kicad_sch", "F:/.../03_usb.kicad_sch"],\n'
        '   "output":  "F:/.../01_power_usb.kicad_sch"}\n'
        '  Optional: {"parent": "F:/.../top.kicad_sch"} updates the\n'
        "    parent hierarchy (removes the merged source sheet entries\n"
        "    and adds the combined sheet entry).\n"
        "  Optional: {\"lateral_gap_mm\": 25.4} override the gap between\n"
        "    merged sources (default from layout_config.json).\n"
        "Safety: refuses if combined components > max_combined_components "
        "or if sources share no electrical net (configurable in "
        "layout_config.json -> combine_sheets)."
    ),
    input_schema={"sources": list, "output": str},
)
async def combine_sheets(args: Dict[str, Any]) -> Dict[str, Any]:
    raw_sources = args.get("sources") or []
    if not isinstance(raw_sources, list) or len(raw_sources) < 2:
        return {"content": [{"type": "text",
                              "text": "ERROR: 'sources' must be a list of "
                                       "at least 2 .kicad_sch paths"}],
                 "is_error": True}
    output_path = Path(str(args.get("output", "")).strip()).expanduser()
    if not output_path or output_path.suffix.lower() != ".kicad_sch":
        return {"content": [{"type": "text",
                              "text": "ERROR: 'output' must be a "
                                       ".kicad_sch path"}],
                 "is_error": True}

    cfg = _load_cfg()
    max_components = int(cfg.get("max_combined_components", 30))
    require_shared = bool(cfg.get("require_shared_net", True))
    lateral_gap = float(args.get("lateral_gap_mm",
                                   cfg.get("lateral_gap_mm", 25.4)))

    # 1) Parse every source
    source_paths: List[Path] = []
    source_roots: List[list] = []
    for sp in raw_sources:
        p = Path(str(sp)).expanduser()
        if not p.exists():
            return {"content": [{"type": "text",
                                  "text": f"ERROR: source not found: {p}"}],
                     "is_error": True}
        try:
            root = sexpdata.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"content": [{"type": "text",
                                  "text": (f"ERROR: failed to parse {p}: "
                                            f"{exc}")}],
                     "is_error": True}
        if not isinstance(root, list) or _head(root) != "kicad_sch":
            return {"content": [{"type": "text",
                                  "text": (f"ERROR: {p} is not a "
                                            "kicad_sch file")}],
                     "is_error": True}
        source_paths.append(p)
        source_roots.append(root)

    # 2) Safety: count combined components
    total_comps = 0
    for r in source_roots:
        for c in r[1:]:
            if isinstance(c, list) and _head(c) == "symbol":
                # Skip power-port symbols from the count
                lib = _first_child(c, "lib_id")
                if lib and len(lib) >= 2:
                    if str(lib[1]).lower().startswith("power:"):
                        continue
                total_comps += 1
    if total_comps > max_components:
        return {"content": [{"type": "text",
                              "text": (f"REFUSED: combined sheet would "
                                        f"have {total_comps} components "
                                        f"(> max_combined_components="
                                        f"{max_components}). Keep them as "
                                        "separate hierarchy sheets — that "
                                        "is the canonical EDA convention "
                                        "for designs this size.")}],
                 "is_error": True}

    # 3) Safety: shared-net check
    if require_shared and len(source_roots) >= 2:
        nets_per_source = [_net_names(r) for r in source_roots]
        shared = set.intersection(*nets_per_source) if nets_per_source else set()
        if not shared:
            return {"content": [{"type": "text",
                                  "text": ("REFUSED: the source sheets "
                                            "share no electrical net. "
                                            "Merging unrelated sheets onto "
                                            "one page just clusters them — "
                                            "doesn't actually connect "
                                            "them. Set require_shared_net="
                                            "false in JSON if you really "
                                            "want this anyway.")}],
                     "is_error": True}

    # 4) Ref-collision resolution: rename collisions in source[1..]
    rename_mappings = _rename_collisions(source_roots)
    renamed_total = 0
    for i, m in rename_mappings.items():
        _apply_ref_rename(source_roots[i], m)
        renamed_total += len(m)

    # 5) Build the merged root. Start from source[0] (kept in place).
    merged = sexpdata.loads(sexpdata.dumps(source_roots[0]))  # deep copy
    items_added = 0

    # Each subsequent source gets shifted right by its predecessors' extent
    running_dx = 0.0
    for i, src in enumerate(source_roots[1:], start=1):
        # Compute prior root's extent (the running merged sheet)
        ext = _sheet_extent(merged)
        running_dx = max(running_dx, ext[2]) + lateral_gap
        # Shift source i so its leftmost point is at `running_dx`
        src_ext = _sheet_extent(src)
        dx = running_dx - src_ext[0]
        items_added += _merge_into(merged, src, dx=dx, dy=0.0)

    # 6) Strip any (sheet_instances ...) duplicates and inherit one fresh
    merged_filtered = [merged[0]] + [
        c for c in merged[1:]
        if not (isinstance(c, list) and _head(c) == "sheet_instances")
    ]
    # KiCad requires (sheet_instances ...) — keep the FIRST one from the
    # source-0 root or synthesize a minimal one.
    si = None
    for c in source_roots[0][1:]:
        if isinstance(c, list) and _head(c) == "sheet_instances":
            si = c
            break
    if si is not None:
        merged_filtered.append(si)

    # 7) Write the combined sheet to disk
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(sexpdata.dumps(merged_filtered),
                                  encoding="utf-8")
    except OSError as exc:
        return {"content": [{"type": "text",
                              "text": f"ERROR: cannot write {output_path}: {exc}"}],
                 "is_error": True}

    # 8) Optionally update the parent hierarchy:
    #    (a) remove the (sheet ...) entries pointing at the merged
    #        source files
    #    (b) ADD a new (sheet ...) entry pointing at the combined file,
    #        reusing the FIRST removed sheet's position + size so the
    #        layout stays similar
    parent_path = args.get("parent")
    parent_update_msg = ""
    if parent_path:
        try:
            pp = Path(str(parent_path)).expanduser()
            ptext = pp.read_text(encoding="utf-8")
            proot = sexpdata.loads(ptext)
            source_filenames = {s.name for s in source_paths}
            # Read the combined sheet's UUID so the parent's new (sheet)
            # entry references it correctly (KiCad matches sheet
            # instances by UUID).
            combined_uuid = ""
            try:
                comb_root = sexpdata.loads(
                    output_path.read_text(encoding="utf-8"))
                for c in comb_root[1:]:
                    if (isinstance(c, list) and _head(c) == "uuid"
                            and len(c) >= 2):
                        combined_uuid = str(c[1])
                        break
            except Exception:
                pass
            # Drop (sheet ...) entries whose Sheetfile is in
            # source_filenames; remember the FIRST removed sheet's
            # position+size so we can reuse them for the new combined-
            # sheet entry.
            kept = []
            removed_n = 0
            first_removed_pos = None
            first_removed_size = None
            for c in proot[1:]:
                if isinstance(c, list) and _head(c) == "sheet":
                    sheetfile = ""
                    pos = None
                    size = None
                    for prop in c[1:]:
                        if isinstance(prop, list):
                            ph = _head(prop)
                            if (ph == "property" and len(prop) >= 3
                                    and str(prop[1]) == "Sheetfile"):
                                sheetfile = str(prop[2])
                            elif ph == "at" and len(prop) >= 3:
                                try:
                                    pos = (float(prop[1]), float(prop[2]))
                                except (TypeError, ValueError): pass
                            elif ph == "size" and len(prop) >= 3:
                                try:
                                    size = (float(prop[1]), float(prop[2]))
                                except (TypeError, ValueError): pass
                    if sheetfile in source_filenames:
                        removed_n += 1
                        if first_removed_pos is None and pos is not None:
                            first_removed_pos = pos
                            first_removed_size = size
                        continue
                kept.append(c)
            # Build the NEW (sheet ...) entry for the combined file,
            # appended to the parent's children list. Reuses the first
            # removed sheet's position+size — falls back to (50, 50,
            # 60, 40) when nothing was found.
            if combined_uuid and removed_n > 0:
                np = first_removed_pos or (50.0, 50.0)
                ns = first_removed_size or (60.0, 40.0)
                # Derive a Sheetname from the combined file's stem
                # (e.g. "01_power_can.kicad_sch" -> "POWER_CAN")
                stem = output_path.stem
                stem_no_num = re.sub(r"^\d+_", "", stem)
                sheet_name_display = stem_no_num.upper() or stem
                sheet_uuid = combined_uuid
                # Construct via sexpdata so the format matches the
                # rest of the file. Built as a list-of-lists then
                # serialized by sexpdata.dumps along with the root.
                Sym = sexpdata.Symbol
                new_sheet = [
                    Sym("sheet"),
                    [Sym("at"), float(np[0]), float(np[1])],
                    [Sym("size"), float(ns[0]), float(ns[1])],
                    [Sym("exclude_from_sim"), Sym("no")],
                    [Sym("in_bom"), Sym("yes")],
                    [Sym("on_board"), Sym("yes")],
                    [Sym("dnp"), Sym("no")],
                    [Sym("stroke"),
                     [Sym("width"), 0.0],
                     [Sym("type"), Sym("solid")]],
                    [Sym("fill"), [Sym("color"), 0, 0, 0, 0.0]],
                    [Sym("uuid"), sheet_uuid],
                    [Sym("property"), "Sheetname", sheet_name_display,
                     [Sym("at"), float(np[0]), float(np[1] - 0.508), 0],
                     [Sym("effects"),
                      [Sym("font"), [Sym("size"), 1.524, 1.524]],
                      [Sym("justify"), Sym("left"), Sym("bottom")]]],
                    [Sym("property"), "Sheetfile", output_path.name,
                     [Sym("at"), float(np[0]), float(np[1] + ns[1] + 1.524), 0],
                     [Sym("effects"),
                      [Sym("font"), [Sym("size"), 1.524, 1.524]],
                      [Sym("justify"), Sym("left"), Sym("top")]]],
                    [Sym("instances"),
                     [Sym("project"), "",
                      [Sym("path"), "/" + sheet_uuid, [Sym("page"), "1"]]]],
                ]
                kept.append(new_sheet)
            proot[:] = [proot[0]] + kept
            pp.write_text(sexpdata.dumps(proot), encoding="utf-8")
            parent_update_msg = (
                f"\n  Parent {pp.name} updated: removed {removed_n} "
                f"(sheet ...) entries; "
                f"{'added 1 new entry for the combined sheet' if combined_uuid and removed_n > 0 else 'no new entry added'}.")
        except Exception as exc:
            parent_update_msg = (f"\n  WARN: parent update failed "
                                  f"({type(exc).__name__}: {exc}); combined "
                                  "sheet still written.")

    summary = (
        f"combine_sheets: merged {len(source_paths)} sheets into "
        f"{output_path.name}\n"
        f"  components in merged sheet: {total_comps}\n"
        f"  items copied + offset:       {items_added}\n"
        f"  refdes collisions renamed:  {renamed_total}\n"
        f"  lateral gap:                {lateral_gap} mm"
        f"{parent_update_msg}"
    )
    return {
        "content": [{"type": "text", "text": summary}],
        "ok": True,
        "path": str(output_path),
        "components": total_comps,
        "renamed_refs": renamed_total,
    }
