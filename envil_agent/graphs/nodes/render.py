"""Render + preview nodes.

`render_node` calls the deterministic engine and returns `stats`.
After the engine writes the .kicad_sch, `render_node` dispatches a
post-render repair pass (`lint.repair.repair_after_render`) that
applies wiring-rule mutations gated by `wiring_rules.*` flags in
layout_config.json. Currently covers R4 (split 4-way junctions) and
R9 (emit bus segments for IR-declared buses).

`preview_node` is best-effort SVG export of the parent + every child
sheet; failures here are non-fatal (the .kicad_sch is already on disk).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from ...intent.engine import (
    _export_preview_svg,
    _load_layout_config,
    render as engine_render,
)
from ...lint.repair import repair_after_render
from ...settings import out_dir as _out_dir


def render_node(state: Dict[str, Any]) -> Dict[str, Any]:
    ir = state["ir"]
    explicit_out_path = state.get("out_path", "").strip()
    out_dir = state.get("out_dir", "").strip() or str(_out_dir())
    force_hierarchy = bool(state.get("force_hierarchy", False))
    force_single_sheet = bool(state.get("force_single_sheet", False))

    # --- Post-architect layout re-decision (the 3-way choose-layout core) -----
    # 'Choose layout' runs BEFORE the architect, so it can only GUESS the mode
    # from PROMPT KEYWORDS -- and that guess is unstable: the SAME "design a
    # complete CAN logger" prompt scored complexity 69 -> HIERARCHY on one run
    # and 54 -> FLAT on the next, flipping the layout for an identical board.
    # Now that the architect has emitted the REAL IR we re-pick the 3-way
    # (FLAT / SINGLE_SHEET_BLOCKS / HIERARCHY) from the ACTUAL block count, which
    # is the ground truth the prompt was only proxying for. Thresholds come
    # straight from the engine's own sheet_decision config so the node verdict
    # and the engine never diverge. Block-count driven (not component-count) so
    # a HIERARCHY pick always HAS the blocks to split into sheets. Only fires on
    # an AUTO decision -- an explicit user "single sheet" / "hierarchy" request,
    # or an agent force flag, is honoured untouched.
    _decision = str(state.get("render_mode_decision") or "")
    _override = None
    _sd = _load_layout_config().get("sheet_decision", {}) or {}
    if _sd.get("post_architect_relayout", True) and "explicit" not in _decision.lower():
        _max_blk = int(_sd.get("single_sheet_max_blocks", 5))
        _min_blk = int(_sd.get("hierarchy_min_blocks", 3))
        _n_blk = len(getattr(ir, "blocks", []) or [])
        _n_comp = len(getattr(ir, "components", []) or [])
        if _n_blk > _max_blk:
            _new_h, _new_s, _tier = True, False, "HIERARCHY"
        elif _n_blk >= _min_blk:
            _new_h, _new_s, _tier = False, True, "SINGLE_SHEET_BLOCKS"
        else:
            _new_h, _new_s, _tier = False, False, "FLAT"
        if (_new_h, _new_s) != (force_hierarchy, force_single_sheet):
            force_hierarchy, force_single_sheet = _new_h, _new_s
            _override = (
                f"post-architect re-decision -> {_tier} from ACTUAL IR: "
                f"blocks={_n_blk}, components={_n_comp} "
                f"(SINGLE_SHEET_BLOCKS if blocks>={_min_blk}, HIERARCHY if "
                f"blocks>{_max_blk}). Prompt-based pre-decision was [{_decision}]"
            )

    # Cursor-style user-chosen project name (gated project_naming.propose_in_preview).
    # When the agent passed a project_name -- the name the user accepted or typed in
    # the build preview -- AND we are NOT overwriting an open file, rename the circuit
    # so the folder, .kicad_sch basename, child sheets and title block all follow the
    # user's choice instead of the architect's auto ir.name. The existing safe_name
    # sanitiser below then slugs it for the path. No project_name (or flag off) ->
    # ir.name untouched -> byte-identical to the fully-automatic build.
    _proj_name = (state.get("project_name") or "").strip()
    if _proj_name and not explicit_out_path:
        try:
            _pn_cfg = _load_layout_config().get("project_naming", {}) or {}
            if _pn_cfg.get("propose_in_preview", True):
                ir.name = _proj_name
        except Exception:
            ir.name = _proj_name

    if explicit_out_path:
        target = Path(explicit_out_path)
        circuit_out_dir = target.parent
    else:
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", ir.name).strip("_") or "circuit"
        # When the architect declares blocks AND we're forced single-sheet,
        # output one file in out_dir directly (not a per-circuit subfolder)
        # to match the flat-single-sheet path's convention.
        if force_single_sheet:
            circuit_out_dir = Path(out_dir)
        else:
            circuit_out_dir = Path(out_dir) / safe_name if ir.blocks else Path(out_dir)

    try:
        stats = engine_render(
            ir, circuit_out_dir,
            force_hierarchy=force_hierarchy,
            force_single_sheet=force_single_sheet,
        )
    except Exception as exc:
        return {"error": f"render failed: {type(exc).__name__}: {exc}"}

    # Overwrite the user's open file when out_path was supplied so eeschema
    # auto-refresh hits the right path. Mirrors what build_circuit did
    # before the graph refactor.
    if explicit_out_path:
        generated_path = Path(stats["path"])
        target = Path(explicit_out_path)
        if generated_path.exists() and generated_path != target:
            try:
                target.write_text(
                    generated_path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                generated_path.unlink()
                stats["path"] = str(target)
                target_pro = target.with_suffix(".kicad_pro")
                src_pro = generated_path.with_suffix(".kicad_pro")
                if not target_pro.exists() and src_pro.exists():
                    target_pro.write_text(
                        src_pro.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                if src_pro.exists() and src_pro != target_pro:
                    src_pro.unlink(missing_ok=True)
                # Mirror the .kicad_pro handling for the empty companion
                # .kicad_pcb that _write_kicad_pro emits next to the
                # generated schematic. Without this it was orphaned in the
                # temp folder: copy it to the target only when the target
                # has no board yet (never clobber a real laid-out PCB),
                # then delete the generated stub so no stray empty board
                # is left behind.
                target_pcb = target.with_suffix(".kicad_pcb")
                src_pcb = generated_path.with_suffix(".kicad_pcb")
                if not target_pcb.exists() and src_pcb.exists():
                    target_pcb.write_text(
                        src_pcb.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                if src_pcb.exists() and src_pcb != target_pcb:
                    src_pcb.unlink(missing_ok=True)
                stats["project"] = (str(target_pro) if target_pro.exists()
                                    else stats.get("project", ""))
            except OSError as exc:
                return {"error": f"failed to overwrite {target}: {exc}"}

    # Post-render repair pass (gated by wiring_rules.* in layout_config).
    # Runs on the parent sheet and each child sheet so hierarchical
    # outputs get the same treatment. Repair summary is folded into
    # stats so the chat reply can surface what was mutated.
    repair_summaries: List[Dict[str, Any]] = []
    try:
        parent_path = Path(stats.get("path", ""))
        if parent_path.exists():
            r = repair_after_render(parent_path, ir=ir)
            if r.get("ran") or r.get("skipped"):
                repair_summaries.append({"path": str(parent_path), **r})
        for c in stats.get("children", []) or []:
            cpath = c.get("path", "")
            if not cpath:
                continue
            cp = Path(cpath)
            if not cp.exists():
                continue
            r = repair_after_render(cp, ir=ir)
            if r.get("ran") or r.get("skipped"):
                repair_summaries.append({"path": str(cp), **r})
    except Exception as exc:
        # Repair is advisory --- never fail the render because of it.
        repair_summaries.append({
            "path": stats.get("path", ""),
            "error": f"{type(exc).__name__}: {exc}",
        })
    if repair_summaries:
        stats["repairs"] = repair_summaries

    # Surface the FINAL layout tier (and any post-architect override reason) so
    # the trace's draw step shows which of the 3 modes actually rendered, not
    # just the pre-architect guess.
    _final_tier = ("HIERARCHY" if force_hierarchy
                   else "SINGLE_SHEET_BLOCKS" if force_single_sheet
                   else "FLAT")
    result: Dict[str, Any] = {"stats": stats, "layout_final": _final_tier}
    if _override is not None:
        result["render_mode_decision"] = _override
        result["force_hierarchy"] = force_hierarchy
        result["force_single_sheet"] = force_single_sheet
    return result


def preview_node(state: Dict[str, Any]) -> Dict[str, Any]:
    stats = state.get("stats") or {}
    paths: List[str] = []
    try:
        parent_svg = _export_preview_svg(Path(stats["path"]))
        if parent_svg:
            paths.append(str(parent_svg))
        for c in stats.get("children", []):
            cpath = c.get("path", "")
            if not cpath:
                continue
            child_svg = _export_preview_svg(Path(cpath))
            if child_svg:
                paths.append(str(child_svg))
    except Exception:
        paths = []
    return {"preview_svgs": paths}
