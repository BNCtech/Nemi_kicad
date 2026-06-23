"""Tool: export a Bill of Materials (BOM) CSV from a .kicad_sch via
kicad-cli. Universal — works on any schematic, no per-circuit logic.

NAMED PRESETS — pass `preset` arg to pick the column shape:
  - kicad_default : matches what eeschema's Tools->Generate BOM dialog
                    produces by default (Reference, Qty, Value, DNP,
                    Exclude from BOM, Exclude from Board, Footprint,
                    Datasheet)
  - altium        : professional fab BOM (S.No, Name, Designator,
                    Description, Manufacturer, MPN, Footprint, Quantity)
  - minimal       : 3 columns (Refs, Value, Qty) — quick part count
  - verbose       : every KiCad field + custom Manufacturer/MPN columns
  - fab_with_pricing : altium + empty Supplier Unit Price / Subtotal

Output: `<basename>-bom.csv` next to the input .kicad_sch. The parts
house (Mouser / Digikey / LCSC) accepts the file directly for quoting.

All preset shapes (fields, labels, grouping, delimiters) come from
`layout_config.json:bom_export.presets.<name>`. Edit JSON to add a
new preset or modify an existing one; never edit this file."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("bom_export", {}) or {}
    except Exception:
        return {}


def _kicad_cli(cfg: Dict[str, Any]) -> str:
    if cfg.get("kicad_cli_command"):
        return str(cfg["kicad_cli_command"])
    try:
        from ..intent.engine import _load_layout_config
        return str(_load_layout_config().get("erc_check", {}).get(
            "kicad_cli_command", "kicad-cli"))
    except Exception:
        return "kicad-cli"


@tool(
    name="export_bom",
    description=(
        "Export a Bill of Materials CSV from a .kicad_sch. Pick a "
        "named preset to control column shape:\n"
        "  kicad_default - matches eeschema's Tools->Generate BOM dialog\n"
        "  altium        - professional fab BOM (S.No, Designator, MPN, ...)\n"
        "  minimal       - 3-column quick summary\n"
        "  verbose       - every field including custom Mfr/MPN/Description\n"
        "  fab_with_pricing - altium + empty Supplier Unit Price columns\n"
        "Args:\n"
        '  {"sch_path": "C:/.../proj.kicad_sch"}                # required\n'
        '  {"sch_path": "...", "preset": "kicad_default"}       # column shape\n'
        '  {"sch_path": "...", "preset": "altium"}              # fab format\n'
        '  {"sch_path": "...", "exclude_dnp": true}             # skip DNP\n'
        '  {"sch_path": "...", "output": "C:/x/custom.csv"}     # path override\n'
        "All preset shapes live in layout_config.json:bom_export.presets — "
        "add a new preset by editing JSON, no Python changes."
    ),
    input_schema={"sch_path": str},
)
async def export_bom(args: dict[str, Any]) -> dict[str, Any]:
    sch_path = Path(str(args.get("sch_path", "")).strip()).expanduser()
    if not sch_path.exists():
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: .kicad_sch not found: {sch_path}"}],
            "is_error": True,
        }
    if sch_path.suffix.lower() != ".kicad_sch":
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: expected .kicad_sch, got {sch_path.suffix}"}],
            "is_error": True,
        }

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {
            "content": [{"type": "text",
                          "text": "export_bom disabled in layout_config.json"}],
            "is_error": True,
        }

    cli = _kicad_cli(cfg)
    timeout = int(cfg.get("timeout_seconds", 30))

    # Pick preset — call arg wins over default; fall back to flat keys
    # if presets section is absent (old config compatibility).
    presets = cfg.get("presets") or {}
    preset_name = str(args.get("preset") or cfg.get("default_preset", "kicad_default"))
    preset = presets.get(preset_name) if presets else None
    if preset is None and presets:
        # Caller asked for an unknown preset — fall back to default and
        # mention the available names.
        preset = presets.get(cfg.get("default_preset", "kicad_default"), {})
    if preset is None:
        preset = cfg  # legacy flat-key config

    out_path = args.get("output") or str(
        sch_path.with_name(sch_path.stem + "-bom.csv"))
    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    fields = str(preset.get("fields",
        "Reference,Value,Footprint,${QUANTITY},${DNP}"))
    labels = str(preset.get("labels", "Refs,Value,Footprint,Qty,DNP"))
    group_by = str(preset.get("group_by", "Value,Footprint"))
    sort_field = str(preset.get("sort_field", "Value"))
    field_delim = str(preset.get("field_delimiter", ","))
    string_delim = str(preset.get("string_delimiter", '"'))
    ref_delim = str(preset.get("ref_delimiter", ","))
    ref_range_delim = str(preset.get("ref_range_delimiter", "-"))
    exclude_dnp = bool(args.get("exclude_dnp", preset.get("exclude_dnp", False)))

    cmd = [
        cli, "sch", "export", "bom",
        "--output", str(out_p),
        "--fields", fields,
        "--labels", labels,
        "--sort-field", sort_field,
        "--field-delimiter", field_delim,
        "--string-delimiter", string_delim,
        "--ref-delimiter", ref_delim,
        "--ref-range-delimiter", ref_range_delim,
    ]
    if group_by:
        cmd.extend(["--group-by", group_by])
    if exclude_dnp:
        cmd.append("--exclude-dnp")
    cmd.append(str(sch_path))

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        ok = (r.returncode == 0) and out_p.exists()
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": "ERROR: BOM export timed out"}],
                 "is_error": True}
    except FileNotFoundError:
        return {"content": [{"type": "text",
                              "text": f"ERROR: kicad-cli not found: {cli}"}],
                 "is_error": True}

    if not ok:
        err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "no output produced"
        return {"content": [{"type": "text",
                              "text": f"ERROR: BOM export failed (exit={r.returncode}): {err}"}],
                 "is_error": True}

    # Post-process: append config-driven blank columns (price, subtotal etc.)
    # so the BOM matches the fab/assembly house template the user uploaded.
    extra_cols = list(preset.get("extra_blank_columns", []))
    if extra_cols:
        try:
            text_lines = out_p.read_text(encoding="utf-8").splitlines()
            if text_lines:
                # Detect line separator from the header line (CSV format
                # used the configured field_delimiter)
                sep = field_delim
                # Quoted blank cell to keep the row width consistent
                empty_cell = f"{string_delim}{string_delim}"
                new_lines = []
                for i, line in enumerate(text_lines):
                    if i == 0:
                        suffix = sep + sep.join(
                            f"{string_delim}{c}{string_delim}" for c in extra_cols)
                    else:
                        suffix = sep + sep.join(empty_cell for _ in extra_cols)
                    new_lines.append(line + suffix)
                out_p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        except Exception:
            pass  # blank columns are cosmetic; never fail the export

    # Count rows so the user gets a quick part-count summary
    row_count = 0
    try:
        with out_p.open("r", encoding="utf-8") as f:
            row_count = sum(1 for _ in f) - 1  # subtract header
            row_count = max(row_count, 0)
    except Exception:
        pass

    return {
        "content": [{"type": "text",
                      "text": (f"BOM exported -> {out_p}\n"
                                f"  distinct parts: {row_count}\n"
                                f"  grouped by: {group_by}\n"
                                f"  fields: {labels}")}],
        "ok": True,
        "path": str(out_p),
        "rows": row_count,
    }
