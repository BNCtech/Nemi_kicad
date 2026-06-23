"""Tool: render a .kicad_pcb to 3D PNG views via kicad-cli pcb render.

Generates one image per requested side (default: top + bottom). Pairs
with the SVG schematic preview — together they give the user a full
visual review without opening eeschema / pcbnew. Universal: works on
any .kicad_pcb, no per-circuit logic.

Output files:
  <basename>-3d-top.png
  <basename>-3d-bottom.png

All flags (width, height, quality, sides) come from
`layout_config.json:render_pcb_3d` per feedback_no_hardcode_json_config.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, List

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("render_pcb_3d", {}) or {}
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
    name="render_pcb_3d",
    description=(
        "Render a .kicad_pcb to PNG 3D views via kicad-cli. Default "
        "produces top + bottom PNGs at 1600×900. Useful for fast visual "
        "review during a chat session — no need to open pcbnew.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}             # required\n'
        '  {"pcb_path": "...", "sides": ["top","bottom","front","back","left","right"]}\n'
        '  {"pcb_path": "...", "width": 2400, "height": 1350}    # higher-res\n'
        '  {"pcb_path": "...", "quality": "high"}                # nicer render\n'
        '  {"pcb_path": "...", "perspective": true}              # 3D camera\n'
        "All defaults (sides, dimensions, quality) come from "
        "layout_config.json:render_pcb_3d."
    ),
    input_schema={"pcb_path": str},
)
async def render_pcb_3d(args: dict[str, Any]) -> dict[str, Any]:
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
                          "text": "render_pcb_3d disabled in layout_config.json"}],
            "is_error": True,
        }

    cli = _kicad_cli(cfg)
    timeout = int(cfg.get("timeout_seconds", 90))
    width  = int(args.get("width",  cfg.get("width",  1600)))
    height = int(args.get("height", cfg.get("height", 900)))
    quality = str(args.get("quality", cfg.get("quality", "basic")))
    background = str(args.get("background", cfg.get("background", "opaque")))
    perspective = bool(args.get("perspective", cfg.get("perspective", False)))
    floor = bool(args.get("floor", cfg.get("floor", False)))
    sides: List[str] = list(args.get("sides", cfg.get("sides", ["top", "bottom"])))

    valid_sides = {"top", "bottom", "left", "right", "front", "back"}
    sides = [s.lower() for s in sides if s.lower() in valid_sides]
    if not sides:
        sides = ["top"]

    produced: List[str] = []
    errors: List[str] = []

    for side in sides:
        out_png = pcb_path.with_name(f"{pcb_path.stem}-3d-{side}.png")
        cmd = [
            cli, "pcb", "render",
            "--output", str(out_png),
            "--width", str(width),
            "--height", str(height),
            "--side", side,
            "--quality", quality,
            "--background", background,
        ]
        if perspective:
            cmd.append("--perspective")
        if floor:
            cmd.append("--floor")
        cmd.append(str(pcb_path))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=timeout)
            if r.returncode == 0 and out_png.exists():
                produced.append(str(out_png))
            else:
                err = (r.stderr or "").strip().splitlines()[-1] if r.stderr else "no image"
                errors.append(f"{side}: {err}")
        except subprocess.TimeoutExpired:
            errors.append(f"{side}: timeout after {timeout}s")
        except FileNotFoundError:
            return {"content": [{"type": "text",
                                  "text": f"ERROR: kicad-cli not found: {cli}"}],
                     "is_error": True}

    if not produced:
        return {
            "content": [{"type": "text",
                          "text": "ERROR: no 3D views produced.\n  " + "\n  ".join(errors)}],
            "is_error": True,
        }

    summary = [f"rendered {len(produced)} 3D view(s):"]
    for p in produced:
        summary.append(f"  - {p}")
    if errors:
        summary.append(f"  (skipped {len(errors)}: " + "; ".join(errors) + ")")
    summary.append(f"\n  dimensions: {width}×{height}, quality: {quality}")

    return {
        "content": [{"type": "text", "text": "\n".join(summary)}],
        "ok": True,
        "images": produced,
        "errors": errors,
    }
