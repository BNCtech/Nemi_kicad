"""Tool: export PCB manufacturing files via kicad-cli.

Generates the standard fab+assembly bundle from a .kicad_pcb:
  - Gerbers (one .gbr per layer + .gbrjob)
  - Drill files (.drl, Excellon format)
  - Pick-and-place position file (.csv) for assembly
  - Optional: PDF assembly drawing + STEP 3D model

Universal — works on ANY .kicad_pcb path. All flags + layer selections
live in `layout_config.json:pcb_export` so per-project tuning needs
zero code changes (per user rule feedback_no_hardcode_json_config).

Output: a `gerbers/` subfolder next to the input .kicad_pcb so the
user has one folder to ZIP for the fab house.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    """Read pcb_export section from layout_config.json. Returns {} on
    failure so downstream `.get(..., default)` keeps working."""
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("pcb_export", {})
    except Exception:
        return {}


def _kicad_cli(cfg: Dict[str, Any]) -> str:
    # Falls back to erc_check.kicad_cli_command if pcb_export doesn't set
    # one — single edit point for the kicad-cli path.
    if cfg.get("kicad_cli_command"):
        return str(cfg["kicad_cli_command"])
    try:
        from ..intent.engine import _load_layout_config
        return str(_load_layout_config().get("erc_check", {}).get(
            "kicad_cli_command", "kicad-cli"))
    except Exception:
        return "kicad-cli"


def _run(cmd: List[str], timeout: int) -> Dict[str, Any]:
    """Run a subprocess, return {ok, returncode, stdout, stderr}. Never
    raises — wraps everything so the tool result is always serialisable."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                            timeout=timeout)
        return {
            "ok": r.returncode == 0,
            "returncode": r.returncode,
            "stdout": r.stdout[-2000:] if r.stdout else "",
            "stderr": r.stderr[-2000:] if r.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": "timeout"}
    except FileNotFoundError:
        return {"ok": False, "returncode": -1, "stdout": "",
                 "stderr": f"executable not found: {cmd[0]}"}
    except OSError as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _zip_dir(src: Path, zip_path: Path) -> bool:
    """Make a .zip of the gerber folder so the user has one drag-to-fab
    artefact. Returns True on success."""
    try:
        base = zip_path.with_suffix("")
        shutil.make_archive(str(base), "zip", str(src))
        return zip_path.exists()
    except Exception:
        return False


@tool(
    name="export_pcb",
    description=(
        "Export PCB manufacturing files from a .kicad_pcb. Produces "
        "Gerbers (all copper + silkscreen + mask + paste + edge cuts + "
        "drawing layers), Excellon drill files, and a CSV pick-and-place "
        "file inside a `gerbers/` folder next to the input .kicad_pcb. "
        "Optionally also produces a PDF assembly drawing and STEP 3D model.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}        # required\n'
        '  {"pcb_path": "...", "include_step": true}    # 3D model\n'
        '  {"pcb_path": "...", "include_pdf": true}     # assembly drawing\n'
        '  {"pcb_path": "...", "zip": true}             # also create .zip\n'
        "All defaults (layers, drill format, units) come from "
        "layout_config.json:pcb_export — no per-circuit hardcoding."
    ),
    input_schema={"pcb_path": str},
)
async def export_pcb(args: dict[str, Any]) -> dict[str, Any]:
    pcb_path = Path(str(args.get("pcb_path", "")).strip()).expanduser()
    if not pcb_path.exists():
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: .kicad_pcb not found: {pcb_path}"}],
            "is_error": True,
        }
    if pcb_path.suffix.lower() != ".kicad_pcb":
        return {
            "content": [{"type": "text",
                          "text": f"ERROR: expected .kicad_pcb, got {pcb_path.suffix}"}],
            "is_error": True,
        }

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {
            "content": [{"type": "text",
                          "text": "export_pcb disabled in layout_config.json"}],
            "is_error": True,
        }

    cli = _kicad_cli(cfg)
    timeout = int(cfg.get("timeout_seconds", 60))
    layers = str(cfg.get("gerber_layers",
        "F.Cu,B.Cu,F.Paste,B.Paste,F.Silkscreen,B.Silkscreen,"
        "F.Mask,B.Mask,Edge.Cuts,F.Courtyard,B.Courtyard,F.Fab,B.Fab"))
    drill_format = str(cfg.get("drill_format", "excellon"))
    drill_units = str(cfg.get("drill_units", "mm"))
    pos_units = str(cfg.get("pos_units", "mm"))
    pos_format = str(cfg.get("pos_format", "csv"))

    include_step = bool(args.get("include_step", cfg.get("include_step", False)))
    include_pdf = bool(args.get("include_pdf", cfg.get("include_pdf", False)))
    want_zip = bool(args.get("zip", cfg.get("zip", True)))

    out_dir = pcb_path.parent / "gerbers"
    out_dir.mkdir(parents=True, exist_ok=True)

    results: List[Dict[str, Any]] = []

    # 1) Gerbers
    r = _run([
        cli, "pcb", "export", "gerbers",
        "--output", str(out_dir),
        "--layers", layers,
        str(pcb_path),
    ], timeout)
    results.append({"step": "gerbers", **r})

    # 2) Drill
    r = _run([
        cli, "pcb", "export", "drill",
        "--output", str(out_dir),
        "--format", drill_format,
        "--drill-origin", "absolute",
        "--excellon-units", drill_units,
        str(pcb_path),
    ], timeout)
    results.append({"step": "drill", **r})

    # 3) Pick-and-place
    pos_out = out_dir / (pcb_path.stem + "-pos.csv")
    r = _run([
        cli, "pcb", "export", "pos",
        "--output", str(pos_out),
        "--format", pos_format,
        "--units", pos_units,
        str(pcb_path),
    ], timeout)
    results.append({"step": "pos", **r})

    # 4) Optional: PDF assembly drawing
    if include_pdf:
        pdf_out = out_dir / (pcb_path.stem + "-assembly.pdf")
        r = _run([
            cli, "pcb", "export", "pdf",
            "--output", str(pdf_out),
            "--layers", "F.Silkscreen,F.Fab,Edge.Cuts",
            str(pcb_path),
        ], timeout)
        results.append({"step": "pdf", **r})

    # 5) Optional: STEP 3D
    if include_step:
        step_out = out_dir / (pcb_path.stem + ".step")
        r = _run([
            cli, "pcb", "export", "step",
            "--output", str(step_out),
            "--subst-models",
            str(pcb_path),
        ], timeout)
        results.append({"step": "step", **r})

    # 6) Optional: zip everything for one-click fab upload
    zip_path = None
    if want_zip:
        zip_path = pcb_path.parent / (pcb_path.stem + "-gerbers.zip")
        ok = _zip_dir(out_dir, zip_path)
        results.append({"step": "zip", "ok": ok,
                         "path": str(zip_path) if ok else ""})

    listing = sorted([str(p) for p in out_dir.iterdir() if p.is_file()])

    summary_lines = [f"PCB export → {out_dir}"]
    for r in results:
        label = r["step"]
        if r.get("ok"):
            summary_lines.append(f"  ✓ {label}")
        else:
            err = (r.get("stderr") or "").strip().splitlines()[-1] if r.get("stderr") else "failed"
            summary_lines.append(f"  ✗ {label}: {err}")
    if zip_path and zip_path.exists():
        summary_lines.append(f"  → ZIP: {zip_path}")
    summary_lines.append(f"  files: {len(listing)}")

    return {
        "content": [{"type": "text", "text": "\n".join(summary_lines)}],
        "ok": all(r.get("ok") for r in results),
        "output_dir": str(out_dir),
        "files": listing,
        "zip_path": str(zip_path) if (zip_path and zip_path.exists()) else "",
        "results": results,
    }
