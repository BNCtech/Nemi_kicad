"""Tool: after-every-PCB-mutation verifier.

Composes drc_check + render_pcb_3d into one tool call so the agent can
verify a board change with a single action and the user always sees
the post-change state in chat.

This is NOT the same as ship_design (which is the FINAL fab-readiness
finisher: ERC + BOM + DRC + Gerbers + 3D). pcb_verify is the LIGHT
in-loop check the agent fires after touching the board — fast (DRC +
render, no Gerber export), low-noise (one card), idempotent.

Universal — every config in `layout_config.json:pcb_verify`. Tool
delegates to the existing drc_check / render_pcb_3d tools; no new
kicad-cli wrapping here.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from claude_agent_sdk import tool


def _load_cfg() -> Dict[str, Any]:
    try:
        from ..intent.engine import _load_layout_config
        return _load_layout_config().get("pcb_verify", {}) or {}
    except Exception:
        return {}


def _step_enabled(cfg: Dict[str, Any], name: str, default: bool = True) -> bool:
    sub = (cfg.get("steps", {}) or {}).get(name, {})
    if isinstance(sub, dict):
        return bool(sub.get("enabled", default))
    return bool(sub) if sub is not None else default


def _step_strict(cfg: Dict[str, Any], name: str) -> bool:
    sub = (cfg.get("steps", {}) or {}).get(name, {})
    return bool(sub.get("strict", False)) if isinstance(sub, dict) else False


def _check_silkscreen_on_pad(pcb_path: Path) -> Optional[List[str]]:
    """Walk the .kicad_pcb, return a list of human-readable strings
    describing silkscreen items (text / line / poly on F.SilkS or
    B.SilkS) whose bbox overlaps a pad in the same footprint.

    Returns None if the file can't be parsed. Pure structural check —
    catches the worst offenders (text label written ON a pad). Does
    NOT do sub-mm geometry; it's a fast first-pass before sending
    boards to fab. Per [feedback_no_hardcode_json_config]: layer names
    come from JSON.
    """
    try:
        import sexpdata  # local import — pcb_verify shouldn't fail to
                          # import when sexpdata is missing.
        from ..intent.engine import _load_layout_config
    except Exception:
        return None
    try:
        text = pcb_path.read_text(encoding="utf-8")
        root = sexpdata.loads(text)
    except Exception:
        return None
    if not isinstance(root, list):
        return None
    silk_cfg = (_load_layout_config().get("pcb_verify", {})
                 .get("steps", {}).get("silkscreen", {})) or {}
    silk_layers = set(silk_cfg.get("layers", ["F.SilkS", "B.SilkS"]))
    pad_inflate_mm = float(silk_cfg.get("pad_inflate_mm", 0.05))

    def _head_of(node):
        if isinstance(node, list) and node:
            h = node[0]
            return getattr(h, "value", lambda: h)() if hasattr(h, "value") else str(h)
        return None

    def _layer_of(node):
        for c in node[1:]:
            if isinstance(c, list) and _head_of(c) == "layer":
                if len(c) >= 2:
                    return str(c[1]).strip('"')
        return ""

    def _at_of(node):
        for c in node[1:]:
            if isinstance(c, list) and _head_of(c) == "at":
                try:
                    return float(c[1]), float(c[2])
                except Exception:
                    return 0.0, 0.0
        return 0.0, 0.0

    def _size_of_pad(pad):
        for c in pad[1:]:
            if isinstance(c, list) and _head_of(c) == "size":
                try:
                    return float(c[1]), float(c[2])
                except Exception:
                    return 0.0, 0.0
        return 0.0, 0.0

    def _bbox_overlap(b1, b2):
        return not (b1[2] < b2[0] or b1[0] > b2[2]
                    or b1[3] < b2[1] or b1[1] > b2[3])

    violations: List[str] = []
    for fp in root[1:]:
        if not (isinstance(fp, list) and _head_of(fp) == "footprint"):
            continue
        fp_x, fp_y = _at_of(fp)
        # Get reference for error message
        ref = ""
        for c in fp[1:]:
            if (isinstance(c, list) and _head_of(c) == "property"
                    and len(c) >= 3 and str(c[1]).strip('"') == "Reference"):
                ref = str(c[2]).strip('"')
                break
        pad_bboxes: List[tuple] = []
        silk_items: List[tuple] = []
        for c in fp[1:]:
            if not isinstance(c, list):
                continue
            head = _head_of(c)
            if head == "pad":
                px, py = _at_of(c)
                pw, ph = _size_of_pad(c)
                pad_bboxes.append((
                    fp_x + px - pw / 2 - pad_inflate_mm,
                    fp_y + py - ph / 2 - pad_inflate_mm,
                    fp_x + px + pw / 2 + pad_inflate_mm,
                    fp_y + py + ph / 2 + pad_inflate_mm,
                ))
            elif head in ("fp_text", "fp_line", "fp_poly"):
                layer = _layer_of(c)
                if layer not in silk_layers:
                    continue
                # Rough bbox for fp_text using its position; for line/poly
                # we use a small region around its `(at ...)` or origin.
                tx, ty = _at_of(c)
                silk_items.append((head, layer,
                                    (fp_x + tx - 1.0, fp_y + ty - 0.5,
                                     fp_x + tx + 1.0, fp_y + ty + 0.5)))
        for (kind, layer, sb) in silk_items:
            for pb in pad_bboxes:
                if _bbox_overlap(sb, pb):
                    violations.append(f"{ref}: {kind} on {layer} over pad")
                    break
    return violations


@tool(
    name="pcb_verify",
    description=(
        "Verify a .kicad_pcb after a mutation: runs DRC + renders 3D, "
        "returns one compact card with error/warning counts and "
        "screenshot paths. Use this AFTER any PCB-mutating tool "
        "(auto_place_pcb, auto_outline_pcb, auto_zones_pcb, "
        "auto_mounting_holes_pcb, auto_fiducials_pcb, "
        "auto_thermal_vias_pcb, set_design_rules, drc_autofix) so the "
        "user sees the post-change state without typing.\n"
        "Args:\n"
        '  {"pcb_path": "C:/.../proj.kicad_pcb"}        # required\n'
        '  {"pcb_path": "...", "skip_render": true}      # DRC only (faster)\n'
        '  {"pcb_path": "...", "strict": true}           # fail on any DRC err\n'
        "All defaults in layout_config.json:pcb_verify. NOT the same as "
        "ship_design — pcb_verify is the in-loop quick check, "
        "ship_design is the final fab-readiness gate."
    ),
    input_schema={"pcb_path": str},
)
async def pcb_verify(args: dict[str, Any]) -> dict[str, Any]:
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
                          "text": "ERROR: expected .kicad_pcb"}],
            "is_error": True,
        }

    cfg = _load_cfg()
    if not cfg.get("enabled", True):
        return {
            "content": [{"type": "text",
                          "text": "pcb_verify disabled in layout_config.json"}],
            "is_error": True,
        }

    do_drc    = _step_enabled(cfg, "drc", True) and not args.get("skip_drc")
    do_render = _step_enabled(cfg, "render", True) and not args.get("skip_render")
    strict    = bool(args.get("strict", _step_strict(cfg, "drc")))
    tpl       = cfg.get("card_template", {}) or {}

    # Lazy-import so a broken sub-tool doesn't break this tool's import
    DRC = importlib.import_module("envil_agent.tools.drc_check")
    R3D = importlib.import_module("envil_agent.tools.render_pcb_3d")

    lines: List[str] = [f"# Verify — {pcb_path.name}"]
    stages: List[Dict[str, Any]] = []
    overall_ok = True

    # --- DRC ---
    drc_payload: Optional[Dict[str, Any]] = None
    if do_drc:
        r = await DRC.drc_check.handler({"pcb_path": str(pcb_path)})
        # Three outcomes:
        #   (1) is_error=true       — DRC tool itself failed (kicad-cli
        #                             missing, report unreadable, etc.).
        #                             Mark stage as "unavailable", NOT
        #                             "VERIFY FAILED" — the design isn't
        #                             broken, the tool is.
        #   (2) error_count >= 0    — DRC ran. Use the numbers.
        #   (3) error_count == -1   — defensive: old code path. Treat as
        #                             unavailable too rather than reporting
        #                             nonsense "-1 errors" to chat.
        if r.get("is_error"):
            err_text = str(r.get("content", [{}])[0]
                            .get("text", "DRC unavailable")
                            ).splitlines()[0][:200]
            lines.append("  — DRC unavailable: " + err_text)
            stages.append({"name": "drc", "ok": None,
                           "detail": "unavailable",
                           "parse_error": r.get("parse_error", "")})
            drc_payload = {"error_count": None, "warning_count": None,
                           "unavailable": True,
                           "reason": err_text,
                           "issues": []}
            # Do NOT flip overall_ok here — tool failure is a separate
            # signal from design failure.
        else:
            err = int(r.get("error_count", 0))
            warn = int(r.get("warning_count", 0))
            if err < 0 or warn < 0:
                # Backwards-compat sentinel from older drc_check returns.
                lines.append("  — DRC unavailable (report could not be "
                              "parsed; older kicad-cli or schema drift)")
                stages.append({"name": "drc", "ok": None,
                               "detail": "unavailable_sentinel"})
                drc_payload = {"error_count": None, "warning_count": None,
                               "unavailable": True,
                               "reason": "legacy -1 sentinel",
                               "issues": []}
            else:
                drc_payload = {"error_count": err, "warning_count": warn,
                               "issues": r.get("issues", [])[:20]}
                clean = (err == 0)
                if strict and err > 0:
                    overall_ok = False
                if clean:
                    text = (tpl.get("drc_clean",
                                     "DRC clean (0 errors, {w} warnings)")
                            .format(w=warn))
                else:
                    text = (tpl.get("drc_errors",
                                    "DRC: {n} errors, {w} warnings")
                            .format(n=err, w=warn))
                lines.append(("  ✓ " if clean else "  ✗ ") + text)
                stages.append({"name": "drc", "ok": clean,
                               "error_count": err, "warning_count": warn})
                # Surface a couple of issue titles for hints.
                for issue in (r.get("issues") or [])[:3]:
                    desc = str(issue.get("description", "")).strip().splitlines()[0]
                    sev = str(issue.get("severity", "?"))
                    if desc:
                        lines.append(f"      [{sev}] {desc[:120]}")
    else:
        stages.append({"name": "drc", "ok": None, "detail": "skipped"})

    # --- Silkscreen-on-pad check (R2.4) ---
    # Per rule "Avoid silkscreen on pads". Walks every footprint, looks
    # for fp_text / fp_line / fp_poly on F.SilkS or B.SilkS whose
    # bounding box overlaps the bbox of any same-footprint pad. Reports
    # a count + a few examples. JSON-gated (steps.silkscreen.enabled).
    silk_cfg = (cfg.get("steps", {}) or {}).get("silkscreen", {}) or {}
    if silk_cfg.get("enabled", True) and not args.get("skip_silkscreen"):
        violations = _check_silkscreen_on_pad(pcb_path)
        if violations is None:
            lines.append("  — silkscreen check unavailable (parse failed)")
            stages.append({"name": "silkscreen", "ok": None,
                            "detail": "parse_failed"})
        elif not violations:
            lines.append("  ✓ silkscreen clear of pads")
            stages.append({"name": "silkscreen", "ok": True,
                            "violations": 0})
        else:
            ok = False
            if bool(silk_cfg.get("strict", False)):
                overall_ok = False
            lines.append(f"  ✗ silkscreen on pads: {len(violations)} item(s)")
            for v in violations[:3]:
                lines.append(f"      {v}")
            stages.append({"name": "silkscreen", "ok": False,
                            "violations": len(violations),
                            "examples": violations[:10]})

    # --- 3D render ---
    render_images: List[str] = []
    if do_render:
        r = await R3D.render_pcb_3d.handler({"pcb_path": str(pcb_path)})
        render_images = list(r.get("images") or [])
        ok = bool(render_images)
        if not ok:
            overall_ok = False
        if render_images:
            lines.append("  ✓ " + tpl.get("render_done",
                                           "rendered top + bottom (3D)"))
            for p in render_images:
                lines.append(f"      {Path(p).name}")
        else:
            err_list = (r.get("errors") or []) or [str(r.get("content", [{}])[0].get("text", ""))[:120]]
            lines.append("  ✗ render failed: " + "; ".join(err_list))
        stages.append({"name": "render", "ok": ok, "images": render_images})
    else:
        lines.append("  — " + tpl.get("render_skipped", "render skipped"))
        stages.append({"name": "render", "ok": None, "detail": "skipped"})

    lines.append("")
    lines.append("**VERIFY OK**" if overall_ok else "**VERIFY FAILED**")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}],
        "ok": overall_ok,
        "pcb_path": str(pcb_path),
        "drc": drc_payload,
        "images": render_images,
        "stages": stages,
    }
