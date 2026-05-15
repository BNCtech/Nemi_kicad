"""Deterministic Bill of Materials extraction from a .kicad_sch.

Pure-Python: no LLM calls, no supplier APIs. Reads what the schematic actually
declares. If the user populated MPN / Manufacturer properties, they appear in
the row; if not, those columns are blank — never guessed. Procurement-grade
data must come from the user's CAD, not from a model.

All conventions (refdes letter codes, property aliases, sort order, display
widths, sentinel values) live in bom_config.json — never hardcoded here.
Add a new IC family or a new property alias by editing the JSON, not the code.
"""

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import hierarchy
from ._config_loader import load as _load_config
from .schematic_extractor import SchematicExtractor  # kept for direct callers


def _bom_cfg() -> Dict[str, Any]:
    return _load_config("bom_config")


def _conv_cfg() -> Dict[str, Any]:
    return _load_config("conventions")


def _ci_get(props: Dict[str, str], aliases: List[str]) -> str:
    lower = {k.lower(): v for k, v in props.items()}
    for k in aliases:
        v = lower.get(k.lower())
        if v:
            return v
    return ""


def _ref_prefix(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return m.group(1).upper() if m else "?"


def _ref_num(ref: str) -> int:
    m = re.search(r"(\d+)$", ref or "")
    return int(m.group(1)) if m else 0


def _is_power_symbol(lib_id: str, reference: str) -> bool:
    cfg = _conv_cfg()["power_symbol"]
    lib_id = lib_id or ""
    reference = reference or ""
    if any(lib_id.startswith(p) for p in cfg["lib_id_prefixes"]):
        return True
    if any(reference.startswith(p) for p in cfg["reference_prefixes"]):
        return True
    return False


def _refdes_info(ref: str) -> Dict[str, Any]:
    cfg = _conv_cfg()["refdes"]
    prefix = _ref_prefix(ref)
    info = cfg["prefixes"].get(prefix)
    if info:
        return info
    return {"priority": cfg["default_priority"], "category": "other"}


def _is_missing_value(value: str) -> bool:
    sentinels = {s.lower() for s in _conv_cfg()["missing_value_sentinels"]["values"]}
    return (value or "").strip().lower() in sentinels


def extract_rows(schematic_path) -> List[Dict[str, Any]]:
    """Return one row per unique (MPN || value+footprint) part, with refdes list and qty.

    Power-port symbols and parts marked `in_bom no` are excluded — they're not
    real BOM line items. DNP parts are kept but flagged so they appear separately.
    """
    # Walk the WHOLE project (root sheet + all child sheets). For single-sheet
    # files this is identical to the single-file path. For complex hierarchies
    # that reuse the same child sheet from multiple parents, each instance
    # contributes its own physical parts (correct BOM behaviour).
    comps = hierarchy.aggregate_components(schematic_path)

    aliases = _bom_cfg()["property_aliases"]

    # Dedupe physical parts within a single sheet instance: a multi-unit IC
    # (e.g. 74HC125 = 4 gates) appears as multiple symbol instances sharing
    # one Reference INSIDE one sheet. The BOM counts physical packages, so
    # keep the first instance per (sheet, reference). Across different sheet
    # instances the same refdes legitimately denotes different physical chips.
    seen_keys: set = set()
    unique_comps: List[Dict[str, Any]] = []
    for c in comps:
        ref = c.get("reference", "")
        if not ref:
            continue
        key = (c.get("sheet", "/"), ref)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_comps.append(c)
    comps = unique_comps

    groups: Dict[tuple, Dict[str, Any]] = defaultdict(
        lambda: {
            "references": [],
            "value": "",
            "footprint": "",
            "lib_id": "",
            "mpn": "",
            "manufacturer": "",
            "description": "",
            "dnp": False,
            "properties_sample": {},
        }
    )

    for c in comps:
        ref = c.get("reference", "")
        if not ref or _is_power_symbol(c.get("lib_id", ""), ref):
            continue
        if not c.get("in_bom", True):
            continue

        props = c.get("properties", {})
        mpn = _ci_get(props, aliases["mpn"])
        mfr = _ci_get(props, aliases["manufacturer"])
        desc = _ci_get(props, aliases["description"])
        value = c.get("value", "") or ""
        footprint = c.get("footprint", "") or ""
        dnp = bool(c.get("dnp", False))

        # Group key: prefer MPN when present (most specific), else value+footprint.
        # DNP rows are split out so they don't merge with their populated twins.
        key = (mpn or f"{value}|{footprint}", dnp)

        g = groups[key]
        g["references"].append(ref)
        g["value"] = g["value"] or value
        g["footprint"] = g["footprint"] or footprint
        g["lib_id"] = g["lib_id"] or c.get("lib_id", "")
        g["mpn"] = g["mpn"] or mpn
        g["manufacturer"] = g["manufacturer"] or mfr
        g["description"] = g["description"] or desc
        g["dnp"] = g["dnp"] or dnp
        if not g["properties_sample"]:
            g["properties_sample"] = dict(props)

    rows: List[Dict[str, Any]] = []
    for g in groups.values():
        refs_sorted = sorted(g["references"], key=lambda r: (_ref_prefix(r), _ref_num(r)))
        rows.append(
            {
                "qty": len(refs_sorted),
                "references": refs_sorted,
                "value": g["value"],
                "footprint": g["footprint"],
                "lib_id": g["lib_id"],
                "mpn": g["mpn"],
                "manufacturer": g["manufacturer"],
                "description": g["description"],
                "dnp": g["dnp"],
                "properties": g["properties_sample"],
            }
        )

    rows.sort(
        key=lambda r: (
            r["dnp"],
            _refdes_info(r["references"][0])["priority"],
            _ref_prefix(r["references"][0]),
            (r["value"] or "").lower(),
        )
    )
    return rows


def validate_rows(rows: List[Dict[str, Any]], strict_mpn: bool = False) -> List[Dict[str, str]]:
    """Return a list of issues found in the BOM. Each issue: {severity, refs, message}.

    severity: 'critical' (won't manufacture), 'warning' (review), 'info' (FYI).
    strict_mpn: when True, parts in `refdes.strict_mpn_categories` without an MPN
    are reported as critical.
    """
    issues: List[Dict[str, str]] = []
    strict_categories = set(_bom_cfg()["strict_mpn_categories"])

    # Duplicate refdes detection that is hierarchy-aware. A ref appearing N
    # times in ONE row is fine — that's the same physical part replicated by
    # complex-hierarchy sheet reuse (same child .kicad_sch included twice).
    # A ref appearing in TWO DIFFERENT rows is a real bug: two distinct parts
    # have been numbered the same. Dedupe within a row before counting across.
    rows_per_ref: Dict[str, int] = defaultdict(int)
    for r in rows:
        for ref in set(r["references"]):
            rows_per_ref[ref] += 1
    dups = sorted(ref for ref, n in rows_per_ref.items() if n > 1)
    if dups:
        issues.append(
            {"severity": "critical", "refs": ", ".join(dups), "message": "duplicate reference designator"}
        )

    for r in rows:
        refs = _format_refs(r["references"])

        if _is_missing_value(r["value"]):
            issues.append({"severity": "critical", "refs": refs, "message": "missing Value"})

        if not r["footprint"]:
            issues.append({"severity": "warning", "refs": refs, "message": "missing Footprint"})

        info = _refdes_info(r["references"][0])
        if strict_mpn and info["category"] in strict_categories and not r["mpn"]:
            issues.append(
                {"severity": "critical", "refs": refs, "message": "no MPN — cannot procure"}
            )

        if r["dnp"]:
            issues.append({"severity": "info", "refs": refs, "message": "marked DNP (will not be assembled)"})

    return issues


def _format_refs(refs: List[str]) -> str:
    """Render the references list for human/CSV display.

    Collapses sheet-instance reuse: if R201 appears 4 times (one per
    complex-hierarchy instance), show "R201 (×4)" instead of "R201, R201, R201, R201".
    """
    counts: Dict[str, int] = defaultdict(int)
    order: List[str] = []
    for r in refs:
        if r not in counts:
            order.append(r)
        counts[r] += 1
    parts = []
    for r in order:
        n = counts[r]
        parts.append(f"{r} (×{n})" if n > 1 else r)
    return ", ".join(parts)


def _row_to_csv_value(row: Dict[str, Any], field: str) -> str:
    if field == "qty":
        return str(row["qty"])
    if field == "references":
        return _format_refs(row["references"])
    if field == "dnp":
        return "DNP" if row["dnp"] else ""
    return row.get(field, "") or ""


def to_csv(rows: List[Dict[str, Any]], out_path) -> Path:
    """Write a manufacturer-ready CSV. Returns the written path."""
    columns = _bom_cfg()["csv_columns"]
    fields = [k for k in columns.keys() if not k.startswith("_")]
    headers = [columns[k] for k in fields]

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in rows:
            writer.writerow([_row_to_csv_value(r, field) for field in fields])
    return out


def to_text(rows: List[Dict[str, Any]], issues: Optional[List[Dict[str, str]]] = None) -> str:
    """Render the BOM as a printable table."""
    if not rows:
        return "(no BOM rows — schematic has no in_bom components)"

    cfg = _bom_cfg()["text_table"]
    refs_max = int(cfg["references_max_width"])
    foot_max = int(cfg["footprint_max_width"])
    ellipsis = cfg["ellipsis"]

    def _truncate(s: str, n: int) -> str:
        return s if len(s) <= n else s[: max(0, n - len(ellipsis))] + ellipsis

    headers = ["Qty", "References", "Value", "Footprint", "MPN", "Mfr"]
    table = [headers]
    for r in rows:
        refs = _format_refs(r["references"])
        if r["dnp"]:
            refs += "  [DNP]"
        table.append(
            [
                str(r["qty"]),
                _truncate(refs, refs_max),
                r["value"] or "",
                _truncate(r["footprint"] or "", foot_max),
                r["mpn"] or "",
                r["manufacturer"] or "",
            ]
        )

    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    lines = []
    for i, row in enumerate(table):
        line = "  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
        lines.append(line)
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))

    total_lines = sum(r["qty"] for r in rows if not r["dnp"])
    dnp_lines = sum(r["qty"] for r in rows if r["dnp"])
    lines.append("")
    lines.append(f"{len(rows)} unique parts, {total_lines} pieces to assemble"
                 + (f"  ({dnp_lines} DNP)" if dnp_lines else ""))

    if issues:
        lines.append("")
        lines.append("ISSUES:")
        for it in issues:
            lines.append(f"  [{it['severity']}] {it['refs']}: {it['message']}")

    return "\n".join(lines)
