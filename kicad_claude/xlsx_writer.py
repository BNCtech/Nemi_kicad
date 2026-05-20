"""Excel export for the BOM. Optional dependency on openpyxl.

Layout / styling / which sheets to include / which columns to render are all
driven by bomdoc_config.json. Nothing about column order, header text, or
fill colours is hardcoded here.

Two sheets when `xlsx.include_summary_sheet` is true:
  - "BOM"     : the line items, columns per the chosen bom_type
  - "Summary" : totals + health scorecard + per-distributor breakdown

Falls back to a clear ImportError message when openpyxl isn't installed —
the CLI catches it and tells the user `pip install openpyxl`.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from . import bom as _bom
from ._config_loader import load as _load_config


def _cfg() -> Dict[str, Any]:
    return _load_config("bomdoc_config")


def write(
    rows: List[Dict[str, Any]],
    out_path,
    issues: Optional[List[Dict[str, str]]] = None,
    scorecard: Optional[Dict[str, Any]] = None,
    bom_type: str = "procurement",
) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise ImportError(
            "Excel export needs openpyxl. Install with: pip install openpyxl"
        ) from e

    cfg = _cfg()["xlsx"]
    fields = _bom._columns_for(bom_type)
    headers = [_bom._header_for(f) for f in fields]

    wb = Workbook()
    ws = wb.active
    ws.title = cfg.get("sheet_name", "BOM")

    header_font = Font(bold=bool(cfg.get("header_bold", True)))
    header_fill = PatternFill("solid", fgColor=cfg.get("header_fill", "FFEFEFEF"))

    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="left", vertical="center")

    crit_fill = PatternFill("solid", fgColor="FFFFE6E6")
    warn_fill = PatternFill("solid", fgColor="FFFFF5E6")
    dnp_font = Font(italic=True, color="FF888888")

    crit_refs = {it["refs"] for it in (issues or []) if it.get("severity") == "critical"}
    warn_refs = {it["refs"] for it in (issues or []) if it.get("severity") == "warning"}

    for r in rows:
        row_values = [_bom._row_field(r, f) for f in fields]
        ws.append(row_values)
        excel_row = ws.max_row
        refs_str = _bom._format_refs(r["references"])
        if refs_str in crit_refs:
            for c in range(1, len(headers) + 1):
                ws.cell(row=excel_row, column=c).fill = crit_fill
        elif refs_str in warn_refs:
            for c in range(1, len(headers) + 1):
                ws.cell(row=excel_row, column=c).fill = warn_fill
        if r.get("dnp"):
            for c in range(1, len(headers) + 1):
                ws.cell(row=excel_row, column=c).font = dnp_font

    for i, _ in enumerate(headers, start=1):
        col = get_column_letter(i)
        max_len = max(
            (len(str(ws.cell(row=row, column=i).value or "")) for row in range(1, ws.max_row + 1)),
            default=10,
        )
        ws.column_dimensions[col].width = min(max(10, max_len + 2), 60)

    if cfg.get("freeze_header", True):
        ws.freeze_panes = "A2"
    if cfg.get("auto_filter", True):
        ws.auto_filter.ref = ws.dimensions

    if cfg.get("include_summary_sheet", True) and scorecard:
        ws2 = wb.create_sheet("Summary")
        ws2["A1"] = "Health Scorecard"
        ws2["A1"].font = Font(bold=True, size=14)
        rowi = 3
        for k in (
            "lines_total", "lines_active",
            "pct_with_mpn", "pct_with_mfr", "pct_with_price",
            "pct_safe_source", "lifecycle_warn", "lifecycle_crit",
            "total_unit_cost", "total_ext_cost", "currency",
        ):
            v = scorecard.get(k)
            if v is None:
                continue
            ws2.cell(row=rowi, column=1, value=k.replace("_", " ").title())
            ws2.cell(row=rowi, column=2, value=v)
            rowi += 1

        rowi += 1
        ws2.cell(row=rowi, column=1, value="Lifecycle breakdown").font = Font(bold=True)
        rowi += 1
        for k, v in (scorecard.get("lifecycle_breakdown") or {}).items():
            ws2.cell(row=rowi, column=1, value=k)
            ws2.cell(row=rowi, column=2, value=v)
            rowi += 1

        rowi += 1
        ws2.cell(row=rowi, column=1, value="By distributor").font = Font(bold=True)
        rowi += 1
        for k, v in (scorecard.get("by_distributor") or {}).items():
            ws2.cell(row=rowi, column=1, value=k)
            ws2.cell(row=rowi, column=2, value=v)
            rowi += 1

        if issues:
            rowi += 1
            ws2.cell(row=rowi, column=1, value="Issues").font = Font(bold=True)
            rowi += 1
            ws2.cell(row=rowi, column=1, value="Severity").font = Font(bold=True)
            ws2.cell(row=rowi, column=2, value="Refs").font = Font(bold=True)
            ws2.cell(row=rowi, column=3, value="Message").font = Font(bold=True)
            rowi += 1
            for it in issues:
                ws2.cell(row=rowi, column=1, value=it["severity"])
                ws2.cell(row=rowi, column=2, value=it["refs"])
                ws2.cell(row=rowi, column=3, value=it["message"])
                rowi += 1
        for col_letter, width in (("A", 28), ("B", 28), ("C", 80)):
            ws2.column_dimensions[col_letter].width = width

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out
