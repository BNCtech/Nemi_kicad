"""End-to-end CLI pipeline for the universal layout engine.

Chains Steps 1-6 (graph -> classifier -> placer -> router -> label_placer
-> emitter -> quality + optional hierarchical splitter) behind one entry
point:

  python -m ai_backend.kicad_layout.pipeline \
      --input  circuit.kicad_sch \
      --output ./out/

Writes intermediate artifacts (`graph.json`, `classified.json`,
`placement.json`, `routed.json`) to `--output` along with the generated
`circuit_layout.kicad_sch` and `quality.json`.

Programmatic callers use `api.run_layout(...)` instead of the CLI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import load_config
from . import classifier as _classifier
from . import placer as _placer
from . import router as _router
from . import label_placer as _label_placer
from . import emitter as _emitter
from . import quality as _quality
from . import hierarchical as _hierarchical
from . import ipc_notify as _ipc_notify
from .connectivity_graph import build_graph, graph_to_dict


def _write_json(obj: Any, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return str(path)


def run_layout(
    input_path,
    output_dir,
    *,
    skip_hierarchical: bool = False,
    skip_quality: bool = False,
    auto_display: bool = True,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Run the full pipeline. Returns a result dict with artifact paths +
    quality summary. Non-zero quality errors do NOT raise — the caller
    decides what to do based on result["quality"]["totals"]."""
    in_p = Path(input_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_config("layout_config")

    def _log(msg: str) -> None:
        if verbose:
            sys.stderr.write(msg + "\n")

    _log(f"[1/6] graph    : {in_p}")
    graph = graph_to_dict(build_graph(in_p))
    graph_path = _write_json(graph, out_dir / "graph.json")

    _log(f"[2/6] classify : {graph_path}")
    classified = _classifier.classify(graph)
    classified_path = _write_json(classified, out_dir / "classified.json")

    _log(f"[3/6] place    : {classified_path}")
    placement = _placer.place(classified)
    # P10 — pin-role aware placement post-pass. Moves 2-pin satellite
    # passives (decaps, pullups, biasing R) to sit adjacent to the
    # specific anchor IC pin they connect to (instead of wherever the
    # grid placer dropped them inside the role bucket).
    prp_cfg = cfg.get("pin_role_placement") or {}
    if prp_cfg.get("enabled", True):
        from . import pin_role_placement as _prp
        prp_stats = _prp.refine_placement_by_pin_role(
            placement, classified, in_p,
            offset_from_pin_mm=float(prp_cfg.get("offset_from_pin_mm", 5.08)),
            max_passive_move_mm=float(prp_cfg.get("max_passive_move_mm", 25.4)),
            min_clearance_mm=float(prp_cfg.get("min_clearance_mm", 1.27)),
        )
        _log(f"[3b/6] pin-role : moved={prp_stats['satellites_moved']} "
             f"xtal={prp_stats.get('crystals_centered', 0)} "
             f"diff={prp_stats.get('diff_pairs_aligned', 0)} "
             f"skip_no_anchor={prp_stats['satellites_skipped_no_anchor']} "
             f"skip_too_far={prp_stats['satellites_skipped_too_far']} "
             f"skip_collision={prp_stats['satellites_skipped_collision']}")
        placement["_pin_role_stats"] = prp_stats
    # P15.4 — regulator chain layout. Snap each regulator's input
    # bulk caps + protection + output bulk caps + ferrite onto a
    # consistent axis next to the regulator so the chain reads as a
    # canonical VIN→protection→regulator→bulk→ferrite line. Runs after
    # pin_role_placement so the regulator's own position is already
    # locked.
    rc_cfg = cfg.get("regulator_chain_layout") or {}
    if rc_cfg.get("enabled", True):
        from . import power_intent as _pi
        try:
            _tree = _pi.build_power_tree(in_p, classified=classified)
            rc_stats = _pi.refine_regulator_chains(
                placement, _tree, in_p,
                chain_spacing_mm=float(rc_cfg.get("chain_spacing_mm", 7.62)),
                max_chain_move_mm=float(rc_cfg.get("max_chain_move_mm", 25.4)),
                min_clearance_mm=float(rc_cfg.get("min_clearance_mm", 1.27)),
            )
            _log(f"[3c/6] reg-chain: chains={rc_stats['regulator_chains']} "
                 f"moved={rc_stats['chain_components_moved']} "
                 f"skip_too_far={rc_stats['chain_skipped_too_far']} "
                 f"skip_collision={rc_stats['chain_skipped_collision']}")
            placement["_regulator_chain_stats"] = rc_stats
        except Exception as _exc:  # pragma: no cover
            _log(f"[3c/6] reg-chain: skipped ({_exc})")
    placement_path = _write_json(placement, out_dir / "placement.json")

    _log(f"[4/6] route    : {placement_path}")
    routed = _router.route(placement, in_p)
    routed_path = _write_json(routed, out_dir / "routed.json")

    _log(f"[5/6] labels   : {routed_path}")
    routed = _label_placer.place_labels(routed, placement, in_p)

    # P11 — local-label-to-wire promotion. For label-named nets whose
    # pins are within `max_distance_mm` of each other, replace the
    # labels with direct Manhattan wires. Long-haul nets (3V3 / SDA /
    # SCL crossing the whole sheet) keep their labels. This shifts the
    # wire-edges:label-edges ratio toward wires — the visible reason
    # human schematics don't look like floating-label spaghetti.
    lw_cfg = (cfg.get("local_wire_synthesis") or {})
    if lw_cfg.get("enabled", True):
        from . import local_wire_synthesis as _lws
        lw_stats = _lws.promote_local_labels_to_wires(
            routed, placement, in_p,
            max_distance_mm=float(lw_cfg.get("max_distance_mm", 80.0)),
            min_pins_per_net=int(lw_cfg.get("min_pins_per_net", 2)),
        )
        _log(f"[5b/6] local-wires: promoted={lw_stats['nets_promoted']} "
             f"wires_added={lw_stats['wires_added']} "
             f"labels_removed={lw_stats['labels_removed']} "
             f"bus_lanes={lw_stats.get('bus_lanes_aligned', 0)}")
        routed["_local_wire_synthesis"] = lw_stats

    _write_json(routed, out_dir / "routed.json")  # overwrite with resolved labels

    out_sch = out_dir / f"{in_p.stem}_layout.kicad_sch"
    _log(f"[6/6] emit     : {out_sch}")
    emit_stats = _emitter.emit(placement, routed, in_p, out_sch)

    hierarchical_result: Optional[Dict[str, Any]] = None
    h_cfg = cfg.get("hierarchical_split") or {}
    if (not skip_hierarchical
            and h_cfg.get("enabled", False)
            and _hierarchical.should_split(placement, h_cfg,
                                             classified=classified,
                                             routed=routed)):
        _log("[opt ] hierarchical split")
        split = _hierarchical.split_placement(
            placement, routed,
            classified=classified, source_schematic=str(in_p),
        )
        parent_path = out_dir / f"{in_p.stem}_top.kicad_sch"
        children_dir = out_dir / "sheets"
        hierarchical_result = _hierarchical.emit_hierarchical(
            split, in_p, parent_path, children_dir,
        )

    quality_result: Optional[Dict[str, Any]] = None
    if not skip_quality:
        _log(f"[ +  ] quality  : {out_sch}")
        quality_result = _quality.run_quality(placement, routed, out_sch)
        _quality.write_quality_json(quality_result, out_dir / "quality.json")

    # Tell eeschema to display the new sheet. Best-effort — if the chat
    # server isn't running, the file is still on disk and the user can
    # open it manually. If hierarchical mode produced a parent sheet,
    # open THAT (the parent is the entry point); else open the flat sheet.
    notify_target = (hierarchical_result or {}).get("parent_path") or str(out_sch)
    notify_result: Optional[Dict[str, Any]] = None
    if auto_display:
        _log(f"[ +  ] display  : {notify_target}")
        notify_result = _ipc_notify.notify_eeschema(notify_target)

    return {
        "input":  str(in_p),
        "output_dir": str(out_dir),
        "artifacts": {
            "graph":      graph_path,
            "classified": classified_path,
            "placement":  placement_path,
            "routed":     routed_path,
            "schematic":  str(out_sch),
        },
        "emit":         emit_stats,
        "hierarchical": hierarchical_result,
        "quality":      quality_result,
        "auto_display": notify_result,
    }


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="kicad_layout.pipeline")
    ap.add_argument("--input",  required=True, help="source .kicad_sch")
    ap.add_argument("--output", required=True, help="output directory")
    ap.add_argument("--skip-hierarchical", action="store_true",
                    help="force flat output even when split threshold is met")
    ap.add_argument("--skip-quality", action="store_true",
                    help="skip post-emit quality checks")
    ap.add_argument("--no-auto-display", action="store_true",
                    help="don't ask the chat server to open the generated "
                         "schematic in eeschema")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    result = run_layout(
        args.input, args.output,
        skip_hierarchical=args.skip_hierarchical,
        skip_quality=args.skip_quality,
        auto_display=not args.no_auto_display,
        verbose=args.verbose,
    )
    print(f"wrote {result['artifacts']['schematic']}")
    if result.get("hierarchical"):
        h = result["hierarchical"]
        print(f"  hierarchical: parent={h['parent_path']}  "
              f"children={h['stats']['children']}  "
              f"cross_sheet_nets={h['stats']['cross_sheet_nets']}")
    if result.get("quality"):
        totals = result["quality"]["totals"]
        print(f"  quality:  errors={totals.get('error', 0)}  "
              f"warnings={totals.get('warning', 0)}  "
              f"info={totals.get('info', 0)}")
    if result.get("auto_display"):
        ad = result["auto_display"]
        if ad.get("ok"):
            print(f"  display:  ok (eeschema clients: "
                  f"{(ad.get('response') or {}).get('ipc_clients', 0)})")
        else:
            print(f"  display:  skipped ({ad.get('reason', 'unknown')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
