"""Bill of Materials extraction from a .kicad_sch — Altium-class.

Layered design (each layer optional, controlled by extract_rows kwargs):

  1. Schematic   — pure: refdes, value, footprint, custom properties.
  2. Library     — pulls Description / Datasheet / Footprint / keywords from
                   the .kicad_sym defs the schematic references. Free.
  3. BomDoc      — overlays user overrides + cached AI resolution from
                   <project>.bomdoc.json (Altium-style ActiveBOM persistence).
  4. AI resolve  — Claude fills missing MPN / Manufacturer / Price / Lifecycle
                   per unique part, cached forever in the bomdoc.
  5. Variants    — KiCad assembly variants (or the variants section of
                   bomdoc_config.json) drop refs that aren't fitted in this
                   build.
  6. Quantity    — applies the quantity-break tiers from bomdoc_config to
                   compute per-line and total ext price for an order qty.

All conventions (refdes letters, property aliases, sort order, display
widths, sentinel values, manufacturer-host map, AI prompt, distributor list,
quantity tiers, lifecycle states, column layouts per BOM type) live in JSON
config — bom_config.json + bomdoc_config.json + conventions.json. Nothing
hardcoded here.
"""

import csv
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import bomdoc as _bomdoc
from . import hierarchy
from . import lib_resolver
from ._config_loader import load as _load_config
from .schematic_extractor import SchematicExtractor  # kept for direct callers


def _bom_cfg() -> Dict[str, Any]:
    return _load_config("bom_config")


def _bomdoc_cfg() -> Dict[str, Any]:
    return _load_config("bomdoc_config")


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


def extract_rows(
    schematic_path,
    enrich_from_lib: bool = True,
    bomdoc_overlay: Optional[Dict[str, Any]] = None,
    variant: str = "default",
) -> List[Dict[str, Any]]:
    """Return one row per unique (MPN || value+footprint) part, with refdes list and qty.

    Power-port symbols and parts marked `in_bom no` are excluded — they're not
    real BOM line items. DNP parts are kept but flagged so they appear separately.

    enrich_from_lib: when True (default), each component is augmented with the
        Description / Datasheet / Footprint / keywords carried by its source
        .kicad_sym definition. Schematic instance values always win — only
        blanks are filled.
    bomdoc_overlay: a loaded bomdoc dict from `bomdoc.load(...)`. When given,
        per-line user overrides + cached AI resolutions are merged onto each
        row after grouping.
    variant: name of an assembly variant to filter by. The default variant
        keeps every non-DNP part. Other variants pull dnp_refs from
        bomdoc_config.json:variants.
    """
    project_dir = str(Path(schematic_path).parent)

    comps = hierarchy.aggregate_components(schematic_path)

    if enrich_from_lib:
        comps = [lib_resolver.enrich_component(c, project_dir) for c in comps]

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
            "datasheet": "",
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
        ds_raw = props.get("Datasheet", "") or props.get("datasheet", "") or ""
        datasheet = "" if _is_missing_value(ds_raw) else ds_raw
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
        g["datasheet"] = g["datasheet"] or datasheet
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
                "datasheet": g["datasheet"],
                "dnp": g["dnp"],
                "properties": g["properties_sample"],
                # Procurement fields filled by bomdoc / AI later. Always present
                # so downstream renderers can read them blindly. Lifecycle is
                # init blank (not "Unknown") so bomdoc.merge_into_row's truthiness
                # check lets a resolved lifecycle fill in. Display layers fall
                # back to "Unknown" when still blank.
                "preferred_distributor": "",
                "unit_price": None,
                "currency": "",
                "ext_price": None,
                "lifecycle": "",
                "alternates": [],
                "notes": "",
            }
        )

    if bomdoc_overlay is not None:
        for r in rows:
            _bomdoc.merge_into_row(bomdoc_overlay, r)

    rows = _apply_variant(rows, variant)

    rows.sort(
        key=lambda r: (
            r["dnp"],
            _refdes_info(r["references"][0])["priority"],
            _ref_prefix(r["references"][0]),
            (r["value"] or "").lower(),
        )
    )
    return rows


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

def _apply_variant(rows: List[Dict[str, Any]], variant: str) -> List[Dict[str, Any]]:
    """Apply a build variant: drop refs listed under variants[<name>].dnp_refs,
    re-flagging the row as DNP when ALL its refs are dropped, otherwise just
    removing the dropped refs from the references list and decrementing qty."""
    variants = _bomdoc_cfg().get("variants", {})
    spec = variants.get(variant)
    if not spec:
        return rows
    dnp_set = set(spec.get("dnp_refs") or [])
    if not dnp_set:
        return rows

    out: List[Dict[str, Any]] = []
    for r in rows:
        kept = [ref for ref in r["references"] if ref not in dnp_set]
        if not kept:
            r2 = dict(r)
            r2["dnp"] = True
            out.append(r2)
            continue
        if len(kept) == len(r["references"]):
            out.append(r)
            continue
        r2 = dict(r)
        r2["references"] = kept
        r2["qty"] = len(kept)
        out.append(r2)
    return out


# ---------------------------------------------------------------------------
# AI enrichment (Altium-class procurement fields)
# ---------------------------------------------------------------------------

def enrich_with_ai(
    rows: List[Dict[str, Any]],
    schematic_path,
    doc: Dict[str, Any],
    *,
    force_refresh: bool = False,
    only_missing: bool = True,
    progress=None,
) -> List[Dict[str, Any]]:
    """Fill MPN / Manufacturer / Price / Lifecycle / Alternates per row using
    the Claude-backed PartResolver, caching results into `doc` (the bomdoc).

    `progress` is an optional callable `(idx, total, row) -> None` for status
    UI in the CLI. Imported lazily so the BOM core has zero hard dependency
    on the resolver / claude client.
    """
    from .part_resolver import PartResolver  # lazy: keeps import cost off the fast path

    project_dir = str(Path(schematic_path).parent)
    resolver = PartResolver(project_dir=project_dir)

    total = len(rows)
    for i, r in enumerate(rows):
        if progress:
            try:
                progress(i, total, r)
            except Exception:
                pass

        if only_missing and not force_refresh and r.get("mpn") and r.get("manufacturer") \
                and r.get("unit_price") is not None and r.get("lifecycle") not in ("", "Unknown"):
            continue

        lib_props: Dict[str, str] = {}
        if r.get("lib_id"):
            res = lib_resolver.resolve(r["lib_id"], project_dir)
            lib_props = res.get("properties", {})

        resolved = resolver.resolve_and_cache(doc, r, lib_props, force=force_refresh)

        if resolved.get("mpn") and not r.get("mpn"):
            r["mpn"] = resolved["mpn"]
        if resolved.get("manufacturer") and not r.get("manufacturer"):
            r["manufacturer"] = resolved["manufacturer"]
        if resolved.get("preferred_distributor"):
            r["preferred_distributor"] = resolved["preferred_distributor"]
        if resolved.get("unit_price_usd") is not None:
            r["unit_price"] = float(resolved["unit_price_usd"])
            r["currency"] = _bomdoc_cfg()["distributors"]["currency"]
        if resolved.get("lifecycle"):
            r["lifecycle"] = resolved["lifecycle"]
        if resolved.get("alternates"):
            r["alternates"] = resolved["alternates"]
        if resolved.get("notes"):
            r["notes"] = resolved["notes"]
        r["_ai_confidence"] = resolved.get("confidence", "")

    return rows


# ---------------------------------------------------------------------------
# Quantity breaks
# ---------------------------------------------------------------------------

def apply_quantity_breaks(
    rows: List[Dict[str, Any]],
    boards: int = 1,
) -> List[Dict[str, Any]]:
    """Compute ext_price for each row given an order of `boards` units.

    Per-line order qty = row.qty * boards (excluding DNP). The discount tier
    used is the highest tier whose min_qty <= order qty. Pure local — no API.
    Edit bomdoc_config.json:quantity_breaks.tiers to retune.
    """
    tiers = sorted(
        _bomdoc_cfg()["quantity_breaks"]["tiers"],
        key=lambda t: int(t["min_qty"]),
    )

    def discount_for(qty: int) -> float:
        applicable = [t for t in tiers if int(t["min_qty"]) <= qty]
        return float(applicable[-1]["discount"]) if applicable else 0.0

    for r in rows:
        if r.get("dnp"):
            r["ext_price"] = 0.0
            r["order_qty"] = 0
            r["discount"] = 0.0
            continue
        qty = int(r["qty"]) * max(1, int(boards))
        r["order_qty"] = qty
        unit = r.get("unit_price")
        if unit is None:
            r["ext_price"] = None
            r["discount"] = 0.0
            continue
        d = discount_for(qty)
        r["discount"] = d
        r["ext_price"] = round(qty * float(unit) * (1.0 - d), 4)
    return rows


# ---------------------------------------------------------------------------
# Health scorecard
# ---------------------------------------------------------------------------

def health_scorecard(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate metrics for the BOM. Mirrors what Altium ActiveBOM shows at
    the top of the document. All thresholds come from bomdoc_config.json."""
    cfg = _bomdoc_cfg()
    states_warn = set(cfg["lifecycle"]["warn_states"])
    states_crit = set(cfg["lifecycle"]["critical_states"])
    min_alts = int(cfg["validation"]["min_alternates_for_safe"])

    n = len(rows)
    n_active = sum(1 for r in rows if not r.get("dnp"))
    n_with_mpn = sum(1 for r in rows if r.get("mpn"))
    n_with_price = sum(1 for r in rows if r.get("unit_price") is not None)
    n_with_mfr = sum(1 for r in rows if r.get("manufacturer"))
    n_safe_source = sum(
        1 for r in rows
        if len(r.get("alternates") or []) >= min_alts or r.get("mpn")
    )
    n_warn_lc = sum(1 for r in rows if r.get("lifecycle") in states_warn)
    n_crit_lc = sum(1 for r in rows if r.get("lifecycle") in states_crit)
    total_unit_cost = sum(
        float(r.get("unit_price") or 0.0) * int(r["qty"])
        for r in rows if not r.get("dnp")
    )
    total_ext = sum(
        float(r.get("ext_price") or 0.0) for r in rows if not r.get("dnp")
    )

    by_distributor: Dict[str, int] = defaultdict(int)
    for r in rows:
        d = r.get("preferred_distributor") or ""
        if d and not r.get("dnp"):
            by_distributor[d] += int(r["qty"])

    by_lifecycle: Dict[str, int] = defaultdict(int)
    for r in rows:
        by_lifecycle[r.get("lifecycle") or "Unknown"] += 1
    # Make sure scorecard never reports "" — coerce blank to "Unknown" for display
    if "" in by_lifecycle:
        by_lifecycle["Unknown"] = by_lifecycle.pop("")

    def pct(num: int, denom: int) -> float:
        return round(100.0 * num / denom, 1) if denom else 0.0

    return {
        "lines_total":      n,
        "lines_active":     n_active,
        "pct_with_mpn":     pct(n_with_mpn, n),
        "pct_with_mfr":     pct(n_with_mfr, n),
        "pct_with_price":   pct(n_with_price, n),
        "pct_safe_source":  pct(n_safe_source, n),
        "lifecycle_warn":   n_warn_lc,
        "lifecycle_crit":   n_crit_lc,
        "lifecycle_breakdown": dict(by_lifecycle),
        "by_distributor":      dict(by_distributor),
        "total_unit_cost":     round(total_unit_cost, 4),
        "total_ext_cost":      round(total_ext, 4),
        "currency":            cfg["distributors"]["currency"],
    }


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

        # Procurement health (only fires when AI/bomdoc enrichment has run —
        # rows without these fields populated are simply skipped).
        bcfg = _bomdoc_cfg()
        warn_lc = set(bcfg["lifecycle"]["warn_states"])
        crit_lc = set(bcfg["lifecycle"]["critical_states"])
        lc = r.get("lifecycle") or ""
        if lc in crit_lc:
            issues.append({"severity": "critical", "refs": refs,
                           "message": f"lifecycle = {lc}; will not be available"})
        elif lc in warn_lc:
            issues.append({"severity": "warning", "refs": refs,
                           "message": f"lifecycle = {lc}; consider redesign"})

        if bcfg["validation"]["single_source_is_warning"]:
            if r.get("mpn") and not (r.get("alternates") or []):
                issues.append({"severity": "warning", "refs": refs,
                               "message": "single-source — no alternates listed"})

        if bcfg["validation"]["low_confidence_is_warning"]:
            if r.get("_ai_confidence") == "low":
                issues.append({"severity": "warning", "refs": refs,
                               "message": "AI low-confidence MPN — verify before ordering"})

        if bcfg["validation"]["missing_price_is_warning"]:
            if r.get("mpn") and r.get("unit_price") is None and not r.get("dnp"):
                issues.append({"severity": "info", "refs": refs,
                               "message": "no price — costed at $0 in totals"})

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


def _row_field(row: Dict[str, Any], field: str) -> str:
    """Stringify a row field for tabular output. Knows about the procurement
    fields too (price, ext_price, alternates, lifecycle, etc.)."""
    if field == "qty":
        return str(row["qty"])
    if field == "references":
        return _format_refs(row["references"])
    if field == "dnp":
        return "DNP" if row.get("dnp") else ""
    if field == "alternates":
        return ", ".join(row.get("alternates") or [])
    if field == "unit_price":
        v = row.get("unit_price")
        return "" if v is None else f"{float(v):.4f}"
    if field == "ext_price":
        v = row.get("ext_price")
        return "" if v is None else f"{float(v):.2f}"
    if field == "order_qty":
        v = row.get("order_qty")
        return "" if v is None else str(v)
    if field == "discount":
        v = row.get("discount")
        return "" if v in (None, 0, 0.0) else f"{float(v) * 100:.0f}%"
    return str(row.get(field, "") or "")


def _columns_for(bom_type: str) -> List[str]:
    """Resolve the column list for a BOM type, falling back to the full set
    declared in bom_config.json:csv_columns when the type is unknown."""
    if not bom_type:
        bom_type = "procurement"
    types = _bomdoc_cfg().get("bom_types", {})
    if bom_type in types:
        return list(types[bom_type])
    base = _bom_cfg()["csv_columns"]
    return [k for k in base.keys() if not k.startswith("_")]


def _header_for(field: str) -> str:
    base = _bom_cfg()["csv_columns"]
    if field in base and not field.startswith("_"):
        return base[field]
    extras = _bomdoc_cfg().get("extra_columns", {})
    if field in extras:
        return extras[field]
    return field.replace("_", " ").title()


def to_csv(
    rows: List[Dict[str, Any]],
    out_path,
    bom_type: str = "procurement",
) -> Path:
    """Write a CSV with the column subset for the requested bom_type.
    Column keys + order are fully data-driven from JSON config.
    """
    fields = _columns_for(bom_type)
    headers = [_header_for(f) for f in fields]

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in rows:
            writer.writerow([_row_field(r, field) for field in fields])
    return out


def to_json(
    rows: List[Dict[str, Any]],
    issues: Optional[List[Dict[str, str]]] = None,
    scorecard: Optional[Dict[str, Any]] = None,
) -> str:
    """Programmatic JSON export. Includes everything — every field on every
    row, all issues, and the scorecard. Suitable for ERP / downstream tools."""
    import json as _json
    payload = {
        "rows": rows,
        "issues": issues or [],
        "scorecard": scorecard or {},
    }
    return _json.dumps(payload, indent=2, default=str)


def to_xml(
    rows: List[Dict[str, Any]],
    bom_type: str = "procurement",
) -> str:
    """Minimal XML export — one <Component> per row, fields per the bom_type
    column set. No external dependency."""
    from xml.sax.saxutils import escape
    fields = _columns_for(bom_type)
    parts = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<BOM>"]
    for r in rows:
        parts.append("  <Component>")
        for field in fields:
            tag = _header_for(field).replace(" ", "")
            val = escape(_row_field(r, field))
            parts.append(f"    <{tag}>{val}</{tag}>")
        parts.append("  </Component>")
    parts.append("</BOM>")
    return "\n".join(parts)


def to_html(
    rows: List[Dict[str, Any]],
    issues: Optional[List[Dict[str, str]]] = None,
    scorecard: Optional[Dict[str, Any]] = None,
    bom_type: str = "procurement",
) -> str:
    """Self-contained HTML report. CSS is inline (from bomdoc_config) so the
    file is portable — open it anywhere, no external assets."""
    from html import escape
    cfg = _bomdoc_cfg()["html"]
    fields = _columns_for(bom_type)
    headers = [_header_for(f) for f in fields]

    crit_refs = {it["refs"] for it in (issues or []) if it.get("severity") == "critical"}
    warn_refs = {it["refs"] for it in (issues or []) if it.get("severity") == "warning"}

    out = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        f"<title>{escape(cfg['title'])}</title>",
        f"<style>{cfg['css']}</style>",
        "</head><body>",
        f"<h1>{escape(cfg['title'])}</h1>",
        f"<small class='muted'>BOM type: {escape(bom_type)} &middot; "
        f"{len(rows)} unique line items</small>",
    ]

    if cfg.get("include_scorecard") and scorecard:
        out.append("<h2>Health</h2>")
        out.append("<div class='scorecard'>")
        for k, label in [
            ("pct_with_mpn", "% with MPN"),
            ("pct_with_mfr", "% with Mfr"),
            ("pct_with_price", "% with Price"),
            ("pct_safe_source", "% safe sourcing"),
            ("lifecycle_warn", "Lifecycle warn"),
            ("lifecycle_crit", "Lifecycle critical"),
        ]:
            v = scorecard.get(k)
            cls = ""
            if k == "lifecycle_crit" and v:
                cls = "crit"
            elif k == "lifecycle_warn" and v:
                cls = "warn"
            elif isinstance(v, (int, float)) and k.startswith("pct_") and v >= 90:
                cls = "ok"
            out.append(f"<div><b>{label}</b><br><span class='{cls}'>{escape(str(v))}</span></div>")
        if "total_ext_cost" in scorecard:
            out.append(
                f"<div><b>Total ext cost</b><br>{escape(str(scorecard['total_ext_cost']))} "
                f"{escape(str(scorecard.get('currency','')))}</div>"
            )
        out.append("</div>")

    out.append("<h2>Items</h2><table><thead><tr>")
    out.extend(f"<th>{escape(h)}</th>" for h in headers)
    out.append("</tr></thead><tbody>")
    for r in rows:
        refs_str = _format_refs(r["references"])
        cls = "dnp" if r.get("dnp") else ""
        if refs_str in crit_refs:
            cls = "crit"
        elif refs_str in warn_refs:
            cls = (cls + " warn").strip()
        out.append(f"<tr class='{cls}'>")
        for field in fields:
            out.append(f"<td>{escape(_row_field(r, field))}</td>")
        out.append("</tr>")
    out.append("</tbody></table>")

    if issues:
        out.append("<h2>Issues</h2><table><thead><tr><th>Severity</th><th>Refs</th><th>Message</th></tr></thead><tbody>")
        for it in issues:
            cls = "crit" if it["severity"] == "critical" else ("warn" if it["severity"] == "warning" else "")
            out.append(
                f"<tr class='{cls}'><td>{escape(it['severity'])}</td>"
                f"<td>{escape(it['refs'])}</td><td>{escape(it['message'])}</td></tr>"
            )
        out.append("</tbody></table>")

    out.append("</body></html>")
    return "\n".join(out)


def to_text_report(
    rows: List[Dict[str, Any]],
    issues: Optional[List[Dict[str, str]]] = None,
    scorecard: Optional[Dict[str, Any]] = None,
    bom_type: str = "procurement",
) -> str:
    """Printable BOM table for the terminal, with the procurement scorecard
    appended below. Falls back to the legacy `to_text` 6-column layout when
    bom_type is 'schematic' or rows have no procurement fields."""
    base = to_text(rows, issues)
    if not scorecard or not _bomdoc_cfg()["report"].get("show_total_ext_cost", True):
        return base

    rcfg = _bomdoc_cfg()["report"]
    lines = [base, "", "HEALTH SCORECARD:"]
    if rcfg.get("show_health_pct", True):
        lines.append(
            f"  MPN coverage:        {scorecard.get('pct_with_mpn', 0)}%   "
            f"Mfr: {scorecard.get('pct_with_mfr', 0)}%   "
            f"Price: {scorecard.get('pct_with_price', 0)}%   "
            f"Safe sourcing: {scorecard.get('pct_safe_source', 0)}%"
        )
    if rcfg.get("show_lifecycle_breakdown", True):
        lines.append(f"  Lifecycle:           {dict(scorecard.get('lifecycle_breakdown', {}))}")
    if rcfg.get("show_per_distributor", True):
        lines.append(f"  By distributor:      {dict(scorecard.get('by_distributor', {}))}")
    if rcfg.get("show_total_unit_cost", True):
        lines.append(
            f"  Total unit cost:     "
            f"{scorecard.get('total_unit_cost', 0)} {scorecard.get('currency', '')}"
        )
    if rcfg.get("show_total_ext_cost", True):
        lines.append(
            f"  Total ext cost:      "
            f"{scorecard.get('total_ext_cost', 0)} {scorecard.get('currency', '')} "
            f"(after quantity discounts)"
        )
    return "\n".join(lines)


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
