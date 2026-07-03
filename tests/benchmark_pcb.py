"""benchmark_pcb.py — honest engine-quality benchmark across real boards.

Runs the full electrical pipeline on each board and emits the metrics that
actually prove engine quality (per the 2026-07-01 review):

  board size | reasoning confidence | route completion % | vias | DRC before->after | power-net warnings

It NEVER mutates the source: each board is copied to a scratch dir (with its
.kicad_pro sibling for net-class widths/clearance) and the tools run on the copy.
Numbers are reported as-measured — a badly-placed fixture shows badly-placed
numbers. That honesty is the point.

Run:
    python -m tests.benchmark_pcb                       # all boards in _envil_out
    python -m tests.benchmark_pcb path/to/board.kicad_pcb ...
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow "python tests/benchmark_pcb.py" from the ai_backend dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from envil_agent.tools.drc_check import drc_check
from envil_agent.tools.route_pcb_astar import route_pcb_astar
from envil_agent.tools.power_integrity_pcb import power_integrity_pcb
from envil_agent.tools import pcb_reasoning


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _load_drc_categories() -> Dict[str, Any]:
    try:
        p = (Path(__file__).resolve().parent.parent
             / "envil_agent" / "config" / "drc_categories.json")
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


_CATCFG = _load_drc_categories()
_TYPE_CAT = _CATCFG.get("type_to_category", {})
_FALLBACK_CAT = _CATCFG.get("fallback_category", "other")


def _categorize(issues: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count ERROR-severity violations per responsibility bucket + connectivity
    (which is warning-severity but the metric that proves routing helped)."""
    out: Dict[str, int] = {}
    for i in issues:
        cat = _TYPE_CAT.get(i.get("type", ""), _FALLBACK_CAT)
        sev = i.get("severity", "")
        # errors count everywhere; connectivity is warning-severity but tracked.
        if sev == "error" or cat == "connectivity":
            out[cat] = out.get(cat, 0) + 1
    return out


async def _drc(pcb: str) -> Dict[str, Any]:
    r = await drc_check.handler({"pcb_path": pcb})
    if r.get("is_error"):
        return {"errors": -1, "warnings": -1, "cats": {}}
    return {"errors": int(r.get("error_count", 0)),
            "warnings": int(r.get("warning_count", 0)),
            "cats": _categorize(r.get("issues", []) or []),
            "issues": r.get("issues", []) or []}


def _board_size(pcb: str) -> Optional[str]:
    try:
        import sexpdata
        from envil_agent.tools.route_pcb_simple import _edge_bbox, _head
        root = sexpdata.loads(Path(pcb).read_text(encoding="utf-8"))
        eb = _edge_bbox(root)
        if eb:
            return f"{eb[2]-eb[0]:.0f}x{eb[3]-eb[1]:.0f}mm"
    except Exception:
        pass
    return None


def _reasoning_stats(ir_or_pcb: str) -> Dict[str, Any]:
    rep = pcb_reasoning.analyze(Path(ir_or_pcb))
    if not rep.get("ok"):
        return {"parts": 0}
    comps = rep.get("components", [])
    confs = [c.get("confidence") for c in comps if isinstance(c.get("confidence"), (int, float))]
    low = rep.get("low_confidence", []) or []
    return {
        "parts": len(comps),
        "anchor": rep.get("anchor"),
        "domains": rep.get("emergent_domains", []),
        "avg_conf": round(sum(confs) / len(confs), 2) if confs else None,
        "low_conf": len(low),
    }


async def _bench_board(pcb_src: str, learn: bool = False) -> Dict[str, Any]:
    name = Path(pcb_src).stem
    tmp = tempfile.mkdtemp()
    pcb = os.path.join(tmp, Path(pcb_src).name)
    shutil.copy(pcb_src, pcb)
    pro_src = str(Path(pcb_src).with_suffix(".kicad_pro"))
    if os.path.exists(pro_src):
        shutil.copy(pro_src, str(Path(pcb).with_suffix(".kicad_pro")))

    reason_report = pcb_reasoning.analyze(Path(pcb))
    row: Dict[str, Any] = {"board": name, "size": _board_size(pcb)}
    row.update({f"reason_{k}": v for k, v in _reasoning_stats(pcb).items()})

    drc_before = await _drc(pcb)
    rr = await route_pcb_astar.handler({"pcb_path": pcb})
    routed = int(rr.get("routed", 0))
    skipped = rr.get("skipped", []) or []
    # Route completion = routed / (routed + genuinely-unrouted). "poured" and
    # "already has copper" skips are not failures, so they don't count against it.
    fails = [s for s in skipped
             if "poured" not in s.get("reason", "")
             and "already has copper" not in s.get("reason", "")]
    denom = routed + len(fails)
    row["routable_nets"] = denom
    row["routed"] = routed
    row["route_pct"] = round(100.0 * routed / denom, 0) if denom else 100.0
    row["vias"] = int(rr.get("vias", 0))
    row["route_mm"] = rr.get("length_mm", 0)

    drc_after = await _drc(pcb)
    row["drc_before"] = f"{drc_before['errors']}E/{drc_before['warnings']}W"
    row["drc_after"] = f"{drc_after['errors']}E/{drc_after['warnings']}W"
    row["drc_delta_err"] = drc_after["errors"] - drc_before["errors"]
    # Per-category delta — the honest signal. ROUTING must be <= 0 (the router
    # never adds a short/clearance); CONNECTIVITY should be negative (nets got
    # connected). manufacturing/placement/fixture deltas are NOT the router's job.
    cb, ca = drc_before["cats"], drc_after["cats"]
    cats = set(cb) | set(ca)
    row["cat_delta"] = {c: ca.get(c, 0) - cb.get(c, 0)
                        for c in cats if ca.get(c, 0) - cb.get(c, 0) != 0}
    row["routing_added"] = ca.get("routing", 0) - cb.get("routing", 0)
    row["connectivity_delta"] = ca.get("connectivity", 0) - cb.get("connectivity", 0)

    pir = await power_integrity_pcb.handler({"pcb_path": pcb})
    row["pwr_warn"] = int(pir.get("warn_count", 0)) if not pir.get("is_error") else "-"

    # Close the confidence loop (opt-in): attribute this board's routing failures
    # back to the parts and persist the learned per-lib_id bias. Off by default so
    # a pure measurement run never mutates the learned store.
    if learn:
        try:
            from envil_agent.intent import confidence_feedback as cf
            row["learned"] = cf.record_observations(
                reason_report, drc_after.get("issues", []))
        except Exception as exc:                            # noqa: BLE001
            row["learned"] = {"error": str(exc)}
    return row


async def _main(argv: List[str]) -> int:
    root = _repo_root()
    learn = "--learn" in argv
    argv = [a for a in argv if a != "--learn"]
    if argv:
        boards = argv
    else:
        boards = sorted(glob.glob(str(root / "_envil_out" / "**" / "*.kicad_pcb"),
                                  recursive=True)
                        + glob.glob(str(root / "_envil_out" / "*.kicad_pcb")))
        boards = sorted(set(boards))
    if not boards:
        print("no boards found — pass .kicad_pcb paths as args")
        return 1

    rows: List[Dict[str, Any]] = []
    for b in boards:
        print(f"... benchmarking {Path(b).stem}" + ("  [+learn]" if learn else ""))
        try:
            rows.append(await _bench_board(b, learn=learn))
        except Exception as exc:                            # noqa: BLE001
            rows.append({"board": Path(b).stem, "error": f"{type(exc).__name__}: {exc}"})

    # Also run reasoning-only on IR fixtures (no board to route/DRC).
    ir_rows: List[Dict[str, Any]] = []
    for ir in sorted(glob.glob(str(root / "ai_backend" / "tests" /
                                   "reasoning_fixtures" / "*.envil-ir.json"))):
        st = _reasoning_stats(ir)
        st["board"] = Path(ir).name.replace(".envil-ir.json", "")
        ir_rows.append(st)

    print("\n" + "=" * 92)
    print("PCB ENGINE BENCHMARK — routed boards")
    print("=" * 92)
    hdr = (f"{'board':<20}{'size':<11}{'parts':>6}{'conf':>6}{'low':>5}"
           f"{'route%':>8}{'vias':>6}{'DRC before':>12}{'DRC after':>11}"
           f"{'rt+':>5}{'conn':>6}{'pwrW':>6}")
    print(hdr)
    print("-" * 98)
    for r in rows:
        if r.get("error"):
            print(f"{r['board']:<20}{r['error']}")
            continue
        print(f"{r['board']:<20}{str(r.get('size') or '-'):<11}"
              f"{r.get('reason_parts', 0):>6}{str(r.get('reason_avg_conf') or '-'):>6}"
              f"{r.get('reason_low_conf', 0):>5}{r.get('route_pct', 0):>7.0f}%"
              f"{r.get('vias', 0):>6}{r.get('drc_before', '-'):>12}"
              f"{r.get('drc_after', '-'):>11}"
              f"{r.get('routing_added', 0):>+5}{r.get('connectivity_delta', 0):>+6}"
              f"{str(r.get('pwr_warn')):>6}")
    print("-" * 98)
    print("  rt+  = ROUTING errors ADDED by the router (must be <= 0 — it never shorts/under-clears)")
    print("  conn = connectivity (unconnected) delta (negative = nets got connected)")
    # Per-board category breakdown so mixed deltas aren't hidden.
    for r in rows:
        if r.get("cat_delta"):
            parts = ", ".join(f"{c} {d:+d}" for c, d in sorted(r["cat_delta"].items()))
            print(f"    {r['board']}: {parts}")

    if learn:
        print("\nCONFIDENCE FEEDBACK — learned per-lib_id bias this run:")
        any_learned = False
        for r in rows:
            for lib, info in (r.get("learned") or {}).items():
                if isinstance(info, dict) and "bias" in info:
                    any_learned = True
                    print(f"    {lib}: bias {info['bias']:+.2f} "
                          f"(obs {info['observations']})  [{r['board']}]")
        if not any_learned:
            print("    (no routing failures to attribute — nothing learned)")

    print("\n" + "=" * 60)
    print("REASONING-ONLY fixtures (no board geometry)")
    print("=" * 60)
    print(f"{'fixture':<18}{'parts':>6}{'avg_conf':>9}{'low':>5}  anchor / domains")
    print("-" * 60)
    for r in ir_rows:
        print(f"{r['board']:<18}{r.get('parts', 0):>6}{str(r.get('avg_conf') or '-'):>9}"
              f"{r.get('low_conf', 0):>5}  {r.get('anchor', '-')} / "
              f"{', '.join(r.get('domains', [])) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.get_event_loop().run_until_complete(_main(sys.argv[1:])))
